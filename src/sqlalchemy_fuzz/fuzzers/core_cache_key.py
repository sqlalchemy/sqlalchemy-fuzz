"""
Core statements vs. the compiled cache.

The compiled cache relies on one rule: two statements with equal cache
keys compile to the same SQL, and the bound parameter values extracted
from one line up with the compiled form of the other.

Generates random Core statements against three small tables: selects with
joins and outer joins, subqueries, scalar subqueries, EXISTS / IN
subqueries, CTEs (including recursive and nesting ones), correlate() /
correlate_except(), lateral(), set operations, with_only_columns(), label
styles, type_coerce() / cast(), literal_column(), text() fragments and
TextualSelect, values(), aggregates, ORDER BY / LIMIT / OFFSET / FETCH,
FOR UPDATE, literal_execute and expanding parameters, .params(), and
insert() / update() / delete() with RETURNING.  Each statement T is built
from a plain-data spec, which is built again for each use, so that every
statement has new alias / subquery / table() objects.  Alongside T, the
seed generates:

literal variant L
    T with only literal values changed (bound values, IN list lengths,
    LIMIT / OFFSET values, ...).

structural variants M
    T with one structural change each (an operator, a flag, a join type,
    the correlation, a literal's type, ...).

and checks:

collision
    a variant with the same cache key as T must compile the same as T, on
    each of several dialects: the same SQL string, result columns, bound
    parameter types and literal_execute parameters.  Anonymous parameter
    names aren't part of the cache key, only their positions, so they're
    compared by position.

anon_labels
    as for collision, but where the only difference is the numbering of
    anonymous labels ("coalesce_2" vs. "coalesce_3"), which share their
    counter with anonymous parameter names; these become result keys.

literal_key
    L must have the same cache key as T.

params
    for a variant with the same cache key as T, compiling T into a cache
    and then looking the variant up in it must give the same parameters
    as compiling the variant on its own.

execution
    T executed on SQLite after L and the structural variants ran on the
    same cache must return the same rows (and, for DML, leave the same
    table contents) as T executed with ``compiled_cache=None``.

A cache key that changes for a structural change while the SQL stays the
same is only a missed cache hit, and isn't reported.

"""

from __future__ import annotations

import copy
import difflib
import operator
import pprint
import random
import re
import time
import traceback
import warnings

from sqlalchemy import and_
from sqlalchemy import BigInteger
from sqlalchemy import bindparam
from sqlalchemy import case
from sqlalchemy import cast
from sqlalchemy import Column
from sqlalchemy import column
from sqlalchemy import create_engine
from sqlalchemy import delete
from sqlalchemy import event
from sqlalchemy import except_
from sqlalchemy import except_all
from sqlalchemy import ForeignKey
from sqlalchemy import func
from sqlalchemy import insert
from sqlalchemy import Integer
from sqlalchemy import intersect
from sqlalchemy import intersect_all
from sqlalchemy import LABEL_STYLE_DISAMBIGUATE_ONLY
from sqlalchemy import LABEL_STYLE_NONE
from sqlalchemy import LABEL_STYLE_TABLENAME_PLUS_COL
from sqlalchemy import literal
from sqlalchemy import literal_column
from sqlalchemy import MetaData
from sqlalchemy import not_
from sqlalchemy import Numeric
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy import String
from sqlalchemy import Table
from sqlalchemy import table
from sqlalchemy import Text
from sqlalchemy import text
from sqlalchemy import true
from sqlalchemy import type_coerce
from sqlalchemy import union
from sqlalchemy import union_all
from sqlalchemy import update
from sqlalchemy import values
from sqlalchemy.dialects import mssql
from sqlalchemy.dialects import mysql
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects import sqlite

NAME = "core-cache-key"
DESCRIPTION = "Core statement cache keys vs. the compiled cache"

CHECKS = {
    "collision": "has the same cache key as a variant of it that compiles "
    "differently (SQL, result columns or bound parameter types)",
    "anon_labels": "has the same cache key as a variant of it whose "
    "anonymous labels, which become result keys, are numbered differently",
    "literal_key": "gets a different cache key when built again with only "
    "literal values changed",
    "params": "gets different bound parameter values from the compiled "
    "form of an equal-key statement than when compiled on its own",
    "execution": "returns different rows when run after its variants on "
    "the same cache than with compiled_cache=None",
}

# variants per seed with one structural change each
NUM_MUTANTS = 4


# ----------------------------------------------------------------- schema

INT, STR = "int", "str"

TABLES = {
    "t1": {"id": INT, "a": INT, "b": STR},
    "t2": {"id": INT, "t1_id": INT, "a": INT, "b": STR},
    "t3": {"id": INT, "t2_id": INT, "a": INT, "b": STR},
}
# pairs of tables that join() can join without an ON clause
FKS = {("t1", "t2"), ("t2", "t1"), ("t2", "t3"), ("t3", "t2")}

ROWS = {
    "t1": [
        (1, 1, "x"),
        (2, 2, "y"),
        (3, 3, "z"),
        (4, None, "x"),
        (5, 5, None),
    ],
    "t2": [
        (1, 1, 1, "x"),
        (2, 1, 2, "y"),
        (3, 2, 3, "x"),
        (4, 3, None, "z"),
        (5, None, 5, "y"),
        (6, 5, 1, None),
    ],
    "t3": [(1, 1, 1, "y"), (2, 2, 2, "x"), (3, 2, 3, "z"), (4, 6, 4, "x")],
}

# literal values, by domain; changing a value within its domain is a
# literal-only change
DOMAINS = {
    INT: [0, 1, 2, 3, 5],
    STR: ["x", "y", "z", "w"],
    "pat": ["x%", "%y", "%", "_"],
    "id": [100, 101, 102],
    "n": [0, 1, 2, 3],
}
DOMAIN_KIND = {INT: INT, STR: STR, "pat": STR, "id": INT, "n": INT}

TYPES = {
    "Integer": (INT, Integer),
    "BigInteger": (INT, BigInteger),
    "Numeric": (INT, lambda: Numeric(10, 2)),
    "String": (STR, String),
    "String(10)": (STR, lambda: String(10)),
    "Text": (STR, Text),
}
SQL_TYPES = {INT: Integer, STR: String}

LIT_MODES = ["plain", "literal", "typed", "bindparam", "execute"]
LITERAL_COLUMNS = {INT: ["1", "2", "1 + 1"], STR: ["'x'", "'y'"]}
BINOPS = {
    INT: ["+", "-", "*"],
    STR: ["+"],
}
BINOP_FNS = {"+": operator.add, "-": operator.sub, "*": operator.mul}
# function name -> (argument type, result type); functions with the same
# signature can replace each other
FUNCS = {
    "abs": (INT, INT),
    "lower": (STR, STR),
    "upper": (STR, STR),
    "trim": (STR, STR),
    "length": (STR, INT),
}
AGGS = {INT: ["count", "max", "min", "sum"], STR: ["max", "min"]}
CMPS = {
    "==": operator.eq,
    "!=": operator.ne,
    "<": operator.lt,
    ">": operator.gt,
    "<=": operator.le,
    ">=": operator.ge,
}
LIKES = ["like", "ilike", "not_like", "not_ilike"]
STARTS = ["startswith", "endswith", "contains"]
IN_MODES = ["list", "expanding", "execute"]
CORRS = [None, "none", "except_none", "parent", "except_parent", "except_one"]
SET_OPS = {
    "union": union,
    "union_all": union_all,
    "intersect": intersect,
    "except_": except_,
    # not supported by SQLite, so only compiled
    "intersect_all": intersect_all,
    "except_all": except_all,
}
LABEL_STYLES = {
    None: None,
    "none": LABEL_STYLE_NONE,
    "tablename_plus_col": LABEL_STYLE_TABLENAME_PLUS_COL,
    "disambiguate_only": LABEL_STYLE_DISAMBIGUATE_ONLY,
}
COMMENTS = [None, "/* a */", "/* b */"]

METADATA = MetaData()
TABLE_OBJS = {
    "t1": Table(
        "t1",
        METADATA,
        Column("id", Integer, primary_key=True),
        Column("a", Integer),
        Column("b", String(10)),
    ),
    "t2": Table(
        "t2",
        METADATA,
        Column("id", Integer, primary_key=True),
        Column("t1_id", ForeignKey("t1.id")),
        Column("a", Integer),
        Column("b", String(10)),
    ),
    "t3": Table(
        "t3",
        METADATA,
        Column("id", Integer, primary_key=True),
        Column("t2_id", ForeignKey("t2.id")),
        Column("a", Integer),
        Column("b", String(10)),
    ),
}


