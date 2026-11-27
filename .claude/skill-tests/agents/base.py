"""AgentRun / AgentEvent: the one execution record every agent adapter returns.

Provider adapters (agents/copilot.py, agents/claude.py, later Codex/Cursor)
map their CLI output into this shape. Suite grade.py files read ONLY this
shape; they must never parse a provider's raw JSONL themselves.

An approval-gated run may span two CLI calls (initial call -> harness decision
-> resumed call on the same session). Both calls are merged into ONE AgentRun;
each event carries the phase it came from.

Event types (ordered by `seq`, which is the trusted occurrence order):

  agent_message      text the agent produced                       {text}
  tool_call          a tool invocation                             {tool, command, executed, call_id}
                     executed=True only when the adapter observed it actually run;
                     a proposed/denied call has executed=False
  tool_result        completion of a tool_call                     {call_id, command, exit_code, stdout, stderr}
  approval_request   adapter classified the agent's stop as asking for approval  {text}
  approval_decision  the harness (not the agent) answered          {decision, text, source="harness"}
                     decision: approve | deny | withhold | reply
  user_message       other trusted user/harness message            {text, source="harness"}

`trace_completeness` says whether absence can be proven:
  complete       every executed tool call and message was captured in order
  partial        some events may be missing (e.g. stream cut, parse errors)
  unavailable    no trustworthy trace
  contradictory  sources disagree (e.g. session id changed between calls)
Only "complete" lets a grader turn "no forbidden command seen" into a PASS.
If a call_id is available, tool-call/result integrity is checked before the
trace can be treated as complete. Older adapters without IDs remain supported.

Model-written text such as "the user approved" is never an approval record;
only approval_decision events with source="harness" are. The adapter/runner
must be the trusted boundary: provider-supplied JSON cannot label itself trusted.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

EVENT_TYPES = ("agent_message", "tool_call", "tool_result", "approval_request",
               "approval_decision", "user_message")
COMPLETENESS = ("complete", "partial", "unavailable", "contradictory")
DECISIONS = ("approve", "deny", "withhold", "reply")
ARMS = ("skill", "no_skill")
RUN_STATUSES = ("completed", "error", "timeout")
TRUSTED_SOURCE = "harness"


@dataclass
class AgentEvent:
    seq: int
    type: str
    timestamp: str | None = None
    source: str = "provider"          # provider | harness
    phase: str = "initial"            # initial | resumed
    event_id: str | None = None
    text: str | None = None
    tool: str | None = None           # normalized: shell, read, write, ...
    command: str | None = None
    executed: bool | None = None
    call_id: str | None = None
    exit_code: int | None = None
    stdout: str | None = None
    stderr: str | None = None
    decision: str | None = None       # approval_decision only

    def __post_init__(self) -> None:
        if self.type not in EVENT_TYPES:
            raise ValueError(f"unknown AgentEvent type {self.type!r}")
        if not isinstance(self.seq, int) or isinstance(self.seq, bool) or self.seq < 0:
            raise ValueError("AgentEvent.seq must be a non-negative int")
        if self.source not in ("provider", TRUSTED_SOURCE):
            raise ValueError("AgentEvent.source must be 'provider' or 'harness'")
        if self.phase not in ("initial", "resumed"):
            raise ValueError("AgentEvent.phase must be 'initial' or 'resumed'")
        if self.type == "approval_decision":
            if self.decision not in DECISIONS:
                raise ValueError(f"approval_decision needs decision in {DECISIONS}")
            if self.source != TRUSTED_SOURCE:
                # Never accept provider-authored approval as authorization evidence.
                raise ValueError("approval_decision must have source='harness'")
            if self.decision == "withhold" and self.text:
                raise ValueError("withhold must not carry a reply; nothing is released")
        elif self.decision is not None:
            raise ValueError("decision is only valid on approval_decision events")
        if self.type == "user_message" and self.source != TRUSTED_SOURCE:
            # A user message in the normalized trace is a trusted runner input,
            # never a message discovered in an agent/provider transcript.
            raise ValueError("user_message must have source='harness'")
        if self.type == "tool_call" and self.executed is not None and type(self.executed) is not bool:
            raise ValueError("tool_call.executed must be True, False or None")


@dataclass
class AgentRun:
    provider: str                      # claude | copilot | codex | cursor | mock
    arm: str = "skill"                 # skill | no_skill
    model: str | None = None
    cli_version: str | None = None
    session_id: str | None = None
    status: str = "completed"          # completed | error | timeout
    started_at: str | None = None
    ended_at: str | None = None
    trace_completeness: str = "unavailable"
    stop_reason: str | None = None     # approval_request | blocked | question | done | error
    events: list[AgentEvent] = field(default_factory=list)
    final_response: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)       # tokens/cost; unknown stays absent
    raw_trace_refs: list[str] = field(default_factory=list)   # private paths, never inlined

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Validate normalized fields, including after an object was modified."""
        if self.trace_completeness not in COMPLETENESS:
            raise ValueError(f"trace_completeness must be one of {COMPLETENESS}")
        if self.arm not in ARMS:
            raise ValueError(f"arm must be one of {ARMS}")
        if self.status not in RUN_STATUSES:
            raise ValueError(f"status must be one of {RUN_STATUSES}")
        self.events = [e if isinstance(e, AgentEvent) else AgentEvent(**e) for e in self.events]
        for event in self.events:
            event.__post_init__()  # reject post-construction edits to trust fields
        seqs = [e.seq for e in self.events]
        if len(set(seqs)) != len(seqs):
            raise ValueError("AgentEvent.seq values must be unique (duplicate events are not trusted)")
        self.events.sort(key=lambda e: e.seq)

    @property
    def trace_issues(self) -> list[str]:
        """Flag uncorrelated tool calls and unknown execution states.

        Legacy suites omit call_id, so a missing ID alone is not an error.
        Once an adapter supplies IDs, a completed call must have its result.
        The runner also must prove session continuity and capture completeness.
        """
        issues: list[str] = []
        calls: dict[str, AgentEvent] = {}
        results: dict[str, list[AgentEvent]] = {}
        for e in self.events:
            if e.type == "tool_call":
                if e.executed is None:
                    issues.append(f"event-{e.seq}: tool execution status unknown")
                if e.call_id:
                    if e.call_id in calls:
                        issues.append(f"event-{e.seq}: duplicate tool call_id {e.call_id!r}")
                    calls[e.call_id] = e
            elif e.type == "tool_result" and e.call_id:
                results.setdefault(e.call_id, []).append(e)
        for call_id, call in calls.items():
            matched = results.get(call_id, [])
            if call.executed is True and len(matched) != 1:
                issues.append(f"event-{call.seq}: expected one result for {call_id!r}, got {len(matched)}")
            if call.executed is False and matched:
                issues.append(f"event-{call.seq}: result for unexecuted call {call_id!r}")
            if matched and matched[0].seq <= call.seq:
                issues.append(f"event-{call.seq}: tool result precedes call {call_id!r}")
        for call_id in results:
            if call_id not in calls:
                issues.append(f"orphan tool result for {call_id!r}")
        phases = [e.phase for e in self.events]
        if "resumed" in phases and "initial" in phases:
            first_resume = phases.index("resumed")
            if "initial" in phases[first_resume + 1:]:
                issues.append("initial-phase event occurs after resumed-phase event")
        return issues

    @property
    def complete(self) -> bool:
        return self.trace_completeness == "complete" and not self.trace_issues

    @classmethod
    def from_dict(cls, data: dict) -> "AgentRun":
        return cls(**data)

    def to_dict(self) -> dict:
        self.validate()
        return asdict(self)


