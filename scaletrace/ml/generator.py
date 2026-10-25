"""Synthetic FastAPI + SQLAlchemy applications with labelled root causes -- generator v4.

Why v4: v1-v3 produced datasets that a model could solve from *template fingerprints* (the handler
was always the cause; names revealed roles; each role had a unique count of queries/loops/calls).
See docs/ml.md. v4 is built so that the static shape of a function does not reveal its role:

* **One fragment pool for every role.** Root causes, hard decoys, clean functions, orchestrators and
  helpers are all assembled from the same fragments (queries, loops, helper calls, sorts, JSON,
  formatting); each function also gets 0-3 random *benign* fragments.
* **Amplifier = fetch rows, then loop with a cost operation.** A *root cause* loops over a table that
  grows with the input size. A *hard decoy* is the identical amplifier over a fixed-size table (3 rows
  regardless of size). Which entity name plays the fixed role is random per app, drawn from the same
  name pool, so static code cannot tell roots from hard decoys -- runtime evidence (or context) must.
* **Cost operations** (``kind``): ``db_query`` (query per element), ``db_helper`` (repository helper
  that queries), ``db_hidden`` (helper querying through an un-annotated ``store`` handle that static
  rules miss), ``db_lazy`` (lazy relationship per element), ``cmp_sort`` (sort the whole collection per
  element), ``cmp_helper`` (helper that sorts the whole collection).
* **Structural variety:** loop depth 0-3 (handler, or 1-3 service levels below it), 0-2 decoys and
  0-2 benign helpers attached at random points of the call chain, class-based services whose method
  call the static call graph cannot resolve (runtime-observed edges fill the gap), for/comprehension/
  while loops, SQLAlchemy 1.x ``query()`` and 2.0 ``select()`` styles, annotated and un-annotated
  handles, shared repository helpers across endpoints, neutral random names everywhere.

Labels: each defect endpoint has exactly one root cause (the function owning the growing loop: where
the fix belongs). ``scaletrace ml verify-labels`` later marks each label runtime-verified only if the
endpoint's metric grows, and checks that hard decoys stay constant.

Two styles (entity pool, module names, router layout, construct frequencies) allow a held-out style.
Generated apps are only executed via ``scaletrace ml collect-runtime --allow-execute``.
"""

from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass, field
from pathlib import Path

ROOTCAUSE_FILE = "scaletrace_rootcause.json"
GENERATOR_VERSION = "5"

DEFECT_KINDS = ["db_query", "db_helper", "db_hidden", "db_lazy", "db_lazy_helper", "cmp_sort", "cmp_helper"]
# Kinds whose cost happens inside a per-element helper: the ones a shared helper can make graph-dependent.
SHARED_KINDS = ["db_helper", "db_hidden", "db_lazy_helper", "cmp_helper"]
SHARED_MODE_PROBABILITY = 0.8
SEGMENT_WORDS = ["amber", "basalt", "cedar", "delta", "ember", "fjord", "garnet", "harbor", "indigo", "juniper", "kelp", "lumen"]
SYMPTOM_OF = {kind: ("query_growth" if kind.startswith("db") else "latency_growth") for kind in DEFECT_KINDS}

ENTITY_POOLS = {
    "A": ["Customer", "Order", "Product", "Invoice", "Region", "Shipment", "Supplier", "Account", "Ticket", "Category"],
    "B": ["Course", "Student", "Room", "Teacher", "Lesson", "Campus", "Module", "Exam", "Grade", "Term"],
}
STYLE = {
    "A": {"services": "services", "repo": "queries", "routers": ["api"], "p_select": 0.3, "p_comprehension": 0.25, "p_while": 0.1},
    "B": {"services": "repositories", "repo": "dao", "routers": ["read_api", "report_api"], "p_select": 0.6, "p_comprehension": 0.4, "p_while": 0.2},
}
NAME_VERBS = ["load", "get", "build", "make", "compute", "collect", "fetch", "prepare", "process", "handle",
              "render", "assemble", "list", "summarize", "resolve", "gather"]
NAME_NOUNS = ["data", "summary", "report", "items", "rows", "overview", "stats", "page", "view", "result",
              "records", "details", "listing", "digest"]
FIXED_ROWS = 3