# ------------------------------------------------------ spec generation
#
# a "spec" is a plain data structure describing a statement: nested dicts,
# each with a "k" key naming the kind of node.  build() turns it into an
# actual statement, creating new alias / subquery / table() objects each
# time it's called.
#
# while generating, the FROM elements that a SELECT can refer to are kept
# as "levels": a list with one entry per enclosing SELECT, outermost
# first, each a list of leaves (tables, subqueries, ...) as
# (columns, table name) tuples, where columns maps name -> INT / STR and
# the table name is set only for tables that aren't aliased, which text()
# may refer to by name.  A column is referred to by
# {"k": "col", "up": levels up from the current SELECT, "s": leaf index,
# "c": column name}.


class Gen:
    def __init__(self, seed):
        self.rng = random.Random(seed)
        self.count = 0

    def name(self, prefix):
        self.count += 1
        return f"{prefix}{self.count}"


def gen_lit(g, dom):
    rng = g.rng
    mode = rng.choice(["plain"] * 3 + LIT_MODES[1:])
    spec = {"k": "lit", "v": rng.choice(DOMAINS[dom]), "dom": dom}
    spec["mode"] = mode
    if mode == "typed":
        spec["type"] = _pick_type(rng, DOMAIN_KIND[dom])
    elif mode == "bindparam":
        spec["name"] = g.name("p")
    return spec


def _pick_type(rng, kind):
    return rng.choice([n for n, (k, _) in TYPES.items() if k == kind])


def pick_col(g, scope, t):
    """a column of type t from the given levels, or None"""
    rng = g.rng
    cands = []
    for up, level in enumerate(reversed(scope)):
        for s, (cols, _) in enumerate(level):
            for c, ct in cols.items():
                if t is None or ct == t:
                    cands.append((up, s, c, cols))
    if not cands:
        return None
    current = [cand for cand in cands if cand[0] == 0]
    if current and (len(current) == len(cands) or rng.random() < 0.75):
        cands = current
    else:
        cands = [cand for cand in cands if cand[0] > 0]
    up, s, c, cols = rng.choice(cands)
    # other columns of the same type, for structural mutations
    alt = [n for n, nt in cols.items() if nt == cols[c] and n != c]
    return {"k": "col", "up": up, "s": s, "c": c, "alt": alt}


def gen_expr(g, scope, t, sdepth, edepth=0):
    rng = g.rng
    r = rng.random()
    if r < 0.55 or edepth >= 2:
        col = pick_col(g, scope, t)
        if col is not None:
            return col
        return gen_lit(g, t)
    elif r < 0.63:
        return gen_lit(g, t)
    elif r < 0.71:
        return {
            "k": "op",
            "t": t,
            "op": rng.choice(BINOPS[t]),
            "l": gen_expr(g, scope, t, sdepth, edepth + 1),
            "r": gen_expr(g, scope, t, sdepth, edepth + 1),
        }
    elif r < 0.78:
        if rng.random() < 0.3:
            return {
                "k": "fn",
                "name": "coalesce",
                "args": [
                    gen_expr(g, scope, t, sdepth, edepth + 1),
                    gen_lit(g, t),
                ],
            }
        name = rng.choice([n for n, (_, rt) in FUNCS.items() if rt == t])
        return {
            "k": "fn",
            "name": name,
            "args": [gen_expr(g, scope, FUNCS[name][0], sdepth, edepth + 1)],
        }
    elif r < 0.85:
        return {
            "k": rng.choice(["cast", "tc"]),
            "to": _pick_type(rng, t),
            "e": gen_expr(
                g, scope, rng.choice([INT, STR]), sdepth, edepth + 1
            ),
        }
    elif r < 0.89:
        return {"k": "lc", "t": t, "text": rng.choice(LITERAL_COLUMNS[t])}
    elif r < 0.95 and sdepth < 2:
        sel, _ = gen_select(g, scope, sdepth + 1, types=[t], single=True)
        return {"k": "ssq", "sel": sel}
    else:
        whens = [
            [
                gen_cond(g, scope, sdepth, edepth + 1),
                gen_expr(g, scope, t, sdepth, edepth + 1),
            ]
            for _ in range(rng.randint(1, 2))
        ]
        return {
            "k": "case",
            "whens": whens,
            "else": gen_lit(g, t) if rng.random() < 0.7 else None,
        }


def gen_agg(g, scope, t, sdepth):
    rng = g.rng
    fn = rng.choice(AGGS[t])
    argt = rng.choice([INT, STR]) if fn == "count" else t
    return {
        "k": "agg",
        "fn": fn,
        "t": argt,
        "e": gen_expr(g, scope, argt, sdepth, 1),
        "distinct": rng.random() < 0.15,
    }


def gen_cond(g, scope, sdepth, edepth=0):
    rng = g.rng
    r = rng.random()
    t = rng.choice([INT, INT, STR])
    if r < 0.40 or edepth >= 2:
        right = (
            gen_lit(g, t)
            if rng.random() < 0.6
            else gen_expr(g, scope, t, sdepth, edepth + 1)
        )
        return {
            "k": "cmp",
            "op": rng.choice(list(CMPS)),
            "l": gen_expr(g, scope, t, sdepth, edepth + 1),
            "r": right,
        }
    elif r < 0.48:
        spec = {
            "k": "in",
            "e": gen_expr(g, scope, t, sdepth, edepth + 1),
            "dom": t,
            "vals": [rng.choice(DOMAINS[t]) for _ in range(rng.randint(0, 3))],
            "neg": rng.random() < 0.2,
            "mode": rng.choice(IN_MODES),
        }
        if spec["mode"] == "expanding":
            spec["name"] = g.name("p")
        return spec
    elif r < 0.55:
        return {
            "k": rng.choice(["and", "or"]),
            "items": [
                gen_cond(g, scope, sdepth, edepth + 1)
                for _ in range(rng.randint(2, 3))
            ],
        }
    elif r < 0.58:
        cond = gen_cond(g, scope, sdepth, edepth + 1)
        if cond["k"] == "text":
            # not_() needs a ColumnElement
            return cond
        return {"k": "not", "c": cond}
    elif r < 0.63:
        return {
            "k": "isnull",
            "e": gen_expr(g, scope, t, sdepth, edepth + 1),
            "neg": rng.random() < 0.5,
        }
    elif r < 0.68:
        return {
            "k": "between",
            "e": gen_expr(g, scope, INT, sdepth, edepth + 1),
            "lo": gen_lit(g, INT),
            "hi": gen_lit(g, INT),
            "sym": rng.random() < 0.15,
        }
    elif r < 0.74:
        if rng.random() < 0.6:
            op, pat = rng.choice(LIKES), gen_lit(g, "pat")
        else:
            op, pat = rng.choice(STARTS), gen_lit(g, STR)
        return {
            "k": "like",
            "op": op,
            "e": gen_expr(g, scope, STR, sdepth, edepth + 1),
            "pat": pat,
            # autoescape=True needs a plain string
            "esc": (op in LIKES or pat["mode"] == "plain")
            and rng.random() < 0.2,
        }
    elif r < 0.79:
        named = [tname for _, tname in scope[-1] if tname] if scope else []
        return {
            "k": "text",
            "col": f"{rng.choice(named)}.a" if named else None,
            "op": rng.choice(["<", ">"]),
            "p": gen_lit(g, INT),
            "pn": g.name("tp"),
        }
    elif r < 0.88 and sdepth < 2:
        sel, _ = gen_select(g, scope, sdepth + 1)
        return {"k": "exists", "sel": sel, "neg": rng.random() < 0.3}
    elif r < 0.96 and sdepth < 2:
        sel, _ = gen_select(g, scope, sdepth + 1, types=[t])
        return {
            "k": "insub",
            "e": gen_expr(g, scope, t, sdepth, edepth + 1),
            "sel": sel,
            "neg": rng.random() < 0.3,
        }
    else:
        return {
            "k": "cmp",
            "op": rng.choice(list(CMPS)),
            "l": gen_expr(g, scope, t, sdepth, edepth + 1),
            "r": gen_lit(g, t),
        }


def gen_table(g, levels, level):
    rng = g.rng
    name = rng.choice(list(TABLES))
    # a table already in this FROM list or an enclosing one without an
    # alias is aliased, so that it's neither repeated in a FROM list nor
    # correlated away
    alias = rng.random() < 0.3 or any(
        tname == name for lvl in levels + [level] for _, tname in lvl
    )
    level.append((dict(TABLES[name]), None if alias else name))
    return {
        "k": "table",
        "name": name,
        "alias": alias,
        "lw": rng.random() < 0.3,
    }


def _derived_cols(types):
    return {f"c{i}": t for i, t in enumerate(types)}


def gen_derived(g, sdepth):
    """a SELECT or set operation for use as a subquery or CTE"""
    if g.rng.random() < 0.2:
        return gen_compound(g, [], sdepth)
    return gen_select(g, [], sdepth)


