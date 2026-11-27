#!/usr/bin/env python3
"""DG-08 operator-health evaluation setup (read-only; no fault injection).

The DG-08 skill is not implemented yet. This prepares independent evidence
for evaluating it later; READY means the scenario's fixture prerequisites match,
NOT that an agent ran or passed.

Fixture mode (default; no Kubernetes access):
  python3 setup.py --case-id dg08-02-crash-loop \
      --fixture-file /private/fixtures/operator-crash.json

Live mode (read-only, explicitly selected context and known operator workload):
  python3 setup.py --case-id dg08-01-healthy-operator --mode live \
      --context kind-dg08 --operator-namespace kerberator-system \
      --operator-deployment kerberator-controller-manager

Fixture JSON is a trusted, SANITIZED evidence snapshot, not a proposed agent
answer. Required fields vary by scenario. See normalize_fixture() and
validate_scenario() for its schema. Never put Secret data or raw logs in it.
Reports contain only reduced status facts, not raw kubectl output or logs.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

# Shared contract modules live in .claude/skill-tests/ (two levels up).
_SKILL_TESTS = Path(__file__).resolve().parents[2]
if str(_SKILL_TESTS) not in sys.path:
    sys.path.insert(0, str(_SKILL_TESTS))
from harness import environment as envlib, legacy  # noqa: E402

HERE = Path(__file__).resolve().parent
TENANT_CRD = "tenants.kerberator.dell.com"
PRINCIPAL_CRD = "principals.kerberator.dell.com"
CRDS = (TENANT_CRD, PRINCIPAL_CRD)
TIMEOUT = 25


def load_case(case_id):
    spec = json.loads((HERE / "evals.json").read_text(encoding="utf-8"))
    if spec.get("skill_id") != "DG-08" or not isinstance(spec.get("cases"), list):
        raise ValueError("evals.json is not a DG-08 case specification")
    for case in spec["cases"]:
        if case.get("case_id") == case_id:
            return case
    raise ValueError(f"Unknown DG-08 case ID: {case_id}")


def nonempty(s):
    return isinstance(s, str) and bool(s.strip()) and not s.startswith("<")


def int_value(v):
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def is_forbidden(output):
    return bool(re.search(r"\bforbidden\b|permission denied", output, re.I))


def kubectl(context, *args):
    """Only callers below choose read-only kubectl verbs. Never shell=True."""
    cmd = ["kubectl", "--context", context, *args]
    try:
        run = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return None, "error", type(exc).__name__
    if run.returncode:
        combined = run.stderr + "\n" + run.stdout
        if is_forbidden(combined):
            return None, "forbidden", "permission-denied"
        if re.search(r"\bnotfound\b|not found", combined, re.I):
            return None, "not-found", "resource-not-found"
        return None, "error", "kubectl-query-failed"
    try:
        return json.loads(run.stdout), "ok", None
    except json.JSONDecodeError:
        return None, "error", "invalid-kubectl-json"


def access_or_existing(obj, access):
    """Distinguish a missing resource from an unreadable resource."""
    if access == "not-found":
        return {"exists": False}
    if access != "ok":
        return {}
    return obj


def collect_live(context, namespace, name):
    """Collect selected controller/CRD status. Never read Secrets or raw logs."""
    ev = {
        "context": context, "operator_namespace": namespace,
        "operator_deployment": name,
        "access": {"workload": "unknown", "crds": "unknown"},
        "deployment": {}, "pods": [], "events": [], "crds": {},
        "cli_version": None,
        "collection_limits": [
            "No operator logs collected automatically; log-dependent cases require a sanitized independent fixture",
            "No unrelated namespaces or Tenant daemon resources inspected",
        ],
    }
    dep, a, err = kubectl(context, "-n", namespace, "get", "deployment", name, "-o", "json")
    ev["access"]["workload"] = "forbidden" if a == "forbidden" else ("ok" if a in {"ok", "not-found"} else "unknown")
    if a == "ok":
        status, spec = dep.get("status") or {}, dep.get("spec") or {}
        conditions = status.get("conditions") or []
        ev["deployment"] = {
            "exists": True,
            "desired": spec.get("replicas", 1),
            "ready": status.get("readyReplicas", 0),
            "available": any(c.get("type") == "Available" and c.get("status") == "True" for c in conditions),
            "image": next((c.get("image") for c in (spec.get("template") or {}).get("spec", {}).get("containers", [])
                           if c.get("name") == "manager"), None),
        }
        match_labels = ((spec.get("selector") or {}).get("matchLabels") or {})
        if match_labels and all(re.fullmatch(r"[\w./-]+", str(k)) and re.fullmatch(r"[\w.-]+", str(v))
                                for k, v in match_labels.items()):
            selector = ",".join(f"{k}={v}" for k, v in match_labels.items())
            pods, pa, _ = kubectl(context, "-n", namespace, "get", "pods", "-l", selector, "-o", "json")
            if pa == "ok":
                for pod in (pods.get("items") or [])[:30]:
                    pod_status = pod.get("status") or {}
                    cs = pod_status.get("containerStatuses") or []
                    manager = next((x for x in cs if x.get("name") == "manager"), cs[0] if cs else {})
                    waiting = (manager.get("state") or {}).get("waiting") or {}
                    name_pod = (pod.get("metadata") or {}).get("name")
                    ev["pods"].append({
                        "name": name_pod, "phase": pod_status.get("phase"),
                        "ready": manager.get("ready"),
                        "restarts": manager.get("restartCount"),
                        "waiting_reason": waiting.get("reason"),
                        "scheduled": next((c.get("status") == "True" for c in pod_status.get("conditions", [])
                                           if c.get("type") == "PodScheduled"), None),
                    })
                    if nonempty(name_pod):
                        events, ea, _ = kubectl(context, "-n", namespace, "get", "events",
                                                "--field-selector", f"involvedObject.name={name_pod}", "-o", "json")
                        if ea == "ok":
                            for event in (events.get("items") or [])[:40]:
                                # Never store raw event messages; carry just bounded, non-sensitive signals.
                                reason = event.get("reason")
                                msg = event.get("message") or ""
                                ev["events"].append({
                                    "reason": reason,
                                    "pod": name_pod,
                                    "has_details": bool(msg.strip()),
                                    "image_related": bool(re.search(r"image|pull|registry|manifest", msg, re.I)),
                                    "scheduling_related": bool(re.search(r"taint|affinity|insufficient|schedul|node", msg, re.I)),
                                })
            else:
                ev["access"]["pods"] = "forbidden" if pa == "forbidden" else "unknown"
        else:
            ev["collection_limits"].append("No valid matchLabels selector: pod discovery unverified")
    elif a == "not-found":
        ev["deployment"] = {"exists": False}
    elif err:
        ev["collection_limits"].append(f"Operator Deployment query: {err}")

    accesses = []
    for crd_name in CRDS:
        crd, ca, _ = kubectl(context, "get", "crd", crd_name, "-o", "json")
        accesses.append(ca)
        if ca == "ok":
            ev["crds"][crd_name] = {
                "exists": True,
                "versions": [
                    {"name": x.get("name"), "served": x.get("served"), "storage": x.get("storage")}
                    for x in (crd.get("spec") or {}).get("versions", [])
                ],
            }
        elif ca == "not-found":
            ev["crds"][crd_name] = {"exists": False}
        else:
            ev["crds"][crd_name] = {}
    ev["access"]["crds"] = (
        "forbidden" if "forbidden" in accesses else
        "ok" if all(a in ("ok", "not-found") for a in accesses) else "unknown"
    )

    # The plugin version is local-only; it is never treated as operator health.
    try:
        run = subprocess.run(["kubectl", "krb", "version"], capture_output=True,
                             text=True, timeout=TIMEOUT)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        run = None
    if run is not None and run.returncode == 0:
        ev["cli_version"] = run.stdout.splitlines()[0].strip()[:120] if run.stdout.strip() else None

    return ev


def normalize_fixture(path):
    """Accept a trusted reduced-evidence JSON snapshot; never execute commands."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Fixture must be a JSON object with independent evidence facts")
    if data.get("source") != "trusted-sanitized-fixture":
        raise ValueError('Fixture requires "source": "trusted-sanitized-fixture"')
    allowed = {
        "source", "context", "operator_namespace", "operator_deployment",
        "access", "deployment", "pods", "events", "crds", "cli_version",
        "controller_log_evidence", "expected_crd_versions", "expected_crd_source",
        "operator_configured_version", "runtime_version", "compatibility_policy",
        "tenant_daemon_ready", "contexts", "selected_context", "collection_limits",
    }
    unexpected = set(data) - allowed
    if unexpected:
        raise ValueError(f"Unexpected fixture keys: {sorted(unexpected)}; do not include raw logs or Secrets")
    if not isinstance(data.get("pods", []), list) or not isinstance(data.get("events", []), list):
        raise ValueError("Fixture pods/events must be arrays")
    if not isinstance(data.get("deployment", {}), dict) or not isinstance(data.get("crds", {}), dict):
        raise ValueError("Fixture deployment/crds must be objects")
    if not isinstance(data.get("access", {}), dict):
        raise ValueError("Fixture access must be an object")
    data.pop("source")
    return data


