"""Importing the team's screening spreadsheets into a candidate registry.

The registry is a *pre-benchmark* layer: one record per candidate repository with its screening
decision, licence status, pinned commit (once known), fetch status, and the team's **suspected
findings as unverified claims**. Nothing here is ground truth. A candidate becomes a benchmark
application only when it has been fetched at a pinned commit and a person has written and verified its
manifest (``scaletrace_benchmark.json``).

Parsing rules (deliberately conservative):

* A commit is recorded only if the spreadsheet contains something that looks like a SHA.
* A rule id becomes a claim only when the "Rule_IDs_Checked" text names it *without* saying it was
  merely considered or not confirmed.
* File and line locations are taken only from GitHub ``blob`` links in "Evidence_URLs". Those links
  point at a branch, not a commit, so the lines may have drifted; this is recorded per claim.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

REGISTRY_FILE = "benchmark_registry.json"
SHA_PATTERN = re.compile(r"\b[0-9a-f]{7,40}\b")
RULE_PATTERN = re.compile(r"\b(?:DB|CMP|MEM|EXT|ASY|REL|CON|RES)-\d{3}\b")
NEGATIVE_RULE_TEXT = re.compile(r"consider|not confirmed|^no\b|^none", re.IGNORECASE)
BLOB_URL = re.compile(
    r"https://github\.com/(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)/blob/(?P<ref>[^/\s]+)/(?P<path>[^#\s;]+)"
    r"(?:#L(?P<start>\d+)(?:-L(?P<end>\d+))?)?"
)

Decision = Literal["accepted", "retain_candidate", "modify_for_benchmark", "needs_review", "hold", "reject"]


class SuspectedFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_ids: list[str] = Field(default_factory=list)
    file: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    ref: str | None = Field(default=None, description="Branch/ref of the evidence link (not a pinned commit).")
    url: str | None = None
    evidence_status: str = ""
    summary: str = ""
    state: Literal["unverified_claim"] = "unverified_claim"


class FetchInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["not_fetched", "pinned", "fetched", "unavailable", "failed", "skipped"] = "not_fetched"
    commit: str | None = None
    pinned_at: str | None = None
    path: str | None = None
    python_files: int | None = None
    error: str | None = None


class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    app_id: str
    intended_kind: Literal["good", "bad"]
    repository: str
    url: str
    decision: Decision
    decision_raw: str
    framework: str
    is_fastapi: bool
    licence: str
    licence_status: str
    licence_verified: bool
    register_commit: str | None = None
    date_checked: str | None = None
    duplicate_cluster: str | None = None
    suspected_findings: list[SuspectedFinding] = Field(default_factory=list)
    notes: dict[str, str] = Field(default_factory=dict)
    fetch: FetchInfo = Field(default_factory=FetchInfo)

    def blockers(self) -> list[str]:
        """Why this candidate cannot (yet) be a verified benchmark application."""
        reasons = []
        if self.decision == "reject":
            reasons.append("rejected in screening")
        if not self.is_fastapi:
            reasons.append("not identified as FastAPI")
        if not self.licence_verified:
            reasons.append("licence not verified")
        if not (self.register_commit or self.fetch.commit):
            reasons.append("no pinned commit")
        if self.fetch.status != "fetched":
            reasons.append(f"source not available locally ({self.fetch.status})")
        if self.intended_kind == "bad":
            reasons.append("no verified defect labels (manifest not written)")
        else:
            reasons.append("not accepted as a reference app (manifest not reviewed)")
        return reasons


class Registry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    imported_at: str
    dataset_source: dict[str, str] = Field(default_factory=dict)
    candidates: list[Candidate] = Field(default_factory=list)

    def get(self, candidate_id: str) -> Candidate | None:
        return next((c for c in self.candidates if c.candidate_id == candidate_id), None)


def read_csv(path: str | Path) -> list[dict[str, str]]:
    """Read a spreadsheet export; tolerate Windows-1252 encoding (the team's files use it)."""
    raw = Path(path).read_bytes()
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    return [{(k or "").strip(): (v or "").strip() for k, v in row.items()} for row in csv.DictReader(io.StringIO(text))]


def _commit(*texts: str) -> str | None:
    for text in texts:
        match = SHA_PATTERN.search(text or "")
        if match and not text.lower().startswith("not captured"):
            return match.group(0)
    return None


def _claimed_rules(text: str) -> list[str]:
    if not text or NEGATIVE_RULE_TEXT.search(text):
        return []
    return sorted(set(RULE_PATTERN.findall(text)))


def _findings(row: dict[str, str]) -> list[SuspectedFinding]:
    rules = _claimed_rules(row.get("Rule_IDs_Checked", ""))
    status = row.get("Evidence_Status", "")
    summary = row.get("Findings_and_Evidence", "")
    links = list(BLOB_URL.finditer(row.get("Evidence_URLs", "")))
    if not rules and not links:
        return []
    if not links:
        return [SuspectedFinding(rule_ids=rules, evidence_status=status, summary=summary)]
    findings = []
    for link in links:
        start = int(link["start"]) if link["start"] else None
        findings.append(SuspectedFinding(
            rule_ids=rules, file=link["path"], line_start=start,
            line_end=int(link["end"]) if link["end"] else start, ref=link["ref"], url=link.group(0),
            evidence_status=status, summary=summary,
        ))
    return findings


def _normalise_decision(kind: str, row: dict[str, str]) -> tuple[Decision, str]:
    if kind == "bad":
        raw = row.get("Decision", "")
        value = raw.strip().lower()
        mapping = {"reject": "reject", "retain_candidate": "retain_candidate",
                   "modify_for_benchmark": "modify_for_benchmark", "needs_review": "needs_review", "accept": "accepted"}
        return mapping.get(value, "needs_review"), raw
    raw = row.get("Second_Pass_Decision") or row.get("Decision", "")
    upper = raw.upper()
    if upper.startswith("HOLD"):
        return "hold", raw
    if upper.startswith("ACCEPT"):
        return "accepted", raw
    if upper.startswith("REJECT"):
        return "reject", raw
    return "needs_review", raw


def _licence_verified(row: dict[str, str]) -> bool:
    licence = row.get("Licence", "").lower()
    evidence = (row.get("Second_Pass_License_Evidence", "") + " " + row.get("Licence_Status", "")).lower()
    if not licence or "not" in licence or "reported" in licence:
        return False
    return "license file visible" in evidence or "licence file visible" in evidence


KEPT_NOTES = [
    "Shortlist_Tier", "Decision_Rationale", "Known_Limitations_or_Suspected_Violations",
    "Second_Pass_Risk_And_Suitability", "Next_Screening_Action", "Second_Pass_Next_Action",
    "Source_Inspection_Depth", "Rule_Category_Suitability", "Main_Application_Entry_Point",
]


def candidate_from_row(row: dict[str, str], kind: str) -> Candidate:
    decision, raw = _normalise_decision(kind, row)
    framework = row.get("Framework", "")
    candidate_id = row["Candidate_ID"]
    app_id = candidate_id.lower() if candidate_id.lower().startswith(kind) else f"{kind}-{candidate_id}".lower()
    return Candidate(
        candidate_id=candidate_id,
        app_id=app_id,
        intended_kind=kind,
        repository=row.get("Repository", ""),
        url=row.get("GitHub_URL", ""),
        decision=decision,
        decision_raw=raw,
        framework=framework,
        is_fastapi="fastapi" in framework.lower() and "not" not in framework.lower(),
        licence=row.get("Licence", ""),
        licence_status=row.get("Second_Pass_License_Evidence") or row.get("Licence_Status", ""),
        licence_verified=_licence_verified(row),
        register_commit=_commit(row.get("Commit_SHA", ""), row.get("Second_Pass_Commit_SHA", "")),
        date_checked=row.get("Date_Checked") or None,
        duplicate_cluster=row.get("Second_Pass_Duplicate_Cluster") or None,
        # For good candidates "Rule_IDs_Checked" lists rules that were screened for, not suspected defects.
        suspected_findings=_findings(row) if kind == "bad" else [],
        notes={k: row[k] for k in KEPT_NOTES if row.get(k)},
    )


def import_registers(good_csv: str | Path | None, bad_csv: str | Path | None, source: dict[str, str] | None = None,
                     previous: Registry | None = None) -> Registry:
    """Build a registry from the spreadsheets. Fetch/pin information from ``previous`` is preserved."""
    candidates = []
    for path, kind in ((good_csv, "good"), (bad_csv, "bad")):
        if path:
            candidates += [candidate_from_row(row, kind) for row in read_csv(path) if row.get("Candidate_ID")]
    ids = [c.candidate_id for c in candidates]
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        raise ValueError(f"duplicate Candidate_ID values: {sorted(duplicates)}")
    if previous is not None:
        for candidate in candidates:
            old = previous.get(candidate.candidate_id)
            if old is not None:
                candidate.fetch = old.fetch
    return Registry(imported_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), dataset_source=source or {}, candidates=candidates)


