"""
ORM loading vs. the compiled cache.

Generates random statements against a fixed inheritance-heavy mapping:
a root entity (plain, aliased() or with_polymorphic()) plus a tree of loader
options (selectinload, joinedload, subqueryload, immediateload, defaultload,
of_type(), relationship.and_(), selectin_polymorphic(), defer / undefer /
load_only, with_expression(), with_loader_criteria()).  Each statement T is
run several ways, all of which must produce the same result:

cold
    T executed with ``compiled_cache=None``.

sibling
    On an empty cache, execute a sibling statement S, then T.  S has the
    same loader option structure as T, but a different parent statement
    (extra WHERE, ORDER BY, LIMIT, join, ...), new aliased() /
    with_polymorphic() objects and possibly different literal values in
    criteria and expressions.  Catches cached sub-statements (selectin
    loads, subquery loads, ...) that pick up state from the wrong parent.

repeat
    On an empty cache, execute T twice, each time with new aliased() /
    with_polymorphic() objects.  Catches state from the cached statement
    not lining up with the statement actually passed.

polymorphic_load
    The whole run is performed against two otherwise identical mappings,
    one of which sets ``polymorphic_load="selectin"`` on a subclass.  As
    that is only a loading optimization, a statement that runs against one
    mapping must not raise against the other.

The result compared is the loaded object graph, read from each object's
``__dict__`` so that nothing is lazy loaded, along with the number of SQL
statements emitted.

"""

from __future__ import annotations

import pprint
import random
import traceback
import warnings

from sqlalchemy import Column
from sqlalchemy import create_engine
from sqlalchemy import event
from sqlalchemy import ForeignKey
from sqlalchemy import inspect
from sqlalchemy import Integer
from sqlalchemy import literal
from sqlalchemy import select
from sqlalchemy import String
from sqlalchemy.orm import aliased
from sqlalchemy.orm import declarative_base
from sqlalchemy.orm import defaultload
from sqlalchemy.orm import defer
from sqlalchemy.orm import immediateload
from sqlalchemy.orm import joinedload
from sqlalchemy.orm import load_only
from sqlalchemy.orm import query_expression
from sqlalchemy.orm import relationship
from sqlalchemy.orm import RelationshipProperty
from sqlalchemy.orm import selectin_polymorphic
from sqlalchemy.orm import selectinload
from sqlalchemy.orm import Session
from sqlalchemy.orm import subqueryload
from sqlalchemy.orm import undefer
from sqlalchemy.orm import with_expression
from sqlalchemy.orm import with_loader_criteria
from sqlalchemy.orm import with_polymorphic

NAME = "orm-loading"
DESCRIPTION = "ORM loader options vs. the compiled cache"

CHECKS = {
    "sibling": "loads something different when run after a sibling "
    "statement, same loader options but a different parent statement, "
    "was cached",
    "repeat": "loads something different when run a second time on the "
    "same cache, with new aliased() / with_polymorphic() objects",
    "polymorphic_load": "raises against only one of the two mappings, "
    'with and without polymorphic_load="selectin"',
}


# ----------------------------------------------------------------- model

RELS = {
    "Company": [("employees", "Person")],
    "Person": [("paperwork", "Paperwork")],
    "Engineer": [("machines", "Machine")],
    "Manager": [("reports", "Report")],
    "Boss": [],
    "Machine": [("parts", "Part")],
    "Paperwork": [],
    "Report": [],
    "Part": [],
}
COLS = {
    "Company": ["name"],
    "Person": ["name"],
    "Engineer": ["primary_language"],
    "Manager": ["manager_name"],
    "Boss": ["golf_swing"],
    "Machine": ["name"],
    "Part": ["name"],
    "Paperwork": ["description"],
    "Report": ["title"],
}
SUPER = {"Engineer": "Person", "Manager": "Person", "Boss": "Manager"}
SUBS = {"Person": ["Engineer", "Manager", "Boss"], "Manager": ["Boss"]}

# literal values used in criteria; each matches some rows, so that criteria
# with different values load different results
VALUES = {
    "Company": ["c1", "c2"],
    "Person": ["p1", "p2", "p3", "p5", "p7"],
    "Engineer": ["java", "python"],
    "Manager": ["mn2", "mn7"],
    "Boss": ["fore", "slice"],
    "Machine": ["m1", "m3"],
    "Part": ["pt1", "pt3"],
    "Paperwork": ["pw1", "pw3", "pw5", "pw6"],
    "Report": ["r1", "r3"],
}

MAPPINGS = ("default", "polymorphic_load")