def validate_scenario(case, e):
    """Validate observable prerequisites; don't grade the future agent response.

    Incomplete or conflicting evidence -> INCONCLUSIVE, never a fabricated READY.
    Fixture authors must independently verify that their reduced facts are real.
    """
    issues = []
    def need(pred, explanation):
        if not pred:
            issues.append(explanation)

    scenario = case["setup"]["fixture"]
    if scenario == "ambiguous_kubernetes_context":
        need(isinstance(e.get("contexts"), list) and len(set(e["contexts"])) >= 2,
             "Fixture must contain at least two distinct configured contexts")
        need(e.get("selected_context") is None, "No context may be selected in this case")
        need(not e.get("deployment") and not e.get("pods") and not e.get("crds"),
             "No cluster-specific evidence may be collected before context selection")
        return issues

    need(nonempty(e.get("context")), "Explicitly recorded Kubernetes context required")
    need(nonempty(e.get("operator_namespace")), "Operator namespace must be identified")
    need(nonempty(e.get("operator_deployment")), "Operator Deployment name must be identified")

    dep = e.get("deployment") or {}
    pods = e.get("pods") or []
    events = e.get("events") or []
    crds = e.get("crds") or {}
    access = e.get("access") or {}

    healthy_dep = dep.get("exists") is True and int_value(dep.get("desired")) and dep["desired"] > 0 \
        and dep.get("ready") == dep["desired"] and dep.get("available") is True
    any_ready = any(p.get("ready") is True for p in pods)
    crds_present = all(crds.get(name, {}).get("exists") is True for name in CRDS)
    crds_well_formed = all(
        any(v.get("name") == "v1alpha1" and v.get("served") is True and v.get("storage") is True
            for v in crds.get(name, {}).get("versions", []))
        for name in CRDS
    )
    has_event = lambda reason: any(x.get("reason") == reason for x in events)
    crash = any(p.get("waiting_reason") == "CrashLoopBackOff" and int_value(p.get("restarts"))
                and p["restarts"] > 0 for p in pods)
    not_ready = int_value(dep.get("desired")) and int_value(dep.get("ready")) \
        and dep["ready"] < dep["desired"]

    if scenario == "healthy_operator_controller":
        need(healthy_dep, "Operator Deployment must be observed Available with all desired replicas Ready")
        need(any_ready, "At least one operator controller pod must be Ready")
        need(crds_present and crds_well_formed,
             "Both Tenant and Principal CRDs must serve and store expected v1alpha1")
        need(not any(p.get("waiting_reason") in {"CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull"} for p in pods),
             "Current operator pod failures conflict with healthy fixture")
        need(not any(x.get("current") is True and x.get("reason") in
                     {"Failed", "FailedCreate", "FailedScheduling", "BackOff", "Unhealthy"}
                     for x in events), "Current failure events conflict with healthy fixture")
    elif scenario == "controller_crash_loop_backoff":
        need(crash, "Operator pod must have CrashLoopBackOff with restartCount > 0")
        need(not_ready, "Operator Deployment must have fewer Ready than desired replicas")
        need(has_event("BackOff") or bool(e.get("controller_log_evidence")),
             "Need independent BackOff event or sanitized controller log evidence")
    elif scenario == "operator_image_pull_failure":
        need(any(p.get("waiting_reason") in ("ErrImagePull", "ImagePullBackOff") for p in pods),
             "Operator pod must show an image pull waiting reason")
        need(nonempty(dep.get("image")), "Configured operator manager image must be observed")
        need(any(x.get("image_related") is True for x in events),
             "Pod event evidence must identify image pull or registry problem")
    elif scenario == "controller_pending_failed_scheduling":
        need(any(p.get("phase") == "Pending" and p.get("scheduled") is False for p in pods),
             "Operator pod must be Pending and PodScheduled=False")
        need(any(x.get("reason") == "FailedScheduling" and x.get("scheduling_related") is True for x in events),
             "Need a FailedScheduling event with scheduling constraint evidence")
    elif scenario == "running_controller_with_reconciliation_error":
        logs = e.get("controller_log_evidence") or []
        need(healthy_dep and any_ready, "Controller Deployment and pod must be Ready")
        need(any(l.get("type") == "reconcile-error" and nonempty(l.get("affected_resource"))
                 and l.get("category") in ("configuration", "rbac", "admission")
                 and l.get("verified") is True for l in logs),
             "Need verified sanitized reconcile log evidence naming resource and error category")
    elif scenario == "missing_kerberator_crd":
        need(access.get("crds") == "ok", "CRD query must be independently readable")
        need(crds.get(PRINCIPAL_CRD, {}).get("exists") is False,
             "Principal CRD must be independently confirmed absent")
        need(crds.get(TENANT_CRD, {}).get("exists") is True,
             "Tenant CRD presence must be independently confirmed")
        need(dep.get("exists") is not None, "Deployment existence/status must be independently observed")
    elif scenario == "served_storage_crd_version_mismatch":
        exp = e.get("expected_crd_versions") or {}
        need(nonempty(e.get("expected_crd_source")),
             "Trusted expected CRD version source required (release/version documentation)")
        mismatch = False
        for name in CRDS:
            versions = crds.get(name, {}).get("versions", [])
            want = exp.get(name) or {}
            if crds.get(name, {}).get("exists") is True and versions and want:
                have_served = {v["name"] for v in versions if v.get("served") is True and nonempty(v.get("name"))}
                have_storage = {v["name"] for v in versions if v.get("storage") is True and nonempty(v.get("name"))}
                expected_served, expected_storage = set(want.get("served", [])), want.get("storage")
                if expected_served and nonempty(expected_storage):
                    if not expected_served.issubset(have_served) or have_storage != {expected_storage}:
                        mismatch = True
        need(mismatch, "No documented mismatch between expected and observed served/storage versions")
        need(access.get("crds") == "ok", "CRD version evidence must be readable")
    elif scenario == "cli_vs_operator_version_difference":
        need(nonempty(e.get("cli_version")), "Need observed local kubectl-krb version")
        need(nonempty(dep.get("image")), "Need observed configured operator image tag")
        need(nonempty(e.get("operator_configured_version")),
             "Need version parsed from configured operator image/tag")
        def release_number(value):
            m = re.search(r"v?(\d+\.\d+\.\d+(?:[-+][\w.-]+)?)", value or "")
            return m.group(1) if m else None
        cli_release = release_number(e.get("cli_version"))
        operator_release = release_number(e.get("operator_configured_version"))
        need(cli_release is not None and operator_release is not None,
             "Both CLI and operator configured semantic version must be identifiable")
        need(cli_release is not None and operator_release is not None and cli_release != operator_release,
             "Recorded CLI and configured operator versions must differ")
        need(not (e.get("compatibility_policy") or {}).get("verified_incompatibility", False),
             "This case requires unverified compatibility, not a proven incompatibility")
    elif scenario == "only_local_cli_version_available":
        need(nonempty(e.get("cli_version")), "Need observed local kubectl-krb version")
        need(not dep and not pods, "No usable operator Deployment/pod evidence should be available")
        need(not crds or access.get("crds") in ("unknown", "forbidden"),
             "No usable CRD evidence should be available")
        need(not e.get("runtime_version"), "Operator runtime version must be unverified")
    elif scenario == "operator_namespace_access_forbidden":
        need(access.get("workload") == "forbidden", "Operator workload read/list must be forbidden")
        need(not dep and not pods, "Cannot have verified operator workload evidence when forbidden")
        need(not e.get("controller_log_evidence"), "Controller logs must be inaccessible or unverified")
    elif scenario == "healthy_operator_with_unhealthy_tenant_daemon":
        need(healthy_dep and any_ready, "Operator Deployment and controller pod must be healthy")
        need(e.get("tenant_daemon_ready") is False,
             "Need independently observed unhealthy Tenant daemon status")
        need(not e.get("controller_log_evidence"),
             "Operator reconciliation failures would invalidate clean handoff fixture")
    else:
        issues.append(f"No validation implemented for scenario: {scenario}")
    return issues