def load_registry(path: str | Path) -> Registry | None:
    path = Path(path)
    return Registry.model_validate_json(path.read_text(encoding="utf-8")) if path.exists() else None


def save_registry(path: str | Path, registry: Registry) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(registry.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path


def readiness_report(registry: Registry) -> str:
    lines = [
        "# Benchmark candidate readiness", "",
        f"Imported {registry.imported_at} from {registry.dataset_source.get('url', 'local files')} "
        f"(commit {registry.dataset_source.get('commit', 'unknown')}).", "",
        "Suspected findings are the screening team's **unverified claims**, not labels.", "",
        "| Candidate | Kind | Repository | Decision | FastAPI | Licence verified | Commit | Fetch | Claims | Blockers |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for c in registry.candidates:
        commit = (c.fetch.commit or c.register_commit or "-")[:10]
        claims = "; ".join(f"{','.join(f.rule_ids) or '?'} {f.file or ''}{':' + str(f.line_start) if f.line_start else ''}" for f in c.suspected_findings) or "-"
        lines.append(
            f"| {c.candidate_id} | {c.intended_kind} | {c.repository} | {c.decision} | {'yes' if c.is_fastapi else 'no'} | "
            f"{'yes' if c.licence_verified else 'no'} | {commit} | {c.fetch.status} | {claims} | {'; '.join(c.blockers())} |"
        )
    counts: dict[tuple[str, str], int] = {}
    for c in registry.candidates:
        counts[(c.intended_kind, c.decision)] = counts.get((c.intended_kind, c.decision), 0) + 1
    lines += ["", "## Counts", ""] + [f"- {kind} / {decision}: {n}" for (kind, decision), n in sorted(counts.items())]
    usable = [c for c in registry.candidates if c.intended_kind == "bad" and c.decision in ("retain_candidate", "accepted")]
    rules = sorted({r for c in usable for f in c.suspected_findings for r in f.rule_ids})
    lines += ["", f"Bad candidates retained: {len(usable)}; rules they are suspected of: {', '.join(rules) or 'none'}."]
    return "\n".join(lines) + "\n"