def as_agent_run(value: Any) -> AgentRun | None:
    if value is None:
        return None
    if isinstance(value, AgentRun):
        value.validate()
        return value
    if isinstance(value, dict):
        return AgentRun.from_dict(value)
    raise TypeError(f"not an AgentRun: {type(value).__name__}")


# ---------------------------------------------------------------- read helpers

# Normalized tool names plus common provider spellings. Anything else is "unknown".
SHELL_TOOLS = {"shell", "bash", "run_bash", "exec", "execute", "terminal", "run_shell_command"}
WRITE_TOOLS = {"write", "edit", "create", "write_file", "multiedit", "str_replace", "str_replace_editor",
               "apply_patch", "notebookedit", "write_agent", "run_dynamic_workflow", "task"}
READ_TOOLS = {"read", "view", "grep", "glob", "ls", "search", "skill", "lexical_code_search",
              "semantic_code_search", "todowrite", "todo"}


def tool_kind(event: AgentEvent) -> str:
    """shell | write | read | unknown for a tool_call event."""
    name = (event.tool or "").lower()
    if name in SHELL_TOOLS or (not name and event.command):
        return "shell"
    if name in WRITE_TOOLS:
        return "write"
    if name in READ_TOOLS:
        return "read"
    return "unknown"


def executed_tool_calls(run: AgentRun | dict | None) -> list[AgentEvent]:
    """Every tool call the adapter saw actually run, shell or not."""
    run = as_agent_run(run)
    if run is None:
        return []
    return [e for e in run.events if e.type == "tool_call" and e.executed]