class Model:
    def __init__(self, polymorphic_load):
        self.classes = {}
        self.engine = None
        self.statement_count = 0
        self._setup(polymorphic_load)

    def _setup(self, polymorphic_load):
        Base = declarative_base()

        class Company(Base):
            __tablename__ = "company"
            id = Column(Integer, primary_key=True)
            name = Column(String(20))
            employees = relationship("Person", order_by="Person.id")

        class Person(Base):
            __tablename__ = "person"
            id = Column(Integer, primary_key=True)
            company_id = Column(ForeignKey("company.id"))
            name = Column(String(20))
            type = Column(String(20))
            expr = query_expression()
            paperwork = relationship("Paperwork", order_by="Paperwork.id")
            __mapper_args__ = {
                "polymorphic_on": type,
                "polymorphic_identity": "person",
            }

        class Engineer(Person):
            __tablename__ = "engineer"
            id = Column(ForeignKey("person.id"), primary_key=True)
            primary_language = Column(String(20))
            machines = relationship("Machine", order_by="Machine.id")
            __mapper_args__ = {"polymorphic_identity": "engineer"}
            if polymorphic_load:
                __mapper_args__["polymorphic_load"] = "selectin"

        class Manager(Person):
            __tablename__ = "manager"
            id = Column(ForeignKey("person.id"), primary_key=True)
            manager_name = Column(String(20))
            golf_swing = Column(String(20))
            reports = relationship("Report", order_by="Report.id")
            __mapper_args__ = {"polymorphic_identity": "manager"}

        class Boss(Manager):
            # single table inheritance from the joined inheritance Manager
            __mapper_args__ = {"polymorphic_identity": "boss"}

        class Paperwork(Base):
            __tablename__ = "paperwork"
            id = Column(Integer, primary_key=True)
            person_id = Column(ForeignKey("person.id"))
            description = Column(String(20))

        class Machine(Base):
            __tablename__ = "machine"
            id = Column(Integer, primary_key=True)
            engineer_id = Column(ForeignKey("engineer.id"))
            name = Column(String(20))
            parts = relationship("Part", order_by="Part.id")

        class Part(Base):
            __tablename__ = "part"
            id = Column(Integer, primary_key=True)
            machine_id = Column(ForeignKey("machine.id"))
            name = Column(String(20))

        class Report(Base):
            __tablename__ = "report"
            id = Column(Integer, primary_key=True)
            manager_id = Column(ForeignKey("manager.id"))
            title = Column(String(20))

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
        ):
            self.classes[cls.__name__] = cls

        self.engine = engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)

        @event.listens_for(engine, "before_cursor_execute")
        def count(*arg):  # noqa: U100
            self.statement_count += 1

        def pw(*ids):
            return [Paperwork(id=i, description=f"pw{i}") for i in ids]

        def machine(i, parts):
            return Machine(
                id=i,
                name=f"m{i}",
                parts=[Part(id=p, name=f"pt{p}") for p in parts],
            )

        def reports(*ids):
            return [Report(id=i, title=f"r{i}") for i in ids]

        with Session(engine) as sess:
            sess.add_all(
                [
                    Company(
                        id=1,
                        name="c1",
                        employees=[
                            Engineer(
                                id=1,
                                name="p1",
                                primary_language="java",
                                paperwork=pw(1, 2),
                                machines=[machine(1, [1, 2]), machine(2, [])],
                            ),
                            Manager(
                                id=2,
                                name="p2",
                                manager_name="mn2",
                                paperwork=pw(3),
                                reports=reports(1, 2),
                            ),
                            Boss(
                                id=3,
                                name="p3",
                                manager_name="mn3",
                                golf_swing="fore",
                                paperwork=pw(4),
                                reports=reports(3),
                            ),
                            Person(id=4, name="p4"),
                        ],
                    ),
                    Company(
                        id=2,
                        name="c2",
                        employees=[
                            Engineer(
                                id=5,
                                name="p5",
                                primary_language="python",
                                paperwork=pw(5),
                                machines=[machine(3, [3])],
                            ),
                            Engineer(
                                id=6,
                                name="p6",
                                primary_language="java",
                                machines=[machine(4, [])],
                            ),
                            Manager(
                                id=7,
                                name="p7",
                                manager_name="mn7",
                                golf_swing="slice",
                                paperwork=pw(6, 7),
                                reports=reports(4),
                            ),
                            Person(id=8, name="p8"),
                        ],
                    ),
                ]
            )
            sess.commit()


def lineage(cls):
    """the given class name plus the names of its superclasses"""
    out = [cls]
    while cls in SUPER:
        cls = SUPER[cls]
        out.append(cls)
    return out


