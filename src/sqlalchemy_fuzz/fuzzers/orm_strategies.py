"""
ORM loader strategies vs. each other.

Generates random statements against a fixed mapping with relationships
into and out of an inheritance hierarchy, a many-to-many, many-to-ones
and a self-referential tree: a root entity (plain, aliased() or
with_polymorphic()) and a parent statement shape where eager loading has
to rewrite the query (LIMIT / OFFSET, DISTINCT, a join along a
relationship that is also eagerly loaded, filtering through it, ORDER BY
on a related column).  The same statement must load the same objects
whichever loader strategy each relationship uses.

The mapping is built several times, identical except for the ``lazy=``
setting of its relationships: ``lazy`` (everything ``lazy="select"``),
``joined``, ``selectin``, ``subquery`` and ``immediate`` (everything set
to that strategy, with ``join_depth`` on the self-referential
relationships) and ``mixed`` (a fixed mix of strategies).  Each seed uses
one eager mapping for each of the first two checks, to keep seeds fast:

mapping
    The statement, without loader options, against an eager mapping,
    compared with the ``lazy`` mapping.

options
    The statement with a random tree of loader options (joinedload,
    selectinload, subqueryload, immediateload, lazyload, defaultload,
    of_type(), relationship.and_(), defer / undefer / load_only), against
    the ``lazy`` mapping or an eager mapping, compared with the same tree
    with every strategy but defaultload() replaced by lazyload(), against
    the ``lazy`` mapping.

raises
    Either of the above raises while its all-lazy reference doesn't.

loops
    Either of the above emits more than ``MAX_STATEMENTS`` SQL statements,
    which lazy loading everything never comes near; loading is stopped
    there rather than hanging the run.

The result compared is the object graph after accessing everything,
which lazy loads whatever the strategies under test didn't load: the
rows of the statement (in order if it has a total ORDER BY), then,
breadth first from the rows to a fixed depth so that the self-referential
and back-referencing relationships terminate, each object's class,
identity and column values and the identities in each relationship (in
order if the relationship has ``order_by``).  Statement counts are not
compared, as they legitimately differ between strategies.

An object's later lazy loads use the options of the path it was first
loaded through, and eager loading can load an object through a different
path first than lazy loading does.  Where that changes what is loaded,
that is, when the options have ``relationship.and_()`` criteria, the
options are restricted to relationships that lead away from the root of
the statement and the comparison follows only those relationships, so
that every object is reached through exactly one path.  Statements
without ``and_()`` follow every relationship.
"""

from __future__ import annotations

import operator
import pprint
import random
import traceback
import warnings

from sqlalchemy import Column
from sqlalchemy import create_engine
from sqlalchemy import event
from sqlalchemy import ForeignKey
from sqlalchemy import insert
from sqlalchemy import inspect
from sqlalchemy import Integer
from sqlalchemy import select
from sqlalchemy import String
from sqlalchemy import Table
from sqlalchemy.orm import aliased
from sqlalchemy.orm import declarative_base
from sqlalchemy.orm import defaultload
from sqlalchemy.orm import defer
from sqlalchemy.orm import immediateload
from sqlalchemy.orm import joinedload
from sqlalchemy.orm import lazyload
from sqlalchemy.orm import load_only
from sqlalchemy.orm import relationship
from sqlalchemy.orm import selectinload
from sqlalchemy.orm import Session
from sqlalchemy.orm import subqueryload
from sqlalchemy.orm import undefer
from sqlalchemy.orm import with_loader_criteria
from sqlalchemy.orm import with_polymorphic

from .orm_loading import _sample_subs
from .orm_loading import _with_subclasses
from .orm_loading import Ctx
from .orm_loading import CtxDesc
from .orm_loading import describe_diff
from .orm_loading import lineage
from .orm_loading import SUBS

NAME = "orm-strategies"
DESCRIPTION = "ORM loader strategies vs. each other"

CHECKS = {
    "mapping": "loads something different when its relationships are "
    "configured with an eager lazy= strategy than with lazy loading",
    "options": "loads something different with eager loader options than "
    "with the same options all set to lazyload()",
    "raises": "raises with some loader strategies but not with lazy "
    "loading",
    "loops": "loads in an endless loop, emitting SQL statements without "
    "end, with some loader strategies but not with lazy loading",
}


