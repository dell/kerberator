#!/usr/bin/env python3
"""Kerberator single-case evaluator (shared-suite contract from PR #62).

This module coordinates ONE evaluation. It does not implement Kubernetes
business logic, provider CLI parsing, safety rules, pricing, or benchmarks.

Public integration contracts:
    suite.setup.prepare(scenario, environment) -> SetupResult dict
    adapter.run_agent(request: ExecutionRequest,
                      approval_callback: Callable[[ApprovalRequest], ApprovalDecision]) -> AgentRun
    suite.verify.verify(scenario, setup_result, environment) -> CheckResult
    suite.grade.grade(scenario, setup_result, agent_run, system_result) -> CheckResult
    safety.grade_safety(scenario, setup_result, agent_run, system_result, environment)
        -> CheckResult

The provider adapter must stop at an approval request and call the callback;
only the runner creates trusted approval_decision trace events. Approval text
alone is NEVER authorization. Real authorization must also be enforced by RBAC
and by the agent adapter's execution controls.

The no-skill arm must be enforced by the adapter's actual tool/skill permissions
and workspace layout, not merely its name. This is not a sandbox.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
import importlib
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Callable, Mapping, Protocol
from uuid import uuid4

from agents.base import AgentRun, as_agent_run
from harness.approval import ApprovalController, ApprovalRequest, ApprovalDecision, EvaluationError
from grading.results import CheckResult, RunReport, inconclusive
from harness import environment as envlib
from harness import scenario as scenario_lib

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 3)


def _exception_label(exc: Exception) -> str:
    # Exception text can contain secrets or the contents of a CLI response.
    return type(exc).__name__


@dataclass(frozen=True)
class EvaluationConfig:
    suite_id: str
    case_id: str
    provider: str
    arm: str = "skill"
    model: str | None = None
    run_id: str = field(default_factory=lambda: uuid4().hex)

    def __post_init__(self) -> None:
        for name in ("suite_id", "case_id", "run_id"):
            if not _SAFE_ID.fullmatch(getattr(self, name)) or getattr(self, name) in (".", ".."):
                raise ValueError(f"{name} must be a safe path component")
        if self.arm not in ("skill", "no_skill"):
            raise ValueError("arm must be skill or no_skill")
        if not isinstance(self.provider, str) or not self.provider.strip():
            raise ValueError("provider is required")


@dataclass(frozen=True)
class ExecutionRequest:
    """Agent-visible information only; never pass the private scenario or env."""
    prompt: str
    provider: str
    arm: str
    model: str | None
    timeout_seconds: float
    workspace_dir: str
    context: str | None
    namespace: str | None
    agent_kubeconfig_path: str | None
    allowed_verbs: tuple[str, ...]
    skill_access: bool
    run_id: str


class AgentAdapter(Protocol):
    def run_agent(self, request: ExecutionRequest,
                  approval_callback: Callable[[ApprovalRequest], ApprovalDecision]) -> AgentRun: ...


def _write_private_json(path: Path, value: dict) -> None:
    """Refuse overwrite, never write secrets or raw provider traces here."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise EvaluationError("report path is a symlink")
    # Atomic-ish exclusive create: never clobber a previous experimental run.
    serialized = json.dumps(value, indent=2, sort_keys=True, default=str) + "\n"
    fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(serialized)
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def _module_result(module: Any, fn_name: str, *args: Any) -> CheckResult:
    from grading.results import as_check_result
    fn = getattr(module, fn_name)
    value = as_check_result(fn(*args))
    if value is None:
        return inconclusive("missing_result", f"{fn_name} returned no result")
    return value


def _make_request(config: EvaluationConfig, scenario: dict, setup: dict, env: dict) -> ExecutionRequest:
    inputs = setup.get("agent_inputs") or {}
    prompt = inputs.get("prompt") or scenario["prompt"]
    if not isinstance(prompt, str) or not prompt.strip():
        raise EvaluationError("resolved prompt is missing")
    context = inputs.get("context", setup.get("context") or env.get("context"))
    agent_kubeconfig = inputs.get("kubeconfig") or inputs.get("agent_kubeconfig") or env.get("agent_kubeconfig_path")
    permissions = env.get("permissions") or {}
    if not isinstance(permissions, dict):
        raise EvaluationError("environment.permissions must be a dictionary")
    allowed = permissions.get("agent_allowed_verbs", ())
    if not isinstance(allowed, (list, tuple)):
        raise EvaluationError("agent_allowed_verbs must be a list")
    # Do not expose the entire scenario, setup, fixture definitions, expected
    # outcomes, evaluator kubeconfig, or artifact directory to the adapter.
    return ExecutionRequest(
        prompt=prompt, provider=config.provider, arm=config.arm, model=config.model,
        timeout_seconds=float(env.get("timeout_seconds", 180)),
        workspace_dir=str(env["workspace_dir"]),
        context=context, namespace=setup.get("namespace") or env.get("namespace"),
        agent_kubeconfig_path=str(agent_kubeconfig) if agent_kubeconfig else None,
        allowed_verbs=tuple(allowed), skill_access=config.arm == "skill", run_id=config.run_id,
    )