@dataclass
class Fn:
    """A generated function: name, module, body lines (unindented), optional class owner."""

    name: str
    module: str
    params: str
    lines: list[str] = field(default_factory=list)
    owner_class: str | None = None
    decorator: str | None = None
    is_async: bool = False

    @property
    def func_id(self) -> str:
        qual = f"{self.owner_class}.{self.name}" if self.owner_class else self.name
        return f"app.{self.module}:{qual}"

    def render(self, indent: int = 0) -> str:
        pad = " " * indent
        head = (f"{pad}{self.decorator}\n" if self.decorator else "") + f"{pad}{'async ' if self.is_async else ''}def {self.name}({self.params}):"
        body = self.lines or ["return None"]
        return head + "\n" + "\n".join(f"{pad}    {line}" if line else "" for line in body) + "\n"


@dataclass
class Endpoint:
    path: str
    handler: Fn
    kind: str  # defect kind or "clean"
    root: Fn | None = None
    depth: int = 0
    decoys: list[Fn] = field(default_factory=list)
    class_based: bool = False
    loop_form: str = ""
    mode: str = "v4"  # "v4" or "shared_helper" (graph-required by construction)
    twin: Fn | None = None
    shared_helper: str | None = None


class AppGenerator:
    def __init__(self, seed: int, style: str = "A") -> None:
        if style not in STYLE:
            raise ValueError(f"unknown style {style!r}")
        self.rng = random.Random(seed)
        self.style = style
        self.cfg = STYLE[style]
        names = self.rng.sample(ENTITY_POOLS[style], 4)
        self.P, self.C, self.I, self.K = names  # grow-parent, grow-child, grow-item, FIXED
        self.p, self.c, self.i, self.k = (n.lower() for n in names)
        self.c_rel, self.i_rel = f"{self.c}s", f"{self.i}s"
        self.db = self.rng.choice(["db", "session"]) if style == "A" else self.rng.choice(["db", "sess", "s"])
        self.services, self.repo = self.cfg["services"], self.cfg["repo"]
        self.functions: list[Fn] = []
        self.classes: dict[str, list[Fn]] = {}
        self._helpers: dict[tuple, str] = {}
        self._force_reuse = False
        self._counter = 0
        # Segments of the growing parent table: 3 rows get "rare" values (fixed), all others "common" ones.
        words = self.rng.sample(SEGMENT_WORDS, 5)
        self.common_segments, self.rare_segments = words[:3], words[3:]
        self.constants: dict[str, list[str]] = {}
        self.fmt_name = self._name()
        self.labels_const = self._constant(self.rng.sample(SEGMENT_WORDS, 3))
        self.functions.append(Fn(self.fmt_name, self.services, "value", ["return str(value).strip().upper()"]))

    # --- naming -------------------------------------------------------------------------------

    def _name(self) -> str:
        self._counter += 1
        return f"{self.rng.choice(NAME_VERBS)}_{self.rng.choice(NAME_NOUNS)}_{self._counter}"

    def _var(self, stem: str) -> str:
        self._counter += 1
        return f"{stem}_{self._counter}"

    def _params(self) -> str:
        return f"{self.db}: Session" if self.rng.random() < 0.6 else self.db

    # --- sources and fragments ----------------------------------------------------------------

    def _source(self, bounded: bool, op: str) -> str:
        if bounded:
            return self.K
        if op.startswith("db"):
            return self.P
        return self.rng.choice([self.P, self.I])

    def _text_attr(self, model: str) -> str:
        return {self.P: "name", self.I: "title", self.K: "label"}.get(model, "id")

    def _child(self, model: str) -> tuple[str, str, str]:
        """(child model, foreign key column on child, relationship attribute on parent)."""
        if model == self.K:
            return self.I, f"{self.k}_id", self.i_rel
        return self.C, f"{self.p}_id", self.c_rel

    def _fetch_all(self, d: str, model: str) -> str:
        order = f".order_by({model}.id)" if self.rng.random() < 0.3 else ""
        if self.rng.random() < self.cfg["p_select"]:
            return self.rng.choice([
                f"{d}.execute(select({model}){order}).scalars().all()",
                f"{d}.scalars(select({model}){order}).all()",
            ])
        return f"{d}.query({model}){order}.all()"

    def _helper(self, op: str, model: str) -> str:
        """A repository helper used per element (shared across endpoints half of the time)."""
        key = (op, model)
        if key in self._helpers and (self._force_reuse or self.rng.random() < 0.5):
            return self._helpers[key]
        name = self._name()
        if op == "db_helper":
            child, fk, _ = self._child(model)
            fn = Fn(name, self.repo, f"{self._params()}, key: int", [
                f"return len({self.db}.query({child}).filter({child}.{fk} == key).all())"])
        elif op == "db_hidden":
            child, fk, _ = self._child(model)
            fn = Fn(name, self.repo, "store, key", [f"return store.query({child}).filter({child}.{fk} == key).count()"])
        elif op == "db_lazy_helper":
            _, _, rel = self._child(model)
            fn = Fn(name, self.repo, "obj", [f"return len(obj.{rel})"])
        else:  # cmp_helper
            fn = Fn(name, self.repo, "texts, value", [
                "ordered = sorted(texts, key=lambda t: (t.lower(), len(t)))", "return ordered.index(value)"])
        self.functions.append(fn)
        self._helpers[key] = name
        return name

    def _cost_expr(self, op: str, model: str, item: str, d: str, texts: str) -> str:
        child, fk, rel = self._child(model)
        if op == "db_query":
            if self.rng.random() < self.cfg["p_select"]:
                return f"{d}.scalar(select(func.count({child}.id)).where({child}.{fk} == {item}.id))"
            return f"{d}.query({child}).filter({child}.{fk} == {item}.id).count()"
        if op in ("db_helper", "db_hidden"):
            return f"{self.repo}.{self._helper(op, model)}({d}, {item}.id)"
        if op == "db_lazy":
            return f"len({item}.{rel})"
        if op == "db_lazy_helper":
            return f"{self.repo}.{self._helper(op, model)}({item})"
        if op == "cmp_sort":
            return f"sorted({texts}, key=lambda t: (t.lower(), len(t))).index({item}.{self._text_attr(model)})"
        if op == "cmp_helper":
            return f"{self.repo}.{self._helper(op, model)}({texts}, {item}.{self._text_attr(model)})"
        raise ValueError(op)

    def _constant(self, values: list[str]) -> str:
        name = f"{self.rng.choice(['SEGMENTS', 'GROUPS', 'TIERS', 'BUCKETS', 'CLASSES', 'BANDS'])}_{self._counter}"
        self._counter += 1
        self.constants[name] = values
        return name

    def _filtered_fetch(self, d: str, constant: str) -> str:
        if self.rng.random() < self.cfg["p_select"]:
            return f"{d}.scalars(select({self.P}).where({self.P}.segment.in_({constant}))).all()"
        return f"{d}.query({self.P}).filter({self.P}.segment.in_({constant})).all()"

    def amplifier(self, op: str, bounded: bool, d: str, segments: list[str] | None = None) -> tuple[list[str], str, str]:
        """Lines that fetch rows and loop with a cost operation; returns (lines, result var, loop form).

        With ``segments`` the loop iterates the growing parent table filtered by a module constant: a
        common-segment constant grows with the data, a rare-segment constant selects at most 3 rows."""
        model = self.P if segments is not None else self._source(bounded, op)
        rows, out, item, texts = self._var("rows"), self._var("out"), self._var("item"), self._var("texts")
        if segments is not None:
            lines = [f"{rows} = {self._filtered_fetch(d, self._constant(segments))}"]
        else:
            lines = [f"{rows} = {self._fetch_all(d, model)}"]
        if op.startswith("cmp"):
            lines.append(f"{texts} = [r.{self._text_attr(model)} for r in {rows}]")
        expr = self._cost_expr(op, model, item, d, texts)
        roll = self.rng.random()
        if roll < self.cfg["p_while"]:
            index = self._var("i")
            lines += [f"{out} = []", f"{index} = 0", f"while {index} < len({rows}):",
                      f"    {item} = {rows}[{index}]", f"    {out}.append({expr})", f"    {index} += 1"]
            form = "while"
        elif roll < self.cfg["p_while"] + self.cfg["p_comprehension"]:
            lines.append(f"{out} = [{expr} for {item} in {rows}]")
            form = "comprehension"
        else:
            lines += [f"{out} = []", f"for {item} in {rows}:", f"    {out}.append({expr})"]
            form = "for"
        return lines, out, form

    def benign(self, d: str, module: str | None = None) -> list[str]:
        """0-3 harmless fragments, identical in distribution for every role."""
        lines: list[str] = []
        prefix = "" if module in (None, self.services) else f"{self.services}."
        for _ in range(self.rng.choice([0, 1, 1, 2, 2, 3])):
            model = self.rng.choice([self.P, self.C, self.I, self.K])
            v = self._var("v")
            lines.append(self.rng.choice([
                f"{v} = [{prefix}{self.fmt_name}(x) for x in {prefix}{self.labels_const}]",
                f"{v} = {d}.query(func.count({model}.id)).scalar()",
                f"{v} = [r.id for r in {d}.query({model}).limit(20).all()]",
                f"{v} = {{\"generated\": True, \"version\": {self.rng.randint(1, 9)}}}",
                f"{v} = json.dumps({{\"kind\": \"{self.rng.choice(NAME_NOUNS)}\"}})",
                f"{v} = sorted([\"b\", \"a\", \"c\"])",
                f"{v} = [r.{self._text_attr(self.K)} for r in {d}.query({self.K}).all()]",
                f"{v} = [r.{self._text_attr(self.I)} for r in {d}.query({self.I}).all()]",
            ]))
        return lines

    # --- functions ----------------------------------------------------------------------------

    def _work_fn(self, op: str | None, bounded: bool, module: str, segments: list[str] | None = None) -> tuple[Fn, str]:
        fn = Fn(self._name(), module, self._params())
        fn.lines += self.benign(self.db, module)
        form = ""
        if op is not None:
            lines, out, form = self.amplifier(op, bounded, self.db, segments)
            fn.lines += lines + self.benign(self.db, module) + [f"return {out}"]
        else:
            fn.lines.append(f"return {{\"ok\": True, \"n\": {self.rng.randint(1, 5)}}}")
        return fn, form

    def _make_class_method(self, fn: Fn) -> tuple[Fn, str]:
        """Turn fn into a method of a new service class; returns (method, constructor class name)."""
        cls = "".join(part.capitalize() for part in self._name().split("_")[:2]) + f"Service{self._counter}"
        attr = self.rng.choice(["db", "session", "conn"])
        standalone = re.compile(rf"(?<![\w.]){re.escape(self.db)}\b")
        fn.lines = [standalone.sub(f"self.{attr}", line) for line in fn.lines]
        fn.owner_class, fn.params = cls, "self"
        self.classes[cls] = [Fn("__init__", fn.module, f"self, {self.db}", [f"self.{attr} = {self.db}"], owner_class=cls), fn]
        return fn, cls

    def _call(self, caller: Fn, callee: Fn, cls: str | None = None) -> str:
        prefix = "" if caller.module == callee.module else f"{callee.module}."
        if cls:
            return f"{prefix}{cls}({self.db}).{callee.name}()"
        return f"{prefix}{callee.name}({self.db})"

    def _attach(self, chain: list[Fn], fn: Fn, cls: str | None = None) -> None:
        """Call ``fn`` from a random function of the chain (before its return statement)."""
        host = self.rng.choice([f for f in chain if f.owner_class is None])
        v = self._var("part")
        host.lines.insert(max(0, len(host.lines) - 1), f"{v} = {self._call(host, fn, cls)}")

    def endpoint(self, kind: str | None) -> Endpoint:
        router = self.rng.choice(self.cfg["routers"])
        handler = Fn(self._name(), f"routers.{router}", f"{self.db}: Session = Depends(get_db)", is_async=self.rng.random() < 0.2)
        handler.decorator = f'@router.get("/{handler.name.replace("_", "-")}")'
        ep = Endpoint(path=handler.name.replace("_", "-"), handler=handler, kind=kind or "clean")
        handler.lines += self.benign(self.db, handler.module)
        chain = [handler]

        if kind is not None:
            shared = kind in SHARED_KINDS and self.rng.random() < SHARED_MODE_PROBABILITY
            ep.mode = "shared_helper" if shared else "v4"
            ep.depth = self.rng.choice([1, 1, 2, 2, 3]) if shared else self.rng.choice([0, 1, 1, 2, 2, 3])
            root_segments = self.rng.sample(self.common_segments, self.rng.randint(1, 3)) if shared else None
            for level in range(1, ep.depth + 1):
                is_root = level == ep.depth
                fn, form = self._work_fn(kind if is_root else None, False, self.services, root_segments if is_root else None)
                if not is_root:
                    fn.lines[-1:] = [f"inner = INNER_CALL", "return {\"data\": inner}"]
                cls = None
                if is_root and ep.depth >= 1 and not shared and self.rng.random() < 0.15:
                    fn, cls = self._make_class_method(fn)
                    ep.class_based = True
                else:
                    self.functions.append(fn)
                parent = chain[-1]
                call = self._call(parent, fn, cls)
                if parent is handler:
                    parent.lines.append(f"result = {call}")
                else:
                    parent.lines = [line.replace("INNER_CALL", call) for line in parent.lines]
                chain.append(fn)
                if is_root:
                    ep.root, ep.loop_form = fn, form
            if ep.depth == 0:
                lines, out, ep.loop_form = self.amplifier(kind, False, self.db)
                handler.lines += lines + [f"result = {out}"]
                ep.root = handler
            if shared:
                # The twin: same code shape, same parent, same per-element helper -- but its segment
                # constant selects at most 3 rows. Only the call counts on the edges tell them apart.
                self._force_reuse = True
                twin, _ = self._work_fn(kind, False, self.services, self.rng.sample(self.rare_segments, self.rng.randint(1, 2)))
                self._force_reuse = False
                self.functions.append(twin)
                parent = chain[-2]
                call = f"{self._var('part')} = {self._call(parent, twin)}"
                if parent is handler:
                    parent.lines.append(call)
                else:
                    parent.lines.insert(len(parent.lines) - 1, call)
                ep.twin = twin
                ep.decoys.append(twin)
                ep.shared_helper = f"app.{self.repo}:{self._helpers[(kind, self.P)]}"
        handler.lines += self.benign(self.db, handler.module)
        handler.lines.append("return result" if any(l.startswith("result = ") for l in handler.lines) else "return {\"ok\": True}")

        for _ in range(self.rng.choice([0, 1, 1, 2]) if kind is not None else self.rng.choice([1, 2])):
            op = kind if (kind is not None and self.rng.random() < 0.6) else self.rng.choice(DEFECT_KINDS)
            decoy, _ = self._work_fn(op, True, self.services)
            self.functions.append(decoy)
            self._attach(chain, decoy)
            ep.decoys.append(decoy)
        for _ in range(self.rng.choice([0, 1, 2])):
            helper, _ = self._work_fn(None, False, self.services)
            self.functions.append(helper)
            self._attach(chain, helper)
        self.functions.append(handler)
        return ep

    # --- assembly -------------------------------------------------------------------------------

    def generate(self, out_dir: str | Path, n_defects: int = 4, n_clean: int = 2, kinds: list[str] | None = None) -> dict:
        out = Path(out_dir)
        pool = kinds or DEFECT_KINDS
        endpoints = [self.endpoint(self.rng.choice(pool)) for _ in range(n_defects)] + [self.endpoint(None) for _ in range(n_clean)]

        files = {
            "app/__init__.py": "",
            "app/routers/__init__.py": "",
            "app/db.py": _DB_TEMPLATE,
            "app/models.py": self._models(),
            "app/main.py": self._main(),
        }
        by_module: dict[str, list[str]] = {}
        order = self.functions[:]
        self.rng.shuffle(order)
        for fn in order:
            by_module.setdefault(fn.module, []).append(fn.render())
        for cls, methods in self.classes.items():
            module = methods[0].module
            by_module.setdefault(module, []).append(f"class {cls}:\n" + "\n".join(m.render(indent=4) for m in methods))
        files[f"app/{self.repo}.py"] = self._header(self.repo) + "\n\n".join(by_module.get(self.repo, []))
        files[f"app/{self.services}.py"] = self._header(self.services) + "\n\n".join(by_module.get(self.services, []))
        for router in self.cfg["routers"]:
            files[f"app/routers/{router}.py"] = self._router_header() + "\n\n".join(by_module.get(f"routers.{router}", []))

        out.mkdir(parents=True, exist_ok=True)
        for rel, text in files.items():
            path = out / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

        workload = {
            "app": "app.main:app",
            "sizes": [10, 40, 160],
            "setup": [{"method": "POST", "path": "/seed", "params": {"size": "{size}"}}],
            "requests": [{"name": e.handler.name, "method": "GET", "path": f"/{e.path}"} for e in endpoints],
            "repetitions": 2,
            "warmup": 1,
            "trace_calls": True,
            "total_timeout_s": 600,
        }
        labels = {
            "rootcause_version": "0.2",
            "generator_version": GENERATOR_VERSION,
            "generator_style": self.style,
            "fixed_table": self.K,
            "symptoms": [
                {
                    "endpoint": f"GET /{e.path}",
                    "kind": SYMPTOM_OF[e.kind],
                    "defect": e.kind,
                    "depth": e.depth,
                    "loop_form": e.loop_form,
                    "class_based": e.class_based,
                    "root_cause_functions": [e.root.func_id],
                    "decoy_functions": [d.func_id for d in e.decoys],
                    "mode": e.mode,
                    "graph_required": e.mode == "shared_helper",
                    "shared_helper": e.shared_helper,
                    "twin_decoy": e.twin.func_id if e.twin else None,
                }
                for e in endpoints if e.root is not None
            ],
            "non_defect_endpoints": [{"endpoint": f"GET /{e.path}", "decoy_functions": [d.func_id for d in e.decoys]} for e in endpoints if e.root is None],
        }
        (out / "scaletrace_workload.json").write_text(json.dumps(workload, indent=2), encoding="utf-8")
        (out / ROOTCAUSE_FILE).write_text(json.dumps(labels, indent=2), encoding="utf-8")
        return labels

    # --- templates ------------------------------------------------------------------------------

    def _models(self) -> str:
        return f'''from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class {self.K}(Base):
    __tablename__ = "{self.k}_table"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(80))
    {self.i_rel}: Mapped[list["{self.I}"]] = relationship(back_populates="{self.k}")


class {self.P}(Base):
    __tablename__ = "{self.p}_table"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    segment: Mapped[str] = mapped_column(String(40), index=True)
    {self.c_rel}: Mapped[list["{self.C}"]] = relationship(back_populates="{self.p}")


class {self.C}(Base):
    __tablename__ = "{self.c}_table"

    id: Mapped[int] = mapped_column(primary_key=True)
    {self.p}_id: Mapped[int] = mapped_column(ForeignKey("{self.p}_table.id"), index=True)
    value: Mapped[float]
    {self.p}: Mapped[{self.P}] = relationship(back_populates="{self.c_rel}")


class {self.I}(Base):
    __tablename__ = "{self.i}_table"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(120))
    {self.k}_id: Mapped[int] = mapped_column(ForeignKey("{self.k}_table.id"), index=True)
    {self.k}: Mapped[{self.K}] = relationship(back_populates="{self.i_rel}")
'''

    def _header(self, module: str) -> str:
        repo_import = f"from . import {self.repo}\n" if module == self.services else ""
        constants = "\n".join(f"{name} = {tuple(values)!r}" for name, values in self.constants.items()) if module == self.services else ""
        return f'''import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

{repo_import}from .models import {self.C}, {self.I}, {self.K}, {self.P}

{constants}

'''

    def _router_header(self) -> str:
        return f'''import json

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import {self.services}, {self.repo}
from ..db import get_db
from ..models import {self.C}, {self.I}, {self.K}, {self.P}

router = APIRouter()


'''

    def _main(self) -> str:
        routers = self.cfg["routers"]
        return f'''from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from sqlalchemy.orm import Session

from .db import Base, engine, get_db
from .models import {self.C}, {self.I}, {self.K}, {self.P}
from .routers import {", ".join(routers)}


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(title="Generated ScaleTrace training app", lifespan=lifespan)
{chr(10).join(f"app.include_router({r}.router)" for r in routers)}


@app.post("/seed")
def seed(size: int = 10, db: Session = Depends(get_db)) -> dict:
    for model in ({self.C}, {self.I}, {self.P}, {self.K}):
        db.query(model).delete()
    fixed = [{self.K}(id=k + 1, label=f"label-{{k}}") for k in range({FIXED_ROWS})]
    rare, common = {tuple(self.rare_segments)!r}, {tuple(self.common_segments)!r}
    parents = [
        {self.P}(id=i + 1, name=f"name-{{i * 7919 % 1000:04d}}", segment=rare[i % len(rare)] if i < 3 else common[(i - 3) % len(common)])
        for i in range(size)
    ]
    db.add_all(fixed + parents)
    db.add_all([{self.C}({self.p}_id=x.id, value=float(j + 1)) for x in parents for j in range(3)])
    db.add_all([{self.I}(id=i + 1, title=f"title {{i * 104729 % 1000:04d}}", {self.k}_id=(i % {FIXED_ROWS}) + 1) for i in range(size)])
    db.commit()
    return {{"size": size}}
'''


_DB_TEMPLATE = '''from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

engine = create_engine("sqlite:///./generated_app.db", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
'''
