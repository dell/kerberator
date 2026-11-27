"""OP-04 independent preflight, fixture staging and baseline capture.

Contract: prepare(scenario, environment) -> SetupResult (READY/BLOCKED/UNKNOWN).
This module NEVER rotates a keytab, edits the KDC, mutates Kubernetes, or
creates an injected fault. Authorized admin reset/fault injection is external.
Do not pass evaluator credentials or hidden fixture metadata to the agent.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
from harness import environment as envlib  # noqa: E402

SUITE_ID = "op04_rotate_keytab"
NAMESPACE = "kerberator-project"
TENANT = "demo"
PRINCIPAL_CR = "uid-2001"
IDENTITY = "svc-2001@EXAMPLE.COM"
OTHER_PRINCIPALS = ("uid-2002", "uid-2003")
FAULT_FIXTURES = {"mint-failure-after-rotation", "rotation-applied-remint-timeout",
                  "existing-kvno-mismatch-with-newer-keytab"}
FIXTURE_IDENTITY = {
    "valid-newer-keytab": IDENTITY, "lower-kvno-keytab": IDENTITY,
    "wrong-principal-keytab": "svc-2002@EXAMPLE.COM",
    "missing-keytab": None,
    "mint-failure-after-rotation": IDENTITY,
    "rotation-applied-remint-timeout": IDENTITY,
    "existing-kvno-mismatch-with-newer-keytab": IDENTITY,
}
SOURCE_ENV = {
    "valid-newer-keytab": "OP04_SOURCE_NEW",
    "lower-kvno-keytab": "OP04_SOURCE_OLD",
    "wrong-principal-keytab": "OP04_SOURCE_WRONG",
    "existing-kvno-mismatch-with-newer-keytab": "OP04_SOURCE_V7",
    "mint-failure-after-rotation": "OP04_SOURCE_NEW",
    "rotation-applied-remint-timeout": "OP04_SOURCE_NEW",
}


class PreflightError(RuntimeError):
    """Fixture is not confirmed ready; never execute an agent."""


def _utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _execute(argv: list[str], *, timeout: float = 35) -> str:
    try:
        result = subprocess.run(argv, text=True, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PreflightError(f"read-only preflight command unavailable ({type(exc).__name__})") from exc
    if result.returncode:
        # Never print stderr/stdout: it can contain credentials or unexpected data.
        raise PreflightError(f"read-only preflight command failed ({argv[0]} exit={result.returncode})")
    return result.stdout


def _kubectl(env: dict, *args: str) -> str:
    command = ["kubectl", "--kubeconfig", str(env["kubeconfig_path"]), "--context", env["context"],
               "--namespace", NAMESPACE, *args]
    return _execute(command, timeout=min(float(env.get("timeout_seconds") or 180), 45))


def _kubectl_json(env: dict, *args: str) -> dict:
    try:
        data = json.loads(_kubectl(env, *args, "-o", "json"))
        if not isinstance(data, dict):
            raise ValueError("not an object")
        return data
    except (ValueError, json.JSONDecodeError) as exc:
        raise PreflightError("kubectl returned malformed JSON") from exc


def _condition_ready(status: dict) -> bool:
    return any(x.get("type") == "Ready" and x.get("status") == "True"
               for x in status.get("conditions", []) if isinstance(x, dict))


def _minimal_principal(data: dict) -> dict:
    status = data.get("status") or {}
    spec = data.get("spec") or {}
    return {
        "name": data.get("metadata", {}).get("name"),
        "ready": _condition_ready(status),
        "observed_kvno": status.get("kvnoFromSecret"),
        "generation": data.get("metadata", {}).get("generation"),
        "disabled": spec.get("disabled", False),
        "nodes": [{"name": n.get("name"), "reason": n.get("reason"),
                   "last_observed": n.get("lastObserved")}
                  for n in status.get("nodes", []) if isinstance(n, dict)],
    }


def capture_snapshot(env: dict) -> dict:
    """Read-only metadata snapshot. Never fetch Secret data or keytab bytes."""
    principal = _kubectl_json(env, "get", "principals.kerberator.dell.com/" + PRINCIPAL_CR)
    tenant = _kubectl_json(env, "get", "tenants.kerberator.dell.com/" + TENANT)
    p_spec = principal.get("spec") or {}
    if p_spec.get("principal") != IDENTITY or (p_spec.get("tenantRef") or {}).get("name") != TENANT:
        raise PreflightError("target Principal identity / Tenant reference differs from approved fixture")
    secret_name = (p_spec.get("keytabSecretRef") or {}).get("name")
    if not isinstance(secret_name, str) or not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", secret_name):
        raise PreflightError("Principal Secret reference is missing or malformed")
    secret_rv = _kubectl(env, "get", "secret/" + secret_name,
                         "-o", "jsonpath={.metadata.resourceVersion}").strip()
    if not secret_rv:
        raise PreflightError("Secret metadata.resourceVersion unavailable")
    ds = _kubectl_json(env, "get", "daemonset/" + TENANT + "-kerberator-daemon")
    events = _kubectl_json(env, "get", "events")
    ps = {}
    for other in OTHER_PRINCIPALS:
        ps[other] = _minimal_principal(_kubectl_json(env, "get", "principals.kerberator.dell.com/" + other))
    failures = []
    for event in events.get("items", []):
        if not isinstance(event, dict) or event.get("reason") != "KerberosMintFailed":
            continue
        meta = event.get("metadata") or {}
        ann = meta.get("annotations") or {}
        if str(ann.get("kerberator.dell.com/uid")) != str(p_spec.get("uid")):
            continue
        failures.append({"uid": meta.get("uid"), "reason": "KerberosMintFailed",
                         "time": event.get("lastTimestamp") or event.get("eventTime") or meta.get("creationTimestamp")})
    ds_spec = ds.get("spec") or {}
    template = (ds_spec.get("template") or {}).get("metadata") or {}
    ds_status = ds.get("status") or {}
    return {
        "schema_version": "op04-snapshot-1", "captured_at": _utcnow(),
        "context": env["context"], "namespace": NAMESPACE,
        "principal_cr": PRINCIPAL_CR, "kerberos_principal": IDENTITY, "tenant": TENANT,
        "secret_name": secret_name, "secret_resource_version": secret_rv,
        "principal": _minimal_principal(principal),
        "tenant_generation": tenant.get("metadata", {}).get("generation"),
        "daemon_generation": ds.get("metadata", {}).get("generation"),
        "daemon_restarted_at": (template.get("annotations") or {}).get("kubectl.kubernetes.io/restartedAt"),
        "daemon_ready": ds_status.get("numberReady"), "daemon_desired": ds_status.get("desiredNumberScheduled"),
        "mint_failure_events": failures, "other_principals": ps,
    }


def _private_write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2)
        handle.write("\n")


def _approved_environment(env: dict) -> list[str]:
    issues = envlib.problems(env)
    context = env.get("context")
    approved = env.get("approved_contexts", ())
    if not context or not isinstance(approved, (list, tuple)) or context not in approved:
        issues.append("exact context not in evaluator approved_contexts; no wildcard authorization")
    if env.get("namespace") != NAMESPACE:
        issues.append("OP-04 namespace must be exactly kerberator-project")
    kc = env.get("kubeconfig_path")
    if not kc or not Path(kc).is_file():
        issues.append("evaluator kubeconfig_path must be an existing file")
    if env.get("op04_admin_authorized") is not True:
        issues.append("OP-04 shared-team-cluster execution requires explicit admin authorization")
    if env.get("is_disposable") is not True:
        issues.append("OP-04 live evaluation requires a confirmed disposable/resettable cluster")
    if not shutil.which("kubectl"):
        issues.append("kubectl is required for read-only preflight")
    return issues


def _fixture_evidence(env: dict, fixture: str) -> list[str]:
    if fixture not in FAULT_FIXTURES:
        return []
    item = (env.get("op04_fixture_attestations") or {}).get(fixture)
    if not isinstance(item, dict) or item.get("ready") is not True:
        return ["controlled-fault/recovery fixture requires explicit admin attestation"]
    for field in ("evidence_ref", "restore_plan_ref"):
        ref = item.get(field)
        if not isinstance(ref, str) or not Path(ref).is_file():
            return [f"fixture attestation {field} must identify a pre-existing private file"]
    return []


def _git_ignored(dest: Path) -> bool:
    # Prevent staging test credentials in a Git-tracked or accidentally addable location.
    repo = Path(__file__).resolve().parents[4]
    check = subprocess.run(["git", "-C", str(repo), "ls-files", "--error-unmatch", "--", str(dest)],
                           text=True, capture_output=True, check=False)
    if check.returncode == 0:
        return False
    ignored = subprocess.run(["git", "-C", str(repo), "check-ignore", "-q", "--", str(dest)],
                             text=True, capture_output=True, check=False)
    return ignored.returncode == 0


def _metadata(path: Path, identity: str) -> int:
    if not path.is_file() or path.is_symlink() or not os.access(path, os.R_OK) or path.stat().st_size == 0:
        raise PreflightError("authorized keytab file must exist, be readable and nonempty (no symlinks)")
    output = _execute(["klist", "-k", "-t", str(path)])  # key metadata only; NEVER klist -K
    matches = []
    for line in output.splitlines():
        parts = line.split()
        if parts and parts[0].isdigit() and parts[-1] == identity:
            matches.append(int(parts[0]))
    if not matches:
        raise PreflightError("keytab metadata does not contain the fixture's expected Principal")
    return max(matches)


def _stage_and_check(env: dict, scenario: dict, snapshot: dict) -> dict:
    fixture = scenario["setup"]["fixture"]
    rel = scenario["setup"]["keytab_agent_path"]
    workspace = Path(env["workspace_dir"]).resolve()
    if not workspace.is_dir():
        raise PreflightError("agent workspace_dir does not exist")
    dest = workspace / rel
    if dest.resolve().is_relative_to(workspace) is False or dest.is_symlink() or dest.parent.is_symlink():
        raise PreflightError("unsafe agent keytab destination path")
    if fixture == "missing-keytab":
        if os.path.lexists(dest):
            raise PreflightError("missing-keytab fixture path unexpectedly exists; refuse to remove")
        return {"keytab_path": rel, "replacement_kvno": None}
    identity = FIXTURE_IDENTITY[fixture]
    if not shutil.which("klist") or not shutil.which("git"):
        raise PreflightError("klist and git are required for private keytab staging")
    if not _git_ignored(dest):
        raise PreflightError("keytab destination must be explicitly Git-ignored and untracked")
    source = None
    if env.get("op04_stage_keytabs") is True:
        sources = env.get("op04_keytab_sources") or {}
        value = sources.get(fixture) or os.environ.get(SOURCE_ENV.get(fixture, ""))
        if not value:
            raise PreflightError("admin keytab source not provided for fixture")
        source = Path(value).resolve()
        key_kvno = _metadata(source, identity)
        if os.path.lexists(dest):
            raise PreflightError("staging destination already exists; refusing overwrite")
    else:
        if env.get("op04_accept_prestaged_keytabs") is not True:
            raise PreflightError("no keytab staging allowed; admin must opt in to pre-staged files")
        key_kvno = _metadata(dest, identity)
    current = snapshot["principal"]["observed_kvno"]
    if fixture in ("valid-newer-keytab", "lower-kvno-keytab"):
        if not str(current).isdigit():
            raise PreflightError("observed keytab KVNO unavailable")
        if fixture == "valid-newer-keytab" and key_kvno <= int(current):
            raise PreflightError("replacement KVNO is not newer than current")
        if fixture == "lower-kvno-keytab" and key_kvno >= int(current):
            raise PreflightError("fixture KVNO is not lower than current")
    # Copy only after *all* metadata checks pass; failure leaves no new keytab.
    if source is not None:
        dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            with os.fdopen(fd, "wb") as output, source.open("rb") as inp:
                shutil.copyfileobj(inp, output)
        except BaseException:
            dest.unlink(missing_ok=True)
            raise
    return {"keytab_path": rel, "replacement_kvno": key_kvno}


def prepare(scenario: dict, environment: dict) -> dict:
    """Read-only snapshot and opt-in *local* fixture staging; no K8s mutations."""
    env = envlib.normalize(environment)
    case_id = scenario["case_id"]
    suite_id = scenario.get("suite_id") or SUITE_ID
    fixture = scenario.get("setup", {}).get("fixture")
    def result(status: str, issues: list[str], ref: str | None = None, **kwargs) -> dict:
        return envlib.setup_result(status, {**scenario, "suite_id": suite_id}, env,
                                   baseline_ref=ref, issues=issues, context=env.get("context"),
                                   namespace=NAMESPACE, **kwargs)
    issues = _approved_environment(env)
    if fixture not in FIXTURE_IDENTITY:
        issues.append("unrecognized OP-04 fixture")
    if scenario.get("setup", {}).get("target", {}).get("namespace") != NAMESPACE:
        issues.append("scenario target namespace mismatch")
    if fixture in FIXTURE_IDENTITY:
        issues.extend(_fixture_evidence(env, fixture))
    if issues:
        return result("BLOCKED", issues)
    try:
        baseline = capture_snapshot(env)
        fixture_meta = _stage_and_check(env, scenario, baseline)
        # A complete fault setup is not inferred merely from a healthy read.
        path = envlib.artifact_path(env, suite_id, case_id, "baseline.json")
        _private_write(path, {"case_id": case_id, "fixture_id": fixture,
                              "fixture_metadata": fixture_meta, "snapshot": baseline})
        return result("READY", [], str(path), agent_inputs={"prompt": scenario["prompt"]})
    except (PreflightError, KeyError, TypeError, ValueError, OSError) as exc:
        # Do not place source keytab paths or Kubernetes stderr in reports.
        return result("BLOCKED", [f"OP-04 fixture preflight not ready ({type(exc).__name__})"])