def gen_leaf(g, levels, level, sdepth):
    rng = g.rng
    r = rng.random() if sdepth < 2 else 0
    if r < 0.55:
        return gen_table(g, levels, level)
    elif r < 0.68:
        sel, types = gen_derived(g, sdepth + 1)
        level.append((_derived_cols(types), None))
        return {
            "k": "subq",
            "sel": sel,
            "lat": False,
            "name": g.name("sq") if rng.random() < 0.2 else None,
        }
    elif r < 0.78:
        sel, types = gen_derived(g, sdepth + 1)
        level.append((_derived_cols(types), None))
        return {
            "k": "cte",
            "sel": sel,
            "name": g.name("cte") if rng.random() < 0.3 else None,
            "nesting": rng.random() < 0.2,
        }
    elif r < 0.84 and level:
        # LATERAL may refer to the FROM elements before it
        sel, types = gen_select(g, levels + [list(level)], sdepth + 1)
        level.append((_derived_cols(types), None))
        return {"k": "subq", "sel": sel, "lat": True, "name": None}
    elif r < 0.89:
        level.append(({"n": INT}, None))
        return {
            "k": "rcte",
            "anchor": rng.choice(["lit", "table"]),
            "start": gen_lit(g, "n"),
            "stop": gen_lit(g, "n"),
            "step": rng.choice([1, 2]),
            "op": rng.choice(["<", "<="]),
            "all": rng.random() < 0.7,
            "name": g.name("rc") if rng.random() < 0.3 else None,
        }
    elif r < 0.95:
        types = [INT, rng.choice([INT, STR])]
        level.append(({"id": INT, "a": types[1]}, None))
        return {
            "k": "textsq",
            "table": rng.choice(list(TABLES)),
            "op": rng.choice(["<", ">"]),
            "p": gen_lit(g, INT),
            "pn": g.name("tp"),
            "types": types,
        }
    elif r < 0.98:
        level.append(({"c0": INT, "c1": STR}, None))
        return {
            "k": "values",
            "rows": [
                [gen_lit(g, INT), gen_lit(g, STR)]
                for _ in range(rng.randint(1, 3))
            ],
            "lb": rng.random() < 0.3,
            "name": g.name("v") if rng.random() < 0.5 else None,
        }
    else:
        return gen_table(g, levels, level)


def gen_from(g, levels, level, sdepth):
    rng = g.rng
    if sdepth >= 2 or rng.random() > 0.3:
        return gen_leaf(g, levels, level, sdepth)

    first = len(level)
    if rng.random() < 0.3:
        left = gen_from(g, levels, level, sdepth + 1)
    else:
        left = gen_leaf(g, levels, level, sdepth)
    right = gen_leaf(g, levels, level, sdepth)
    rcols = level[-1][0]
    spec = {
        "k": "join",
        "l": left,
        "r": right,
        "outer": rng.random() < 0.4,
        "full": rng.random() < 0.1,
        # Select.join_from(), for a join whose left side isn't a join
        "via": "fn" if left["k"] == "join" else rng.choice(["fn", "method"]),
    }
    if right["k"] == "subq" and right["lat"]:
        spec["on"] = {"k": "true"}
    elif (
        left["k"] == right["k"] == "table"
        and (left["name"], right["name"]) in FKS
        and not (left["lw"] or right["lw"])
        and rng.random() < 0.3
    ):
        spec["on"] = None
    else:
        rc = rng.choice(list(rcols))
        t = rcols[rc]
        lefts = [
            (s, c)
            for s in range(first, len(level) - 1)
            for c, ct in level[s][0].items()
            if ct == t
        ]
        rref = {"k": "col", "up": 0, "s": len(level) - 1, "c": rc, "alt": []}
        if lefts:
            s, c = rng.choice(lefts)
            lref = {"k": "col", "up": 0, "s": s, "c": c, "alt": []}
        else:
            lref = gen_lit(g, t)
        spec["on"] = {"k": "cmp", "op": "==", "l": rref, "r": lref}
    return spec


def gen_select(
    g, levels, sdepth, types=None, single=False, member=False, top=False
):
    """returns the spec and the types of its columns"""
    rng = g.rng
    level = []
    froms = [gen_from(g, levels, level, sdepth)]
    if rng.random() < 0.12:
        froms.append(gen_from(g, levels, level, sdepth))
    scope = levels + [level]

    if types is None:
        types = [rng.choice([INT, INT, STR]) for _ in range(rng.randint(1, 3))]
    agg = rng.random() < (0.5 if single else 0.15)
    cols = []
    for i, t in enumerate(types):
        if agg and (i == len(types) - 1 or rng.random() < 0.3):
            cols.append(gen_agg(g, scope, t, sdepth))
        else:
            cols.append(gen_expr(g, scope, t, sdepth))

    spec = {
        "k": "select",
        "froms": froms,
        "explicit": rng.random() < 0.8,
        "cols": cols,
        "where": [
            gen_cond(g, scope, sdepth) for _ in range(rng.choice([0, 1, 1, 2]))
        ],
        "group_by": agg and rng.random() < 0.8,
        "having": None,
        "order_by": [],
        "limit": None,
        "offset": None,
        "fetch": None,
        "distinct": rng.random() < 0.08,
        "corr": None,
        "for_update": None,
        "prefix": None,
        "suffix": None,
    }
    if agg and rng.random() < 0.3:
        spec["having"] = {
            "k": "cmp",
            "op": rng.choice(list(CMPS)),
            "l": gen_agg(g, scope, INT, sdepth),
            "r": gen_lit(g, INT),
        }
    if not member:
        if rng.random() < 0.35:
            for _ in range(rng.randint(1, 2)):
                item = {
                    "desc": rng.random() < 0.4,
                    "nulls": rng.choice([None, None, "first", "last"]),
                }
                if rng.random() < 0.7:
                    item["col"] = rng.randrange(len(cols))
                    item["by"] = rng.choice(["label", "expr"])
                else:
                    item["e"] = gen_expr(g, scope, rng.choice([INT, STR]), 2)
                spec["order_by"].append(item)
        if single and cols[0]["k"] != "agg":
            spec["limit"] = 1
        elif rng.random() < 0.2:
            spec["limit"] = rng.choice(DOMAINS["n"])
        if rng.random() < 0.1:
            spec["offset"] = rng.choice(DOMAINS["n"])
        if spec["limit"] is None and rng.random() < 0.02:
            spec["fetch"] = {
                "n": rng.choice(DOMAINS["n"]),
                "ties": rng.random() < 0.3,
                "pct": rng.random() < 0.2,
            }
    if levels and rng.random() < 0.4:
        spec["corr"] = rng.choice(CORRS[1:])
    if rng.random() < 0.04:
        spec["prefix"] = rng.choice(COMMENTS[1:])
    if rng.random() < 0.04:
        spec["suffix"] = rng.choice(COMMENTS[1:])
    if top:
        spec["labels"] = rng.random() < 0.5
        spec["label_style"] = rng.choice(list(LABEL_STYLES))
        spec["woc"] = None
        if rng.random() < 0.15:
            spec["woc"] = {
                "cols": sorted(
                    rng.sample(range(len(cols)), rng.randint(1, len(cols)))
                ),
                "maintain": rng.random() < 0.5,
            }
        if rng.random() < 0.05:
            spec["for_update"] = {
                "nowait": rng.random() < 0.3,
                "read": rng.random() < 0.3,
                "skip_locked": rng.random() < 0.3,
                "key_share": rng.random() < 0.2,
                "of": rng.random() < 0.3,
            }
    return spec, types


def gen_compound(g, levels, sdepth, top=False):
    rng = g.rng
    first, types = gen_select(g, levels, sdepth, member=True)
    sels = [first] + [
        gen_select(g, levels, sdepth, types=types, member=True)[0]
        for _ in range(rng.choice([1, 1, 2]))
    ]
    spec = {
        "k": "compound",
        "op": rng.choice(list(SET_OPS)[:4] * 3 + list(SET_OPS)[4:]),
        "sels": sels,
        "order": rng.random() < 0.3,
        "desc": rng.random() < 0.5,
        "limit": rng.choice(DOMAINS["n"]) if rng.random() < 0.2 else None,
    }
    return spec, types


def gen_returning(g, name):
    rng = g.rng
    if rng.random() < 0.5:
        return None
    cols = list(TABLES[name])
    return {
        "cols": sorted(
            rng.sample(cols, rng.randint(1, len(cols))), key=cols.index
        ),
        "expr": (
            gen_expr(g, [[(TABLES[name], name)]], INT, 1)
            if rng.random() < 0.3
            else None
        ),
    }


