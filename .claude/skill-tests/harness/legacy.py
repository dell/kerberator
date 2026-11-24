"""Converters from each suite's pre-standard result shapes to CheckResult.

Suites keep their original logic and CLI; their new public verify()/grade()
call the old code and pass its output through these helpers.

Safety-audit findings: until grading/common.py (shared safety grader) is in
place, suites keep their own command-safety checks so nothing gets weaker.
Those findings are re-labelled `legacy_audit.<rule>` so the reporting layer
and grading/common.py can recognise and de-duplicate them later.
"""
from __future__ import annotations

from typing import Any, Iterable

from grading.results import CheckResult, Finding, normalize_status, rule_id

LEGACY_AUDIT_PREFIX = "legacy_audit."


def _rid(name: Any, safety_rules: Iterable[str]) -> str:
    rid = rule_id(name)
    return LEGACY_AUDIT_PREFIX + rid if str(name) in set(safety_rules) or rid in set(safety_rules) else rid


def from_checks(checks: Iterable[dict], *, name_key: str, status_key: str, reason_key: str,
                evidence_refs: list[str] | None = None, safety_rules: Iterable[str] = (),
                severity_key: str | None = None) -> CheckResult:
    """Legacy list of {name, verdict, detail}-style dicts -> CheckResult."""
    safety_rules = list(safety_rules)
    findings = []
    seen: dict[str, int] = {}
    for c in checks:
        rid = _rid(c.get(name_key, "unnamed"), safety_rules)
        seen[rid] = seen.get(rid, 0) + 1
        if seen[rid] > 1:                      # keep rule ids unique within one result
            rid = f"{rid}.{seen[rid]}"
        findings.append(Finding(
            rule_id=rid,
            status=normalize_status(c.get(status_key)),
            reason=str(c.get(reason_key) or c.get(name_key) or ""),
            evidence_refs=list(evidence_refs or []),
            severity=(c.get(severity_key) if severity_key else None)
            or ("critical" if rid.startswith(LEGACY_AUDIT_PREFIX) else None),
        ))
    return CheckResult.from_findings(findings)


def event_refs(trace: dict, indexes: Iterable[int]) -> list[str]:
    """Legacy grader event indexes -> stable 'event-<seq>' references."""
    events = trace.get("events", [])
    out = []
    for i in indexes or []:
        if isinstance(i, int) and 0 <= i < len(events):
            out.append(f"event-{events[i].get('seq', i)}")
    return out


def with_overall(result: CheckResult, status: str | None) -> CheckResult:
    """Never let a legacy overall verdict be *more* positive than its findings.

    If the old code produced a worse overall verdict than its findings imply
    (e.g. it had an internal check it didn't list), keep the worse one.
    """
    if status is None:
        return result
    order = {"PASS": 0, "INCONCLUSIVE": 1, "FAIL": 2}
    legacy = normalize_status(status)
    if order[legacy] > order[result.status]:
        result.findings.append(Finding("legacy_overall_verdict", legacy,
                                       f"original grader verdict was {status}"))
        result.status = legacy
    return result
