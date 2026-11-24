"""The trusted `environment` the runner passes to prepare() and verify().

Eleven canonical keys (see the Environment Fields spec):

    workspace_dir, artifact_dir, kubeconfig_path, context, namespace,
    is_disposable, allow_fixture_mutation, allowed_fixture_ids,
    allowed_targets, permissions{fixture_allowed_verbs, agent_allowed_verbs},
    timeout_seconds

Missing safety switches default to the SAFE value (no mutation, not
disposable). Suites may read extra optional keys (e.g. operator_namespace,
agent_as, mode); unknown keys are kept, never required.

The agent never sees this object, the evaluator kubeconfig, or artifact_dir.
"""
from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import Any, Iterator

REQUIRED_KEYS = ("workspace_dir", "artifact_dir", "kubeconfig_path", "context", "namespace",
                 "is_disposable", "allow_fixture_mutation", "allowed_fixture_ids",
                 "allowed_targets", "permissions", "timeout_seconds")

_SAFE_DEFAULTS: dict[str, Any] = {
    "kubeconfig_path": None, "context": None, "namespace": None,
    "is_disposable": False, "allow_fixture_mutation": False,
    "allowed_fixture_ids": [], "allowed_targets": [],
    "permissions": {"fixture_allowed_verbs": [], "agent_allowed_verbs": ["get", "list", "watch"]},
    "timeout_seconds": 180,
}


def normalize(environment: dict | None) -> dict:
    """Fill safe defaults; never turns a safety switch on."""
    env = {**_SAFE_DEFAULTS, **(environment or {})}
    env["is_disposable"] = env.get("is_disposable") is True
    env["allow_fixture_mutation"] = env.get("allow_fixture_mutation") is True
    return env


def problems(environment: dict) -> list[str]:
    env = normalize(environment)
    out = []
    for key in ("workspace_dir", "artifact_dir"):
        if not env.get(key):
            out.append(f"environment.{key} is required")
    if env.get("workspace_dir") and env.get("artifact_dir"):
        ws = Path(env["workspace_dir"]).resolve()
        art = Path(env["artifact_dir"]).resolve()
        if art == ws or ws in art.parents:
            out.append("artifact_dir must be outside the agent workspace_dir")
    return out


def mutation_gate(environment: dict, fixture_id: str | None) -> list[str]:
    """Issues that forbid setup from changing the cluster. Empty == allowed.

    Necessary, not sufficient: the runner is still responsible for proving the
    cluster identity and handing setup a credential limited to fixture scope.
    """
    env = normalize(environment)
    issues = []
    if not env["allow_fixture_mutation"]:
        issues.append("fixture mutation not authorized (allow_fixture_mutation is not true)")
    if not env["is_disposable"]:
        issues.append("environment is not marked disposable")
    if fixture_id is not None and fixture_id not in env["allowed_fixture_ids"]:
        issues.append(f"fixture {fixture_id!r} is not in allowed_fixture_ids")
    if not env.get("context"):
        issues.append("no explicit Kubernetes context")
    return issues


@contextlib.contextmanager
def kube_env(environment: dict) -> Iterator[None]:
    """Point kubectl subprocesses at the evaluator kubeconfig for the duration.

    Suites call kubectl with --context; this makes --context resolve inside
    environment.kubeconfig_path instead of whatever ~/.kube/config is.
    """
    path = normalize(environment).get("kubeconfig_path")
    old = os.environ.get("KUBECONFIG")
    if path:
        os.environ["KUBECONFIG"] = str(path)
    try:
        yield
    finally:
        if path:
            if old is None:
                os.environ.pop("KUBECONFIG", None)
            else:
                os.environ["KUBECONFIG"] = old


def artifact_path(environment: dict, suite_id: str, case_id: str, name: str) -> Path:
    """Private evaluator file location: <artifact_dir>/<suite_id>/<case_id>/<name>."""
    base = normalize(environment).get("artifact_dir")
    if not base:
        raise ValueError("environment.artifact_dir is required")
    p = Path(base) / suite_id / case_id / name
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def setup_result(status: str, scenario: dict, environment: dict, *, baseline_ref: str | None,
                 issues: list[str] | None = None, context: str | None = None,
                 namespace: str | None = None) -> dict:
    """Canonical SetupResult. status: READY | BLOCKED | UNKNOWN."""
    if status not in ("READY", "BLOCKED", "UNKNOWN"):
        raise ValueError(f"bad setup status {status!r}")
    env = normalize(environment)
    return {
        "status": status,
        "suite_id": scenario.get("suite_id"),
        "case_id": scenario["case_id"],
        "fixture_id": (scenario.get("setup") or {}).get("fixture"),
        "baseline_ref": baseline_ref,
        "context": context if context is not None else env.get("context"),
        "namespace": namespace if namespace is not None else env.get("namespace"),
        "issues": list(issues or []),
    }