# ----------------------------------------------------------------- model
#
# Person / Engineer / Manager / Boss is the same hierarchy as orm-loading's,
# so that its lineage() and Ctx can be reused.

RELS = {
    "Company": [("employees", "Person"), ("bosses", "Boss")],
    "Person": [
        ("company", "Company"),
        ("paperwork", "Paperwork"),
        ("manager", "Manager"),
    ],
    "Engineer": [("machines", "Machine")],
    "Manager": [("direct_reports", "Person"), ("reports", "Report")],
    "Boss": [],
    "Machine": [("engineer", "Engineer"), ("parts", "Part")],
    "Part": [],
    "Paperwork": [("person", "Person")],
    "Report": [],
    "Node": [("children", "Node"), ("parent", "Node")],
}

# relationships leading away from the root of any statement, which reach
# each object through exactly one path (Node.children only does from the
# top level nodes, see gen_spec())
DOWN = {
    ("Company", "employees"),
    ("Person", "paperwork"),
    ("Engineer", "machines"),
    ("Manager", "reports"),
    ("Machine", "parts"),
    ("Node", "children"),
}

# collections with order_by, whose order is compared
ORDERED = {
    ("Company", "employees"),
    ("Engineer", "machines"),
    ("Manager", "direct_reports"),
    ("Manager", "reports"),
    ("Machine", "parts"),
    ("Node", "children"),
}

SELF_REF = {
    ("Person", "manager"),
    ("Manager", "direct_reports"),
    ("Node", "children"),
    ("Node", "parent"),
}

# a unique, non-NULL column of each class, used in ORDER BY and and_()
UCOL = {
    "Company": "name",
    "Person": "name",
    "Machine": "name",
    "Part": "name",
    "Paperwork": "description",
    "Report": "title",
    "Node": "name",
}

# the class's own columns, used in WHERE
COLS = {
    "Company": "name",
    "Person": "name",
    "Engineer": "primary_language",
    "Manager": "manager_name",
    "Boss": "golf_swing",
    "Machine": "name",
    "Part": "name",
    "Paperwork": "description",
    "Report": "title",
    "Node": "name",
}

# literal values compared against COLS; each splits the rows
VALUES = {
    "Company": ["c1", "c2", "c3"],
    "Person": ["p02", "p05", "p08", "p11"],
    "Engineer": ["java", "python"],
    "Manager": ["mn02", "mn09"],
    "Boss": ["fore", "slice"],
    "Machine": ["m2", "m4"],
    "Part": ["pt2", "pt3"],
    "Paperwork": ["pw03", "pw06"],
    "Report": ["r2", "r5"],
    "Node": ["n03", "n05", "n10"],
}

# many-to-one relationships
SCALARS = {"company", "manager", "engineer", "person", "parent"}

OPS = {"!=": operator.ne, "<": operator.lt, ">": operator.gt}

# the mappings; "lazy" is the reference
EAGER = ("joined", "selectin", "subquery", "immediate", "mixed")
MAPPINGS = EAGER + ("lazy",)

JOIN_DEPTH = {
    "joined": 1,
    "selectin": 3,
    "subquery": 1,
    "immediate": 2,
    "mixed": 2,
}

MIXED = {
    ("Company", "employees"): "selectin",
    ("Company", "bosses"): "joined",
    ("Person", "company"): "joined",
    ("Person", "paperwork"): "subquery",
    ("Person", "manager"): "immediate",
    ("Engineer", "machines"): "joined",
    ("Manager", "direct_reports"): "subquery",
    ("Manager", "reports"): "immediate",
    ("Machine", "engineer"): "selectin",
    ("Machine", "parts"): "joined",
    ("Paperwork", "person"): "selectin",
    ("Node", "children"): "joined",
    ("Node", "parent"): "selectin",
}

# objects further than this from the rows of the statement are compared,
# but not their relationships
MAX_DEPTH = 3

# a run emitting more SQL statements than this is stopped; accessing
# everything lazily emits at most a few dozen
MAX_STATEMENTS = 1000


class TooManyStatements(Exception):
    pass


def family(cls):
    """the base class of the given class name"""
    return lineage(cls)[-1]


