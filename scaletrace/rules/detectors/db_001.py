"""DB-001: database query inside an iterative loop.

Direct form: a call ``<db handle>.<db operation>(...)`` re-executed on each iteration of an
enclosing loop in the same function. One finding per method chain, so
``db.query(X).filter(...).all()`` is reported once, at the ``db.query`` call.

Indirect form: a call inside a loop to a project function from which a database operation is
reachable through the (approximate) call graph, e.g. ``for u in users: load_orders(db, u)``.

Not reported: loops strictly bounded by a small literal (``range(3)``, ``("a", "b")``), and
queries in the loop *iterable* (``for u in db.query(User)``), which run once.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator

from scaletrace.analyzer.project import ProjectIndex, unbounded
from scaletrace.rules.detectors.base import Detector, RawMatch, backticked, loop_details, loop_variables, names_in
from scaletrace.schemas import StaticEvidence

PATTERN_DIRECT = "database call nested inside for or while loop"
PATTERN_INDIRECT = "database dependency reachable from loop body"


def discarded_coroutine(project: ProjectIndex, info, call) -> bool:
    """True when the call creates a coroutine that is thrown away, so the helper's body never runs.

    Python semantics: calling an ``async def`` function only creates a coroutine object; its body runs when the
    coroutine is awaited or scheduled. A call whose result is discarded (the call *is* the whole expression
    statement) is therefore never executed. Everything else is kept: ``await helper()``, ``create_task(helper())``,
    ``tasks.append(helper())``, ``x = helper()`` and comprehensions passed to ``gather`` may all run the helper.
    Decorated helpers are kept too, because a decorator can change what calling them does.
    (Added post-hoc, from a locked-test-split review note; see docs/error_analysis.md section 8.)
    """
    target = project.functions.get(call.callee)
    node = getattr(getattr(target, "scope", None), "node", None)
    if not isinstance(node, ast.AsyncFunctionDef) or node.decorator_list:
        return False
    parent = info.ctx.parent(call.node)
    return isinstance(parent, ast.Expr) and parent.value is call.node


class DbQueryInLoopDetector(Detector):
    rule_id = "DB-001"

    def detect(self, project: ProjectIndex) -> Iterator[RawMatch]:
        for info in project.iter_functions():
            ctx = info.ctx
            for access in info.facts.db_accesses:
                if not unbounded(access.loops):
                    continue
                operation, loops = access.operation, access.loops
                chain = ctx.outermost_chain(access.node)
                used_vars = sorted(loop_variables(loops) & names_in(chain))
                innermost = loops[-1]
                description = (
                    f"`{operation.receiver}.{operation.method}(...)` runs on every iteration of "
                    f"`{innermost.header}` (line {innermost.line})"
                )
                if used_vars:
                    description += f"; loop variable(s) {backticked(used_vars)} feed the query"
                description += f". `{operation.receiver}` is treated as a database handle ({operation.handle.describe()})."
                yield RawMatch(
                    info=info,
                    node=access.node,
                    summary=f"`{operation.receiver}.{operation.method}(...)` inside `{innermost.header}`",
                    evidence=[
                        StaticEvidence(
                            pattern=PATTERN_DIRECT,
                            description=description,
                            snippet=ctx.snippet(chain),
                            details={
                                "access": "direct",
                                "db_operation": operation.method,
                                "receiver": operation.receiver,
                                "handle_inference": operation.handle.basis,
                                "handle_inference_detail": operation.handle.detail,
                                "loop_variables_in_query": used_vars,
                                **loop_details(loops),
                            },
                        )
                    ],
                )

            for call in info.facts.calls:
                if call.kind != "call" or call.callee is None or not unbounded(call.loops):
                    continue
                if discarded_coroutine(project, info, call):
                    continue
                path = project.resource_path(call.callee, "database")
                if path is None:
                    continue
                target = project.functions[path[-1]]
                access = target.facts.db_accesses[0]
                chain_text = " -> ".join(project.functions[f].qualname for f in path)
                innermost = call.loops[-1]
                used_vars = sorted(loop_variables(call.loops) & names_in(call.node))
                description = (
                    f"`{call.callee_text}(...)` is called on every iteration of `{innermost.header}` "
                    f"(line {innermost.line}) and reaches the database via {chain_text} "
                    f"(`{access.operation.receiver}.{access.operation.method}(...)` at {target.file}:{access.node.lineno})."
                )
                if used_vars:
                    description += f" Loop variable(s) {backticked(used_vars)} are passed to the call."
                yield RawMatch(
                    info=info,
                    node=call.node,
                    summary=f"`{call.callee_text}(...)` (reaches the database) inside `{innermost.header}`",
                    evidence=[
                        StaticEvidence(
                            pattern=PATTERN_INDIRECT,
                            description=description,
                            snippet=info.ctx.snippet(call.node),
                            details={
                                "access": "indirect",
                                "call_chain": path,
                                "db_operation": access.operation.method,
                                "db_access_location": f"{target.file}:{access.node.lineno}",
                                "handle_inference": access.operation.handle.basis,
                                "loop_variables_in_call": used_vars,
                                **loop_details(call.loops),
                            },
                        )
                    ],
                )
