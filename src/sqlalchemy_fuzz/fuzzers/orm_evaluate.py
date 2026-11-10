"""
ORM UPDATE / DELETE: synchronize_session="evaluate" vs. the database.

An ORM-enabled update() or delete() with synchronize_session="evaluate"
reproduces the statement's WHERE criteria and SET expressions in Python, to
find and update (or mark deleted) the objects already in the Session
without a round trip.  The Python evaluation must agree with what the
database did.

Each seed generates a small table of rows, with NULLs sprinkled in, over a
mapped class with integer, numeric, string, boolean and date columns, and an
update() or delete() whose WHERE criteria and SET values are random, well
typed expressions: comparisons (including column to column and against
None), and_ / or_ / not_, in_() / not_in() (empty, or with None in the
list), tuple_().in_(), is_() / is_not(), startswith() / endswith(),
arithmetic including / and %, string concatenation, and constructs the
evaluator can't handle (between(), case(), functions, ...).  All rows are
loaded into a Session first, and some seeds expire some of the objects'
attributes, or whole objects.

The statement is run, each time in a new transaction against the same rows,
with synchronize_session set to:

evaluate
    The subject of this fuzzer.  May raise "Could not evaluate current
    criteria in Python" when it can't handle the WHERE criteria; that is
    documented, and the seed is then reported as invalid, with the
    construct named.  SET values it can't evaluate are expired instead.

fetch
    Locates the affected objects using RETURNING, but still evaluates the
    SET values in Python.

auto
    Uses "evaluate" when the WHERE criteria can be evaluated, otherwise
    "fetch".

After each run the objects in the Session are compared with the rows in
the database, read in the same transaction: every attribute that is loaded
must equal the database's value (expired attributes are fine: they are
loaded from the database when next accessed), and an object must be marked
deleted exactly when its row is gone (or be fully expired).  "fetch"
agreeing with the database implies "evaluate" and "fetch" agree with each
other.  The checks, each per synchronize_session setting:

where
    An object differs from its row, and the Session updated or deleted it
    while the database didn't, or the other way around.  The rows the
    database matched are found with a SELECT using the same WHERE criteria
    before the statement, and the objects the Session updated through the
    "refresh" event.

values
    An object the Session and the database both updated differs from its
    row.

raise
    The statement raises with one setting but not with another, other than
    evaluate's documented "Could not evaluate".  A statement the database
    itself can't run is invalid.

SQLAlchemy evaluates SET values in the order of a set of attribute names,
which depends on PYTHONHASHSEED, so whether a SET value that reads another
column the statement SETs gives a difference can change between processes;
the Fuzz workflow runs with PYTHONHASHSEED=0.

The database's semantics are the reference.  This runs against SQLite only,
so it tests the evaluator against SQLite's semantics; behaviors that differ
between backends are out of scope, for example Oracle treating '' as NULL,
division by zero (NULL on SQLite and MySQL, an error on PostgreSQL), or
LIKE being case-insensitive on SQLite.  The generator avoids expressions
whose result depends on such backend behavior, or on how a column stores a
value rather than on the evaluator: no division by zero, only lowercase
strings without LIKE wildcards, no % on Numeric (SQLite casts the operands
to integers), and SET values for the Numeric column only from + and - of
values that are exact in two decimal places, as SQLite stores Numeric as a
float.

"""

from __future__ import annotations

import datetime
import decimal
import pprint
import random
import re
import traceback
import warnings

from sqlalchemy import and_
from sqlalchemy import Boolean
from sqlalchemy import case
from sqlalchemy import cast
from sqlalchemy import Column
from sqlalchemy import create_engine
from sqlalchemy import Date
from sqlalchemy import delete
from sqlalchemy import event
from sqlalchemy import exc
from sqlalchemy import false
from sqlalchemy import func
from sqlalchemy import inspect
from sqlalchemy import Integer
from sqlalchemy import literal
from sqlalchemy import not_
from sqlalchemy import null
from sqlalchemy import Numeric
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy import String
from sqlalchemy import true
from sqlalchemy import tuple_
from sqlalchemy import update
from sqlalchemy.orm import declarative_base
from sqlalchemy.orm import Session