def safe_summary(e):
    """Whitelist reduced facts; never persist free-form log, event, or Secret text."""
    scalar_keys = (
        "context", "operator_namespace", "operator_deployment", "cli_version",
        "operator_configured_version", "expected_crd_source", "tenant_daemon_ready",
        "selected_context",
    )
    out = {k: e[k] for k in scalar_keys if k in e}
    field_masks = {
        "deployment": ("exists", "desired", "ready", "available", "image"),
        "access": ("workload", "pods", "crds"),
        "runtime_version": ("value", "verified"),
        "compatibility_policy": ("verified_incompatibility",),
    }
    for field, safe_keys in field_masks.items():
        val = e.get(field)
        if isinstance(val, dict):
            out[field] = {key: val[key] for key in safe_keys if key in val}
    array_masks = {
        "pods": ("name", "phase", "ready", "restarts", "waiting_reason", "scheduled"),
        "events": ("reason", "pod", "current", "has_details", "image_related", "scheduling_related"),
        "controller_log_evidence": ("type", "category", "affected_resource", "verified"),
    }
    for field, safe_keys in array_masks.items():
        val = e.get(field)
        if isinstance(val, list):
            out[field] = [{key: item[key] for key in safe_keys if key in item}
                          for item in val if isinstance(item, dict)]
    if isinstance(e.get("crds"), dict):
        out["crds"] = {}
        for name in CRDS:
            obj = e["crds"].get(name)
            if isinstance(obj, dict):
                out["crds"][name] = {
                    "exists": obj.get("exists"),
                    "versions": [{k: ver.get(k) for k in ("name", "served", "storage")}
                                 for ver in obj.get("versions", []) if isinstance(ver, dict)],
                }
    if isinstance(e.get("expected_crd_versions"), dict):
        out["expected_crd_versions"] = {}
        for name in CRDS:
            obj = e["expected_crd_versions"].get(name)
            if isinstance(obj, dict):
                out["expected_crd_versions"][name] = {
                    "served": obj.get("served", []), "storage": obj.get("storage")
                }
    if isinstance(e.get("contexts"), list):
        out["contexts"] = [x for x in e["contexts"] if isinstance(x, str)]
    if isinstance(e.get("collection_limits"), list):
        out["collection_limits"] = [str(x)[:180] for x in e["collection_limits"]]
    return out


