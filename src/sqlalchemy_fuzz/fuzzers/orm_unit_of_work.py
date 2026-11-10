"""
ORM unit of work: flush timing, savepoints and batching.

Generates random sequences of session operations against a fixed mapping
with cascades, ``post_update``, a self-referential tree, many-to-many
(including self-referential), joined and single table inheritance: add
objects, delete them, change attributes, set many-to-one references,
append / remove / replace collection members, move children between
parents, orphan children, ``merge()`` copies, detached objects and new
object graphs, ``expire()`` / ``refresh()``, ``expunge()`` and re-add,
``commit()``, and SAVEPOINTs with ``begin_nested()`` that are released or
rolled back.  Each sequence is run three ways, which must give the same
result:

flush_each
    ``Session.flush()`` after every operation, so the unit of work only
    ever sees one change at a time.

flush_end
    No explicit flushes except where an operation needs one (see below);
    the session flushes when SQLAlchemy decides to: autoflush, savepoints,
    commit, and once at the end, so a flush may have many changes to put
    in order.

no_insertmanyvalues
    As flush_end, against an engine created with
    ``use_insertmanyvalues=False``, so INSERTs are not batched.

Checks:

flush_timing
    flush_each and flush_end leave the same rows in the database, and
    either both raise or neither does.

persisted
    After the final flush, the objects in the session match what the
    database holds: for every attribute loaded in an object's
    ``__dict__``, the value (or, for relationships, the objects referred
    to) is what the rows say.  The rows are read with plain SQL, not
    loaded through the ORM.

rollback
    The same comparison, right after each SAVEPOINT rollback.  As
    documented, a rollback expires only objects that were changed inside
    the savepoint, and others keep their state, including attributes
    loaded inside the savepoint, which may hold rows the rollback undid.
    So only attributes that were loaded when the savepoint began and not
    reloaded since are compared, and the others are then expired, so that
    the rest of the sequence doesn't see them.

batching
    flush_end and no_insertmanyvalues leave the same rows, and either
    both raise or neither does.

Rows are compared by a ``tag`` column holding each object's logical id
(``P1``, ``C4``, ...), with foreign keys translated into the tag of the
row referred to, since autoincrement primary keys legitimately differ
when rows are inserted in a different order.

A sequence that raises in every mode is skipped as invalid.  The generator
tracks which objects exist and what refers to what, and avoids sequences
that SQLAlchemy is documented or known not to support, which would
otherwise fail a check every time:

* The "DELETE before INSERT" problem,
  https://github.com/sqlalchemy/sqlalchemy/issues/2501: within one flush
  the unit of work INSERTs before it DELETEs, so deleting a row and
  adding one with the same primary key or unique value fails in one
  flush but works when flushed in between.  There are no unique
  constraints besides primary keys, and primary keys are autoincrement
  with ``sqlite_autoincrement=True``, so SQLite never reuses the primary
  key of a deleted row.
* Making a ``Node`` the child of a node that was its descendant earlier in
  the same flush, e.g. swapping a parent and child:
  https://github.com/sqlalchemy/sqlalchemy/issues/1063, the unit of work
  orders rows by both the old and the new parent, so this is a cycle
  that needs ``post_update`` even though only UPDATEs are emitted.
  Autoflush can split the operations since the last flush anywhere, so a
  node is only given a parent if no cycle forms among all the parents
  nodes had since then.
* Deleting an object, or orphaning it, while some other object still
  refers to it through a reference made since the last flush (e.g.
  ``child.keyword = k`` then ``session.delete(k)``): the session would
  hold a reference to a deleted object, which the ORM doesn't resolve
  (see "Notes on Delete - Deleting Objects Referenced from Collections
  and Scalar Relationships" in the docs), so whether the flush raises
  depends on whether the reference was flushed first.  Deleting an
  object referred to by ``Parent.favorite``, which has no reverse side
  that would set it to NULL, is never generated.
* ``Session.delete()`` of an object, or of one whose delete cascade
  reaches an object, that has not been flushed yet: deleting a pending
  object raises, and the delete cascade skips pending objects.
* Pending orphans: every ``Child`` (``delete-orphan``) gets its parent in
  the same operation that creates it, as flushing a ``Child`` without one
  raises.
* Backrefs never emit SQL, so replacing a many-to-one whose old value is
  not loaded would leave a loaded collection on the old parent stale.
  The many-to-one sides of backrefs use ``active_history=True``, which
  loads the old value, as documented for this purpose.
* Operations that only make sense against a flushed session (expire,
  refresh, expunge, merge of an object with a primary key) flush first in
  every mode; otherwise they would legitimately discard or keep pending
  changes depending on the mode.
* References to deleted objects are left in loaded collections and
  scalars after a flush (as documented above), so the comparison
  ignores them, and they are expired before ``expunge()`` / ``add()`` and
  ``merge()`` of a detached object, which would otherwise cascade the
  deleted object back into the session.

The SQLite database enforces foreign keys (``PRAGMA foreign_keys=ON``), so
statements emitted in an order that the foreign keys don't allow fail
rather than going unnoticed.  SAVEPOINT uses the event recipe from the
SQLAlchemy SQLite docs, without which pysqlite doesn't run SAVEPOINT
correctly.

"""

from __future__ import annotations

import collections
import copy
import random
import re
import traceback
import warnings

from sqlalchemy import Column
from sqlalchemy import create_engine
from sqlalchemy import event
from sqlalchemy import ForeignKey
from sqlalchemy import inspect
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import Table
from sqlalchemy.orm import declarative_base
from sqlalchemy.orm import relationship
from sqlalchemy.orm import Session

NAME = "orm-unit-of-work"
DESCRIPTION = "ORM unit of work: flush timing, savepoints and batching"

SUBJECT = "sequence of session operations"

CHECKS = {
    "flush_timing": "leaves different rows in the database, or raises "
    "only in one of the two, when flushed after every operation and "
    "when flushed only where needed",
    "persisted": "leaves objects in the session that don't match the rows "
    "in the database after the final flush",
    "rollback": "leaves objects in the session that don't match the rows "
    "in the database after a SAVEPOINT rollback",
    "batching": "leaves different rows in the database, or raises only in "
    "one of the two, with and without insertmanyvalues",
}