def _with_subclasses(avail, subs):
    avail = list(avail)
    for sub in subs:
        avail.extend(c for c in lineage(sub) if c not in avail)
    return avail


# ------------------------------------------------------ spec generation
#
# a "spec" is a plain data structure describing a statement; it's turned
# into an actual statement by build_stmt(), which creates new aliased() /
# with_polymorphic() objects each time it's called.

STRATEGIES = {
    "selectinload": selectinload,
    "joinedload": joinedload,
    "subqueryload": subqueryload,
    "immediateload": immediateload,
    "defaultload": defaultload,
}

VARIANTS = [
    "none",
    "where_id",
    "where_col",
    "order",
    "limit",
    "distinct",
    "in_subq",
    "join",
]


class CtxDesc:
    """describes the entity that options are being generated against"""

    def __init__(self, base, kind, avail):
        self.base = base
        self.kind = kind  # "plain", "aliased", "wp"
        self.avail = avail  # class names whose attributes may be used


def _sample_subs(rng, base):
    return sorted(rng.sample(SUBS[base], rng.randint(1, len(SUBS[base]))))


def gen_root(rng):
    base = rng.choice(["Company", "Company", "Person", "Person", "Manager"])

    r = rng.random()
    if base in SUBS and r < 0.4:
        entity = ("wp", _sample_subs(rng, base), rng.random() < 0.5)
        ctx = CtxDesc(base, "wp", _with_subclasses(lineage(base), entity[1]))
    elif r < 0.7:
        entity = ("aliased", rng.random() < 0.5)
        ctx = CtxDesc(base, "aliased", lineage(base))
    else:
        entity = ("plain",)
        ctx = CtxDesc(base, "plain", lineage(base))

    spec = {
        "base": base,
        "entity": entity,
        "top_sp": None,
        "wlc": None,
        "variant": rng.choice(VARIANTS),
    }
    if entity[0] == "plain" and base in SUBS and rng.random() < 0.5:
        spec["top_sp"] = _sample_subs(rng, base)
        ctx = CtxDesc(
            base, "plain", _with_subclasses(ctx.avail, spec["top_sp"])
        )
    if rng.random() < 0.2:
        spec["wlc"] = rng.choice(VALUES["Paperwork"])
    spec["opts"] = gen_opts(rng, ctx, 0, 2)
    return spec


def gen_opts(rng, ctx, depth, maxn):
    opts = []
    for _ in range(rng.randint(0 if depth else 1, maxn)):
        rels = [
            (owner, key, target)
            for owner in ctx.avail
            for key, target in RELS[owner]
        ]
        if rels and depth < 3 and rng.random() < 0.7:
            opts.append(gen_rel_opt(rng, rels, depth))
        else:
            opts.append(gen_col_opt(rng, ctx))
    return opts


def gen_col_opt(rng, ctx):
    if "Person" in ctx.avail and rng.random() < 0.4:
        return {"t": "expr", "value": rng.randint(1, 3)}
    owner = rng.choice(ctx.avail)
    return {
        "t": "col",
        "op": rng.choice(["defer", "undefer", "load_only"]),
        "owner": owner,
        "key": rng.choice(COLS[owner]),
    }


def gen_rel_opt(rng, rels, depth):
    owner, key, target = rng.choice(rels)
    spec = {
        "t": "rel",
        "strategy": rng.choice(list(STRATEGIES)),
        "owner": owner,
        "key": key,
        "of_type": None,
        "crit": None,
        "sp": None,
        "children": [],
    }

    tbase = target
    if rng.random() < 0.5:
        tctx = CtxDesc(target, "plain", lineage(target))
    else:
        choices = [("aliased", target)]
        if target in SUBS:
            choices += [("cls", s) for s in SUBS[target]]
            choices += [("aliased", s) for s in SUBS[target]]
            choices.append(("wp", target))
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
            subs = _sample_subs(rng, target)
            spec["of_type"] = ("wp", subs, rng.random() < 0.5)
            tctx = CtxDesc(
                target, "wp", _with_subclasses(lineage(target), subs)
            )

    if rng.random() < 0.3:
        spec["crit"] = rng.choice(VALUES[tbase])

    if tctx.kind == "plain" and tbase in SUBS and rng.random() < 0.5:
        spec["sp"] = _sample_subs(rng, tbase)
        tctx = CtxDesc(
            tbase, "plain", _with_subclasses(tctx.avail, spec["sp"])
        )

    spec["children"] = gen_opts(rng, tctx, depth + 1, 2)
    return spec


