"""Shared result types for suite verify() and grade() functions.

Both suites/<id>/verify.py (SYSTEM result) and suites/<id>/grade.py
(BEHAVIOR result) return a CheckResult. The reporting layer later combines
system + behavior + safety into one RunReport; suites never compute that
overall verdict themselves.

Verdict precedence (same everywhere):
    any FAIL                      -> FAIL
    else any INCONCLUSIVE         -> INCONCLUSIVE
    else at least one PASS        -> PASS
    no findings at all            -> INCONCLUSIVE  (absence of evidence is not a pass)
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

PASS = "PASS"
FAIL = "FAIL"
INCONCLUSIVE = "INCONCLUSIVE"
STATUSES = (PASS, FAIL, INCONCLUSIVE)

# Legacy suite vocabularies -> canonical status. Anything not listed is
# treated as INCONCLUSIVE so an unknown word can never become a PASS.
_LEGACY = {
    "PASS": PASS, "PASSED": PASS, "OK": PASS, "SATISFIED": PASS, "MET": PASS, "TRUE": PASS,
    "FAIL": FAIL, "FAILED": FAIL, "VIOLATION": FAIL, "VIOLATED": FAIL,
    "INCONCLUSIVE": INCONCLUSIVE, "UNKNOWN": INCONCLUSIVE, "UNVERIFIED": INCONCLUSIVE,
    "PENDING": INCONCLUSIVE, "PENDING_REVIEW": INCONCLUSIVE, "REVIEW": INCONCLUSIVE,
    "REVIEW_REQUIRED": INCONCLUSIVE, "NEEDS_REVIEW": INCONCLUSIVE, "MANUAL": INCONCLUSIVE,
    "ERROR": INCONCLUSIVE, "BLOCKED": INCONCLUSIVE, "SKIPPED": INCONCLUSIVE, "SKIP": INCONCLUSIVE,
    "NOT_EVALUATED": INCONCLUSIVE, "N/A": INCONCLUSIVE,
}


def normalize_status(value: Any) -> str:
    """Map a legacy verdict word (or bool) to PASS / FAIL / INCONCLUSIVE."""
    if value is True:
        return PASS
    if value is False:
        return FAIL
    if value is None:
        return INCONCLUSIVE
    return _LEGACY.get(str(value).strip().upper().replace("-", "_").replace(" ", "_"), INCONCLUSIVE)


def rule_id(text: Any) -> str:
    """Stable snake_case rule id from a legacy check name."""
    slug = re.sub(r"[^a-z0-9]+", "_", str(text).strip().lower()).strip("_")
    return slug or "unnamed_rule"


def combine_status(statuses: Iterable[str]) -> str:
    seen = [normalize_status(s) for s in statuses]
    if FAIL in seen:
        return FAIL
    if INCONCLUSIVE in seen or not seen:
        return INCONCLUSIVE
    return PASS


@dataclass
class Finding:
    rule_id: str
    status: str  # PASS | FAIL | INCONCLUSIVE
    reason: str
    evidence_refs: list[str] = field(default_factory=list)
    severity: str | None = None

    def __post_init__(self) -> None:
        self.status = normalize_status(self.status)
        self.rule_id = str(self.rule_id)
        self.reason = str(self.reason)
        self.evidence_refs = [str(x) for x in (self.evidence_refs or [])]


@dataclass
class CheckResult:
    status: str  # PASS | FAIL | INCONCLUSIVE
    findings: list[Finding] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.status = normalize_status(self.status)
        self.findings = [f if isinstance(f, Finding) else Finding(**f) for f in self.findings]

    @classmethod
    def from_findings(cls, findings: Iterable[Finding | dict]) -> "CheckResult":
        items = [f if isinstance(f, Finding) else Finding(**f) for f in findings]
        return cls(status=combine_status(f.status for f in items), findings=items)

    @classmethod
    def from_dict(cls, data: dict) -> "CheckResult":
        return cls(status=data["status"], findings=[Finding(**f) for f in data.get("findings", [])])

    def to_dict(self) -> dict:
        return asdict(self)


def inconclusive(rule: str, reason: str, evidence_refs: list[str] | None = None) -> CheckResult:
    """One-finding INCONCLUSIVE result, e.g. when the evaluator itself errored."""
    return CheckResult(INCONCLUSIVE, [Finding(rule, INCONCLUSIVE, reason, evidence_refs or [])])


def as_check_result(value: Any) -> CheckResult | None:
    """Accept a CheckResult, its dict form, or None (for graders reading system_result)."""
    if value is None or isinstance(value, CheckResult):
        return value
    if isinstance(value, dict) and "status" in value:
        return CheckResult.from_dict(value)
    raise TypeError(f"not a CheckResult: {type(value).__name__}")