MODES = ("flush_each", "flush_end", "no_insertmanyvalues")

# shown as e.g. persisted[flush_end]
MAPPINGS = MODES


# ----------------------------------------------------------------- model

# logical id prefix, i.e. which "family" a class belongs to
FAMILY = {
    "Parent": "P",
    "SpecialParent": "P",
    "Child": "C",
    "BigChild": "C",
    "Keyword": "K",
    "Node": "N",
}
CLASSES_OF = {
    "P": ["Parent", "SpecialParent"],
    "C": ["Child", "BigChild"],
    "K": ["Keyword"],
    "N": ["Node"],
}

# column attributes that can be changed, per class
COLUMNS = {
    "Parent": ["data"],
    "SpecialParent": ["data", "special"],
    "Child": ["data"],
    "BigChild": ["data", "size"],
    "Keyword": ["data"],
    "Node": ["data"],
}

# relationships, per family: key -> (target family, uselist)
RELATIONSHIPS = {
    "P": {
        "children": ("C", True),
        "favorite": ("C", False),
        "keywords": ("K", True),
    },
    "C": {"parent": ("P", False), "keyword": ("K", False)},
    "K": {"parents": ("P", True), "child_refs": ("C", True)},
    "N": {
        "parent": ("N", False),
        "children": ("N", True),
        "links": ("N", True),
        "linked_from": ("N", True),
    },
}

TYPES = {
    "parent": "Parent",
    "special": "SpecialParent",
    "child": "Child",
    "big": "BigChild",
}

VALUES = ["a", "b", "c", None]
SIZES = [1, 2, 3, None]


def _setup_engine(engine):
    """foreign keys on, and the documented recipe for SAVEPOINT"""

    @event.listens_for(engine, "connect")
    def connect(dbapi_connection, connection_record):  # noqa: U100
        # don't let pysqlite emit BEGIN, as it doesn't for SAVEPOINT
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    @event.listens_for(engine, "begin")
    def begin(conn):
        conn.exec_driver_sql("BEGIN")


class Model:
    def __init__(self):
        self.classes = {}
        self.engines = {}
        self._setup()

    def _setup(self):
        Base = declarative_base()
        autoinc = {"sqlite_autoincrement": True}

        parent_keyword = Table(
            "parent_keyword",
            Base.metadata,
            Column("parent_id", ForeignKey("parent.id"), primary_key=True),
            Column("keyword_id", ForeignKey("keyword.id"), primary_key=True),
        )
        node_link = Table(
            "node_link",
            Base.metadata,
            Column("left_id", ForeignKey("node.id"), primary_key=True),
            Column("right_id", ForeignKey("node.id"), primary_key=True),
        )

        class Parent(Base):
            __tablename__ = "parent"
            __table_args__ = autoinc
            id = Column(Integer, primary_key=True)
            tag = Column(String(10))
            type = Column(String(10))
            data = Column(String(10))
            favorite_id = Column(
                ForeignKey("child.id", use_alter=True, name="fk_favorite")
            )
            children = relationship(
                "Child",
                back_populates="parent",
                cascade="all, delete-orphan",
                foreign_keys="Child.parent_id",
            )
            # parent -> child -> parent cycle, broken by post_update
            favorite = relationship(
                "Child", foreign_keys=favorite_id, post_update=True
            )
            keywords = relationship(
                "Keyword", secondary=parent_keyword, back_populates="parents"
            )
            __mapper_args__ = {
                "polymorphic_on": type,
                "polymorphic_identity": "parent",
            }

        class SpecialParent(Parent):
            # single table inheritance
            special = Column(String(10))
            __mapper_args__ = {"polymorphic_identity": "special"}

        class Child(Base):
            __tablename__ = "child"
            __table_args__ = autoinc
            id = Column(Integer, primary_key=True)
            tag = Column(String(10))
            type = Column(String(10))
            data = Column(String(10))
            parent_id = Column(ForeignKey("parent.id"))
            keyword_id = Column(ForeignKey("keyword.id"))
            parent = relationship(
                Parent,
                back_populates="children",
                foreign_keys=parent_id,
                active_history=True,
            )
            keyword = relationship(
                "Keyword", back_populates="child_refs", active_history=True
            )
            __mapper_args__ = {
                "polymorphic_on": type,
                "polymorphic_identity": "child",
            }

        class BigChild(Child):
            # joined table inheritance
            __tablename__ = "big_child"
            id = Column(ForeignKey("child.id"), primary_key=True)
            size = Column(Integer)
            __mapper_args__ = {"polymorphic_identity": "big"}

        class Keyword(Base):
            __tablename__ = "keyword"
            __table_args__ = autoinc
            id = Column(Integer, primary_key=True)
            tag = Column(String(10))
            data = Column(String(10))
            parents = relationship(
                Parent, secondary=parent_keyword, back_populates="keywords"
            )
            # one-to-many without delete cascade; deleting a Keyword sets
            # child.keyword_id to NULL
            child_refs = relationship(Child, back_populates="keyword")

        class Node(Base):
            __tablename__ = "node"
            __table_args__ = autoinc
            id = Column(Integer, primary_key=True)
            tag = Column(String(10))
            data = Column(String(10))
            parent_id = Column(ForeignKey("node.id"))
            parent = relationship(
                "Node",
                remote_side=id,
                back_populates="children",
                active_history=True,
            )
            children = relationship(
                "Node", back_populates="parent", cascade="all"
            )
            links = relationship(
                "Node",
                secondary=node_link,
                primaryjoin=id == node_link.c.left_id,
                secondaryjoin=id == node_link.c.right_id,
                back_populates="linked_from",
            )
            linked_from = relationship(
                "Node",
                secondary=node_link,
                primaryjoin=id == node_link.c.right_id,
                secondaryjoin=id == node_link.c.left_id,
                back_populates="links",
            )

        for cls in (
            Parent,
            SpecialParent,
            Child,
            BigChild,
            Keyword,
            Node,
        ):
            self.classes[cls.__name__] = cls

        for name, kw in (
            ("insertmanyvalues", {}),
            ("no_insertmanyvalues", {"use_insertmanyvalues": False}),
        ):
            engine = create_engine("sqlite://", **kw)
            _setup_engine(engine)
            Base.metadata.create_all(engine)
            self.engines[name] = engine

    def engine_for(self, mode):
        if mode == "no_insertmanyvalues":
            return self.engines["no_insertmanyvalues"]
        return self.engines["insertmanyvalues"]