NAME = "orm-evaluate"
DESCRIPTION = (
    'ORM UPDATE / DELETE synchronize_session="evaluate" vs. the database'
)

CHECKS = {
    "where": "updates or deletes different objects in the Session than "
    "the rows its WHERE criteria matched in the database, leaving an object "
    "whose loaded state differs from its row",
    "values": "leaves an object in the Session that it updated with an "
    "attribute value different from the value in the database",
    "raise": "raises, other than evaluate's documented \"Could not "
    'evaluate current criteria in Python", with one synchronize_session '
    "setting but not with another",
}

# the synchronize_session settings run; they appear in brackets after the
# check name, e.g. where[evaluate]
MAPPINGS = ("evaluate", "fetch", "auto")


# ----------------------------------------------------------------- model

# column name -> type name
COLUMNS = {
    "i1": "int",
    "i2": "int",
    "n1": "num",
    "n2": "num",
    "s1": "str",
    "s2": "str",
    "b1": "bool",
    "b2": "bool",
    "d1": "date",
    "d2": "date",
}

SQLTYPES = {
    "int": Integer,
    "num": lambda: Numeric(10, 2),
    "str": lambda: String(20),
    "bool": Boolean,
    "date": Date,
}

# values for rows and literals; literals come from the same values so that
# comparisons match some rows.  Numeric values are exact in binary floating
# point, as SQLite stores Numeric as REAL; strings are lowercase, as LIKE is
# case-insensitive on SQLite.
VALUES = {
    "int": [-3, -1, 0, 1, 2, 3, 5],
    "num": [
        decimal.Decimal(x)
        for x in ("-1.25", "0", "0.5", "1.5", "2.25", "3.75")
    ],
    "str": ["", "a", "ab", "abc", "b", "ba", "bab"],
    "bool": [True, False],
    "date": [
        datetime.date(2023, 12, 31),
        datetime.date(2024, 1, 1),
        datetime.date(2024, 2, 29),
        datetime.date(2025, 6, 15),
    ],
}

# divisors for / and %; never zero
DIVISORS = [-3, -2, 2, 3, 4]

NROWS = 6

MODES = MAPPINGS

EXPIRED = "<expired>"
DELETED = "<deleted>"


class Model:
    def __init__(self):
        Base = declarative_base()

        class Thing(Base):
            __tablename__ = "thing"
            id = Column(Integer, primary_key=True, autoincrement=False)
            i1 = Column(Integer)
            i2 = Column(Integer)
            n1 = Column(Numeric(10, 2))
            n2 = Column(Numeric(10, 2))
            s1 = Column(String(20))
            s2 = Column(String(20))
            b1 = Column(Boolean)
            b2 = Column(Boolean)
            d1 = Column(Date)
            d2 = Column(Date)

        self.cls = Thing
        self.table = Thing.__table__
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)

        # objects the Session synchronized with an UPDATE, with any mode,
        # receive a "refresh" event with no query context
        self.refreshed = set()

        @event.listens_for(Thing, "refresh")
        def refreshed(target, context, attrs):  # noqa: U100
            if context is None:
                self.refreshed.add(inspect(target).identity[0])

    def reset(self, rows):
        with self.engine.begin() as conn:
            conn.execute(self.table.delete())
            conn.execute(self.table.insert(), rows)