def _environment_preflight(config: EvaluationConfig, env: dict, adapter: Any) -> list[str]:
    issues = list(envlib.problems(env))
    if env.get("allow_agent_execution") is not True:
        issues.append("allow_agent_execution must be explicitly true")
    if config.arm == "no_skill" and getattr(adapter, "supports_no_skill", False) is not True:
        issues.append("adapter must explicitly support enforced no-skill isolation")
    return issues


def evaluate_one(config: EvaluationConfig, environment: Mapping[str, Any], *,
                 adapter: AgentAdapter, safety_grader: Any | None = None,
                 timing_collector: Callable[..., Mapping[str, Any]] | None = None,
                 effort_collector: Callable[..., Mapping[str, Any]] | None = None) -> tuple[RunReport, Path]:
    """Run one case. Never mutate the cluster directly, never interpret provider traces.

    Invariants:
      * Setup BLOCKED/UNKNOWN -> no agent invocation.
      * Independent verification is attempted even after adapter failure.
      * Proven safety/behavior/system FAIL takes precedence over missing data.
      * Missing common safety grader -> INCONCLUSIVE, never an inferred PASS.
      * Reports use an exclusive private file and never embed raw CLI traces.
    """
    began = time.perf_counter()
    started_at = _utcnow()
    env = envlib.normalize(dict(environment))
    env["run_id"], env["arm"] = config.run_id, config.arm
    # Validate the private artifact boundary BEFORE creating any artifact.
    # Otherwise a malformed configuration could place reports in an agent-
    # visible workspace even when execution is blocked by preflight.
    path_issues = envlib.problems(env)
    if path_issues:
        raise EvaluationError("invalid private artifact/workspace boundary: " + "; ".join(path_issues))
    report_path = envlib.artifact_path(env, config.suite_id, config.case_id, "run_report.json")
    workspace = Path(env["workspace_dir"]).resolve()
    resolved_report = report_path.resolve()
    if workspace == resolved_report or workspace in resolved_report.parents:
        raise EvaluationError("report location resolves inside the agent workspace")
    report_path.parent.chmod(0o700)
    timings: dict[str, float] = {}
    errors: list[dict[str, str]] = []
    setup: dict[str, Any] = {"status": "UNKNOWN"}
    agent: AgentRun | None = None
    failed_agent_status: str | None = None
    system: CheckResult | None = None
    behavior: CheckResult | None = None
    safety: CheckResult | None = None
    controller: ApprovalController | None = None
    scenario: dict | None = None
    stage = "preflight"

    try:
        issues = _environment_preflight(config, env, adapter)
        if issues:
            raise EvaluationError("; ".join(issues))
        stage = "scenario"
        scenario = scenario_lib.load_case(config.suite_id, config.case_id)
        if scenario["suite_id"] != config.suite_id or scenario["case_id"] != config.case_id:
            raise EvaluationError("scenario identity does not match config")
        modules = {name: scenario_lib.import_suite_file(config.suite_id, name)
                   for name in ("setup", "verify", "grade")}
        stage = "setup"
        tick = time.perf_counter()
        try:
            setup = modules["setup"].prepare(scenario, env)
            if not isinstance(setup, dict) or setup.get("status") not in ("READY", "BLOCKED", "UNKNOWN"):
                raise EvaluationError("prepare() returned invalid SetupResult")
            if setup.get("case_id") not in (None, config.case_id) or setup.get("suite_id") not in (None, config.suite_id):
                raise EvaluationError("prepare() returned the wrong scenario identity")
            if setup["status"] == "READY":
                if env.get("context") and setup.get("context") and setup["context"] != env["context"]:
                    raise EvaluationError("setup context differs from the approved context")
                if env.get("namespace") and setup.get("namespace") and setup["namespace"] != env["namespace"]:
                    raise EvaluationError("setup namespace differs from the approved namespace")
                baseline = setup.get("baseline_ref")
                if not baseline or not Path(baseline).is_file():
                    raise EvaluationError("READY setup has no existing baseline snapshot")
                resolved_baseline = Path(baseline).resolve()
                if resolved_baseline == workspace or workspace in resolved_baseline.parents:
                    raise EvaluationError("baseline is inside the agent workspace")
        except Exception as exc:
            setup = {"status": "UNKNOWN"}
            errors.append({"stage": stage, "type": _exception_label(exc)})
        finally:
            timings["setup_ms"] = _ms(tick)

        if setup["status"] == "READY":
            stage = "agent"
            tick = time.perf_counter()
            controller = ApprovalController(scenario["approval_policy"], env)
            try:
                request = _make_request(config, scenario, setup, env)
                # The agent must use a separate, restricted identity. Fixture-mode
                # runs can be wholly offline and need no kubeconfig.
                if env.get("mode") != "fixture" and not request.agent_kubeconfig_path:
                    raise EvaluationError("live agent requires its own restricted kubeconfig")
                result = adapter.run_agent(request, controller)
                agent = as_agent_run(result)
                if agent is None:
                    raise EvaluationError("adapter returned no AgentRun")
                if agent.provider != config.provider or agent.arm != config.arm:
                    raise EvaluationError("adapter provider/arm does not match run configuration")
                if agent.complete and not agent.events and not agent.final_response:
                    raise EvaluationError("adapter claimed complete trace without any execution evidence")
                agent = controller.apply_to_run(agent)
            except Exception as exc:
                errors.append({"stage": stage, "type": _exception_label(exc)})
                failed_agent_status = "timeout" if isinstance(exc, TimeoutError) else "error"
                agent = None
            finally:
                timings["agent_ms"] = _ms(tick)

            stage = "verify"
            tick = time.perf_counter()
            try:
                system = _module_result(modules["verify"], "verify", scenario, setup, env)
            except Exception as exc:
                errors.append({"stage": stage, "type": _exception_label(exc)})
                system = inconclusive("verification_error", "independent verification did not complete")
            finally:
                timings["verification_ms"] = _ms(tick)

            stage = "grade"
            tick = time.perf_counter()
            try:
                behavior = _module_result(modules["grade"], "grade", scenario, setup, agent, system)
            except Exception as exc:
                errors.append({"stage": stage, "type": _exception_label(exc)})
                behavior = inconclusive("grading_error", "behavior grading did not complete")
            finally:
                timings["grading_ms"] = _ms(tick)

            stage = "safety"
            tick = time.perf_counter()
            try:
                if safety_grader is None:
                    safety = inconclusive("safety_grader_missing", "shared safety grader not connected")
                else:
                    safety = _module_result(safety_grader, "grade_safety", scenario, setup, agent, system, env)
            except Exception as exc:
                errors.append({"stage": stage, "type": _exception_label(exc)})
                safety = inconclusive("safety_grading_error", "safety grading did not complete")
            finally:
                timings["safety_ms"] = _ms(tick)
    except Exception as exc:
        errors.append({"stage": stage, "type": _exception_label(exc)})
    finally:
        timings["total_ms"] = _ms(began)

    observed: dict[str, Any] = {"approval_requests": len(controller.requests) if controller else 0,
                                 "approval_decisions": len(controller.decisions) if controller else 0}
    if controller and controller.issues:
        # A misconfigured approval gate is missing trusted authorization
        # evidence. It must prevent PASS, but should not erase proven failures.
        gate = inconclusive("approval_gate_error", "; ".join(controller.issues))
        safety = CheckResult.from_findings((safety.findings if safety else []) + gate.findings)
    if agent is not None:
        if timing_collector is not None:
            try:
                extra = timing_collector(agent, dict(timings), env)
                if isinstance(extra, Mapping):
                    # Collector metadata cannot overwrite trusted runner clocks.
                    timings["provider_breakdown"] = dict(extra)
            except Exception as exc:
                errors.append({"stage": "timing", "type": _exception_label(exc)})
        if effort_collector is not None:
            try:
                extra = effort_collector(agent, dict(observed))
                if isinstance(extra, Mapping):
                    # Model-based human effort must not overwrite observed counts.
                    observed["collector_metrics"] = dict(extra)
            except Exception as exc:
                errors.append({"stage": "human_effort", "type": _exception_label(exc)})

    report = RunReport(
        run_id=config.run_id, suite_id=config.suite_id, case_id=config.case_id,
        arm=config.arm, provider=config.provider, setup_status=setup["status"],
        agent_status=agent.status if agent else failed_agent_status,
        agent_trace_complete=agent.complete if agent else None,
        system_result=system, behavior_result=behavior, safety_result=safety,
        model=agent.model if agent and agent.model else config.model,
        timing=timings, human_effort=observed, usage=agent.usage if agent else {},
        artifact_refs=[str(setup["baseline_ref"])] if setup.get("baseline_ref") else [],
    )
    # Keep the report STRICTLY compatible with RunReport.from_dict(). Runner
    # diagnostics are a separate private file rather than an extra JSON key.
    metadata_path = envlib.artifact_path(env, config.suite_id, config.case_id, "runner_metadata.json")
    metadata = {
        "schema_version": "1.0", "started_at": started_at, "finished_at": _utcnow(),
        "stage_errors": errors,
        "setup_issues": [str(x)[:350] for x in setup.get("issues", [])],
        "approval_request_count": observed["approval_requests"],
        "approval_decisions": [d.decision for d in controller.decisions] if controller else [],
        "execution_enabled": env.get("allow_agent_execution") is True,
        "safety_grader_connected": safety_grader is not None,
    }
    _write_private_json(metadata_path, metadata)
    report.artifact_refs.append(str(metadata_path))
    if agent is not None:
        report.artifact_refs.extend(str(x) for x in agent.raw_trace_refs)
    _write_private_json(report_path, report.to_dict())
    return report, report_path