RESET = [
    "UPDATE parent SET favorite_id = NULL",
    "DELETE FROM node_link",
    "DELETE FROM parent_keyword",
    "DELETE FROM big_child",
    "DELETE FROM child",
    "DELETE FROM parent",
    "DELETE FROM keyword",
    "DELETE FROM node",
    "DELETE FROM sqlite_sequence",
]


def reset(engine):
    with engine.begin() as conn:
        for sql in RESET:
            conn.exec_driver_sql(sql)


# ------------------------------------------------------ the rows, by tag


def db_rows(conn):
    """all rows, with primary and foreign keys replaced by tags"""

    def fetch(sql):
        return conn.exec_driver_sql(sql).fetchall()

    tags = {}
    raw = {
        "parent": fetch(
            "SELECT id, tag, type, data, special, favorite_id FROM parent"
        ),
        "child": fetch(
            "SELECT id, tag, type, data, parent_id, keyword_id FROM child"
        ),
        "big_child": fetch("SELECT id, size FROM big_child"),
        "keyword": fetch("SELECT id, tag, data FROM keyword"),
        "node": fetch("SELECT id, tag, data, parent_id FROM node"),
        "parent_keyword": fetch(
            "SELECT parent_id, keyword_id FROM parent_keyword"
        ),
        "node_link": fetch("SELECT left_id, right_id FROM node_link"),
    }
    for table in ("parent", "child", "keyword", "node"):
        tags[table] = {row[0]: row[1] for row in raw[table]}

    def tag(table, id_):
        if id_ is None:
            return None
        return tags[table].get(id_, f"<no {table} row with id {id_}>")

    def sort(rows):
        return sorted(rows, key=repr)

    return {
        "parent": sort(
            (tag_, type_, data, special, tag("child", fav))
            for _, tag_, type_, data, special, fav in raw["parent"]
        ),
        "child": sort(
            (tag_, type_, data, tag("parent", pid), tag("keyword", kid))
            for _, tag_, type_, data, pid, kid in raw["child"]
        ),
        "big_child": sort(
            (tag("child", id_), size) for id_, size in raw["big_child"]
        ),
        "keyword": sort(row[1:] for row in raw["keyword"]),
        "node": sort(
            (tag_, data, tag("node", pid))
            for _, tag_, data, pid in raw["node"]
        ),
        "parent_keyword": sort(
            (tag("parent", pid), tag("keyword", kid))
            for pid, kid in raw["parent_keyword"]
        ),
        "node_link": sort(
            (tag("node", left), tag("node", right))
            for left, right in raw["node_link"]
        ),
    }


def object_view(rows):
    """the rows as objects: tag -> {attribute: value}; also returns
    problems such as duplicate tags, which can't be represented"""
    view = {}
    problems = []

    def new(tag, data):
        if tag in view:
            problems.append(f"more than one row for {tag}")
        view[tag] = data

    def append(tag, key, value):
        if tag not in view:
            return
        if key not in view[tag]:
            problems.append(
                f"{value} refers to {tag} as its {key} owner, but {tag} is "
                f"a {view[tag]['class']}"
            )
            return
        view[tag][key].append(value)

    sizes = dict(rows["big_child"])
    for tag, type_, data, special, fav in rows["parent"]:
        obj = {
            "class": TYPES.get(type_, type_),
            "data": data,
            "favorite": fav,
            "children": [],
            "keywords": [],
        }
        if type_ == "special":
            obj["special"] = special
        new(tag, obj)
    for tag, type_, data, parent, keyword in rows["child"]:
        obj = {
            "class": TYPES.get(type_, type_),
            "data": data,
            "parent": parent,
            "keyword": keyword,
        }
        if type_ == "big":
            obj["size"] = sizes.get(tag, "<no big_child row>")
        new(tag, obj)
    for tag, data in rows["keyword"]:
        new(
            tag,
            {
                "class": "Keyword",
                "data": data,
                "parents": [],
                "child_refs": [],
            },
        )
    for tag, data, parent in rows["node"]:
        new(
            tag,
            {
                "class": "Node",
                "data": data,
                "parent": parent,
                "children": [],
                "links": [],
                "linked_from": [],
            },
        )
    for tag, _, _, parent, keyword in rows["child"]:
        append(parent, "children", tag)
        append(keyword, "child_refs", tag)
    for tag, _, parent in rows["node"]:
        append(parent, "children", tag)
    for parent, keyword in rows["parent_keyword"]:
        append(parent, "keywords", keyword)
        append(keyword, "parents", parent)
    for left, right in rows["node_link"]:
        append(left, "links", right)
        append(right, "linked_from", left)
    for obj in view.values():
        for key, value in obj.items():
            if isinstance(value, list):
                value.sort()
    return view, problems


# ------------------------------------------------------- op generation
#
# a sequence is a list of plain dicts, one per operation, referring to
# objects by logical id.  the generator keeps a model of which objects
# exist and what refers to what, so it only generates operations that are
# valid at that point; see the module docstring.

# operations that flush first in every mode
PRE_FLUSH = {
    "flush",
    "expire",
    "expire_all",
    "refresh",
    "expunge_add",
    "merge_copy",
    "merge_detached",
}

# after these, nothing is pending in any mode
SYNC = PRE_FLUSH | {"commit", "begin_nested", "release", "rollback_nested"}