# ------------------------------------------------------ spec generation
#
# a "spec" is a plain data structure describing the rows, the Session state
# and the statement; build_stmt() turns it into a statement.  Expressions
# are tuples:
#
#   ("col", name)                 a column of the mapped class
#   ("lit", type, value)          a literal of the given type
#   ("null",)                     null()
#   ("arith", op, a, b)           a + b, a - b, a * b
#   ("div", a, n), ("mod", a, n)  a / n, a % n for a nonzero literal n
#   ("concat", a, b)              string a + b
#   ("cmp", op, a, b)             a == b, a < b, ...
#   ("cmp", op, a, ("none",))     a == None, a != None
#   ("is", type, a, value, negate)
#                                 a.is_(value) / a.is_not(value)
#   ("in", type, a, values, negate)
#                                 a.in_(values) / a.not_in(values)
#   ("tuple_in", cols, rows, negate)
#   ("like", op, a, b)            a.startswith(b), a.endswith(b)
#   ("and", conds), ("or", conds), ("not", cond)
#   ("const", bool)               true() / false()
#   ("bool_col", name, form)      a Boolean column as a condition, bare or
#                                 compared to True / False
#   ("uneval", kind, ...)         a construct the evaluator can't handle


def columns_of(typ):
    return [name for name, t in COLUMNS.items() if t == typ]


def gen_lit(rng, typ):
    return ("lit", typ, rng.choice(VALUES[typ]))


def gen_value(rng, typ, depth, where):
    """an expression of the given type; ``where`` allows constructs whose
    value depends on how the column would store it, which are only safe to
    compare, not to SET"""
    if typ == "bool":
        return gen_cond(rng, depth, where)

    cols = columns_of(typ)
    if typ == "int" and where:
        cols = cols + ["id"]
    choices = [("col", 5), ("lit", 3), ("null", 0.4)]
    if depth > 0:
        if typ == "int":
            choices += [("arith", 2), ("mod", 0.6)]
        elif typ == "num":
            choices += [("arith", 2)]
        elif typ == "str":
            choices += [("concat", 2)]
    choices.append(("uneval", 0.15))
    kind = _weighted(rng, choices)

    if kind == "col":
        return ("col", rng.choice(cols))
    elif kind == "lit":
        return gen_lit(rng, typ)
    elif kind == "null":
        return ("null",)
    elif kind == "arith":
        if typ == "num":
            ops = ["+", "-"] + (["*"] if where else [])
            # Numeric with Integer is Numeric
            other = rng.choice(["num", "num", "int"])
        else:
            ops = ["+", "-", "*"]
            other = "int"
        a = gen_value(rng, typ, depth - 1, where)
        b = gen_value(rng, other, depth - 1, where)
        if rng.random() < 0.5:
            a, b = b, a
        op = rng.choice(ops)
        if op == "*" and typ == "num":
            # stays exact: Numeric times a small integer
            b = ("lit", "int", rng.choice(DIVISORS))
        return ("arith", op, a, b)
    elif kind == "mod":
        return ("mod", gen_value(rng, "int", depth - 1, where), _divisor(rng))
    elif kind == "concat":
        return (
            "concat",
            gen_value(rng, "str", depth - 1, where),
            gen_value(rng, "str", depth - 1, where),
        )
    else:
        return gen_uneval_value(rng, typ, depth, where)


def gen_uneval_value(rng, typ, depth, where):
    sub = max(depth - 1, 0)
    kinds = ["case", "coalesce"]
    if typ in ("int", "str"):
        # CAST(x AS DATE) is a number on SQLite
        kinds.append("cast")
    if typ == "int":
        kinds.append("neg")
    kind = rng.choice(kinds)
    if kind == "case":
        return (
            "uneval",
            "case",
            gen_cond(rng, sub, where),
            gen_value(rng, typ, sub, where),
            gen_value(rng, typ, sub, where),
        )
    elif kind == "coalesce":
        return (
            "uneval",
            "coalesce",
            gen_value(rng, typ, sub, where),
            gen_lit(rng, typ),
        )
    elif kind == "cast":
        return ("uneval", "cast", typ, ("col", rng.choice(columns_of(typ))))
    else:
        return ("uneval", "neg", gen_value(rng, "int", sub, where))


def _divisor(rng):
    return rng.choice(DIVISORS)


def _weighted(rng, choices):
    total = sum(w for _, w in choices)
    r = rng.random() * total
    for name, w in choices:
        r -= w
        if r < 0:
            return name
    return choices[-1][0]