def gen_dml(g, kind):
    rng = g.rng
    name = rng.choice(list(TABLES))
    spec = {"k": kind, "table": name, "lw": rng.random() < 0.3}
    level = [(TABLES[name], name)]
    noid = [c for c in TABLES[name] if c != "id"]

    if kind == "insert":
        cols = sorted(rng.sample(noid, rng.randint(1, len(noid))))
        spec["cols"] = cols
        r = rng.random()
        if r < 0.6:
            if rng.random() < 0.2:
                cols.insert(0, "id")
            spec["vals"] = [
                (
                    gen_lit(g, "id")
                    if c == "id"
                    else gen_expr(g, [], TABLES[name][c], 1)
                )
                for c in cols
            ]
        elif r < 0.68:
            spec["multi"] = [
                [rng.choice(DOMAINS[TABLES[name][c]]) for c in cols]
                for _ in range(2)
            ]
        else:
            spec["sel"], _ = gen_select(
                g, [], 1, types=[TABLES[name][c] for c in cols]
            )
    else:
        if rng.random() < 0.15:
            spec["extra"] = gen_table(g, [], level)
        scope = [level]
        spec["where"] = [
            gen_cond(g, scope, 1) for _ in range(rng.choice([0, 1, 1, 2]))
        ]
        if kind == "update":
            cols = rng.sample(noid, rng.randint(1, 2))
            spec["set"] = [
                [c, gen_expr(g, scope, TABLES[name][c], 1)] for c in cols
            ]
            spec["ordered"] = rng.random() < 0.15
            spec["keys"] = rng.choice(["str", "col"])
    spec["ret"] = gen_returning(g, name)
    return spec


def _param_value(rng, dom, many):
    if many:
        return [rng.choice(DOMAINS[dom]) for _ in range(rng.randint(1, 3))]
    return rng.choice(DOMAINS[dom])


def gen_params(g, spec):
    """values for some of the named bound parameters, for .params()"""
    rng = g.rng
    names = []
    for _, node in walk(spec):
        if node["k"] == "lit" and node["mode"] == "bindparam":
            names.append((node["name"], node["dom"], False))
        elif node["k"] == "in" and node["mode"] == "expanding":
            # keeps the list empty or not, as in mutate_literals()
            if node["vals"]:
                names.append((node["name"], node["dom"], True))
        elif node["k"] in ("text", "textsq"):
            names.append((node["pn"], INT, False))
    if not names or rng.random() < 0.6:
        return None
    return [
        [name, dom, many, _param_value(rng, dom, many)]
        for name, dom, many in rng.sample(names, rng.randint(1, len(names)))
    ]


def gen_root(g):
    r = g.rng.random()
    if r < 0.67:
        if r < 0.55:
            spec = gen_select(g, [], 0, top=True)[0]
        else:
            spec = gen_compound(g, [], 0)[0]
        spec["params"] = gen_params(g, spec)
        return spec
    elif r < 0.78:
        return gen_dml(g, "insert")
    elif r < 0.89:
        return gen_dml(g, "update")
    else:
        return gen_dml(g, "delete")


# ------------------------------------------------------------- mutation


def mutate_literals(rng, spec):
    """a copy of the spec with some literal values changed"""
    if isinstance(spec, list):
        return [mutate_literals(rng, elem) for elem in spec]
    elif not isinstance(spec, dict):
        return spec

    out = {key: mutate_literals(rng, value) for key, value in spec.items()}
    k = spec.get("k")
    if k == "lit" and rng.random() < 0.6:
        out["v"] = rng.choice(DOMAINS[spec["dom"]])
    elif k == "in" and rng.random() < 0.6:
        # the parameter's type comes from the first value, or from the
        # expression it's compared to if there's none, so an empty list
        # stays empty and a non-empty one non-empty
        n = rng.randint(1, 4) if spec["vals"] else 0
        out["vals"] = [rng.choice(DOMAINS[spec["dom"]]) for _ in range(n)]
    elif k in ("select", "compound"):
        if spec.get("params") and rng.random() < 0.6:
            out["params"] = [
                [name, dom, many, _param_value(rng, dom, many)]
                for name, dom, many, _ in spec["params"]
            ]
        for key in ("limit", "offset"):
            if spec.get(key) is not None and rng.random() < 0.5:
                out[key] = rng.choice(DOMAINS["n"])
        if spec.get("fetch") and rng.random() < 0.5:
            out["fetch"] = dict(spec["fetch"], n=rng.choice(DOMAINS["n"]))
    elif k == "values" and rng.random() < 0.3:
        out["rows"] = [
            [dict(lit, v=rng.choice(DOMAINS[lit["dom"]])) for lit in row]
            for row in spec["rows"]
        ]
        out["rows"] += out["rows"][: rng.randint(0, 1)]
    elif k == "insert" and spec.get("multi") and rng.random() < 0.5:
        out["multi"] = [
            [
                rng.choice(DOMAINS[TABLES[spec["table"]][c]])
                for c in spec["cols"]
            ]
            for _ in range(rng.randint(1, 3))
        ]
    return out


def _flip(node, key):
    return [{key: not node[key]}]


def _others(node, key, choices):
    return [{key: c} for c in choices if c != node[key]]


def _drop_last(node, key, keep=0):
    if len(node[key]) > keep:
        return [{key: node[key][:-1]}]
    return []


def _toggle(node, key, value):
    return [{key: None if node[key] is not None else value}]


def proposals_select(node):
    out = _drop_last(node, "params") if node.get("params") else []
    out += _flip(node, "distinct") + _flip(node, "explicit")
    out += _toggle(node, "limit", 2) + _toggle(node, "offset", 1)
    out += _others(node, "corr", CORRS)
    out += _drop_last(node, "where") + _drop_last(node, "order_by")
    out += _others(node, "prefix", COMMENTS)
    out += _others(node, "suffix", COMMENTS)
    if node["limit"] is None:
        out += _toggle(node, "fetch", {"n": 2, "ties": False, "pct": False})
    if node["fetch"]:
        for key in ("ties", "pct"):
            out.append(
                {"fetch": dict(node["fetch"], **{key: not node["fetch"][key]})}
            )
    if any(col["k"] == "agg" for col in node["cols"]):
        out += _flip(node, "group_by")
    if node["having"]:
        out.append({"having": None})
    for i, item in enumerate(node["order_by"]):
        changes = [{"desc": not item["desc"]}]
        changes += [{"nulls": n} for n in (None, "first", "last")]
        if "by" in item:
            changes.append({"by": "label" if item["by"] == "expr" else "expr"})
        for change in changes:
            if any(item.get(key) != value for key, value in change.items()):
                order_by = list(node["order_by"])
                order_by[i] = dict(item, **change)
                out.append({"order_by": order_by})
    if "labels" in node:
        # top level only
        out += _flip(node, "labels")
        out += _others(node, "label_style", LABEL_STYLES)
        ncols = len(node["cols"])
        if node["woc"]:
            out.append({"woc": None})
            out.append(
                {
                    "woc": dict(
                        node["woc"], maintain=not node["woc"]["maintain"]
                    )
                }
            )
            if len(node["woc"]["cols"]) > 1:
                out.append(
                    {"woc": dict(node["woc"], cols=node["woc"]["cols"][1:])}
                )
        else:
            out.append(
                {"woc": {"cols": list(range(ncols)), "maintain": False}}
            )
            out.append({"woc": {"cols": list(range(ncols)), "maintain": True}})
        fu = node["for_update"]
        if fu:
            out.append({"for_update": None})
            for key in fu:
                out.append({"for_update": dict(fu, **{key: not fu[key]})})
        else:
            out.append(
                {
                    "for_update": dict.fromkeys(
                        ("nowait", "read", "skip_locked", "key_share", "of"),
                        False,
                    )
                }
            )
    return out


def proposals_compound(node):
    out = _drop_last(node, "params", keep=0) if node.get("params") else []
    out += _others(node, "op", SET_OPS) + _flip(node, "order")
    out += _toggle(node, "limit", 2) + _drop_last(node, "sels", keep=2)
    if node["order"]:
        out += _flip(node, "desc")
    return out


def proposals_lit(node):
    kind = DOMAIN_KIND[node["dom"]]
    out = []
    for mode in LIT_MODES:
        if mode == node["mode"]:
            continue
        if mode == "typed":
            out += [
                {"mode": mode, "type": n}
                for n, (k, _) in TYPES.items()
                if k == kind
            ]
        elif mode == "bindparam":
            out.append({"mode": mode, "name": "pm"})
        else:
            out.append({"mode": mode})
    if node["mode"] == "typed":
        out += [
            {"type": n}
            for n, (k, _) in TYPES.items()
            if k == kind and n != node["type"]
        ]
    return out


def proposals_like(node):
    group = LIKES if node["op"] in LIKES else STARTS
    return _others(node, "op", group) + _flip(node, "esc")


def proposals_cast(node):
    kind = TYPES[node["to"]][0]
    out = [{"k": "tc" if node["k"] == "cast" else "cast"}]
    out += [
        {"to": n}
        for n, (k, _) in TYPES.items()
        if k == kind and n != node["to"]
    ]
    return out