class State:
    """the generator's model of the session"""

    def __init__(self):
        # lid -> class name, for objects that exist
        self.objs = {}
        # lids of objects created since the last flush in every mode
        self.pending = set()
        # (kind, a, b) -> True if made since the last flush in every mode
        #   cp: Child a's parent is Parent b
        #   ck: Child a's keyword is Keyword b
        #   fav: Parent a's favorite is Child b
        #   pk: Parent a has Keyword b
        #   np: Node a's parent is Node b
        #   nl: Node a links to Node b
        self.edges = {}
        # every np edge since the last flush in every mode, including ones
        # since removed
        self.np_window = set()

    def alive(self, family):
        return sorted(
            (lid for lid, cls in self.objs.items() if FAMILY[cls] == family),
            key=_lid_key,
        )

    def targets(self, kind, a):
        return sorted(
            (e[2] for e in self.edges if e[0] == kind and e[1] == a),
            key=_lid_key,
        )

    def sources(self, kind, b):
        return sorted(
            (e[1] for e in self.edges if e[0] == kind and e[2] == b),
            key=_lid_key,
        )

    def target(self, kind, a):
        found = self.targets(kind, a)
        return found[0] if found else None

    def add_edge(self, kind, a, b):
        self.edges[(kind, a, b)] = True
        if kind == "np":
            self.np_window.add((a, b))

    def remove_edge(self, kind, a, b):
        del self.edges[(kind, a, b)]

    def subtree(self, node):
        out = [node]
        for child in self.sources("np", node):
            out.extend(self.subtree(child))
        return out

    def delete_closure(self, lid):
        """objects deleted along with lid by the delete cascade"""
        family = FAMILY[self.objs[lid]]
        if family == "P":
            return [lid] + self.sources("cp", lid)
        elif family == "N":
            return self.subtree(lid)
        return [lid]

    def can_kill(self, dead, deleted):
        """whether the objects in dead can be deleted or orphaned; deleted
        is True if by Session.delete()"""
        dead = set(dead)
        if deleted and dead & self.pending:
            return False
        for (kind, a, b), new in self.edges.items():
            if (a in dead) == (b in dead):
                continue
            # Parent.favorite has no reverse side to set it to NULL
            if kind == "fav" and b in dead:
                return False
            # a reference to a deleted object made since the last flush
            if new:
                return False
        return True

    def kill(self, dead):
        for lid in dead:
            del self.objs[lid]
            self.pending.discard(lid)
        self.edges = {
            e: new
            for e, new in self.edges.items()
            if e[1] not in dead and e[2] not in dead
        }
        self.np_window = {
            (a, b)
            for a, b in self.np_window
            if a not in dead and b not in dead
        }

    def sync(self):
        self.pending = set()
        self.edges = dict.fromkeys(self.edges, False)
        self.np_window = {(a, b) for kind, a, b in self.edges if kind == "np"}

    def np_cycle(self, node, parent):
        """whether making parent the parent of node closes a cycle among
        the parents nodes had since the last flush"""
        seen = set()
        todo = [parent]
        while todo:
            current = todo.pop()
            if current == node:
                return True
            if current in seen:
                continue
            seen.add(current)
            todo.extend(b for a, b in self.np_window if a == current)
        return False


def _lid_key(lid):
    """sort key for logical ids, P2 < P10; rows with some other tag (e.g.
    NULL) sort last"""
    if isinstance(lid, str) and lid[1:].isdigit():
        return 0, lid[0], int(lid[1:])
    return 1, str(lid), 0


