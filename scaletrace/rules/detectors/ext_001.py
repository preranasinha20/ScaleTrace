"""EXT-001: external service call inside a loop.

Direct form: ``requests.get(...)``, ``httpx.post(...)`` or ``client.get(...)`` on a visible HTTP
client (annotated, or constructed from httpx/requests/aiohttp) inside an unbounded loop.
Indirect form: a call in a loop to a project function that reaches such a call.
"""

from __future__ import annotations

from collections.abc import Iterator

from scaletrace.analyzer.project import ProjectIndex, unbounded
from scaletrace.rules.detectors.base import Detector, RawMatch, backticked, loop_details, loop_variables, names_in
from scaletrace.schemas import StaticEvidence

PATTERN_DIRECT = "network client call nested inside loop"
PATTERN_LOOP_VAR = "loop variable used in request arguments"


class ExternalCallInLoopDetector(Detector):
    rule_id = "EXT-001"

    def detect(self, project: ProjectIndex) -> Iterator[RawMatch]:
        for info in project.iter_functions():
            for access in info.facts.external_accesses:
                if not unbounded(access.loops):
                    continue
                call_info, loops = access.info, access.loops
                innermost = loops[-1]
                used_vars = sorted(loop_variables(loops) & names_in(access.node))
                description = (
                    f"`{call_info.receiver}.{call_info.method}(...)` ({call_info.library}, service: "
                    f"{call_info.service}) runs on every iteration of `{innermost.header}` (line {innermost.line})."
                )
                if used_vars:
                    description += f" Loop variable(s) {backticked(used_vars)} are used in the request."
                yield RawMatch(
                    info=info,
                    node=access.node,
                    summary=f"`{call_info.receiver}.{call_info.method}(...)` inside `{innermost.header}`",
                    evidence=[
                        StaticEvidence(
                            pattern=PATTERN_LOOP_VAR if used_vars else PATTERN_DIRECT,
                            description=description,
                            snippet=info.ctx.snippet(access.node),
                            details={
                                "access": "direct",
                                "library": call_info.library,
                                "method": call_info.method,
                                "service": call_info.service,
                                "client_inference": call_info.basis,
                                "loop_variables_in_request": used_vars,
                                **loop_details(loops),
                            },
                        )
                    ],
                )

            for call in info.facts.calls:
                if call.kind != "call" or call.callee is None or not unbounded(call.loops):
                    continue
                path = project.resource_path(call.callee, "external")
                if path is None:
                    continue
                target = project.functions[path[-1]]
                access = target.facts.external_accesses[0]
                innermost = call.loops[-1]
                yield RawMatch(
                    info=info,
                    node=call.node,
                    summary=f"`{call.callee_text}(...)` (reaches an external service) inside `{innermost.header}`",
                    evidence=[
                        StaticEvidence(
                            pattern=PATTERN_DIRECT,
                            description=(
                                f"`{call.callee_text}(...)` is called on every iteration of `{innermost.header}` "
                                f"(line {innermost.line}) and reaches {access.info.service} via "
                                f"{' -> '.join(project.functions[f].qualname for f in path)}."
                            ),
                            snippet=info.ctx.snippet(call.node),
                            details={
                                "access": "indirect",
                                "call_chain": path,
                                "service": access.info.service,
                                "external_call_location": f"{target.file}:{access.node.lineno}",
                                **loop_details(call.loops),
                            },
                        )
                    ],
                )