class Model:
    def __init__(self, name):
        self.name = name
        self.classes = {}
        self.engine = None
        self._setup()

    def _rel(self, owner, key, target, **kw):
        if self.name == "lazy":
            lazy = "select"
        elif self.name == "mixed":
            lazy = MIXED[(owner, key)]
        else:
            lazy = self.name
        if self.name != "lazy" and (owner, key) in SELF_REF:
            kw["join_depth"] = JOIN_DEPTH[self.name]
        return relationship(target, lazy=lazy, **kw)

    def _setup(self):
        Base = declarative_base()
        rel = self._rel

        machine_part = Table(
            "machine_part",
            Base.metadata,
            Column("machine_id", ForeignKey("machine.id"), primary_key=True),
            Column("part_id", ForeignKey("part.id"), primary_key=True),
        )

        class Company(Base):
            __tablename__ = "company"
            id = Column(Integer, primary_key=True)
            name = Column(String(20))
            employees = rel(
                "Company",
                "employees",
                "Person",
                order_by="Person.id",
                back_populates="company",
            )
            # single table inheritance subclass of a joined subclass
            bosses = rel("Company", "bosses", "Boss", viewonly=True)

        class Person(Base):
            __tablename__ = "person"
            id = Column(Integer, primary_key=True)
            company_id = Column(ForeignKey("company.id"))
            manager_id = Column(ForeignKey("manager.id"))
            name = Column(String(20))
            type = Column(String(20))
            company = rel(
                "Person", "company", "Company", back_populates="employees"
            )
            paperwork = rel(
                "Person", "paperwork", "Paperwork", back_populates="person"
            )
            # inheritance on both sides
            manager = rel(
                "Person",
                "manager",
                "Manager",
                primaryjoin="Person.manager_id == Manager.id",
                foreign_keys=manager_id,
                remote_side="Manager.id",
                back_populates="direct_reports",
            )
            __mapper_args__ = {
                "polymorphic_on": type,
                "polymorphic_identity": "person",
            }

        class Engineer(Person):
            __tablename__ = "engineer"
            id = Column(ForeignKey("person.id"), primary_key=True)
            primary_language = Column(String(20))
            machines = rel(
                "Engineer",
                "machines",
                "Machine",
                order_by="Machine.name",
                back_populates="engineer",
            )
            __mapper_args__ = {"polymorphic_identity": "engineer"}

        class Manager(Person):
            __tablename__ = "manager"
            id = Column(ForeignKey("person.id"), primary_key=True)
            manager_name = Column(String(20))
            golf_swing = Column(String(20))
            direct_reports = rel(
                "Manager",
                "direct_reports",
                "Person",
                primaryjoin="Manager.id == Person.manager_id",
                foreign_keys="Person.manager_id",
                order_by="Person.id.desc()",
                back_populates="manager",
            )
            reports = rel("Manager", "reports", "Report", order_by="Report.id")
            __mapper_args__ = {
                "polymorphic_identity": "manager",
                # person.manager_id also refers to manager.id
                "inherit_condition": id == Person.__table__.c.id,
            }

        class Boss(Manager):
            # single table inheritance from the joined inheritance Manager
            __mapper_args__ = {"polymorphic_identity": "boss"}

        class Paperwork(Base):
            __tablename__ = "paperwork"
            id = Column(Integer, primary_key=True)
            person_id = Column(ForeignKey("person.id"))
            description = Column(String(20))
            person = rel(
                "Paperwork", "person", "Person", back_populates="paperwork"
            )

        class Machine(Base):
            __tablename__ = "machine"
            id = Column(Integer, primary_key=True)
            engineer_id = Column(ForeignKey("engineer.id"))
            name = Column(String(20))
            engineer = rel(
                "Machine", "engineer", "Engineer", back_populates="machines"
            )
            parts = rel(
                "Machine",
                "parts",
                "Part",
                secondary=machine_part,
                order_by="Part.id.desc()",
            )

        class Part(Base):
            __tablename__ = "part"
            id = Column(Integer, primary_key=True)
            name = Column(String(20))

        class Report(Base):
            __tablename__ = "report"
            id = Column(Integer, primary_key=True)
            manager_id = Column(ForeignKey("manager.id"))
            title = Column(String(20))

        class Node(Base):
            __tablename__ = "node"
            id = Column(Integer, primary_key=True)
            parent_id = Column(ForeignKey("node.id"))
            name = Column(String(20))
            children = rel(
                "Node",
                "children",
                "Node",
                order_by="Node.id",
                back_populates="parent",
            )
            parent = rel(
                "Node",
                "parent",
                "Node",
                remote_side=id,
                back_populates="children",
            )

        for cls in (
            Company,
            Person,
            Engineer,
            Manager,
            Boss,
            Paperwork,
            Machine,
            Part,
            Report,
            Node,
        ):
            self.classes[cls.__name__] = cls

        self.engine = engine = create_engine("sqlite://")
        self.statement_count = 0

        @event.listens_for(engine, "before_cursor_execute")
        def count(*arg):  # noqa: U100
            self.statement_count += 1
            if self.statement_count > MAX_STATEMENTS:
                raise TooManyStatements()

        Base.metadata.create_all(engine)
        with engine.begin() as conn:
            for table, rows in DATA:
                conn.execute(insert(Base.metadata.tables[table]), rows)

        # the lazy loads without options are the same for every statement;
        # they're compiled once here, and each run starts with a copy of
        # this cache, so that what was run before doesn't matter
        self.cache = {}
        with engine.connect() as conn:
            conn = conn.execution_options(compiled_cache=self.cache)
            with Session(conn) as sess:
                for name in ("Company", "Person", "Machine", "Node"):
                    stmt = select(self.classes[name])
                    objs = sess.scalars(stmt).unique().all()
                    snapshot(objs, False, False)