class Gen:
    def __init__(self, rng):
        self.rng = rng
        self.counter = 0
        self.state = State()
        self.saved = []  # State copies for open savepoints
        self.ops = []
        self.all_lids = []

    def new_lid(self, family):
        self.counter += 1
        lid = f"{family}{self.counter}"
        self.all_lids.append(lid)
        return lid

    def emit(self, op):
        self.ops.append(op)
        if op["op"] in SYNC:
            self.state.sync()

    def pick(self, items):
        return self.rng.choice(items) if items else None

    def value(self):
        return self.rng.choice(VALUES)

    def create(self, cls, lid=None):
        """register a new object, returning its op dict"""
        lid = lid or self.new_lid(FAMILY[cls])
        self.state.objs[lid] = cls
        self.state.pending.add(lid)
        op = {"lid": lid, "cls": cls, "data": self.value()}
        if cls == "SpecialParent":
            op["special"] = self.value()
        elif cls == "BigChild":
            op["size"] = self.rng.choice(SIZES)
        return op

    # each gen_* returns an op dict, or None if not possible right now

    def gen_new(self, family=None):
        rng = self.rng
        st = self.state
        family = family or rng.choice("PPCCCKNN")
        cls = rng.choice(CLASSES_OF[family])
        parents = st.alive("P")
        if family == "C" and not parents:
            return None
        nodes = st.alive("N")
        keywords = st.alive("K")
        op = self.create(cls)
        op["op"] = "new"
        lid = op["lid"]
        if family == "C":
            op["parent"] = rng.choice(parents)
            op["via"] = rng.choice(["append", "m2o"])
            st.add_edge("cp", lid, op["parent"])
            if keywords and rng.random() < 0.4:
                op["keyword"] = rng.choice(keywords)
                st.add_edge("ck", lid, op["keyword"])
        elif family == "P" and keywords and rng.random() < 0.3:
            op["keywords"] = sorted(
                rng.sample(keywords, rng.randint(1, min(2, len(keywords)))),
                key=_lid_key,
            )
            for kw in op["keywords"]:
                st.add_edge("pk", lid, kw)
        elif family == "N" and nodes and rng.random() < 0.7:
            op["parent"] = rng.choice(nodes)
            op["via"] = rng.choice(["append", "m2o"])
            st.add_edge("np", lid, op["parent"])
        return op

    def gen_set(self):
        lid = self.pick(sorted(self.state.objs, key=_lid_key))
        if lid is None:
            return None
        attr = self.rng.choice(COLUMNS[self.state.objs[lid]])
        value = self.rng.choice(SIZES if attr == "size" else VALUES)
        return {"op": "set", "lid": lid, "attr": attr, "value": value}

    def gen_delete(self):
        st = self.state
        lid = self.pick(sorted(st.objs, key=_lid_key))
        if lid is None:
            return None
        dead = st.delete_closure(lid)
        if not st.can_kill(dead, deleted=True):
            return None
        st.kill(dead)
        return {"op": "delete", "lid": lid, "kills": sorted(dead)}

    def _orphan(self, child):
        """the Child will lose its parent; returns kills, or None if that
        can't be done"""
        st = self.state
        st.remove_edge("cp", child, st.target("cp", child))
        if not st.can_kill([child], deleted=False):
            return None
        st.kill([child])
        return [child]

    def _move_child(self, child, parent):
        st = self.state
        old = st.target("cp", child)
        if old == parent:
            return False
        st.remove_edge("cp", child, old)
        st.add_edge("cp", child, parent)
        return True

    def _can_move_node(self, node, parent):
        # the unit of work orders rows using both the old and the new
        # parent, so a node can't become the child of one of its
        # descendants from earlier in the same flush without post_update,
        # https://github.com/sqlalchemy/sqlalchemy/issues/1063
        return parent is None or not self.state.np_cycle(node, parent)

    def _move_node(self, node, parent):
        st = self.state
        old = st.target("np", node)
        if old == parent:
            return False
        if old is not None:
            st.remove_edge("np", node, old)
        if parent is not None:
            st.add_edge("np", node, parent)
        return True

    def gen_m2o(self):
        rng = self.rng
        st = self.state
        choices = []
        for c in st.alive("C"):
            choices += [(c, "parent"), (c, "keyword")]
        for p in st.alive("P"):
            choices.append((p, "favorite"))
        for n in st.alive("N"):
            choices.append((n, "parent"))
        if not choices:
            return None
        lid, attr = rng.choice(choices)
        family = lid[0]
        op = {"op": "m2o", "lid": lid, "attr": attr}
        backup = copy.deepcopy(st)
        if family == "C" and attr == "parent":
            target = self.pick(st.alive("P")) if rng.random() < 0.85 else None
            op["target"] = target
            if target is None:
                op["kills"] = self._orphan(lid)
                if op["kills"] is None:
                    self.state = backup
                    return None
            elif not self._move_child(lid, target):
                return None
        elif family == "C":
            target = self.pick(st.alive("K")) if rng.random() < 0.7 else None
            op["target"] = target
            old = st.target("ck", lid)
            if old == target:
                return None
            if old is not None:
                st.remove_edge("ck", lid, old)
            if target is not None:
                st.add_edge("ck", lid, target)
        elif family == "P":
            target = self.pick(st.alive("C")) if rng.random() < 0.7 else None
            op["target"] = target
            old = st.target("fav", lid)
            if old == target:
                return None
            if old is not None:
                st.remove_edge("fav", lid, old)
            if target is not None:
                st.add_edge("fav", lid, target)
        else:
            target = self.pick(st.alive("N")) if rng.random() < 0.7 else None
            op["target"] = target
            if not self._can_move_node(lid, target):
                return None
            if not self._move_node(lid, target):
                return None
        return op

    # collections: (family, key) -> how to change the model

    def _collection(self, lid, key):
        """current members of a collection, per the model"""
        st = self.state
        kind, reverse = COLLECTION_EDGES[(lid[0], key)]
        if reverse:
            return st.sources(kind, lid)
        return st.targets(kind, lid)

    def _coll_add(self, lid, key, member):
        """model the member being added; False if not possible"""
        st = self.state
        family = lid[0]
        if (family, key) == ("P", "children"):
            return self._move_child(member, lid)
        elif (family, key) == ("K", "child_refs"):
            old = st.target("ck", member)
            if old is not None:
                st.remove_edge("ck", member, old)
            st.add_edge("ck", member, lid)
            return True
        elif (family, key) == ("N", "children"):
            if not self._can_move_node(member, lid):
                return False
            return self._move_node(member, lid)
        kind, reverse = COLLECTION_EDGES[(family, key)]
        st.add_edge(kind, *((member, lid) if reverse else (lid, member)))
        return True

    def _coll_remove(self, lid, key, member, kills):
        """model the member being removed; False if not possible"""
        st = self.state
        family = lid[0]
        if (family, key) == ("P", "children"):
            dead = self._orphan(member)
            if dead is None:
                return False
            kills.extend(dead)
            return True
        kind, reverse = COLLECTION_EDGES[(family, key)]
        st.remove_edge(kind, *((member, lid) if reverse else (lid, member)))
        return True

    def gen_collection(self, which):
        rng = self.rng
        st = self.state
        choices = [
            (lid, key)
            for (family, key) in COLLECTION_EDGES
            for lid in st.alive(family)
        ]
        if not choices:
            return None
        lid, key = rng.choice(choices)
        target_family = RELATIONSHIPS[lid[0]][key][0]
        current = self._collection(lid, key)
        others = [m for m in st.alive(target_family) if m not in current]
        backup = copy.deepcopy(st)
        kills = []
        op = {"op": which, "lid": lid, "attr": key}
        if which == "append":
            member = self.pick(others)
            if member is None or not self._coll_add(lid, key, member):
                self.state = backup
                return None
            op["target"] = member
        elif which == "remove":
            member = self.pick(current)
            if member is None or not self._coll_remove(
                lid, key, member, kills
            ):
                self.state = backup
                return None
            op["target"] = member
        else:
            keep = [m for m in current if rng.random() < 0.6]
            added = [m for m in others if rng.random() < 0.3]
            new = keep + added
            rng.shuffle(new)
            for member in current:
                if member not in keep and not self._coll_remove(
                    lid, key, member, kills
                ):
                    self.state = backup
                    return None
            for member in added:
                if not self._coll_add(lid, key, member):
                    self.state = backup
                    return None
            op["targets"] = new
        if kills:
            op["kills"] = sorted(kills)
        return op

    def gen_expire(self):
        lid = self.pick(sorted(self.state.objs, key=_lid_key))
        if lid is None:
            return None
        cls = self.state.objs[lid]
        op = {"op": self.rng.choice(["expire", "refresh"]), "lid": lid}
        if self.rng.random() < 0.5:
            keys = COLUMNS[cls] + list(RELATIONSHIPS[FAMILY[cls]])
            op["attrs"] = sorted(self.rng.sample(keys, self.rng.randint(1, 2)))
        return op

    def gen_detached(self):
        """expunge_add, merge_copy, merge_detached: a column change"""
        lid = self.pick(sorted(self.state.objs, key=_lid_key))
        if lid is None:
            return None
        attr = self.rng.choice(COLUMNS[self.state.objs[lid]])
        value = self.rng.choice(SIZES if attr == "size" else VALUES)
        return {
            "op": self.rng.choice(
                ["expunge_add", "merge_copy", "merge_detached"]
            ),
            "lid": lid,
            "attr": attr,
            "value": value,
        }

    def gen_merge_new(self):
        """merge() a new Parent with new children and keywords"""
        rng = self.rng
        st = self.state
        op = self.create(rng.choice(CLASSES_OF["P"]))
        op["op"] = "merge_new"
        lid = op["lid"]
        op["children"] = []
        for _ in range(rng.randint(0, 2)):
            child = self.create(rng.choice(CLASSES_OF["C"]))
            st.add_edge("cp", child["lid"], lid)
            op["children"].append(child)
        op["keywords"] = []
        for _ in range(rng.randint(0, 1)):
            kw = self.create("Keyword")
            st.add_edge("pk", lid, kw["lid"])
            op["keywords"].append(kw)
        return op

    def gen_savepoint(self):
        rng = self.rng
        if self.saved and (len(self.saved) >= 2 or rng.random() < 0.6):
            if rng.random() < 0.5:
                self.saved.pop()
                return {"op": "release"}
            self.state = self.saved.pop()
            alive = set(self.state.objs)
            return {
                "op": "rollback_nested",
                "dead": [lid for lid in self.all_lids if lid not in alive],
            }
        # copy after the flush that begin_nested() does
        self.state.sync()
        self.saved.append(copy.deepcopy(self.state))
        return {"op": "begin_nested"}

    def gen_sync(self):
        if self.saved:
            return {"op": "flush"}
        return {"op": self.rng.choice(["flush", "commit"])}