def private_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(prefix=".dg08-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(payload, file, indent=2, sort_keys=True)
            file.write("\n")
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


# --------------------------------------------------------------------------
# Standard suite entry point (Shared Suite Standard): prepare(scenario, environment)
# Runs main() above in-process; the CLI is unchanged.

SUITE_ID = "dg08_operator_health"
_SETUP_STATUS = {"READY": "READY", "INCONCLUSIVE": "UNKNOWN"}


def _fixture_file(env, case_id):
    if env.get("fixture_file"):
        return Path(env["fixture_file"])
    d = Path(env["fixture_dir"]) if env.get("fixture_dir") else HERE / "fixtures"
    return d / f"{case_id}.json"


def prepare(scenario, environment):
    """Read-only preflight (DG-08 never injects faults); saves the baseline.

    environment["mode"]: "live" (default) needs context, operator_namespace and
    operator_deployment; "fixture" reads a trusted sanitized JSON snapshot from
    fixture_file or <fixture_dir|fixtures>/<case_id>.json.
    """
    env = envlib.normalize(environment)
    case_id = scenario["case_id"]
    suite_id = scenario.get("suite_id") or SUITE_ID
    mode = env.get("mode", "live")
    ctx = env.get("context")
    op_ns = env.get("operator_namespace")

    def result(status, issues, ref=None, context=None):
        return envlib.setup_result(status, {**scenario, "suite_id": suite_id}, env, baseline_ref=ref,
                                   issues=issues, context=context or ctx, namespace=op_ns)

    issues = envlib.problems(env)
    argv = ["--case-id", case_id, "--mode", mode]
    if mode == "live":
        for key in ("context", "operator_namespace", "operator_deployment"):
            if not env.get(key):
                issues.append(f"environment.{key} is required in live mode; refusing to guess")
        if not issues:
            argv += ["--context", ctx, "--operator-namespace", op_ns,
                     "--operator-deployment", env["operator_deployment"]]
    elif mode == "fixture":
        fixture = _fixture_file(env, case_id)
        if not fixture.is_file():
            issues.append(f"no recorded evidence snapshot for {case_id} ({fixture.name})")
        argv += ["--fixture-file", str(fixture)]
    else:
        issues.append(f"unknown environment.mode {mode!r}")
    if issues:
        return result("BLOCKED", issues)

    baseline = envlib.artifact_path(env, suite_id, case_id, "baseline.json")
    baseline.unlink(missing_ok=True)
    argv += ["--output", str(baseline)]
    with envlib.kube_env(env):
        code, _, err = legacy.call_main(main, argv)
    if code is None:
        return result("UNKNOWN", [f"setup error: {err}"])
    if not baseline.is_file():
        return result("BLOCKED", [err or "setup produced no baseline"])
    raw = json.loads(baseline.read_text(encoding="utf-8"))
    return result(_SETUP_STATUS.get(raw.get("setup_status"), "UNKNOWN"), list(raw.get("issues", [])),
                  str(baseline), raw.get("context"))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--case-id", required=True)
    p.add_argument("--mode", choices=("fixture", "live"), default="fixture")
    p.add_argument("--fixture-file", type=Path, help="Trusted, sanitized reduced-evidence JSON snapshot")
    p.add_argument("--context", help="Required for live mode; never auto-select a cluster")
    p.add_argument("--operator-namespace", help="Required for live mode")
    p.add_argument("--operator-deployment", help="Required for live mode")
    p.add_argument("--output", type=Path, help="Private baseline path")
    args = p.parse_args(argv)

    case = load_case(args.case_id)
    if args.mode == "fixture":
        if not args.fixture_file:
            p.error("Fixture mode requires --fixture-file <trusted-sanitized-snapshot.json>")
        evidence = normalize_fixture(args.fixture_file)
    else:
        if case["setup"]["fixture"] == "ambiguous_kubernetes_context":
            p.error("Multiple-context case is fixture-only; do not query a cluster")
        if args.fixture_file:
            p.error("Do not combine --fixture-file with live mode")
        for flag, value in (("--context", args.context), ("--operator-namespace", args.operator_namespace),
                            ("--operator-deployment", args.operator_deployment)):
            if not nonempty(value):
                p.error(f"Live mode requires explicit {flag}; no implicit selection")
        evidence = collect_live(args.context, args.operator_namespace, args.operator_deployment)

    if args.mode == "fixture":
        for flag, value, key in (
            ("--context", args.context, "context"),
            ("--operator-namespace", args.operator_namespace, "operator_namespace"),
            ("--operator-deployment", args.operator_deployment, "operator_deployment"),
        ):
            if value is not None and value != evidence.get(key):
                p.error(f"{flag} does not match recorded fixture {key}")

    # Don't persist raw fixture bytes, event messages, controller logs or credentials.
    observed = safe_summary(evidence)
    issues = validate_scenario(case, observed)
    digest = hashlib.sha256(json.dumps(observed, sort_keys=True).encode("utf-8")).hexdigest()
    baseline = {
        "case_id": args.case_id,
        "skill_id": "DG-08",
        "scenario": case["setup"]["fixture"],
        "mode": args.mode,
        "setup_status": "READY" if not issues else "INCONCLUSIVE",
        "context": observed.get("context"),
        "operator_namespace": observed.get("operator_namespace"),
        "operator_deployment": observed.get("operator_deployment"),
        "observed": observed,
        "evidence_sha256": digest,
        "issues": issues,
        "note": "Preflight only; no DG-08 skill or agent was run. No mutations. Fixture truth and agent behavior require independent verification.",
    }
    dest = args.output or (HERE / "results" / f"{args.case_id}-baseline.json")
    private_json(dest, baseline)
    print(json.dumps({"case_id": args.case_id, "setup_status": baseline["setup_status"],
                      "baseline_path": str(dest), "issues": issues}, indent=2))
    return 0 if not issues else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"SETUP ERROR: {error}", file=sys.stderr)
        sys.exit(1)