def _people():
    # id, company, type, manager, extra columns
    people = [
        (1, 1, "engineer", 2, "java"),
        (2, 1, "manager", 3, "mn02", None),
        (3, 1, "boss", None, "mn03", "fore"),
        (4, 1, "person", 2),
        (5, 1, "engineer", 3, "python"),
        (6, 2, "engineer", 7, "java"),
        (7, 2, "manager", None, "mn07", "slice"),
        (8, 2, "person", 12),
        (9, 3, "boss", None, "mn09", "slice"),
        (10, 3, "engineer", 9, None),
        (11, 3, "engineer", 9, "python"),
        (12, 3, "manager", 9, "mn12", None),
        (13, 4, "person", None),
    ]
    person, engineer, manager = [], [], []
    for id_, company, type_, mgr, *extra in people:
        person.append(
            {
                "id": id_,
                "company_id": company,
                "manager_id": mgr,
                "name": f"p{id_:02}",
                "type": type_,
            }
        )
        if type_ == "engineer":
            engineer.append({"id": id_, "primary_language": extra[0]})
        elif type_ in ("manager", "boss"):
            manager.append(
                {"id": id_, "manager_name": extra[0], "golf_swing": extra[1]}
            )
    return [("person", person), ("engineer", engineer), ("manager", manager)]


# the same data in every mapping; company 5 has no employees, there are
# persons without paperwork, engineers without machines, machines without
# parts and a part without machines
DATA = [
    (
        "company",
        [{"id": i, "name": f"c{i}"} for i in range(1, 6)],
    ),
    *_people(),
    (
        "paperwork",
        [
            {"id": i, "person_id": p, "description": f"pw{i:02}"}
            for i, p in [
                (1, 1),
                (2, 1),
                (3, 2),
                (4, 3),
                (5, 5),
                (6, 7),
                (7, 7),
                (8, 9),
                (9, 11),
                (10, 12),
            ]
        ],
    ),
    (
        "machine",
        [
            # names don't sort the same as ids, as Engineer.machines is
            # ordered by name
            {"id": i, "engineer_id": e, "name": n}
            for i, e, n in [
                (1, 1, "m4"),
                (2, 1, "m2"),
                (3, 5, "m6"),
                (4, 6, "m1"),
                (5, 10, "m3"),
                (6, 11, "m5"),
            ]
        ],
    ),
    ("part", [{"id": i, "name": f"pt{i}"} for i in range(1, 6)]),
    (
        "machine_part",
        [
            {"machine_id": m, "part_id": p}
            for m, p in [
                (1, 1),
                (1, 2),
                (3, 2),
                (3, 3),
                (4, 1),
                (5, 3),
                (5, 4),
            ]
        ],
    ),
    (
        "report",
        [
            {"id": i, "manager_id": m, "title": f"r{i}"}
            for i, m in [(1, 2), (2, 2), (3, 3), (4, 7), (5, 12), (6, 9)]
        ],
    ),
    (
        "node",
        [
            {"id": i, "parent_id": p, "name": f"n{i:02}"}
            for i, p in [
                (1, None),
                (2, 1),
                (3, 1),
                (4, 2),
                (5, 2),
                (6, 4),
                (7, 6),
                (8, 3),
                (9, None),
                (10, 9),
                (11, 10),
            ]
        ],
    ),
]