# (family, collection) -> (edge kind, whether the collection's owner is the
# second element of the edge)
COLLECTION_EDGES = {
    ("P", "children"): ("cp", True),
    ("P", "keywords"): ("pk", False),
    ("K", "parents"): ("pk", True),
    ("K", "child_refs"): ("ck", True),
    ("N", "children"): ("np", True),
    ("N", "links"): ("nl", False),
    ("N", "linked_from"): ("nl", True),
}

# the operations that flush in every mode (expire, detached, savepoint,
# sync) are kept rare enough that flush_end mostly has several changes to
# put in order at once
OP_WEIGHTS = [
    ("new", 14),
    ("set", 8),
    ("delete", 8),
    ("m2o", 12),
    ("append", 12),
    ("remove", 7),
    ("replace", 5),
    ("expire", 4),
    ("expire_all", 1),
    ("detached", 4),
    ("merge_new", 2),
    ("savepoint", 6),
    ("sync", 3),
]


def gen_ops(seed):
    rng = random.Random(seed)
    gen = Gen(rng)

    # an initial population, committed
    for family, n in (("K", 2), ("P", 2), ("C", 3), ("N", 3)):
        for _ in range(rng.randint(1, n)):
            gen.emit(gen.gen_new(family))
    gen.emit({"op": "commit"})

    names = [name for name, _ in OP_WEIGHTS]
    weights = [weight for _, weight in OP_WEIGHTS]
    target = rng.randint(8, 22)
    count = 0
    while count < target:
        name = rng.choices(names, weights)[0]
        backup = copy.deepcopy(gen.state)
        if name in ("append", "remove", "replace"):
            op = gen.gen_collection(name)
        elif name == "expire_all":
            op = {"op": "expire_all"}
        else:
            op = getattr(gen, "gen_" + name)()
        if op is None:
            gen.state = backup
            continue
        gen.emit(op)
        count += 1
    while gen.saved:
        gen.saved.pop()
        gen.emit({"op": "release"})
    return gen.ops


# ------------------------------------------------------------- running


def _error(err):
    """exception class and message, without memory addresses, so that
    the same error from different runs reads the same"""
    return type(err).__name__, re.sub(r" at 0x[0-9a-f]+", "", str(err))[:300]


def _status(state):
    for status in ("transient", "pending", "persistent", "deleted"):
        if getattr(state, status):
            return status
    return "detached"