def mutate_literals(rng, spec):
    """return a copy of the spec with some literal values changed"""
    if isinstance(spec, list):
        return [mutate_literals(rng, elem) for elem in spec]
    elif not isinstance(spec, dict):
        return spec

    out = {}
    for key, value in spec.items():
        if key == "crit" and value is not None and rng.random() < 0.5:
            (domain,) = [vs for vs in VALUES.values() if value in vs]
            out[key] = rng.choice(domain)
        elif key == "value" and spec.get("t") == "expr":
            out[key] = rng.randint(4, 6) if rng.random() < 0.5 else value
        elif key == "wlc" and value is not None and rng.random() < 0.5:
            out[key] = rng.choice(VALUES["Paperwork"])
        else:
            out[key] = mutate_literals(rng, value)
    return out


def gen_trial(seed):
    rng = random.Random(seed)
    spec = gen_root(rng)
    sibling = mutate_literals(rng, spec)
    sibling["variant"] = rng.choice(
        [v for v in VARIANTS if v != spec["variant"]]
    )
    return spec, sibling


# ------------------------------------------------------ statement build


class Ctx:
    """the entity that options are being built against"""

    def __init__(self, classes, base, obj, kind):
        self.classes = classes
        self.base = base
        self.obj = obj
        self.kind = kind

    def attr(self, owner, key):
        if self.kind == "plain":
            if owner in lineage(self.base):
                return getattr(self.classes[self.base], key)
            return getattr(self.classes[owner], key)
        elif self.kind == "aliased":
            return getattr(self.obj, key)
        elif owner in lineage(self.base):
            return getattr(self.obj, key)
        else:
            return getattr(getattr(self.obj, owner), key)


def build_opt(classes, spec, ctx):
    if spec["t"] == "expr":
        return with_expression(
            ctx.attr("Person", "expr"), literal(spec["value"])
        )
    elif spec["t"] == "col":
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
        col = tctx.attr(tctx.base, COLS[tctx.base][0])
        attr = attr.and_(col != spec["crit"])

    loader = STRATEGIES[spec["strategy"]](attr)
    if spec["sp"]:
        loader = loader.selectin_polymorphic([classes[s] for s in spec["sp"]])
    if spec["children"]:
        loader = loader.options(
            *[build_opt(classes, child, tctx) for child in spec["children"]]
        )
    return loader


def build_stmt(classes, spec):
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

    opts = []
    if spec["top_sp"]:
        opts.append(
            selectin_polymorphic(root, [classes[s] for s in spec["top_sp"]])
        )
    opts.extend(build_opt(classes, opt, ctx) for opt in spec["opts"])
    if spec["wlc"] is not None:
        paperwork = classes["Paperwork"]
        opts.append(
            with_loader_criteria(
                paperwork, paperwork.description != spec["wlc"]
            )
        )
    stmt = select(root).options(*opts)

    variant = spec["variant"]
    if variant == "where_id":
        stmt = stmt.where(root.id > 0)
    elif variant == "where_col":
        stmt = stmt.where(getattr(root, COLS[base][0]) != "zzz")
    elif variant == "order":
        stmt = stmt.order_by(root.id.desc())
    elif variant == "limit":
        stmt = stmt.limit(100)
    elif variant == "distinct":
        stmt = stmt.distinct()
    elif variant == "in_subq":
        stmt = stmt.where(root.id.in_(select(classes[base].id)))
    elif variant == "join":
        key = RELS[base][0][0] if RELS[base] else "paperwork"
        stmt = stmt.outerjoin(getattr(root, key))
    return stmt


# ------------------------------------------------------------- running


def snapshot(objs):
    """the loaded object graph, read from __dict__ only"""
    seen = {}

    def visit(obj):
        state = inspect(obj)
        ident = (type(obj).__name__,) + tuple(state.identity)
        if ident in seen:
            return ident
        seen[ident] = None
        data = {}
        for prop in state.mapper.attrs:
            if prop.key not in state.dict:
                data[prop.key] = "<unloaded>"
                continue
            value = state.dict[prop.key]
            if not isinstance(prop, RelationshipProperty) or value is None:
                data[prop.key] = value
            elif isinstance(value, list):
                data[prop.key] = [visit(elem) for elem in value]
            else:
                data[prop.key] = visit(value)
        seen[ident] = data
        return ident

    return [visit(obj) for obj in objs], seen


MODELS = {}


def init_worker():
    warnings.simplefilter("ignore")
    MODELS["default"] = Model(polymorphic_load=False)
    MODELS["polymorphic_load"] = Model(polymorphic_load=True)


