"""Human review worksheets and applying their decisions to manifests.

``review-sheet`` writes, per application, a readable worksheet (``review_<app>.md``) and a decisions
file (``review_<app>.json``). Every item -- a screening claim and/or a ScaleTrace rule hit -- starts as
``undecided``. A person edits the decisions file; ``apply-review`` then copies ONLY explicit decisions
into the manifest:

* ``defect``        -> a manifest defect. ``static: verified`` only if ``static_verified_by`` names the
                      person who confirmed it in the code; otherwise ``static: unverified`` (recorded, never
                      counted). ``runtime`` is ``unverified`` (public code is not executed) or
                      ``not_applicable`` (only for rules without a runtime metric, with a note).
* ``not_a_defect``  -> a ``known_non_defects`` entry with the reason (still a rule false positive).
* ``unsure`` / ``undecided`` -> nothing is written; the item is listed as open.

App level: ``exclude`` needs a reason; a licence other than ``unknown`` needs evidence. Nothing is
inferred or defaulted to "verified". Re-applying a review replaces what an earlier application of a
review wrote and leaves everything else in the manifest untouched.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from scaletrace.benchmark.fetch import DRAFT_MARKER
from scaletrace.benchmark.loader import ManifestError, labels_sha256, load_index, load_manifest, verification_state
from scaletrace.benchmark.register import Candidate
from scaletrace.benchmark.schema import MANIFEST_FILE, REVIEW_COPY, SIGNOFF_LOG, Defect, KnownNonDefect, Verification
from scaletrace.diagnosis.correlate import RULE_METRICS
from scaletrace.pipeline import run_static_analysis
from scaletrace.rules.rulebook import Rulebook

REVIEW_REASON_PREFIX = "[review] "
EXCERPT_CONTEXT = 4


class ReviewError(ValueError):
    pass


class AppDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["undecided", "keep", "exclude"] = "undecided"
    exclusion_reason: str = ""
    fastapi_confirmed: Literal["undecided", "yes", "no"] = "undecided"
    licence: str = Field(default="unknown", description="'unknown' or the licence name you saw in the LICENSE file.")
    licence_evidence: str = Field(default="", description="Where you saw it, e.g. 'LICENSE file at pinned commit'.")
    notes: str = ""


class ReviewItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str
    sources: list[Literal["screening_claim", "scaletrace_hit", "reviewer"]] = Field(default_factory=list)
    rule_id: str
    file: str
    line_start: int
    line_end: int
    function: str | None = None
    func_id: str | None = None
    endpoint_candidates: list[str] = Field(default_factory=list, description="Information only.")
    decision: Literal["undecided", "defect", "not_a_defect", "unsure"] = "undecided"
    reason: str = ""
    endpoint: str = ""
    symptom_kind: Literal["", "query_growth", "latency_growth", "memory_growth"] = ""
    root_cause_function: str = Field(default="", description="module:qualname where the fix belongs.")
    static_verified_by: str = ""
    static_rejected_by: str = Field(default="", description="A second person who checked the code and disagrees: static 'rejected'.")
    runtime: Literal["unverified", "not_applicable"] = "unverified"
    runtime_note: str = ""


class ReviewFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_version: str = "1.0"
    app_id: str
    repository: str
    commit: str | None
    kind: Literal["good", "bad"]
    generated_at: str
    reviewer: str = ""
    second_reviewer: str = ""
    review_date: str = ""
    app: AppDecision = Field(default_factory=AppDecision)
    items: list[ReviewItem] = Field(default_factory=list)


def _excerpt(app_dir: Path, file: str, start: int, end: int) -> str:
    try:
        lines = (app_dir / file).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "(file not readable)"
    lo, hi = max(1, start - EXCERPT_CONTEXT), min(len(lines), end + EXCERPT_CONTEXT)
    width = len(str(hi))
    body = "\n".join(f"{'>' if start <= n <= end else ' '} {n:>{width}} | {lines[n - 1]}" for n in range(lo, hi + 1))
    fence = "~~~" if "```" in body else "```"
    return f"{fence}text\n{body}\n{fence}"


def build_review(candidate: Candidate, app_dir: Path) -> tuple[str, ReviewFile]:
    manifest = load_manifest(app_dir)
    static = run_static_analysis(app_dir)
    project = static.project

    def function_at(file: str, line: int):
        best = None
        for info in project.functions.values():
            if info.file == file and info.qualname != "<module>" and info.end_line and info.line <= line <= info.end_line:
                if best is None or info.end_line - info.line < best.end_line - best.line:
                    best = info
        return best

    items: list[ReviewItem] = []
    notes: dict[str, list[str]] = {}

    def first_function_in(file: str, start: int, end: int):
        """Claims come from branch links, so ranges are approximate: use the first line inside a function."""
        for line in range(start, end + 1):
            info = function_at(file, line)
            if info is not None:
                return info
        return None

    def add(source: str, rule_id: str, file: str, start: int, end: int, note: str) -> None:
        for item in items:  # merge a claim and a hit on the same rule and overlapping lines
            if item.rule_id == rule_id and item.file == file and not (end < item.line_start or start > item.line_end):
                if source not in item.sources:
                    item.sources.append(source)
                if source == "scaletrace_hit":
                    # The hit's location is exact; the claim's range may span neighbouring functions.
                    notes[item.item_id].append(f"(Claimed range was lines {item.line_start}-{item.line_end}; using the exact hit location.)")
                    info = function_at(file, start)
                    item.line_start, item.line_end = start, end
                    item.function = info.qualname if info else None
                    item.func_id = info.func_id if info else None
                    item.endpoint_candidates = project.endpoints_reaching(info.func_id) if info else []
                notes[item.item_id].append(note)
                return
        info = function_at(file, start) if source == "scaletrace_hit" else first_function_in(file, start, end)
        item = ReviewItem(
            item_id=f"r{len(items) + 1:02d}", sources=[source], rule_id=rule_id, file=file, line_start=start, line_end=end,
            function=info.qualname if info else None, func_id=info.func_id if info else None,
            endpoint_candidates=project.endpoints_reaching(info.func_id) if info else [],
        )
        items.append(item)
        notes[item.item_id] = [note]

    for claim in candidate.suspected_findings:
        if claim.file and claim.line_start:
            for rule in claim.rule_ids or ["?"]:
                add("screening_claim", rule, claim.file, claim.line_start, claim.line_end or claim.line_start,
                    f"Screening claim ({claim.evidence_status or 'no status'}): {claim.summary[:400]}")
    for finding in static.report.findings:
        evidence = finding.static_evidence[0].description if finding.static_evidence else finding.message
        add("scaletrace_hit", finding.rule_id, finding.file, finding.line, finding.end_line or finding.line,
            f"ScaleTrace {finding.rule_id} ({finding.rule_name}): {evidence}")
    unlocated = [c for c in candidate.suspected_findings if not (c.file and c.line_start)]

    review = ReviewFile(
        app_id=manifest.app_id, repository=candidate.repository, commit=manifest.origin.commit, kind=manifest.kind,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), items=items,
    )
    routes = sorted(project.endpoints())
    lines = [
        f"# Review: {manifest.app_id} ({candidate.repository})", "",
        f"- Kind: **{manifest.kind}**; screening decision: {candidate.decision}; framework (register): {candidate.framework}",
        f"- Pinned commit: `{manifest.origin.commit}` (the commit the screening team saw is unknown)",
        f"- Licence (register): {candidate.licence or 'nothing'}; verified in screening: {'yes' if candidate.licence_verified else 'no'}",
        f"- Source: `benchmark_data/apps/{manifest.app_id}/` (read it; never run it)",
        f"- Routes found by ScaleTrace ({len(routes)}): " + (", ".join(f"`{r}`" for r in routes[:15]) + (" ..." if len(routes) > 15 else "") if routes else "**none**"),
        "", "## What to do", "",
        f"Edit `review_{manifest.app_id}.json` only. For the app: `decision` keep/exclude (+ reason), `fastapi_confirmed`, "
        "`licence` (+ `licence_evidence`, or leave `unknown`). For each item below: `defect` / `not_a_defect` / `unsure`, "
        "a `reason`, and for a defect who confirmed it in the code (`static_verified_by`). Never guess; `unsure` is fine.", "",
    ]
    if manifest.kind == "bad":
        lines.append("Bad app: an item is a `defect` only if you can see in the code that the cost can grow with data or "
                     "requests (e.g. no limit/pagination anywhere on the path). Fill `endpoint`, `symptom_kind` and "
                     "`root_cause_function` if you know them (pick from the candidates shown).")
    else:
        lines.append("Good app: most items should be `not_a_defect` with a reason (bounded by design, admin-only, paginated "
                     "upstream...). A real target defect in a good app is a `defect` with a description.")
    if unlocated:
        lines += ["", "**Screening claims without a location** (find the line or leave them out):", ""]
        lines += [f"- {', '.join(c.rule_ids) or '?'}: {c.summary[:300]}" for c in unlocated]
    if not items:
        lines += ["", "No claims and no ScaleTrace hits. Check the data-access code yourself for target patterns, then decide keep/exclude."]
    for item in items:
        lines += [
            "", f"## {item.item_id}: {item.rule_id} at `{item.file}:{item.line_start}`"
            + (f" in `{item.function}`" if item.function else " (not inside a function)"),
            f"Sources: {', '.join(item.sources)}. Endpoints reaching it: {', '.join(item.endpoint_candidates) or 'none found'}.", "",
        ]
        lines += [f"- {note}" for note in notes[item.item_id]]
        lines += ["", _excerpt(app_dir, item.file, item.line_start, item.line_end)]
    return "\n".join(lines) + "\n", review


def write_review(candidate: Candidate, app_dir: Path, out_dir: Path, force: bool = False) -> tuple[Path, Path] | None:
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path, json_path = out_dir / f"review_{candidate.app_id}.md", out_dir / f"review_{candidate.app_id}.json"
    if json_path.exists() and not force:
        return None  # never overwrite a decisions file someone may be editing
    markdown, review = build_review(candidate, app_dir)
    md_path.write_text(markdown, encoding="utf-8")
    json_path.write_text(review.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return md_path, json_path


def load_review(path: str | Path) -> ReviewFile:
    try:
        return ReviewFile.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        raise ReviewError(f"{path}: invalid review file: {exc}") from exc


def apply_review(review_path: str | Path, apps_dir: Path, rulebook: Rulebook) -> dict:
    """Write a review's explicit decisions into the app's manifest; returns a summary. Raises on bad input."""
    review = load_review(review_path)
    app_dir = apps_dir / review.app_id
    try:
        manifest = load_manifest(app_dir)
    except ManifestError as exc:
        raise ReviewError(str(exc)) from exc
    problems: list[str] = []
    before = verification_state(manifest)
    a = review.app
    if a.decision == "exclude" and not a.exclusion_reason.strip():
        problems.append("app.decision is 'exclude' but exclusion_reason is empty")
    if a.licence.strip().lower() != "unknown" and not a.licence_evidence.strip():
        problems.append("app.licence is set but licence_evidence is empty (leave 'unknown' if not seen)")

    defects, known, open_items = [], [], []
    for item in review.items:
        tag = f"{item.item_id} ({item.rule_id} {item.file}:{item.line_start})"
        if item.decision in ("undecided", "unsure"):
            open_items.append(f"{tag}: {item.decision}")
            continue
        if not item.reason.strip():
            problems.append(f"{tag}: decision '{item.decision}' needs a reason")
            continue
        if item.decision == "not_a_defect":
            known.append(KnownNonDefect(rule_id=item.rule_id, file=item.file, line=item.line_start, function=item.function,
                                        reason=REVIEW_REASON_PREFIX + item.reason.strip()))
            continue
        if item.rule_id not in rulebook.rule_ids:
            problems.append(f"{tag}: unknown rule {item.rule_id}")
            continue
        if item.runtime == "not_applicable":
            if RULE_METRICS.get(item.rule_id) is not None:
                problems.append(f"{tag}: runtime 'not_applicable' is only allowed for rules without a runtime metric "
                                f"({sorted(r for r, m in RULE_METRICS.items() if m is None)}); {item.rule_id} has one")
                continue
            if not item.runtime_note.strip():
                problems.append(f"{tag}: runtime 'not_applicable' needs runtime_note")
                continue
        if not item.function:
            problems.append(f"{tag}: a defect must be inside a function; correct file/line_start/line_end first")
            continue
        verified_by, rejected_by = item.static_verified_by.strip(), item.static_rejected_by.strip()
        if verified_by and rejected_by:
            problems.append(f"{tag}: set static_verified_by or static_rejected_by, not both")
            continue
        reviewer = review.reviewer.strip().split(" (")[0].casefold()
        signer = (verified_by or rejected_by).casefold()
        if signer and signer in {reviewer, "claude"}:  # the AI drafting reviews is never a verifier
            problems.append(f"{tag}: static_verified_by/static_rejected_by must be a second person, not the reviewer ({review.reviewer!r})")
            continue
        defects.append(Defect(
            defect_id=f"{review.app_id}-{item.item_id}", rule_id=item.rule_id, category=rulebook.get(item.rule_id).category,
            file=item.file, function=item.function, line_start=item.line_start, line_end=item.line_end,
            endpoint=item.endpoint or None, symptom_kind=item.symptom_kind or None,
            root_cause_functions=[item.root_cause_function] if item.root_cause_function else [],
            description=item.reason.strip(),
            verification=Verification(
                static="verified" if verified_by else ("rejected" if rejected_by else "unverified"),
                runtime=item.runtime,
                verified_by=verified_by or rejected_by or None,
                date=review.review_date or None,
                notes=item.runtime_note.strip() or ("Runtime not verified: public code is not executed without a sandbox."
                                                     if item.runtime == "unverified" else ""),
            ),
        ))
    if problems:
        raise ReviewError("review not applied:\n  " + "\n  ".join(problems))

    # Defects written by an earlier apply-review (worksheet items rNN, reviewer-added items xNN) are replaced.
    from_review = re.compile(rf"^{re.escape(review.app_id)}-[rx]\d+$")
    manifest.defects = [d for d in manifest.defects if not from_review.match(d.defect_id)] + defects
    manifest.known_non_defects = [k for k in manifest.known_non_defects if not k.reason.startswith(REVIEW_REASON_PREFIX)] + known
    if a.decision == "exclude":
        manifest.split, manifest.exclusion_reason = "excluded", a.exclusion_reason.strip()
    elif manifest.split == "excluded" and a.decision == "keep":
        manifest.split, manifest.exclusion_reason = "unassigned", None
    manifest.origin.license = a.licence.strip() if a.licence.strip().lower() != "unknown" else None
    reviewed = f"Reviewed by {review.reviewer or 'UNNAMED'}" + (f" and {review.second_reviewer}" if review.second_reviewer else "") \
        + f" on {review.review_date or 'UNDATED'} (scaletrace_review.json)."
    description = manifest.description.split(" Reviewed by ")[0].replace(DRAFT_MARKER, "Draft written by ScaleTrace")
    manifest.description = f"{description} {reviewed}"
    if a.notes.strip():
        earlier = re.split(r"(?:^| )Reviewer: ", manifest.run.notes)[0].rstrip()  # re-applying replaces, never appends
        manifest.run.notes = (earlier + " " if earlier else "") + f"Reviewer: {a.notes.strip()}"
    signoffs = _check_lock(apps_dir, review, manifest, before)
    (app_dir / MANIFEST_FILE).write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
    if signoffs:
        with (apps_dir / SIGNOFF_LOG).open("a", encoding="utf-8") as log:
            for entry in signoffs:
                log.write(json.dumps(entry, sort_keys=True) + "\n")
    (app_dir / REVIEW_COPY).write_text(review.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return {
        "app_id": review.app_id,
        "defects": len(defects),
        "source_confirmed": sum(1 for d in defects if d.verification.static == "verified"),
        "known_non_defects": len(known),
        "open_items": open_items,
        "excluded": a.decision == "exclude",
        "licence": manifest.origin.license or "unknown",
        "signoffs_logged": len(signoffs),
    }


def _check_lock(apps_dir: Path, review: ReviewFile, manifest, before: dict[str, dict]) -> list[dict]:
    """On a locked test app, refuse label changes and return the sign-off log entries for verification changes."""
    index = load_index(apps_dir)
    locked = index.lock.apps.get(review.app_id) if index is not None and index.lock is not None else None
    if locked is None:
        return []
    if locked.labels_sha256 is None or labels_sha256(manifest) != locked.labels_sha256:
        raise ReviewError(f"{review.app_id} is in the locked test set: its labels are frozen (rule, location, endpoint, "
                          "decisions, exclusions). Only verification sign-offs may change after the lock.")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    after = verification_state(manifest)
    return [{"logged_at": now, "app_id": review.app_id, "defect_id": defect_id, "before": before.get(defect_id),
             "after": state, "review_file": f"review_{review.app_id}.json", "review_date": review.review_date or None}
            for defect_id, state in after.items() if before.get(defect_id) != state]