class Run:
    """one sequence, run in one mode"""

    def __init__(self, model, mode):
        self.model = model
        self.mode = mode
        self.classes = model.classes
        self.objs = {}  # lid -> object
        self.lids = {}  # id(object) -> lid
        self.keep = []  # every object, so that id()s stay unique
        self.dead = set()
        self.savepoints = []
        # for each open savepoint, id(object) -> attributes that were loaded
        # when it began and haven't been reloaded since
        self.levels = []
        self.rollback_diffs = []

    def bind(self, lid, obj):
        self.objs[lid] = obj
        self.lids[id(obj)] = lid
        self.keep.append(obj)

    def make(self, spec):
        cls = self.classes[spec["cls"]]
        obj = cls(tag=spec["lid"], data=spec["data"])
        if "special" in spec:
            obj.special = spec["special"]
        if "size" in spec:
            obj.size = spec["size"]
        self.bind(spec["lid"], obj)
        return obj

    def get(self, lid):
        return None if lid is None else self.objs[lid]

    def run(self, ops):
        engine = self.model.engine_for(self.mode)
        result = {"error": None, "rollback": self.rollback_diffs}
        sess = Session(engine)
        try:
            for i, op in enumerate(ops):
                try:
                    self.do(sess, i, op)
                    if self.mode == "flush_each":
                        sess.flush()
                except Exception as err:
                    result["error"] = (i, *_error(err))
                    return result
            try:
                sess.flush()
                memory = self.memory_view(sess)
                sess.commit()
            except Exception as err:
                result["error"] = (len(ops), *_error(err))
                return result
        finally:
            sess.close()
            with engine.connect() as conn:
                result["rows"] = db_rows(conn)
            reset(engine)
        result["persisted"] = self.compare(memory, result["rows"])
        return result

    def do(self, sess, i, op):
        kind = op["op"]
        if kind in PRE_FLUSH:
            sess.flush()
        getattr(self, "op_" + kind)(sess, op)
        self.dead.update(op.get("kills", ()))
        # attributes that aren't loaded now are reloaded later, inside the
        # savepoint
        for level in self.levels:
            for obj in sess.identity_map.values():
                if id(obj) in level:
                    level[id(obj)] &= self.loaded(obj)
        if kind == "rollback_nested":
            self.check_rollback(sess, i)

    def op_new(self, sess, op):
        obj = self.make(op)
        parent = self.get(op.get("parent"))
        if parent is not None and op["via"] == "append":
            parent.children.append(obj)
        elif parent is not None:
            obj.parent = parent
            sess.add(obj)
        else:
            sess.add(obj)
        if "keyword" in op:
            obj.keyword = self.get(op["keyword"])
        if "keywords" in op:
            obj.keywords = [self.get(k) for k in op["keywords"]]

    def op_set(self, sess, op):
        setattr(self.get(op["lid"]), op["attr"], op["value"])

    def op_delete(self, sess, op):
        sess.delete(self.get(op["lid"]))

    def op_m2o(self, sess, op):
        setattr(self.get(op["lid"]), op["attr"], self.get(op["target"]))

    def op_append(self, sess, op):
        getattr(self.get(op["lid"]), op["attr"]).append(self.get(op["target"]))

    def op_remove(self, sess, op):
        getattr(self.get(op["lid"]), op["attr"]).remove(self.get(op["target"]))

    def op_replace(self, sess, op):
        setattr(
            self.get(op["lid"]),
            op["attr"],
            [self.get(lid) for lid in op["targets"]],
        )

    def _expire_cascade(self, obj, attrs):
        """the objects and attributes expire() / refresh() unload"""
        if attrs:
            return [(obj, attrs)]
        state = inspect(obj)
        return [(obj, None)] + [
            (o, None)
            for o, _, _, _ in state.mapper.cascade_iterator(
                "refresh-expire", state
            )
        ]

    def reloaded(self, objs):
        """these (object, attributes) will be reloaded inside the open
        savepoints; attributes None means all"""
        for level in self.levels:
            for obj, attrs in objs:
                if id(obj) in level:
                    level[id(obj)] -= set(attrs or self.view_keys(obj))

    def op_expire(self, sess, op):
        obj = self.get(op["lid"])
        self.reloaded(self._expire_cascade(obj, op.get("attrs")))
        sess.expire(obj, op.get("attrs"))

    def op_expire_all(self, sess, op):
        for level in self.levels:
            level.clear()
        sess.expire_all()

    def op_refresh(self, sess, op):
        obj = self.get(op["lid"])
        self.reloaded(self._expire_cascade(obj, op.get("attrs")))
        sess.refresh(obj, op.get("attrs"))

    def forget_dead(self, sess):
        """expire relationships that still refer to deleted objects; these
        are left in place after a flush, as documented, and would be
        cascaded back into the session by add() or merge()"""
        for obj in sess.identity_map.values():
            dict_ = inspect(obj).dict
            stale = []
            for key, (_, uselist) in RELATIONSHIPS[
                FAMILY[type(obj).__name__]
            ].items():
                value = dict_.get(key)
                members = value if uselist and value is not None else [value]
                if any(
                    self.lids.get(id(m)) in self.dead
                    for m in members
                    if m is not None
                ):
                    stale.append(key)
            if stale:
                sess.expire(obj, stale)

    def op_expunge_add(self, sess, op):
        self.forget_dead(sess)
        obj = self.get(op["lid"])
        sess.expunge(obj)
        setattr(obj, op["attr"], op["value"])
        sess.add(obj)

    def op_merge_copy(self, sess, op):
        obj = self.get(op["lid"])
        copy_ = type(obj)()
        copy_.id = obj.id
        setattr(copy_, op["attr"], op["value"])
        self.bind(op["lid"], sess.merge(copy_))

    def op_merge_detached(self, sess, op):
        self.forget_dead(sess)
        obj = self.get(op["lid"])
        mapper = inspect(obj).mapper
        detached = [obj] + [
            o
            for o, _, _, _ in mapper.cascade_iterator("expunge", inspect(obj))
        ]
        keys = [(o, inspect(o).key) for o in detached]
        for level in self.levels:
            level.clear()
        sess.expunge(obj)
        setattr(obj, op["attr"], op["value"])
        sess.merge(obj)
        sess.flush()
        # other objects in the session may still refer to the detached
        # objects
        sess.expire_all()
        for old, key in keys:
            lid = self.lids[id(old)]
            self.bind(lid, sess.get(key[0], key[1]))

    def op_merge_new(self, sess, op):
        parent = self.make(op)
        for spec in op["children"]:
            parent.children.append(self.make(spec))
        for spec in op["keywords"]:
            parent.keywords.append(self.make(spec))
        merged = sess.merge(parent)
        self.bind(op["lid"], merged)
        for obj in list(merged.children) + list(merged.keywords):
            self.bind(obj.tag, obj)

    def op_flush(self, sess, op):
        pass

    def op_commit(self, sess, op):
        sess.commit()

    def op_begin_nested(self, sess, op):
        self.savepoints.append(sess.begin_nested())
        self.levels.append(
            {id(obj): self.loaded(obj) for obj in sess.identity_map.values()}
        )

    def op_release(self, sess, op):
        self.savepoints.pop().commit()
        self.levels.pop()

    def op_rollback_nested(self, sess, op):
        self.savepoints.pop().rollback()
        self.dead = set(op["dead"])

    def check_rollback(self, sess, i):
        """compare the session with the database after a rollback.

        The rollback expires objects that were changed inside the
        savepoint; others keep their state, as documented, even attributes
        that were loaded inside the savepoint and so may hold rows from
        inside it.  Only attributes that were loaded when the savepoint
        began, and not reloaded since, are compared; then the others are
        expired, so that the rest of the sequence doesn't see them.

        """
        level = self.levels.pop()
        memory = self.memory_view(sess, level)
        diffs = self.compare(memory, db_rows(sess.connection()))
        if diffs:
            self.rollback_diffs.append((i, diffs))
        for obj in sess.identity_map.values():
            stale = self.loaded(obj) - level.get(id(obj), set())
            if stale:
                sess.expire(obj, sorted(stale))

    # -- comparing

    def view_keys(self, obj):
        cls = type(obj).__name__
        return COLUMNS[cls] + list(RELATIONSHIPS[FAMILY[cls]])

    def loaded(self, obj):
        dict_ = inspect(obj).dict
        return {key for key in self.view_keys(obj) if key in dict_}

    def describe(self, obj):
        """an object referred to by a relationship, as its tag"""
        lid = self.lids.get(id(obj)) or obj.__dict__.get("tag", "?")
        if lid in self.dead:
            return None
        state = inspect(obj)
        if not state.persistent:
            return f"<{_status(state)} {lid}>"
        return lid

    def memory_view(self, sess, only=None):
        """the loaded state of the objects in the session; only the
        attributes in only[id(object)] if given"""
        view = {}
        for lid, obj in self.objs.items():
            if lid in self.dead:
                continue
            state = inspect(obj)
            if not state.persistent or state.session is not sess:
                view[lid] = f"{_status(state)}, not persistent in the session"
                continue
            family = FAMILY[type(obj).__name__]
            data = {"class": type(obj).__name__}
            keys = self.loaded(obj)
            if only is not None:
                keys &= only.get(id(obj), set())
            for key in COLUMNS[type(obj).__name__]:
                if key in keys:
                    data[key] = state.dict[key]
            for key, (_, uselist) in RELATIONSHIPS[family].items():
                if key not in keys:
                    continue
                value = state.dict[key]
                if uselist:
                    data[key] = sorted(
                        str(m)
                        for m in (self.describe(o) for o in value)
                        if m is not None
                    )
                else:
                    data[key] = None if value is None else self.describe(value)
            view[lid] = data
        return view

    def compare(self, memory, rows):
        """differences between the session and the rows"""
        db, problems = object_view(rows)
        lines = list(problems)
        for lid in sorted(set(memory) | set(db), key=_lid_key):
            mem, row = memory.get(lid), db.get(lid)
            if row is None:
                lines.append(f"{lid}: no row; in the session: {mem}")
                continue
            if mem is None:
                lines.append(f"{lid}: has a row, but not in the session")
                continue
            if isinstance(mem, str):
                lines.append(f"{lid}: {mem}")
                continue
            for key, value in mem.items():
                if value != row.get(key, "<not in row>"):
                    lines.append(
                        f"{lid}.{key}: session has {value!r}, "
                        f"database has {row.get(key, '<not in row>')!r}"
                    )
        return lines