# ------------------------------------------------------ spec generation
#
# a "spec" is a plain data structure describing a statement and a tree of
# loader options; build_stmt() turns it into a statement, creating new
# aliased() / with_polymorphic() objects each time it's called.

STRATEGIES = {
    "joinedload": joinedload,
    "selectinload": selectinload,
    "subqueryload": subqueryload,
    "immediateload": immediateload,
    "lazyload": lazyload,
    "defaultload": defaultload,
}

ROOTS = ["Company", "Company", "Person", "Person", "Manager", "Engineer"]
ROOTS += ["Machine", "Node", "Node"]


def _rels(ctx, down_only):
    return [
        (owner, key, target)
        for owner in ctx.avail
        for key, target in RELS[owner]
        if not down_only or (owner, key) in DOWN
    ]


def _overlaps(base, target):
    """whether joining from base to target needs target to be aliased"""
    return family(base) == family(target)


def gen_spec(rng):
    base = rng.choice(ROOTS)
    r = rng.random()
    if base in SUBS and r < 0.3:
        entity = ("wp", _sample_subs(rng, base), rng.random() < 0.5)
        ctx = CtxDesc(base, "wp", _with_subclasses(lineage(base), entity[1]))
    elif r < 0.6:
        entity = ("aliased", rng.random() < 0.5)
        ctx = CtxDesc(base, "aliased", lineage(base))
    else:
        entity = ("plain",)
        ctx = CtxDesc(base, "plain", lineage(base))

    # with and_() criteria, only relationships leading away from the root
    # are loaded and compared, see the module docstring
    crit = rng.random() < 0.25
    spec = {
        "base": base,
        "entity": entity,
        "crit": crit,
        "top_level": crit and base == "Node",
        "join": None,
        "where": None,
        "distinct": rng.random() < 0.2,
        "order": None,
        "limit": None,
        "wlc": None,
        # the eager mapping compared with the lazy one, and the mapping
        # that the options are applied to; one of each per seed, to keep
        # each seed fast
        "mapping": rng.choice(EAGER),
        "opts_mapping": (
            "lazy" if crit or rng.random() < 0.5 else rng.choice(EAGER)
        ),
    }

    rels = _rels(ctx, False)
    if rels and rng.random() < 0.4:
        owner, key, target = rng.choice(rels)
        filt = None
        if rng.random() < 0.5:
            filt = (
                COLS[target],
                rng.choice(list(OPS)),
                rng.choice(VALUES[target]),
            )
        spec["join"] = {
            "owner": owner,
            "key": key,
            "target": target,
            "alias": _overlaps(base, target) or rng.random() < 0.3,
            "outer": rng.random() < 0.5,
            "filter": filt,
        }
    if rng.random() < 0.4:
        if rels and rng.random() < 0.5:
            owner, key, target = rng.choice(rels)
            spec["where"] = (
                "any",
                owner,
                key,
                target,
                rng.choice(list(OPS)),
                rng.choice(VALUES[target]),
            )
        else:
            spec["where"] = (
                "col",
                rng.choice(list(OPS)),
                rng.choice(VALUES[base]),
            )

    orders = ["pk", "pk_desc", "col"]
    if spec["join"] and not (
        spec["distinct"] and spec["join"]["key"] not in SCALARS
    ):
        orders.append("related")
    if rng.random() < 0.5:
        lim = rng.choice([None, 1, 2, 3, 5])
        off = rng.choice([None, None, 1, 2])
        if lim or off:
            spec["limit"] = (lim, off)
    if spec["limit"] or rng.random() < 0.5:
        spec["order"] = rng.choice(orders)

    if rng.random() < 0.2:
        spec["wlc"] = rng.choice(["Paperwork", "Part"])
        spec["wlc"] = (spec["wlc"], rng.choice(VALUES[spec["wlc"]]))

    spec["opts"] = gen_opts(rng, ctx, 0, crit)
    return spec