def proposals_fn(node):
    if node["name"] not in FUNCS:
        return []
    sig = FUNCS[node["name"]]
    return [
        {"name": n} for n, s in FUNCS.items() if s == sig and n != node["name"]
    ]


def proposals_agg(node):
    out = _flip(node, "distinct")
    if node["fn"] == "count":
        return out
    return out + [
        {"fn": fn} for fn in AGGS[node["t"]] if fn not in (node["fn"], "count")
    ]


def proposals_dml(node):
    out = []
    if node["ret"]:
        out.append({"ret": None})
        if len(node["ret"]["cols"]) > 1:
            out.append(
                {"ret": dict(node["ret"], cols=node["ret"]["cols"][1:])}
            )
        if node["ret"]["expr"]:
            out.append({"ret": dict(node["ret"], expr=None)})
    else:
        out.append({"ret": {"cols": ["id"], "expr": None}})
    out += _flip(node, "lw")
    if node["k"] != "insert":
        out += _drop_last(node, "where")
    if node["k"] == "update":
        out += _flip(node, "ordered") + _others(node, "keys", ["str", "col"])
        out += _drop_last(node, "set", keep=1)
    return out


PROPOSALS = {
    "select": proposals_select,
    "compound": proposals_compound,
    "join": lambda n: (
        _flip(n, "outer")
        + _flip(n, "full")
        + (
            _others(n, "via", ["fn", "method"])
            if n["l"]["k"] != "join"
            else []
        )
    ),
    "table": lambda n: _flip(n, "alias") + _flip(n, "lw"),
    "subq": lambda n: (
        _flip(n, "lat")
        + _toggle(n, "name", "sqm")
        + [{"k": "cte", "nesting": False, "lat": None}]
    ),
    "cte": lambda n: (
        _flip(n, "nesting")
        + _toggle(n, "name", "ctem")
        + [{"k": "subq", "lat": False, "nesting": None}]
    ),
    "rcte": lambda n: (
        _flip(n, "all")
        + _others(n, "op", ["<", "<="])
        + _others(n, "step", [1, 2])
        + _others(n, "anchor", ["lit", "table"])
        + _toggle(n, "name", "rcm")
    ),
    "textsq": lambda n: (
        _others(n, "op", ["<", ">"])
        + [{"types": [INT, STR if n["types"][1] == INT else INT]}]
        + _others(n, "table", TABLES)
    ),
    "values": lambda n: _flip(n, "lb") + _toggle(n, "name", "vm"),
    "col": lambda n: [{"c": c} for c in n["alt"]],
    "lit": proposals_lit,
    "op": lambda n: _others(n, "op", BINOPS[n["t"]]),
    "fn": proposals_fn,
    "agg": proposals_agg,
    "cast": proposals_cast,
    "tc": proposals_cast,
    "lc": lambda n: _others(n, "text", LITERAL_COLUMNS[n["t"]]),
    "case": lambda n: [{"else": None}] if n["else"] else [],
    "cmp": lambda n: _others(n, "op", CMPS) + [{"l": n["r"], "r": n["l"]}],
    "and": lambda n: [{"k": "or"}],
    "or": lambda n: [{"k": "and"}],
    "in": lambda n: (
        _flip(n, "neg")
        + [{"mode": m, "name": "pm"} for m in IN_MODES if m != n["mode"]]
    ),
    "isnull": lambda n: _flip(n, "neg"),
    "between": lambda n: _flip(n, "sym"),
    "like": proposals_like,
    "text": lambda n: _others(n, "op", ["<", ">"]),
    "exists": lambda n: _flip(n, "neg"),
    "insub": lambda n: _flip(n, "neg"),
    "insert": proposals_dml,
    "update": proposals_dml,
    "delete": proposals_dml,
}


def walk(spec, path=()):
    """yield (path, node) for each node of the spec"""
    if isinstance(spec, dict):
        if "k" in spec:
            yield path, spec
        for key, value in spec.items():
            yield from walk(value, path + (key,))
    elif isinstance(spec, list):
        for i, elem in enumerate(spec):
            yield from walk(elem, path + (i,))


def path_text(path):
    out = ""
    for elem in path:
        out += f"[{elem}]" if isinstance(elem, int) else f".{elem}"
    return out.lstrip(".") or "<root>"


def _short(value):
    text_ = repr(value)
    return text_ if len(text_) <= 60 else text_[:57] + "..."


def mutate_structure(rng, spec):
    """a copy of the spec with one structural change, and a description"""
    nodes = [
        (path, node)
        for path, node in walk(spec)
        if PROPOSALS.get(node["k"]) and PROPOSALS[node["k"]](node)
    ]
    if not nodes:
        return None
    path, node = rng.choice(nodes)
    change = rng.choice(PROPOSALS[node["k"]](node))

    out = copy.deepcopy(spec)
    target = out
    for elem in path:
        target = target[elem]
    desc = ", ".join(
        f"{key}: {_short(node.get(key))} -> {_short(value)}"
        for key, value in change.items()
        if node.get(key) != value
    )
    target.update(change)
    return f"{path_text(path)} ({node['k']}) {desc}", out


def gen_trial(seed):
    g = Gen(seed)
    spec = gen_root(g)
    literal_variant = mutate_literals(g.rng, spec)
    mutants = []
    for _ in range(NUM_MUTANTS):
        mutant = mutate_structure(g.rng, spec)
        if mutant and mutant[1] != spec:
            mutants.append(mutant)
    return spec, literal_variant, mutants


# ------------------------------------------------------ statement build


def make_type(name):
    return TYPES[name][1]()


def make_table(name, alias, lw):
    if lw:
        obj = table(
            name, *[column(c, SQL_TYPES[t]) for c, t in TABLES[name].items()]
        )
    else:
        obj = TABLE_OBJS[name]
    return obj.alias() if alias else obj


def build_lit(spec, raw_ok=False):
    value, mode = spec["v"], spec["mode"]
    if mode == "plain":
        return value if raw_ok else literal(value)
    elif mode == "literal":
        return literal(value)
    elif mode == "typed":
        return literal(value, make_type(spec["type"]))
    elif mode == "bindparam":
        return bindparam(spec["name"], value)
    else:
        return literal(value, literal_execute=True)


def build_expr(spec, scopes, raw_ok=False):
    k = spec["k"]
    if k == "col":
        return scopes[-1 - spec["up"]][spec["s"]].c[spec["c"]]
    elif k == "lit":
        return build_lit(spec, raw_ok)
    elif k == "op":
        return BINOP_FNS[spec["op"]](
            build_expr(spec["l"], scopes),
            build_expr(spec["r"], scopes, raw_ok=True),
        )
    elif k == "fn":
        args = [
            build_expr(arg, scopes, raw_ok=i > 0)
            for i, arg in enumerate(spec["args"])
        ]
        return getattr(func, spec["name"])(*args)
    elif k == "agg":
        arg = build_expr(spec["e"], scopes)
        if spec["distinct"]:
            arg = arg.distinct()
        return getattr(func, spec["fn"])(arg)
    elif k == "cast":
        return cast(build_expr(spec["e"], scopes, True), make_type(spec["to"]))
    elif k == "tc":
        return type_coerce(
            build_expr(spec["e"], scopes, True), make_type(spec["to"])
        )
    elif k == "lc":
        return literal_column(spec["text"], SQL_TYPES[spec["t"]])
    elif k == "ssq":
        return build_select(spec["sel"], scopes).scalar_subquery()
    elif k == "case":
        whens = [
            (build_cond(cond, scopes), build_expr(expr, scopes, True))
            for cond, expr in spec["whens"]
        ]
        else_ = spec["else"]
        return case(
            *whens,
            else_=build_expr(else_, scopes, True) if else_ else None,
        )
    raise ValueError(k)


def build_cond(spec, scopes):
    k = spec["k"]
    if k == "cmp":
        return CMPS[spec["op"]](
            build_expr(spec["l"], scopes),
            build_expr(spec["r"], scopes, raw_ok=True),
        )
    elif k == "and":
        return and_(*[build_cond(c, scopes) for c in spec["items"]])
    elif k == "or":
        return or_(*[build_cond(c, scopes) for c in spec["items"]])
    elif k == "not":
        return not_(build_cond(spec["c"], scopes))
    elif k == "in":
        expr = build_expr(spec["e"], scopes)
        vals = list(spec["vals"])
        if spec["mode"] == "expanding":
            vals = bindparam(spec["name"], vals, expanding=True)
        elif spec["mode"] == "execute":
            vals = bindparam(None, vals, expanding=True, literal_execute=True)
        return expr.not_in(vals) if spec["neg"] else expr.in_(vals)
    elif k == "isnull":
        expr = build_expr(spec["e"], scopes)
        return expr.is_not(None) if spec["neg"] else expr.is_(None)
    elif k == "between":
        return build_expr(spec["e"], scopes).between(
            build_lit(spec["lo"], True),
            build_lit(spec["hi"], True),
            symmetric=spec["sym"],
        )
    elif k == "like":
        expr = build_expr(spec["e"], scopes)
        pat = build_lit(spec["pat"], True)
        if spec["op"] in LIKES:
            return getattr(expr, spec["op"])(
                pat, escape="/" if spec["esc"] else None
            )
        return getattr(expr, spec["op"])(pat, autoescape=spec["esc"])
    elif k == "text":
        pn = spec["pn"]
        if spec["col"]:
            sql = f"{spec['col']} {spec['op']} :{pn}"
        else:
            sql = f":{pn} {spec['op']} 2"
        return text(sql).bindparams(**{pn: spec["p"]["v"]})
    elif k == "exists":
        exists = build_select(spec["sel"], scopes).exists()
        return ~exists if spec["neg"] else exists
    elif k == "insub":
        expr = build_expr(spec["e"], scopes)
        sel = build_select(spec["sel"], scopes)
        return expr.not_in(sel) if spec["neg"] else expr.in_(sel)
    elif k == "true":
        return true()
    raise ValueError(k)