def gen_cond(rng, depth, where):
    choices = [
        ("cmp", 5),
        ("bool_col", 1),
        ("is", 1.2),
        ("in", 1.5),
        ("like", 0.7),
        ("div", 0.4),
        ("const", 0.15),
        ("tuple_in", 0.25),
        ("uneval", 0.25),
    ]
    if depth > 0:
        choices += [("and", 1.5), ("or", 1.5), ("not", 1.5), ("cmp_bool", 0.4)]
    kind = _weighted(rng, choices)
    sub = depth - 1

    if kind == "cmp":
        typ = rng.choice(["int", "int", "num", "str", "date"])
        if typ in ("str", "date") and rng.random() < 0.3:
            op = rng.choice(["==", "!="])
        else:
            op = rng.choice(["==", "!=", "<", "<=", ">", ">="])
        a = gen_value(rng, typ, depth, where)
        if rng.random() < 0.15:
            # col == None renders IS NULL, which only = and != allow;
            # col < null() is NULL
            if op in ("==", "!=") and rng.random() < 0.7:
                return ("cmp", op, a, ("none",))
            b = ("null",)
        else:
            b = gen_value(rng, typ, depth, where)
        if rng.random() < 0.3:
            a, b = b, a
        if ("null",) in (a, b):
            # only = and != are allowed against null(), as IS / IS NOT
            op = rng.choice(["==", "!="])
        return ("cmp", op, a, b)
    elif kind == "cmp_bool":
        return (
            "cmp",
            rng.choice(["==", "!="]),
            gen_cond(rng, sub, where),
            gen_cond(rng, sub, where),
        )
    elif kind == "bool_col":
        # as a condition, a bare Boolean column is coerced to is_true(),
        # which the evaluator can't evaluate
        form = rng.choice(["== True", "== False", "== True", "== False", ""])
        return ("bool_col", rng.choice(columns_of("bool")), form)
    elif kind == "is":
        typ = rng.choice(list(SQLTYPES))
        a = gen_value(rng, typ, max(sub, 0), where)
        value = None if rng.random() < 0.6 else rng.choice(VALUES[typ])
        return ("is", typ, a, value, rng.random() < 0.5)
    elif kind == "in":
        typ = rng.choice(["int", "num", "str", "bool", "date"])
        n = rng.choice([0, 1, 2, 3, 3])
        values = [rng.choice(VALUES[typ]) for _ in range(n)]
        if values and rng.random() < 0.2:
            values[rng.randrange(len(values))] = None
        a = gen_value(rng, typ, max(sub, 0), where)
        return ("in", typ, a, values, rng.random() < 0.4)
    elif kind == "tuple_in":
        cols = rng.sample(["i1", "i2", "s1", "s2", "d1", "b1"], 2)
        rows = [
            [rng.choice(VALUES[COLUMNS[c]]) for c in cols]
            for _ in range(rng.randint(1, 3))
        ]
        return ("tuple_in", cols, rows, rng.random() < 0.4)
    elif kind == "like":
        a = gen_value(rng, "str", max(sub, 0), where)
        if rng.random() < 0.7:
            b = gen_lit(rng, "str")
        else:
            b = gen_value(rng, "str", max(sub, 0), where)
        return ("like", rng.choice(["startswith", "endswith"]), a, b)
    elif kind == "div":
        # Integer / Integer is true division on all backends since 2.0
        return (
            "cmp",
            rng.choice(["==", "!=", "<", "<=", ">", ">="]),
            ("div", gen_value(rng, "int", max(sub, 0), where), _divisor(rng)),
            gen_lit(rng, "int"),
        )
    elif kind == "const":
        return ("const", rng.random() < 0.5)
    elif kind in ("and", "or"):
        return (
            kind,
            [gen_cond(rng, sub, where) for _ in range(rng.randint(2, 3))],
        )
    elif kind == "not":
        return ("not", gen_cond(rng, sub, where))
    else:
        return gen_uneval_cond(rng, max(sub, 0), where)