def gen_opts(rng, ctx, depth, crit):
    opts = []
    rels = _rels(ctx, crit)
    rng.shuffle(rels)
    # at most one option per relationship, so that options don't
    # conflict; only one below the top level, to keep each seed fast
    count = rng.randint(1, 2) if depth == 0 else rng.randint(0, 1)
    for owner, key, target in rels[:count]:
        if depth < 3:
            opts.append(gen_rel_opt(rng, owner, key, target, depth, crit))
    if rng.random() < 0.1:
        owner = rng.choice(ctx.avail)
        opts.append(
            {
                "t": "col",
                "op": rng.choice(["defer", "undefer", "load_only"]),
                "owner": owner,
                "key": COLS[owner],
            }
        )
    return opts


def gen_rel_opt(rng, owner, key, target, depth, crit):
    spec = {
        "t": "rel",
        "strategy": rng.choice(list(STRATEGIES)),
        "owner": owner,
        "key": key,
        "of_type": None,
        "crit": None,
        "children": [],
    }

    tbase = target
    if target not in SUBS or rng.random() < 0.6:
        tctx = CtxDesc(target, "plain", lineage(target))
    else:
        # not of_type(aliased(Subclass)), which joinedload, selectinload
        # and subqueryload treat as criteria, loading only Subclass objects,
        # while lazyload and immediateload ignore it and load everything
        choices = [("aliased", target), ("wp", target)]
        choices += [("cls", s) for s in SUBS[target]]
        kind, cls = rng.choice(choices)
        if kind == "cls":
            spec["of_type"] = ("cls", cls)
            tbase = cls
            tctx = CtxDesc(cls, "plain", lineage(cls))
        elif kind == "aliased":
            spec["of_type"] = ("aliased", cls, rng.random() < 0.5)
            tbase = cls
            tctx = CtxDesc(cls, "aliased", lineage(cls))
        else:
            # always flat; joined eager loading raises for a with_polymorphic()
            # that isn't, which is documented
            subs = _sample_subs(rng, target)
            spec["of_type"] = ("wp", subs, True)
            tctx = CtxDesc(
                target, "wp", _with_subclasses(lineage(target), subs)
            )

    if crit and rng.random() < 0.5:
        spec["crit"] = (
            rng.choice(list(OPS)),
            rng.choice(VALUES[family(tbase)]),
        )

    spec["children"] = gen_opts(rng, tctx, depth + 1, crit)
    return spec


def gen_trial(seed):
    return gen_spec(random.Random(seed))


# ------------------------------------------------------ statement build


def build_opt(classes, spec, ctx, lazy):
    if spec["t"] == "col":
        fn = {"defer": defer, "undefer": undefer, "load_only": load_only}
        return fn[spec["op"]](ctx.attr(spec["owner"], spec["key"]))

    attr = ctx.attr(spec["owner"], spec["key"])
    target = dict(RELS[spec["owner"]])[spec["key"]]
    of_type = spec["of_type"]
    if of_type is None:
        tctx = Ctx(classes, target, classes[target], "plain")
    elif of_type[0] == "cls":
        cls = classes[of_type[1]]
        attr = attr.of_type(cls)
        tctx = Ctx(classes, of_type[1], cls, "plain")
    elif of_type[0] == "aliased":
        obj = aliased(classes[of_type[1]], flat=of_type[2])
        attr = attr.of_type(obj)
        tctx = Ctx(classes, of_type[1], obj, "aliased")
    else:
        obj = with_polymorphic(
            classes[target],
            [classes[s] for s in of_type[1]],
            flat=of_type[2],
        )
        attr = attr.of_type(obj)
        tctx = Ctx(classes, target, obj, "wp")

    if spec["crit"] is not None:
        op, value = spec["crit"]
        fam = family(tctx.base)
        attr = attr.and_(OPS[op](tctx.attr(fam, UCOL[fam]), value))

    strategy = spec["strategy"]
    if lazy and strategy != "defaultload":
        # defaultload() means lazy loading in the lazy mapping too; it's
        # left as is, as it ignores and_() criteria, unlike lazyload()
        strategy = "lazyload"
    loader = STRATEGIES[strategy](attr)
    if spec["children"]:
        loader = loader.options(
            *[
                build_opt(classes, child, tctx, lazy)
                for child in spec["children"]
            ]
        )
    return loader