def build_derived(spec, scopes):
    if spec["k"] == "compound":
        return build_compound(spec, scopes)
    return build_select(spec, scopes)


def build_leaf(spec, scopes, level):
    """build a FROM element and add it to the level"""
    k = spec["k"]
    if k == "table":
        leaf = make_table(spec["name"], spec["alias"], spec["lw"])
    elif k == "subq":
        sel = build_derived(spec["sel"], scopes + [level])
        if spec["lat"]:
            leaf = sel.lateral(spec["name"])
        else:
            leaf = sel.subquery(spec["name"])
    elif k == "cte":
        sel = build_derived(spec["sel"], scopes + [level])
        leaf = sel.cte(spec["name"], nesting=spec["nesting"])
    elif k == "rcte":
        if spec["anchor"] == "lit":
            anchor = select(build_lit(spec["start"]).label("n"))
        else:
            t1 = TABLE_OBJS["t1"]
            anchor = select(t1.c.id.label("n")).where(
                t1.c.a == build_lit(spec["start"], True)
            )
        cte = anchor.cte(spec["name"], recursive=True)
        recursive = select((cte.c.n + spec["step"]).label("n")).where(
            CMPS[spec["op"]](cte.c.n, build_lit(spec["stop"], True))
        )
        leaf = (
            cte.union_all(recursive) if spec["all"] else cte.union(recursive)
        )
    elif k == "textsq":
        a_type = SQL_TYPES[spec["types"][1]]
        pn = spec["pn"]
        leaf = (
            text(
                f"SELECT id, a FROM {spec['table']} "
                f"WHERE a {spec['op']} :{pn}"
            )
            .bindparams(**{pn: spec["p"]["v"]})
            .columns(column("id", Integer), column("a", a_type))
            .subquery()
        )
    elif k == "values":
        leaf = values(
            column("c0", Integer),
            column("c1", String),
            name=spec["name"],
            literal_binds=spec["lb"],
        ).data([tuple(lit["v"] for lit in row) for row in spec["rows"]])
    else:
        raise ValueError(k)
    level.append(leaf)
    return leaf


def build_from(spec, scopes, level):
    """build a FROM item, adding its leaves to the level.

    returns a FromClause, or for a join built with Select.join_from(), a
    tuple of its arguments
    """
    if spec["k"] != "join":
        return build_leaf(spec, scopes, level)
    left = build_from(spec["l"], scopes, level)
    if isinstance(left, tuple):
        left = left[0].join(
            left[1], left[2], isouter=left[3]["outer"], full=left[3]["full"]
        )
    right = build_leaf(spec["r"], scopes, level)
    on = build_cond(spec["on"], scopes + [level]) if spec["on"] else None
    if spec["via"] == "method":
        return (left, right, on, spec)
    return left.join(right, on, isouter=spec["outer"], full=spec["full"])


def build_select(spec, scopes):
    level = []
    froms = [build_from(f, scopes, level) for f in spec["froms"]]
    current = scopes + [level]

    exprs = [build_expr(e, current) for e in spec["cols"]]
    if spec.get("labels", True):
        cols = [e.label(f"c{i}") for i, e in enumerate(exprs)]
    else:
        cols = exprs
    stmt = select(*cols)
    for from_ in froms:
        if isinstance(from_, tuple):
            left, right, on, join = from_
            stmt = stmt.join_from(
                left, right, on, isouter=join["outer"], full=join["full"]
            )
        elif spec["explicit"] or len(spec["froms"]) > 1:
            stmt = stmt.select_from(from_)

    if spec["where"]:
        stmt = stmt.where(*[build_cond(c, current) for c in spec["where"]])
    if spec["group_by"]:
        keys = [e for e, es in zip(exprs, spec["cols"]) if es["k"] != "agg"]
        if keys:
            stmt = stmt.group_by(*keys)
    if spec["having"]:
        stmt = stmt.having(build_cond(spec["having"], current))
    for item in spec["order_by"]:
        if "col" in item:
            expr = (cols if item["by"] == "label" else exprs)[item["col"]]
        else:
            expr = build_expr(item["e"], current)
        expr = expr.desc() if item["desc"] else expr.asc()
        if item["nulls"] == "first":
            expr = expr.nulls_first()
        elif item["nulls"] == "last":
            expr = expr.nulls_last()
        stmt = stmt.order_by(expr)
    if spec["limit"] is not None:
        stmt = stmt.limit(spec["limit"])
    if spec["offset"] is not None:
        stmt = stmt.offset(spec["offset"])
    if spec["fetch"]:
        fetch = spec["fetch"]
        stmt = stmt.fetch(
            fetch["n"], with_ties=fetch["ties"], percent=fetch["pct"]
        )
    if spec["distinct"]:
        stmt = stmt.distinct()

    corr = spec["corr"]
    parent = scopes[-1] if scopes else []
    if corr == "none":
        stmt = stmt.correlate(None)
    elif corr == "except_none":
        stmt = stmt.correlate_except(None)
    elif corr == "parent":
        stmt = stmt.correlate(*parent)
    elif corr == "except_parent":
        stmt = stmt.correlate_except(*parent)
    elif corr == "except_one":
        stmt = stmt.correlate_except(*parent[:1])

    if spec["prefix"]:
        stmt = stmt.prefix_with(spec["prefix"])
    if spec["suffix"]:
        stmt = stmt.suffix_with(spec["suffix"])
    if spec.get("label_style"):
        stmt = stmt.set_label_style(LABEL_STYLES[spec["label_style"]])
    if spec.get("for_update"):
        fu = dict(spec["for_update"])
        fu["of"] = level[0] if fu["of"] else None
        stmt = stmt.with_for_update(**fu)
    if spec.get("woc"):
        stmt = stmt.with_only_columns(
            *[cols[i] for i in spec["woc"]["cols"]],
            maintain_column_froms=spec["woc"]["maintain"],
        )
    return stmt


def build_compound(spec, scopes):
    stmt = SET_OPS[spec["op"]](
        *[build_select(sel, scopes) for sel in spec["sels"]]
    )
    if spec["order"]:
        col = stmt.selected_columns[0]
        stmt = stmt.order_by(col.desc() if spec["desc"] else col)
    if spec["limit"] is not None:
        stmt = stmt.limit(spec["limit"])
    return stmt


def build_dml(spec):
    target = make_table(spec["table"], False, spec["lw"])
    level = [target]
    if spec.get("extra"):
        build_leaf(spec["extra"], [], level)
    scopes = [level]

    k = spec["k"]
    if k == "insert":
        stmt = insert(target)
        cols = spec["cols"]
        if "vals" in spec:
            stmt = stmt.values(
                {
                    c: build_expr(e, [], raw_ok=True)
                    for c, e in zip(cols, spec["vals"])
                }
            )
        elif "multi" in spec:
            stmt = stmt.values([dict(zip(cols, row)) for row in spec["multi"]])
        else:
            stmt = stmt.from_select(cols, build_select(spec["sel"], []))
    elif k == "update":
        stmt = update(target)
        if spec["where"]:
            stmt = stmt.where(*[build_cond(c, scopes) for c in spec["where"]])
        pairs = [
            (
                target.c[c] if spec["keys"] == "col" else c,
                build_expr(e, scopes, raw_ok=True),
            )
            for c, e in spec["set"]
        ]
        if spec["ordered"]:
            stmt = stmt.ordered_values(*pairs)
        else:
            stmt = stmt.values(dict(pairs))
    else:
        stmt = delete(target)
        if spec["where"]:
            stmt = stmt.where(*[build_cond(c, scopes) for c in spec["where"]])

    ret = spec["ret"]
    if ret:
        cols = [target.c[c] for c in ret["cols"]]
        if ret["expr"]:
            cols.append(build_expr(ret["expr"], [[target]]).label("r"))
        stmt = stmt.returning(*cols)
    return stmt