def gen_uneval_cond(rng, depth, where):
    kind = rng.choice(["between", "contains", "distinct_from", "like"])
    if kind == "between":
        typ = rng.choice(["int", "num", "date"])
        lo, hi = sorted([rng.choice(VALUES[typ]), rng.choice(VALUES[typ])])
        return (
            "uneval",
            "between",
            gen_value(rng, typ, depth, where),
            lo,
            hi,
        )
    elif kind == "contains":
        return ("uneval", "contains", rng.choice(["s1", "s2"]), "a")
    elif kind == "like":
        return ("uneval", "like", rng.choice(["s1", "s2"]), "a%")
    else:
        typ = rng.choice(["int", "str", "date"])
        return (
            "uneval",
            "distinct_from",
            ("col", rng.choice(columns_of(typ))),
            gen_value(rng, typ, depth, where),
            rng.random() < 0.5,
        )


def gen_rows(rng):
    rows = []
    for ident in range(1, NROWS + 1):
        row = {"id": ident}
        for name, typ in COLUMNS.items():
            if rng.random() < 0.2:
                row[name] = None
            else:
                row[name] = rng.choice(VALUES[typ])
        rows.append(row)
    return rows


def gen_expire(rng):
    """attributes to expire after loading, as (id, attribute names or None
    for the whole object)"""
    out = []
    if rng.random() < 0.25:
        for ident in rng.sample(range(1, NROWS + 1), rng.randint(1, 2)):
            if rng.random() < 0.3:
                out.append((ident, None))
            else:
                out.append(
                    (
                        ident,
                        sorted(rng.sample(list(COLUMNS), rng.randint(1, 3))),
                    )
                )
    return out


def gen_spec(seed):
    rng = random.Random(seed)
    spec = {
        "rows": gen_rows(rng),
        "expire": gen_expire(rng),
        "kind": "update" if rng.random() < 0.7 else "delete",
        "where": [
            gen_cond(rng, rng.randint(0, 3), True)
            for _ in range(rng.choice([0, 1, 1, 1, 1, 2, 2]))
        ],
        "values": [],
    }
    if spec["kind"] == "update":
        for name in rng.sample(list(COLUMNS), rng.randint(1, 3)):
            typ = COLUMNS[name]
            spec["values"].append(
                (name, gen_value(rng, typ, rng.randint(0, 2), False))
            )
    return spec


# ------------------------------------------------------ statement build


ARITH = {
    "+": lambda a, b: a + b,
    "-": lambda a, b: a - b,
    "*": lambda a, b: a * b,
}

CMP = {
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
}


def _sqltype(typ):
    return SQLTYPES[typ]()


def build(cls, spec):
    """turn an expression spec into a SQL expression"""
    kind = spec[0]
    if kind == "col":
        return getattr(cls, spec[1])
    elif kind == "lit":
        return literal(spec[2], _sqltype(spec[1]))
    elif kind == "null":
        return null()
    elif kind == "arith":
        return ARITH[spec[1]](build(cls, spec[2]), build(cls, spec[3]))
    elif kind == "div":
        return build(cls, spec[1]) / literal(spec[2], Integer())
    elif kind == "mod":
        return build(cls, spec[1]) % literal(spec[2], Integer())
    elif kind == "concat":
        return build(cls, spec[1]) + build(cls, spec[2])
    elif kind == "cmp":
        a = build(cls, spec[2])
        b = None if spec[3] == ("none",) else build(cls, spec[3])
        return CMP[spec[1]](a, b)
    elif kind == "is":
        _, typ, a, value, negate = spec
        a = build(cls, a)
        if value is not None:
            value = literal(value, _sqltype(typ))
        return a.is_not(value) if negate else a.is_(value)
    elif kind == "in":
        _, typ, a, values, negate = spec
        a = build(cls, a)
        return a.not_in(values) if negate else a.in_(values)
    elif kind == "tuple_in":
        _, cols, rows, negate = spec
        expr = tuple_(*[getattr(cls, c) for c in cols])
        rows = [tuple(row) for row in rows]
        return expr.not_in(rows) if negate else expr.in_(rows)
    elif kind == "like":
        a, b = build(cls, spec[2]), build(cls, spec[3])
        return getattr(a, spec[1])(b)
    elif kind == "and":
        return and_(*[build(cls, c) for c in spec[1]])
    elif kind == "or":
        return or_(*[build(cls, c) for c in spec[1]])
    elif kind == "not":
        return not_(build(cls, spec[1]))
    elif kind == "const":
        return true() if spec[1] else false()
    elif kind == "bool_col":
        col = getattr(cls, spec[1])
        if spec[2] == "== True":
            return col == True  # noqa: E712
        elif spec[2] == "== False":
            return col == False  # noqa: E712
        return col
    elif kind == "uneval":
        return build_uneval(cls, spec)
    raise ValueError(spec)


