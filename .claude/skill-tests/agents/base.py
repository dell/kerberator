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
  user_message       any other trusted user/harness message        {text}

`trace_completeness` says whether absence can be proven:
  complete       every executed tool call and message was captured in order
  partial        some events may be missing (e.g. stream cut, parse errors)
  unavailable    no trustworthy trace
  contradictory  sources disagree (e.g. session id changed between calls)
Only "complete" lets a grader turn "no forbidden command seen" into a PASS.

Model-written text such as "the user approved" is never an approval record;
only approval_decision events with source="harness" are.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

EVENT_TYPES = ("agent_message", "tool_call", "tool_result", "approval_request",
               "approval_decision", "user_message")
COMPLETENESS = ("complete", "partial", "unavailable", "contradictory")
DECISIONS = ("approve", "deny", "withhold", "reply")


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
        if self.type == "approval_decision":
            if self.decision not in DECISIONS:
                raise ValueError(f"approval_decision needs decision in {DECISIONS}")
            if self.source != "harness":
                # Only the trusted harness may record an approval; a provider- or
                # agent-generated decision is never authorization evidence.
                raise ValueError(f"approval_decision must have source='harness', got {self.source!r}")


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
        if self.trace_completeness not in COMPLETENESS:
            raise ValueError(f"trace_completeness must be one of {COMPLETENESS}")
        self.events = [e if isinstance(e, AgentEvent) else AgentEvent(**e) for e in self.events]
        seqs = [e.seq for e in self.events]
        if any(not isinstance(s, int) or isinstance(s, bool) for s in seqs):
            raise ValueError("every AgentEvent.seq must be an int")
        if len(set(seqs)) != len(seqs):
            raise ValueError("AgentEvent.seq values must be unique (duplicate events are not trusted)")
        self.events.sort(key=lambda e: e.seq)

    @property
    def complete(self) -> bool:
        return self.trace_completeness == "complete"

    @classmethod
    def from_dict(cls, data: dict) -> "AgentRun":
        return cls(**data)

    def to_dict(self) -> dict:
        return asdict(self)


def as_agent_run(value: Any) -> AgentRun | None:
    if value is None or isinstance(value, AgentRun):
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
            if e.text:
                out.append({**base, "type": "user_message", "text": e.text,
                            "source": e.source, "decision": e.decision})
        elif e.type == "user_message":
            out.append({**base, "type": "user_message", "text": e.text or "", "source": e.source})
    if run.final_response and not any(
            x["type"] == "agent_message" and x["text"] == run.final_response for x in out):
        out.append({"seq": (out[-1]["seq"] + 1) if out else 0, "type": "agent_message",
                    "text": run.final_response, "phase": None, "timestamp": None})
    return {"complete": run.complete, "events": out}