def build(spec):
    k = spec["k"]
    if k in ("select", "compound"):
        if k == "select":
            stmt = build_select(spec, [])
        else:
            stmt = build_compound(spec, [])
        if spec.get("params"):
            stmt = stmt.params({p[0]: p[3] for p in spec["params"]})
        return stmt
    return build_dml(spec)


# ------------------------------------------------------------- running

DIALECTS = {}
ENGINE = None
# seconds
STATEMENT_TIMEOUT = 2
DEADLINE = [0.0]


def init_worker():
    global ENGINE
    warnings.simplefilter("ignore")
    DIALECTS["sqlite"] = sqlite.dialect()
    DIALECTS["postgresql"] = postgresql.dialect()
    DIALECTS["mysql"] = mysql.dialect()
    DIALECTS["mssql"] = mssql.dialect()

    ENGINE = create_engine("sqlite://")

    # pysqlite doesn't BEGIN before statements such as WITH .. INSERT, so
    # without this they would be committed rather than rolled back
    @event.listens_for(ENGINE, "connect")
    def connect(dbapi_connection, connection_record):  # noqa: U100
        dbapi_connection.isolation_level = None
        # interrupt a statement that runs too long, e.g. a recursive CTE
        # that doesn't stop because it got the wrong parameters
        dbapi_connection.set_progress_handler(
            lambda: time.monotonic() > DEADLINE[0], 10000
        )

    @event.listens_for(ENGINE, "before_cursor_execute")
    def before_cursor_execute(*arg):  # noqa: U100
        DEADLINE[0] = time.monotonic() + STATEMENT_TIMEOUT

    @event.listens_for(ENGINE, "begin")
    def begin(conn):
        conn.exec_driver_sql("BEGIN")

    METADATA.create_all(ENGINE)
    with ENGINE.begin() as conn:
        for name, rows in ROWS.items():
            cols = list(TABLES[name])
            conn.execute(
                TABLE_OBJS[name].insert(), [dict(zip(cols, r)) for r in rows]
            )


def _error(err):
    # without object addresses, so that the same error is counted once in
    # the list of invalid seeds
    message = re.sub(r" at 0x[0-9a-f]+", "", str(err).split("\n")[0])
    return ("error", type(err).__name__, message[:200])


ANON_LABEL = re.compile(r"%\(([^ )]+) ([^)]*)\)s")


def _bind_forms(compiled, name):
    """the ways a bound parameter name can appear in compiled SQL"""
    name = compiled.escaped_bind_names.get(name, name)
    return (f"%({name})s", f":{name}", f"__[POSTCOMPILE_{name}]")


def anon_bind_names(compiled):
    """name -> canonical name, for the anonymous bound parameters.

    Only the position of an anonymous parameter is part of the cache key,
    not its name ("param_1", "a_2", ...), so these are renamed to
    "<bind N>" in order of appearance before comparing.
    """
    anon = {
        name
        for bind, name in compiled.bind_names.items()
        if bind.key.startswith("%(")
    }

    def position(name):
        found = [
            match.start()
            for form in _bind_forms(compiled, name)
            for match in [re.search(_bind_re(form), compiled.string)]
            if match
        ]
        return (min(found) if found else len(compiled.string), name)

    order = list(compiled.positiontup or ()) + sorted(anon, key=position)
    names = {}
    for name in order:
        if name in anon and name not in names:
            names[name] = f"<bind {len(names) + 1}>"
    return names


def _bind_re(form):
    # ":param_1" mustn't match ":param_12"
    return re.escape(form) + r"(?![\w'])"


def _rename_binds(compiled, names):
    sql = compiled.string
    # longest first, so that "a_1" doesn't replace part of "a_10"
    for name in sorted(names, key=len, reverse=True):
        forms = zip(
            _bind_forms(compiled, name), _bind_forms(compiled, names[name])
        )
        for form, new_form in forms:
            sql = re.sub(
                _bind_re(form),
                lambda m, new_form=new_form: new_form,
                sql,
            )
    return sql


def _anon_labels(labels):
    """replace object ids in anonymous labels such as '%(1234 length)s'
    with their order of appearance"""
    ids = {}

    def repl(match):
        num = ids.setdefault(match.group(1), len(ids) + 1)
        return f"%(<anon {num}> {match.group(2)})s"

    return [ANON_LABEL.sub(repl, label) for label in labels]


def compile_cached(stmt, dialect, cache):
    """compile as Connection.execute() does, with the given compiled cache.

    returns the compiled object and the parameters it would execute with,
    with anonymous parameters renamed as in anon_bind_names()
    """
    out = stmt._compile_w_cache(dialect, compiled_cache=cache, column_keys=[])
    if len(out) == 4:
        # 2.1: the parameters collected from Executable.params()
        compiled, extracted, collected, _ = out
    else:
        (compiled, extracted, _), collected = out, None
    kw = {} if collected is None else {"_collected_params": collected}
    try:
        params = compiled.construct_params(
            extracted_parameters=extracted, escape_names=False, **kw
        )
    except Exception as err:
        return compiled, _error(err)
    names = anon_bind_names(compiled)
    return compiled, {names.get(k, k): v for k, v in params.items()}


def anon_label_names(compiled):
    """name -> canonical name, for anonymous labels and aliases.

    These share their numbering with anonymous bound parameters (a label
    "coalesce_2" comes after a parameter "coalesce_1"), so they're renamed
    to "<label N>" in order of appearance for a second comparison, which
    tells a difference only in their numbering apart from other ones.
    """
    binds = set(compiled.bind_names.values())
    labels = {
        value
        for key, value in compiled.anon_map.items()
        if isinstance(key, str) and " " in key and value not in binds
    }

    def position(name):
        match = re.search(_label_re(name), compiled.string)
        return (match.start() if match else len(compiled.string), name)

    return {
        name: f"<label {i}>"
        for i, name in enumerate(sorted(labels, key=position), 1)
    }


def _label_re(name):
    return r"(?<![\w'])" + re.escape(name) + r"(?![\w'])"


def _rename_labels(sql, names):
    for name in sorted(names, key=len, reverse=True):
        sql = re.sub(_label_re(name), lambda m, n=names[name]: n, sql)
    return sql


def signature(compiled, rename_labels=False):
    """everything about a compiled statement that the cache key must cover;
    with rename_labels, ignoring the numbering of anonymous labels"""
    names = anon_bind_names(compiled)
    labels = anon_label_names(compiled) if rename_labels else {}
    bind_names = compiled.bind_names
    result_columns = compiled._result_columns
    return {
        "sql": _rename_labels(_rename_binds(compiled, names), labels),
        "result columns": list(
            zip(
                [labels.get(rc[0], rc[0]) for rc in result_columns],
                _anon_labels([rc[1] for rc in result_columns]),
                [repr(rc[3]) for rc in result_columns],
            )
        ),
        "bind types": sorted(
            (names.get(name, name), repr(bind.type))
            for bind, name in bind_names.items()
        ),
        "positional order": [
            names.get(name, name) for name in compiled.positiontup or ()
        ],
        "literal_execute": sorted(
            names.get(bind_names.get(b), "?")
            for b in compiled.literal_execute_params
        ),
        "result structure": (
            compiled._ordered_columns,
            compiled._textual_ordered_columns,
            compiled._ad_hoc_textual,
            compiled._loose_column_name_matching,
        ),
    }


def compile_all(stmt):
    """dialect name -> (signature, signature with anonymous labels renamed,
    params), or an error tuple"""
    out = {}
    for name, dialect in DIALECTS.items():
        try:
            compiled, params = compile_cached(stmt, dialect, None)
            out[name] = (
                signature(compiled),
                signature(compiled, rename_labels=True),
                params,
            )
        except Exception as err:
            out[name] = _error(err)
    return out


def execute(spec, cache):
    """run the statement on SQLite, in a transaction that's rolled back"""
    with ENGINE.connect() as conn:
        try:
            stmt = build(spec)
            result = conn.execute(
                stmt, execution_options={"compiled_cache": cache}
            )
            if result.returns_rows:
                out = ("ok", list(result.keys()), [tuple(r) for r in result])
            else:
                out = ("ok", result.rowcount)
            if spec["k"] in ("insert", "update", "delete"):
                t = TABLE_OBJS[spec["table"]]
                rows = conn.execute(
                    select(t).order_by(t.c.id),
                    execution_options={"compiled_cache": None},
                ).all()
                out += ([tuple(r) for r in rows],)
        except Exception as err:
            out = _error(err)
        finally:
            conn.rollback()
    return out


def _is_error(value):
    return isinstance(value, tuple) and value[:1] == ("error",)


def _sql_diff(expected, actual):
    return "\n".join(
        difflib.unified_diff(
            expected.splitlines(),
            actual.splitlines(),
            "statement",
            "variant",
            lineterm="",
            n=1,
        )
    )