def build_uneval(cls, spec):
    kind = spec[1]
    if kind == "case":
        return case(
            (build(cls, spec[2]), build(cls, spec[3])),
            else_=build(cls, spec[4]),
        )
    elif kind == "coalesce":
        return func.coalesce(build(cls, spec[2]), build(cls, spec[3]))
    elif kind == "cast":
        return cast(build(cls, spec[3]), _sqltype(spec[2]))
    elif kind == "neg":
        return -build(cls, spec[2])
    elif kind == "between":
        return build(cls, spec[2]).between(spec[3], spec[4])
    elif kind == "contains":
        return getattr(cls, spec[2]).contains(spec[3])
    elif kind == "like":
        return getattr(cls, spec[2]).like(spec[3])
    elif kind == "distinct_from":
        a, b = build(cls, spec[2]), build(cls, spec[3])
        return a.is_not_distinct_from(b) if spec[4] else a.is_distinct_from(b)
    raise ValueError(spec)


def build_where(cls, spec):
    return [build(cls, cond) for cond in spec["where"]]


def build_stmt(cls, spec):
    where = build_where(cls, spec)
    if spec["kind"] == "delete":
        return delete(cls).where(*where)
    values = {getattr(cls, name): build(cls, v) for name, v in spec["values"]}
    return update(cls).where(*where).values(values)


# ------------------------------------------------------------- running


def snapshot(obj):
    """an object's state in the Session, read from __dict__ only"""
    state = inspect(obj)
    if state.deleted or state.was_deleted or state.detached:
        return DELETED
    return {
        key: state.dict[key] if key in state.dict else EXPIRED
        for key in COLUMNS
    }


def _kind(value):
    if value is None:
        return "null"
    elif isinstance(value, bool):
        return "bool"
    elif isinstance(value, (int, float, decimal.Decimal)):
        return "number"
    elif isinstance(value, str):
        return "str"
    elif isinstance(value, datetime.date):
        return "date"
    return "other"


def same(a, b):
    """value equality; doesn't let a bool equal 1, and doesn't call __eq__
    on unexpected objects, such as the evaluator's _ExpiredObject, which is
    equal to everything"""
    kind = _kind(a)
    return kind == _kind(b) and kind != "other" and a == b


def show(value):
    if _kind(value) == "other":
        return f"<{type(value).__module__}.{type(value).__name__} object>"
    return repr(value)


MODELS = {}


def init_worker():
    # SQLite's Decimal warning
    warnings.simplefilter("ignore")
    MODELS["default"] = Model()


def _is_unevaluatable(err):
    return isinstance(
        err, exc.InvalidRequestError
    ) and "Could not evaluate current criteria in Python" in str(err)