def _load_component(dotted: str, function: str) -> Any:
    module = importlib.import_module(dotted)
    if not callable(getattr(module, function, None)):
        raise EvaluationError(f"{dotted!r} must define {function}()")
    return module


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Kerberator one-case evaluation runner")
    sub = parser.add_subparsers(dest="command", required=True)
    p_list = sub.add_parser("list", help="List case IDs in a standardized suite")
    p_list.add_argument("--suite", required=True)
    p_validate = sub.add_parser("validate", help="Validate standardized suite schema")
    p_validate.add_argument("--suite", required=True)
    p_run = sub.add_parser("run", help="Run one evaluation with a connected provider adapter")
    p_run.add_argument("--suite", required=True)
    p_run.add_argument("--case", required=True)
    p_run.add_argument("--provider", required=True)
    p_run.add_argument("--arm", choices=("skill", "no_skill"), required=True)
    p_run.add_argument("--model")
    p_run.add_argument("--run-id")
    p_run.add_argument("--environment", required=True, type=Path,
                       help="Trusted evaluator-only JSON file. Never put it in the agent workspace.")
    p_run.add_argument("--adapter-module", required=True,
                       help="Trusted module exporting run_agent(request, approval_callback)")
    p_run.add_argument("--safety-module", help="Module exporting grade_safety(...). Missing = INCONCLUSIVE")
    p_run.add_argument("--timing-module", help="Module exporting collect_timing(run, stage_ms, env)")
    p_run.add_argument("--human-module", help="Module exporting summarize_human_effort(run, observed_counts)")
    p_run.add_argument("--execute", action="store_true", help="Required explicit opt-in to execution")
    args = parser.parse_args(argv)
    try:
        if args.command == "list":
            print("\n".join(scenario_lib.case_ids(args.suite)))
            return 0
        if args.command == "validate":
            spec = scenario_lib.load_suite(args.suite)
            print(f"VALID: {spec['suite_id']} ({len(spec['cases'])} cases)")
            return 0
        if not args.execute:
            parser.error("run requires --execute; no implicit agent or cluster execution")
        env = json.loads(args.environment.read_text(encoding="utf-8"))
        if not isinstance(env, dict):
            raise EvaluationError("environment must be a JSON object")
        # Opt-in must be present in both the command line and evaluator config.
        env.setdefault("allow_agent_execution", False)
        adapter = _load_component(args.adapter_module, "run_agent")
        safety = _load_component(args.safety_module, "grade_safety") if args.safety_module else None
        timing = (getattr(_load_component(args.timing_module, "collect_timing"), "collect_timing")
                  if args.timing_module else None)
        effort = (getattr(_load_component(args.human_module, "summarize_human_effort"), "summarize_human_effort")
                  if args.human_module else None)
        cfg = EvaluationConfig(args.suite, args.case, args.provider, args.arm, args.model,
                               args.run_id or uuid4().hex)
        report, path = evaluate_one(cfg, env, adapter=adapter, safety_grader=safety,
                                    timing_collector=timing, effort_collector=effort)
        print(json.dumps({"status": report.overall_status, "report": str(path)}, indent=2))
        return {"PASS": 0, "FAIL": 1, "INCONCLUSIVE": 2}[report.overall_status]
    except (OSError, ValueError, EvaluationError, ImportError) as exc:
        print(f"Evaluator error ({type(exc).__name__}): see trusted configuration", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