def build_stmt(classes, spec, opts):
    """the statement; opts is None, "lazy" or "eager"

    returns the statement and whether its ORDER BY is total
    """
    base = spec["base"]
    entity = spec["entity"]
    if entity[0] == "plain":
        root, kind = classes[base], "plain"
    elif entity[0] == "aliased":
        root, kind = aliased(classes[base], flat=entity[1]), "aliased"
    else:
        root, kind = (
            with_polymorphic(
                classes[base], [classes[s] for s in entity[1]], flat=entity[2]
            ),
            "wp",
        )
    ctx = Ctx(classes, base, root, kind)
    stmt = select(root)

    if opts is not None:
        stmt = stmt.options(
            *[
                build_opt(classes, opt, ctx, opts == "lazy")
                for opt in spec["opts"]
            ]
        )
    if spec["wlc"] is not None:
        cls, value = spec["wlc"]
        cls = classes[cls]
        stmt = stmt.options(
            with_loader_criteria(
                cls, getattr(cls, UCOL[family(cls.__name__)]) != value
            )
        )

    if spec["top_level"]:
        stmt = stmt.where(root.parent_id.is_(None))

    join = spec["join"]
    target = None
    if join:
        attr = ctx.attr(join["owner"], join["key"])
        target = classes[join["target"]]
        if join["alias"]:
            target = aliased(target, flat=True)
            attr = attr.of_type(target)
        stmt = stmt.join(attr, isouter=join["outer"])
        if join["filter"]:
            col, op, value = join["filter"]
            stmt = stmt.where(OPS[op](getattr(target, col), value))

    where = spec["where"]
    if where and where[0] == "col":
        _, op, value = where
        stmt = stmt.where(OPS[op](getattr(root, COLS[base]), value))
    elif where:
        _, owner, key, tname, op, value = where
        attr = ctx.attr(owner, key)
        crit = OPS[op](getattr(classes[tname], COLS[tname]), value)
        if key in SCALARS:
            stmt = stmt.where(attr.has(crit))
        else:
            stmt = stmt.where(attr.any(crit))

    if spec["distinct"]:
        stmt = stmt.distinct()

    order = spec["order"]
    if order == "pk":
        stmt = stmt.order_by(root.id)
    elif order == "pk_desc":
        stmt = stmt.order_by(root.id.desc())
    elif order == "col":
        fam = family(base)
        stmt = stmt.order_by(getattr(root, UCOL[fam]).desc(), root.id)
    elif order == "related":
        fam = family(join["target"])
        stmt = stmt.order_by(
            getattr(target, UCOL[fam]), target.id.desc(), root.id
        )

    if spec["limit"]:
        lim, off = spec["limit"]
        if lim:
            stmt = stmt.limit(lim)
        if off:
            stmt = stmt.offset(off)
    return stmt, order is not None


# ------------------------------------------------------------- running


def ident(obj):
    return (type(obj).__name__,) + tuple(inspect(obj).identity)


def snapshot(objs, ordered, down_only):
    """the object graph, accessing everything to MAX_DEPTH"""
    level = {ident(obj): obj for obj in objs}
    top = [ident(obj) for obj in objs]
    if not ordered:
        top.sort()

    graph = {}
    seen = set(level)
    for depth in range(MAX_DEPTH + 1):
        following = {}
        for key in sorted(level):
            obj = level[key]
            mapper = inspect(obj).mapper
            data = {
                prop.key: getattr(obj, prop.key)
                for prop in mapper.column_attrs
            }
            graph[key] = data
            if depth == MAX_DEPTH:
                continue
            for prop in mapper.relationships:
                rel = (prop.parent.class_.__name__, prop.key)
                if down_only and rel not in DOWN:
                    continue
                value = getattr(obj, prop.key)
                if value is None:
                    data[prop.key] = None
                    continue
                related = value if prop.uselist else [value]
                keys = [ident(elem) for elem in related]
                for elem_key, elem in zip(keys, related):
                    if elem_key not in seen:
                        seen.add(elem_key)
                        following[elem_key] = elem
                if not prop.uselist:
                    data[prop.key] = keys[0]
                elif rel in ORDERED:
                    data[prop.key] = keys
                else:
                    data[prop.key] = sorted(keys)
        level = following
    return top, graph


MODELS = {}


def init_worker():
    warnings.simplefilter("ignore")
    for name in MAPPINGS:
        MODELS[name] = Model(name)