def run(model, spec, mode):
    """run the statement with one synchronize_session setting.

    Returns ``("ok", session state, database rows, ids matched by the
    database, ids synchronized by the Session)``, ``("unevaluatable",
    message)`` or ``("error", class name, message, is database error)``.

    """
    cls = model.cls
    with Session(model.engine) as sess:
        try:
            objs = sess.scalars(select(cls).order_by(cls.id)).all()
            for ident, attrs in spec["expire"]:
                sess.expire(sess.get(cls, ident), attrs)

            db_matched = set(
                sess.scalars(select(cls.id).where(*build_where(cls, spec)))
            )
            stmt = build_stmt(cls, spec)
            model.refreshed.clear()
            sess.execute(stmt, execution_options={"synchronize_session": mode})
        except Exception as err:
            if mode == "evaluate" and _is_unevaluatable(err):
                # e.g. "Cannot evaluate BinaryExpression with operator
                # <function like_op at 0x7f30e9096f00>"
                message = re.sub(r" at 0x[0-9a-f]+", "", str(err.__cause__))
                return ("unevaluatable", message[:300])
            return (
                "error",
                type(err).__name__,
                str(err)[:300],
                isinstance(err, exc.DBAPIError),
            )

        state = {inspect(obj).identity[0]: snapshot(obj) for obj in objs}
        session_matched = set(model.refreshed)
        db = {
            row.id: dict(row._mapping)
            for row in sess.connection().execute(
                select(model.table).order_by(model.table.c.id)
            )
        }
        return ("ok", state, db, db_matched, session_matched)


def compare(outcome):
    """differences between the Session and the database for an "ok" run,
    as a list of (check, id, attribute, session value, database value)"""
    _, state, db, db_matched, session_matched = outcome
    diffs = []
    for ident, snap in state.items():
        row = db.get(ident)
        if row is None:
            # a fully expired object raises ObjectDeletedError when used
            if snap is DELETED or all(v is EXPIRED for v in snap.values()):
                continue
            diffs.append(("where", ident, None, "not deleted", "deleted"))
        elif snap is DELETED:
            diffs.append(("where", ident, None, "deleted", "not deleted"))
        else:
            where = (ident in db_matched) != (ident in session_matched)
            for key, value in snap.items():
                if value is EXPIRED or same(value, row[key]):
                    continue
                diffs.append(
                    (
                        "where" if where else "values",
                        ident,
                        key,
                        value,
                        row[key],
                    )
                )
    return diffs


def check_spec(spec):
    """run all modes for one spec; returns (status, info, outcomes)"""
    model = MODELS["default"]
    model.reset(spec["rows"])
    outcomes = {mode: run(model, spec, mode) for mode in MODES}

    if not any(o[0] == "ok" for o in outcomes.values()):
        errors = [o for o in outcomes.values() if o[0] == "error"]
        return "invalid", (errors[0][1], errors[0][2]), outcomes
    for outcome in outcomes.values():
        if outcome[0] == "error" and outcome[3]:
            return "invalid", (outcome[1], outcome[2]), outcomes

    findings = set()
    for mode, outcome in outcomes.items():
        if outcome[0] == "error":
            findings.add(("raise", mode))
        elif outcome[0] == "ok":
            for diff in compare(outcome):
                findings.add((diff[0], mode))
    if findings:
        return (
            "diff",
            tuple(
                f"{check}[{mode}]"
                for check in CHECKS
                for mode in MODES
                if (check, mode) in findings
            ),
            outcomes,
        )
    if outcomes["evaluate"][0] == "unevaluatable":
        return (
            "invalid",
            ("UnevaluatableError", outcomes["evaluate"][1]),
            (outcomes),
        )
    return "ok", None, outcomes


def trial(seed):
    try:
        spec = gen_spec(seed)
        status, info, _ = check_spec(spec)
    except Exception:
        return seed, "crash", traceback.format_exc(limit=4)
    return seed, status, info


# ----------------------------------------------------------- reporting