MODEL = {}


def init_worker():
    warnings.simplefilter("ignore")
    MODEL["model"] = Model()


def run_all(ops):
    return {mode: Run(MODEL["model"], mode).run(ops) for mode in MODES}


def check_ops(ops):
    """run all checks for one sequence; returns (findings, results)"""
    results = run_all(ops)
    findings = []

    def differ(a, b):
        ra, rb = results[a], results[b]
        if (ra["error"] is None) != (rb["error"] is None):
            return True
        return ra["error"] is None and ra["rows"] != rb["rows"]

    if differ("flush_each", "flush_end"):
        findings.append("flush_timing")
    for mode in MODES:
        if results[mode]["error"] is None and results[mode]["persisted"]:
            findings.append(f"persisted[{mode}]")
    for mode in MODES:
        if results[mode]["rollback"]:
            findings.append(f"rollback[{mode}]")
    if differ("flush_end", "no_insertmanyvalues"):
        findings.append("batching")
    return findings, results


def trial(seed):
    try:
        ops = gen_ops(seed)
        findings, results = check_ops(ops)
    except Exception:
        return seed, "crash", traceback.format_exc(limit=4)
    if findings:
        return seed, "diff", tuple(findings)
    errors = [results[mode]["error"] for mode in MODES]
    if all(errors):
        return seed, "invalid", errors[0][1:]
    return seed, "ok", None


# ----------------------------------------------------------- reporting


def features(ops):
    """coarse set of the features used by a sequence"""
    out = set()
    for op in ops:
        kind = op["op"]
        if kind in ("append", "remove", "replace", "m2o"):
            out.add(f"{kind}:{op['lid'][0]}.{op['attr']}")
        elif kind in ("new", "merge_new"):
            out.add(f"{kind}:{op['cls']}")
        elif kind == "set":
            out.add(f"set:{op['attr']}")
        else:
            out.add(kind)
        if op.get("kills") and kind != "delete":
            out.add("orphan")
    return out


def format_op(i, op):
    rest = {k: v for k, v in op.items() if k not in ("op", "lid")}
    text = f"{i:3}: {op['op']}"
    if "lid" in op:
        text += f" {op['lid']}"
    if rest:
        text += " " + ", ".join(f"{k}={v!r}" for k, v in rest.items())
    return text


def describe_rows(expected, actual):
    lines = []
    for table in expected:
        exp = collections.Counter(expected[table])
        act = collections.Counter(actual[table])
        for row in sorted(exp - act, key=repr):
            lines.append(f"  {table}: only in the first: {row}")
        for row in sorted(act - exp, key=repr):
            lines.append(f"  {table}: only in the second: {row}")
    return lines


def replay(seed):
    init_worker()
    ops = gen_ops(seed)
    print("--- operations")
    for i, op in enumerate(ops):
        print(format_op(i, op))

    findings, results = check_ops(ops)
    for mode in MODES:
        result = results[mode]
        print(f"--- mode: {mode}")
        if result["error"]:
            i, cls, msg = result["error"]
            print(f"  raised at operation {i}: {cls}: {msg}")
        else:
            print("  OK")
        for i, diffs in result["rollback"]:
            print(f"  after the rollback at operation {i}:")
            for line in diffs:
                print(f"    {line}")
        if result["error"] is None and result["persisted"]:
            print("  after the final flush:")
            for line in result["persisted"]:
                print(f"    {line}")
    for check, a, b in (
        ("flush_timing", "flush_each", "flush_end"),
        ("batching", "flush_end", "no_insertmanyvalues"),
    ):
        if check in findings:
            print(f"--- {check}: rows in {a} vs. {b}")
            for line in describe_rows(results[a]["rows"], results[b]["rows"]):
                print(line)
    print(f"--- findings: {findings or 'none'}")


def seed_features(seed):
    return features(gen_ops(seed))