def describe_compile_diff(expected, actual):
    if _is_error(expected) or _is_error(actual):
        return f"statement: {expected!r:.300}\nvariant:   {actual!r:.300}"
    lines = []
    for key in expected:
        if expected[key] == actual[key]:
            continue
        if key == "sql":
            lines.append(_sql_diff(expected[key], actual[key]))
        else:
            lines.append(f"{key}: statement {expected[key]!r:.300}")
            lines.append(f"{' ' * len(key)}  variant   {actual[key]!r:.300}")
    return "\n".join(lines)


def describe_params_diff(expected, actual):
    if _is_error(expected) or _is_error(actual):
        return f"expected: {expected!r:.300}\nactual:   {actual!r:.300}"
    lines = []
    for key in sorted(set(expected) | set(actual)):
        if expected.get(key, "<missing>") != actual.get(key, "<missing>"):
            lines.append(
                f"{key}: expected {expected.get(key, '<missing>')!r}, "
                f"got {actual.get(key, '<missing>')!r}"
            )
    return "\n".join(lines)


def describe_exec_diff(expected, actual):
    lines = [f"expected: {expected!r:.600}", f"actual:   {actual!r:.600}"]
    return "\n".join(lines)


class Log:
    """collects replay output; does nothing unless verbose"""

    def __init__(self, verbose):
        self.verbose = verbose
        self.lines = []

    def __call__(self, text_, indent=2):
        if self.verbose:
            pad = " " * indent
            self.lines.extend(pad + line for line in str(text_).splitlines())


def check_variant(stmt, compiled, label, variant, log):
    """compile checks for a variant with the same cache key as stmt.

    returns the failed checks
    """
    findings = set()
    try:
        var_stmt = build(variant)
    except Exception as err:
        log(f"{label}: can't be built: {_error(err)}")
        return findings
    var_compiled = compile_all(var_stmt)
    for name, dialect in DIALECTS.items():
        expected = compiled[name]
        actual = var_compiled[name]
        if _is_error(expected) and _is_error(actual):
            continue
        if _is_error(expected) or _is_error(actual):
            findings.add("collision")
            log(f"{label} compiles differently on {name}:")
            log(describe_compile_diff(expected, actual), 4)
            continue
        if expected[1] != actual[1]:
            findings.add("collision")
            log(f"{label} compiles differently on {name}:")
            log(describe_compile_diff(expected[0], actual[0]), 4)
            continue
        if expected[0] != actual[0]:
            findings.add("anon_labels")
            log(f"{label} numbers anonymous labels differently on {name}:")
            log(describe_compile_diff(expected[0], actual[0]), 4)

        # look the variant up in a cache that has the statement
        cache = {}
        compile_cached(stmt, dialect, cache)
        _, warm_params = compile_cached(var_stmt, dialect, cache)
        if warm_params != actual[2]:
            findings.add("params")
            log(
                f"{label}: parameters on {name} from the statement's "
                "compiled form differ from compiling it on its own:"
            )
            log(describe_params_diff(actual[2], warm_params), 4)
    return findings


def check_trial(spec, literal_variant, mutants, log):
    """run all checks; returns (status, info)"""
    try:
        stmt = build(spec)
        key = stmt._generate_cache_key()
        compiled = compile_all(stmt)
    except Exception as err:
        log(f"statement can't be built: {_error(err)}")
        return "invalid", _error(err)[1:]
    errors = [c for c in compiled.values() if _is_error(c)]
    if len(errors) == len(compiled):
        log(f"statement doesn't compile: {errors[0]}")
        return "invalid", errors[0][1:]

    for name, value in compiled.items():
        if _is_error(value):
            log(f"--- {name}: raises {value[1]}: {value[2]}", 0)
        else:
            log(f"--- {name}", 0)
            log(value[0]["sql"])
    if key is None:
        log("--- statement has no cache key; nothing to check", 0)
        return "ok", None

    findings = set()
    log("--- checks", 0)
    variants = [("literal variant", literal_variant)] + [
        (f"variant [{i}]", m) for i, (_, m) in enumerate(mutants)
    ]
    for label, variant in variants:
        try:
            var_key = build(variant)._generate_cache_key()
        except Exception as err:
            log(f"{label}: can't be built: {_error(err)}")
            continue
        if var_key is None or var_key != key:
            if label == "literal variant":
                findings.add("literal_key")
                if var_key is None:
                    log(f"{label}: has no cache key")
                else:
                    log(f"{label}: different cache key: {key._diff(var_key)}")
            else:
                log(f"{label}: different cache key")
            continue
        log(f"{label}: same cache key")
        findings |= check_variant(stmt, compiled, label, variant, log)

    if not _is_error(compiled["sqlite"]):
        cold = execute(spec, None)
        if _is_error(cold):
            log(f"execution skipped, raises on SQLite: {cold[1]}: {cold[2]}")
        else:
            cache = {}
            for _, variant in variants:
                execute(variant, cache)
            warm = execute(spec, cache)
            if warm != cold:
                findings.add("execution")
                log("execution after the variants differs:")
                log(describe_exec_diff(cold, warm), 4)
            else:
                log("execution after the variants: same result")

    if findings:
        return "diff", tuple(sorted(findings))
    return "ok", None


def trial(seed):
    try:
        spec, literal_variant, mutants = gen_trial(seed)
        status, info = check_trial(spec, literal_variant, mutants, Log(False))
    except Exception:
        return seed, "crash", traceback.format_exc(limit=4)
    return seed, status, info


# ----------------------------------------------------------- reporting


def features(spec, mutants=()):
    """coarse set of the features used by a spec, to help group findings"""
    out = {f"stmt:{spec['k']}"}
    for _, node in walk(spec):
        k = node["k"]
        if node.get("params"):
            out.add("params()")
        if k == "select":
            for key in ("distinct", "limit", "offset", "fetch", "having"):
                if node[key]:
                    out.add(key)
            if node["order_by"]:
                out.add("order_by")
            if node["group_by"]:
                out.add("group_by")
            if node["corr"]:
                out.add(f"correlate:{node['corr']}")
            if node.get("woc"):
                out.add("with_only_columns")
            if node.get("label_style"):
                out.add(f"label_style:{node['label_style']}")
            if node.get("for_update"):
                out.add("for_update")
            if node["prefix"] or node["suffix"]:
                out.add("prefix/suffix")
        elif k == "compound":
            out.add(node["op"])
        elif k == "join":
            out.add(
                "full_join"
                if node["full"]
                else "outerjoin" if node["outer"] else "join"
            )
            if node["via"] == "method":
                out.add("join_from")
        elif k == "table":
            if node["alias"]:
                out.add("alias")
            if node["lw"]:
                out.add("table()")
        elif k == "subq":
            out.add("lateral" if node["lat"] else "subquery")
        elif k == "cte":
            out.add("nesting_cte" if node["nesting"] else "cte")
        elif k == "rcte":
            out.add("recursive_cte")
        elif k == "lit":
            out.add(f"lit:{node['mode']}")
        elif k == "in":
            out.add(f"in:{node['mode']}")
        elif k == "tc":
            out.add("type_coerce")
        elif k == "ssq":
            out.add("scalar_subquery")
        elif k in ("textsq", "text"):
            out.add("text")
        elif k in ("insert", "update", "delete") and node["ret"]:
            out.add("returning")
        elif k in ("cast", "lc", "values", "case", "exists", "insub", "agg"):
            out.add({"lc": "literal_column", "insub": "in_subquery"}.get(k, k))
    for desc, _ in mutants:
        # e.g. "mutated:select.corr"
        node_kind = desc.split(" (", 1)[1].split(")", 1)[0]
        field = desc.split(") ", 1)[1].split(":", 1)[0]
        out.add(f"mutated:{node_kind}.{field}")
    return out


def replay(seed):
    init_worker()
    spec, literal_variant, mutants = gen_trial(seed)
    print("--- statement spec")
    pprint.pprint(spec, width=79)
    print("--- literal variant: changes to the spec")
    diff = difflib.unified_diff(
        pprint.pformat(spec, width=79).splitlines(),
        pprint.pformat(literal_variant, width=79).splitlines(),
        lineterm="",
        n=0,
    )
    print(
        "\n".join(line for line in diff if not line.startswith(("---", "+++")))
        or "  (none)"
    )
    print("--- structural variants")
    for i, (desc, _) in enumerate(mutants):
        print(f"  [{i}] {desc}")

    log = Log(True)
    status, info = check_trial(spec, literal_variant, mutants, log)
    print("\n".join(log.lines))
    if status == "invalid":
        print(f"--- invalid: {info}")
    else:
        print(f"--- findings: {list(info) if info else 'none'}")


def seed_features(seed):
    spec, _, mutants = gen_trial(seed)
    return features(spec, mutants)