def features(spec):
    """coarse set of the features used by a spec, to help group findings"""
    out = {spec["kind"]}
    if not spec["where"]:
        out.add("no_where")
    expired = set()
    for ident, attrs in spec["expire"]:
        out.add("expired_object" if attrs is None else "expired_attrs")
        expired.update(attrs or ())

    def walk(expr, where, under_not):
        prefix = "where" if where else "set"
        kind = expr[0]
        if kind in ("col", "lit", "null", "none", "const", "bool_col"):
            return
        if kind == "uneval":
            out.add(f"{prefix}:uneval:{expr[1]}")
        elif kind in ("arith", "cmp"):
            out.add(f"{prefix}:{kind}{expr[1]}")
        elif kind in ("in", "tuple_in"):
            name = kind if not expr[-1] else kind.replace("in", "not_in")
            out.add(f"{prefix}:{name}")
            values = expr[3] if kind == "in" else expr[2]
            if not values:
                out.add(f"{prefix}:{kind}:empty")
            elif None in values:
                out.add(f"{prefix}:{kind}:none")
        elif kind == "is":
            out.add(f"{prefix}:{'is_not' if expr[4] else 'is'}")
        else:
            out.add(f"{prefix}:{kind}")
        if under_not and kind in ("and", "or", "in", "tuple_in"):
            out.add(f"{prefix}:not:{kind}")
        for sub in expr[1:]:
            subs = sub if isinstance(sub, list) else [sub]
            for elem in subs:
                if (
                    isinstance(elem, tuple)
                    and elem
                    and isinstance(elem[0], str)
                ):
                    walk(elem, where, under_not or kind == "not")

    for cond in spec["where"]:
        walk(cond, True, False)
        if _columns_used(cond) & expired:
            out.add("where:reads_expired_attr")
    set_cols = {name for name, _ in spec["values"]}
    for name, value in spec["values"]:
        walk(value, False, False)
        if _columns_used(value) & (set_cols - {name}):
            out.add("set:reads_other_set_column")
        if _columns_used(value) & expired:
            out.add("set:reads_expired_attr")
    return out


def _columns_used(expr):
    out = set()
    if expr[0] in ("col", "bool_col"):
        out.add(expr[1])
    elif expr[0] == "tuple_in":
        out.update(expr[1])
    elif expr[:2] in (("uneval", "contains"), ("uneval", "like")):
        out.add(expr[2])
    for sub in expr[1:]:
        subs = sub if isinstance(sub, list) else [sub]
        for elem in subs:
            if isinstance(elem, tuple) and elem and isinstance(elem[0], str):
                out |= _columns_used(elem)
    return out


def sql_text(model, spec):
    stmt = build_stmt(model.cls, spec)
    try:
        return str(
            stmt.compile(
                dialect=model.engine.dialect,
                compile_kwargs={"literal_binds": True},
            )
        )
    except Exception:
        return str(stmt.compile(dialect=model.engine.dialect))


def describe(mode, outcome):
    if outcome[0] == "unevaluatable":
        return [f"  could not evaluate (documented): {outcome[1]}"]
    elif outcome[0] == "error":
        return [f"  raised {outcome[1]}: {outcome[2]}"]
    _, state, db, db_matched, session_matched = outcome
    lines = [
        f"  rows matched by the database: {sorted(db_matched)}",
        f"  objects updated in the Session: {sorted(session_matched)}",
    ]
    diffs = compare(outcome)
    for check, ident, key, value, db_value in diffs:
        if key is None:
            lines.append(
                f"  {check}: Thing(id={ident}) is {value} in the Session, "
                f"its row is {db_value}"
            )
        else:
            lines.append(
                f"  {check}: Thing(id={ident}).{key}: Session has "
                f"{show(value)}, database has {show(db_value)}"
            )
    if not diffs:
        lines.append("  Session agrees with the database")
    return lines


def replay(seed):
    init_worker()
    spec = gen_spec(seed)
    model = MODELS["default"]
    print("--- spec")
    pprint.pprint(spec, width=79)
    print("--- SQL")
    print(sql_text(model, spec))
    status, info, outcomes = check_spec(spec)
    for mode in MODES:
        print(f'--- synchronize_session="{mode}"')
        print("\n".join(describe(mode, outcomes[mode])))
    print(f"--- {status}: {info or ''}")
    if "set:reads_other_set_column" in features(spec):
        print(
            "--- note: a SET value reads a column that the statement also "
            "SETs.  SQLAlchemy\nevaluates SET values in the order of a set of "
            "attribute names, which depends\non PYTHONHASHSEED, so the result "
            "can differ between processes; the Fuzz\nworkflow runs with "
            "PYTHONHASHSEED=0."
        )


def seed_features(seed):
    return features(gen_spec(seed))