def run(model, spec, cache):
    with model.engine.connect() as conn:
        conn = conn.execution_options(compiled_cache=cache)
        with Session(conn) as sess:
            try:
                stmt = build_stmt(model.classes, spec)
                model.statement_count = 0
                objs = sess.scalars(stmt).unique().all()
                return ("ok", model.statement_count, snapshot(objs))
            except Exception as err:
                return ("error", type(err).__name__, str(err)[:300])


def check_spec(spec, sibling):
    """run all checks for one spec; returns (findings, results)"""
    findings = []
    results = {}
    for name in MAPPINGS:
        model = MODELS[name]
        cold = run(model, spec, None)

        cache = {}
        run(model, sibling, cache)
        warm_sibling = run(model, spec, cache)

        cache = {}
        run(model, spec, cache)
        warm_repeat = run(model, spec, cache)

        results[name] = (cold, warm_sibling, warm_repeat)
        if cold[0] == "error":
            continue
        if warm_sibling != cold:
            findings.append(f"sibling[{name}]")
        if warm_repeat != cold:
            findings.append(f"repeat[{name}]")

    default_cold = results["default"][0]
    poly_cold = results["polymorphic_load"][0]
    if (default_cold[0] == "error") != (poly_cold[0] == "error"):
        findings.append("polymorphic_load")

    return findings, results


def trial(seed):
    try:
        spec, sibling = gen_trial(seed)
        findings, results = check_spec(spec, sibling)
    except Exception:
        return seed, "crash", traceback.format_exc(limit=4)
    if findings:
        return seed, "diff", tuple(findings)
    if results["default"][0][0] == "error":
        return seed, "invalid", results["default"][0][1:]
    return seed, "ok", None


# ----------------------------------------------------------- reporting


def features(spec):
    """coarse set of the features used by a spec, to help group findings"""
    out = {f"root:{spec['base']}:{spec['entity'][0]}"}
    if spec["top_sp"]:
        out.add("selectin_polymorphic")
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
                if opt["sp"]:
                    out.add("selectin_polymorphic")
                walk(opt["children"])
            elif opt["t"] == "expr":
                out.add("with_expression")
            else:
                out.add(opt["op"])

    walk(spec["opts"])
    return out


def describe_diff(expected, actual):
    if expected[0] == "error" or actual[0] == "error":
        if expected == actual:
            return ""
        return f"  expected: {expected!r:.400}\n  actual:   {actual!r:.400}"

    lines = []
    if expected[1] != actual[1]:
        lines.append(
            f"  SQL statement count: expected {expected[1]}, got {actual[1]}"
        )
    (top_e, seen_e), (top_a, seen_a) = expected[2], actual[2]
    if top_e != top_a:
        lines.append(f"  rows: expected {top_e}, got {top_a}")
    for ident in sorted(set(seen_e) | set(seen_a), key=str):
        data_e, data_a = seen_e.get(ident), seen_a.get(ident)
        if data_e == data_a:
            continue
        if data_e is None or data_a is None:
            lines.append(f"  {ident}: expected {data_e}, got {data_a}")
            continue
        for key in sorted(set(data_e) | set(data_a)):
            if data_e.get(key) != data_a.get(key):
                lines.append(
                    f"  {ident}.{key}: expected {data_e.get(key)!r}, "
                    f"got {data_a.get(key)!r}"
                )
    return "\n".join(lines)


def replay(seed):
    init_worker()
    spec, sibling = gen_trial(seed)
    print("--- statement spec")
    pprint.pprint(spec, width=79)
    print("--- sibling spec")
    pprint.pprint(sibling, width=79)

    findings, results = check_spec(spec, sibling)
    for name in MAPPINGS:
        cold, warm_sibling, warm_repeat = results[name]
        print(f"--- mapping: {name}")
        if cold[0] == "error":
            print(f"  cold run raised {cold[1]}: {cold[2]}")
            continue
        print(f"  cold run: OK, {cold[1]} SQL statements")
        for label, warm in (
            ("sibling", warm_sibling),
            ("repeat", warm_repeat),
        ):
            print(f"  {label}: {describe_diff(cold, warm).strip() or 'OK'}")
    if "polymorphic_load" in findings:
        print("--- polymorphic_load: raises against only one mapping")
        print(
            describe_diff(
                results["default"][0], results["polymorphic_load"][0]
            )
        )
    print(f"--- findings: {findings or 'none'}")


def seed_features(seed):
    spec, _ = gen_trial(seed)
    return features(spec)