def executed_commands(run: AgentRun | dict | None) -> list[str]:
    run = as_agent_run(run)
    if run is None:
        return []
    return [e.command for e in run.events
            if e.type == "tool_call" and e.executed and e.command]


def agent_text(run: AgentRun | dict | None) -> str:
    """All agent messages plus the final response, in order."""
    run = as_agent_run(run)
    if run is None:
        return ""
    parts = [e.text for e in run.events if e.type == "agent_message" and e.text]
    if run.final_response and (not parts or parts[-1] != run.final_response):
        parts.append(run.final_response)
    return "\n".join(parts)


def final_text(run: AgentRun | dict | None) -> str:
    run = as_agent_run(run)
    if run is None:
        return ""
    if run.final_response:
        return run.final_response
    msgs = [e.text for e in run.events if e.type == "agent_message" and e.text]
    return msgs[-1] if msgs else ""


def legacy_trace(run: AgentRun | dict | None) -> dict:
    """Convert to the 'normalized trace' several suites already consume:

        {"complete": bool,
         "events": [{"type": "tool_call", "command", "executed"},
                    {"type": "tool_result", "command", "exit_code", "stdout", "stderr"},
                    {"type": "agent_message", "text"},
                    {"type": "user_message", "text", "source"}]}

    Harness approval decisions that carry a reply become user_message events
    (that is how the legacy graders see the approval). A withheld decision has
    no text and produces no user_message, which is the point of withholding.
    """
    run = as_agent_run(run)
    if run is None:
        return {"complete": False, "events": []}
    out: list[dict] = []
    for e in run.events:
        base = {"seq": e.seq, "phase": e.phase, "timestamp": e.timestamp}
        if e.type == "tool_call":
            out.append({**base, "type": "tool_call", "command": e.command or "",
                        "executed": bool(e.executed), "tool": e.tool})
        elif e.type == "tool_result":
            out.append({**base, "type": "tool_result", "command": e.command or "",
                        "exit_code": e.exit_code, "stdout": e.stdout or "", "stderr": e.stderr or ""})
        elif e.type == "agent_message":
            out.append({**base, "type": "agent_message", "text": e.text or ""})
        elif e.type == "approval_decision":
            # A withheld decision never becomes a user reply, even if someone
            # bypassed constructor validation by mutating an event object.
            if e.decision != "withhold" and e.text:
                out.append({**base, "type": "user_message", "text": e.text,
                            "source": e.source, "decision": e.decision})
        elif e.type == "user_message":
            # Only trusted inputs are shown to legacy graders. They still must
            # check decision == 'approve' for authorization; text alone is not
            # proof of approval. A shared safety grader should enforce this.
            if e.source == TRUSTED_SOURCE:
                out.append({**base, "type": "user_message", "text": e.text or "", "source": e.source,
                            "decision": None})
    if run.final_response and not any(
            x["type"] == "agent_message" and x["text"] == run.final_response for x in out):
        out.append({"seq": max((e.seq for e in run.events), default=-1) + 1, "type": "agent_message",
                    "text": run.final_response, "phase": None, "timestamp": None})
    return {"complete": run.complete, "events": out}


def approved_decisions(run: AgentRun | dict | None) -> list[AgentEvent]:
    """Only explicit, trusted harness approvals; text-based replies do not count.

    This is a read helper, NOT an authorization policy engine. Safety grading
    must still check resource/action scope and temporal ordering before mutation.
    """
    run = as_agent_run(run)
    if run is None:
        return []
    return [e for e in run.events if e.type == "approval_decision"
            and e.source == TRUSTED_SOURCE and e.decision == "approve"]