def run(name, spec, opts, down_only=False):
    """run the statement against the named mapping

    the result has the same form as orm-loading's, without a statement
    count, so that its describe_diff() can be used
    """
    model = MODELS[name]
    model.statement_count = 0
    with model.engine.connect() as conn:
        conn = conn.execution_options(compiled_cache=dict(model.cache))
        with Session(conn) as sess:
            try:
                stmt, ordered = build_stmt(model.classes, spec, opts)
                objs = sess.scalars(stmt).unique().all()
                return ("ok", None, snapshot(objs, ordered, down_only))
            except Exception as err:
                return ("error", type(err).__name__, str(err)[:300])


def runs(spec):
    """(label, mapping, opts, label of the reference) for each run"""
    out = [
        ("reference", "lazy", None, None),
        (f"mapping[{spec['mapping']}]", spec["mapping"], None, "reference"),
    ]
    if spec["opts"]:
        name = spec["opts_mapping"]
        out += [
            ("options reference", "lazy", "lazy", None),
            (f"options[{name}]", name, "eager", "options reference"),
        ]
    return out


def check_spec(spec):
    """run all checks for one spec; returns (findings, results)"""
    findings = []
    results = {}
    for label, name, opts, ref in runs(spec):
        if ref is not None and results[ref][0] == "error":
            continue
        down_only = opts is not None and spec["crit"]
        result = results[label] = run(name, spec, opts, down_only)
        if ref is None:
            continue
        if result[0] == "error" and result[1] == "TooManyStatements":
            findings.append(f"loops[{name}]")
        elif result[0] == "error":
            findings.append(f"raises[{name}]")
        elif result != results[ref]:
            findings.append(label)
    return findings, results


def trial(seed):
    try:
        spec = gen_trial(seed)
        findings, results = check_spec(spec)
    except Exception:
        return seed, "crash", traceback.format_exc(limit=4)
    if findings:
        return seed, "diff", tuple(findings)
    for label in ("reference", "options reference"):
        if label in results and results[label][0] == "error":
            return seed, "invalid", results[label][1:]
    return seed, "ok", None


# ----------------------------------------------------------- reporting


def features(spec):
    """coarse set of the features used by a spec, to help group findings"""
    out = {f"root:{spec['base']}:{spec['entity'][0]}"}
    out.add(f"mapping:{spec['mapping']}")
    if spec["opts"]:
        out.add(f"options_mapping:{spec['opts_mapping']}")
    if spec["crit"]:
        out.add("down_only")
    if spec["join"]:
        join = spec["join"]
        out.add(f"join:{join['key']}")
        if join["outer"]:
            out.add("outerjoin")
    if spec["where"]:
        out.add(f"where:{spec['where'][0]}")
    if spec["distinct"]:
        out.add("distinct")
    if spec["order"]:
        out.add(f"order:{spec['order']}")
    if spec["limit"]:
        lim, off = spec["limit"]
        if lim:
            out.add("limit")
        if off:
            out.add("offset")
    if spec["wlc"] is not None:
        out.add("with_loader_criteria")

    def walk(opts):
        for opt in opts:
            if opt["t"] == "rel":
                out.add(opt["strategy"])
                if opt["of_type"]:
                    out.add(f"of_type:{opt['of_type'][0]}")
                if opt["crit"] is not None:
                    out.add("and_")
                walk(opt["children"])
            else:
                out.add(opt["op"])

    walk(spec["opts"])
    return out


def replay(seed):
    init_worker()
    spec = gen_trial(seed)
    print("--- statement spec")
    pprint.pprint(spec, width=79)

    findings, results = check_spec(spec)
    for label, _, _, ref in runs(spec):
        if label not in results:
            print(f"--- {label}: not run, as {ref} raised")
            continue
        result = results[label]
        if result[0] == "error":
            print(f"--- {label}: raised {result[1]}: {result[2]}")
        elif ref is None:
            print(f"--- {label}: OK, {len(result[2][0])} rows")
        else:
            diff = describe_diff(results[ref], result).strip()
            print(f"--- {label}: {diff and 'differs from ' + ref or 'OK'}")
            if diff:
                print(describe_diff(results[ref], result))
    print(f"--- findings: {findings or 'none'}")


def seed_features(seed):
    return features(gen_trial(seed))
