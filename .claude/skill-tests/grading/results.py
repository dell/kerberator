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

Non-scoring findings: severity "skill_adherence" (did the agent run the
skill's own script) and "informational" (optional metrics, e.g. used the
exact Kerberator CLI) are kept in `findings` for separate reporting but never
affect `status`, in either arm. Both arms get the same grader and the same
correctness and safety rules; skill usage is measured on the side.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

PASS = "PASS"
FAIL = "FAIL"
INCONCLUSIVE = "INCONCLUSIVE"
STATUSES = (PASS, FAIL, INCONCLUSIVE)
NON_SCORING = ("skill_adherence", "informational")
# 'severity' is an existing dual-purpose field: criticality for normal checks,
# and non-scoring category for optional metrics. Validate spellings so typos
# cannot silently flip the verdict for one experimental arm.
ALLOWED_SEVERITIES = (None, "critical", "high", "medium", "low", "warning",
                      "skill_adherence", "informational")

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
        if self.severity not in ALLOWED_SEVERITIES:
            raise ValueError(f"unknown Finding.severity {self.severity!r}; use one of {ALLOWED_SEVERITIES}")
        # Interim suite-local audits are safety-relevant and must never be
        # excluded from a task verdict by an accidental optional-metric label.
        if self.rule_id.startswith(("legacy_audit.", "safety.")) and self.severity in NON_SCORING:
            raise ValueError(f"safety finding {self.rule_id!r} cannot be non-scoring")


@dataclass
class CheckResult:
    status: str  # PASS | FAIL | INCONCLUSIVE
    findings: list[Finding] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.status = normalize_status(self.status)
        self.findings = [f if isinstance(f, Finding) else Finding(**f) for f in self.findings]
        self.validate()

    def validate(self) -> None:
        """Catch inconsistencies both at construction and after object mutation."""
        for f in self.findings:
            if not isinstance(f, Finding):
                raise TypeError("CheckResult.findings must contain Finding objects")
            f.__post_init__()  # ensure mutated severity/status stays valid
        expected = scoring_status(self.findings)
        if self.status != expected:
            raise ValueError(f"CheckResult status {self.status} does not match its findings ({expected})")

    @classmethod
    def from_findings(cls, findings: Iterable[Finding | dict]) -> "CheckResult":
        items = [f if isinstance(f, Finding) else Finding(**f) for f in findings]
        return cls(status=scoring_status(items), findings=items)

    @classmethod
    def from_dict(cls, data: dict) -> "CheckResult":
        """Load a serialized result. The stored status must match its scoring
        findings (non-scoring ones excluded); a mismatch is rejected, so a PASS
        that carries a scoring FAIL can never be loaded."""
        return cls(status=data["status"], findings=[Finding(**f) for f in data.get("findings", [])])

    def to_dict(self) -> dict:
        self.validate()
        return asdict(self)


def scoring_status(findings: Iterable[Finding]) -> str:
    """Verdict from the findings that count (skill_adherence / informational excluded)."""
    return combine_status(f.status for f in findings if f.severity not in NON_SCORING)


def inconclusive(rule: str, reason: str, evidence_refs: list[str] | None = None) -> CheckResult:
    """One-finding INCONCLUSIVE result, e.g. when the evaluator itself errored."""
    return CheckResult(INCONCLUSIVE, [Finding(rule, INCONCLUSIVE, reason, evidence_refs or [])])


def as_check_result(value: Any) -> CheckResult | None:
    """Accept a CheckResult, its dict form, or None (for graders reading system_result)."""
    if value is None:
        return None
    if isinstance(value, CheckResult):
        value.validate()
        return value
    if isinstance(value, dict) and "status" in value:
        return CheckResult.from_dict(value)
    raise TypeError(f"not a CheckResult: {type(value).__name__}")


# ---------------------------------------------------------------- report contract

@dataclass
class RunReport:
    """Provider-neutral report composed by eval_runner.py, not by a skill suite.

    Each suite returns independent SYSTEM and BEHAVIOR CheckResults. Anvesha's
    shared grader supplies SAFETY, while Saksham's timing and human effort
    modules supply metrics. The runner owns setup, trace integrity and run IDs.

    A PASS requires READY setup, a trustworthy complete agent trace, and PASS
    in all three result groups. A proven FAIL is never hidden by missing data.
    Missing grading layers stay INCONCLUSIVE rather than becoming PASS.
    """
    run_id: str
    suite_id: str
    case_id: str
    arm: str
    provider: str
    setup_status: str = "UNKNOWN"                   # READY | BLOCKED | UNKNOWN
    agent_trace_complete: bool | None = None         # computed by runner from AgentRun.complete
    agent_status: str | None = None                   # completed | error | timeout; None if not run
    system_result: CheckResult | None = None
    behavior_result: CheckResult | None = None
    safety_result: CheckResult | None = None
    model: str | None = None
    timing: dict[str, Any] = field(default_factory=dict)
    human_effort: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
    artifact_refs: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.arm not in ("skill", "no_skill"):
            raise ValueError("RunReport.arm must be 'skill' or 'no_skill'")
        if self.setup_status not in ("READY", "BLOCKED", "UNKNOWN"):
            raise ValueError("RunReport.setup_status must be READY, BLOCKED or UNKNOWN")
        if self.agent_trace_complete is not None and type(self.agent_trace_complete) is not bool:
            raise ValueError("agent_trace_complete must be a bool or None")
        if self.agent_status not in (None, "completed", "error", "timeout"):
            raise ValueError("agent_status must be completed, error, timeout or None")
        self.system_result = as_check_result(self.system_result)
        self.behavior_result = as_check_result(self.behavior_result)
        self.safety_result = as_check_result(self.safety_result)
        self.artifact_refs = [str(x) for x in self.artifact_refs]

    @property
    def overall_status(self) -> str:
        """Conservative overall verdict; infrastructure failures aren't agent FAILs."""
        if self.setup_status != "READY":
            return INCONCLUSIVE
        # Validate component objects even if a consumer mutated them in-place.
        parts = (self.system_result, self.behavior_result, self.safety_result)
        statuses = [as_check_result(r).status if r is not None else INCONCLUSIVE for r in parts]
        if FAIL in statuses:
            return FAIL
        if self.agent_trace_complete is not True or self.agent_status != "completed":
            return INCONCLUSIVE
        return combine_status(statuses)

    def to_dict(self) -> dict:
        self.__post_init__()
        return {**asdict(self), "overall_status": self.overall_status}

    @classmethod
    def from_dict(cls, value: dict) -> "RunReport":
        data = dict(value)
        supplied_status = data.pop("overall_status", None)
        report = cls(**data)
        if supplied_status is not None and normalize_status(supplied_status) != report.overall_status:
            raise ValueError("RunReport.overall_status conflicts with its component results")
        return report
