#!/usr/bin/env python3
"""Independent, read-only post-run verification for OP-02 (create a Tenant).

  python3 verify.py --case-id op02-02-happy-path \\
      --baseline results/op02-02-happy-path-baseline.json --context kind-kerberator

Checks the REAL cluster against the case's expected outcome, never the agent's
claim. Two kinds, from evals.json `expected.verify`:

  no-change       a fresh snapshot equals setup.py's baseline: no Tenant or
                  namespace added or removed, no spec, Secret, DaemonSet or node changed
  tenant-created  exactly the requested Tenant was added (namespace/name), with the
                  requested realm and image and a krb5Conf naming the realm; the
                  operator built its children (DaemonSet, roster ConfigMap, keytab
                  Secret, Role, RoleBinding) owned by it; the daemon is either Ready
                  on every scheduled node (daemon=ready) or failing to pull its image
                  (daemon=image-pull-failure); nothing else in the snapshot changed.
                  The Tenant's Ready=True is never used: it only means "reconciled".

The operator is asynchronous, so readiness checks retry until --timeout. A check
that never settles is INCONCLUSIVE; a definitely wrong state is FAIL. Agent
behaviour (approval before the create, no other writes) belongs to grade.py.

Exit: 0 PASS, 1 FAIL, 2 INCONCLUSIVE, 3 verification error.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time

# Shared contract modules live in .claude/skill-tests/ (two levels up).
_SKILL_TESTS = Path(__file__).resolve().parents[2]
if str(_SKILL_TESTS) not in sys.path:
    sys.path.insert(0, str(_SKILL_TESTS))
from grading.results import CheckResult, Finding, inconclusive, rule_id  # noqa: E402
from harness import environment as envlib, legacy  # noqa: E402

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("op02_setup", HERE / "setup.py")
fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fixture)

PULL_ERRORS = ("ErrImagePull", "ImagePullBackOff", "InvalidImageName")


def condition(obj, ctype):
    for c in (obj.get("status") or {}).get("conditions") or []:
        if c.get("type") == ctype:
            return c.get("status") or ""
    return ""


def wait(fn, timeout):
    deadline = time.time() + timeout
    while True:
        try:
            if fn():
                return True
        except Exception:
            pass
        if time.time() >= deadline:
            return False
        time.sleep(5)


def no_change(c, baseline, timeout):
    """Retry briefly: a write the agent made just before it stopped may still be landing."""
    before = baseline["snapshot"]
    diffs = []
    for _ in range(3):
        after = fixture.snapshot(c)
        diffs = [k for k in sorted(set(before) | set(after)) if before.get(k) != after.get(k)]
        if diffs:
            break
        time.sleep(min(5, timeout))
    if not diffs:
        return [], [], [("cluster matches the setup baseline", True)]
    detail = []
    bt = {(t["namespace"], t["name"]) for t in before["tenants"]}
    at = {(t["namespace"], t["name"]) for t in after["tenants"]}
    if at - bt:
        detail.append(f"Tenants added: {sorted(at - bt)}")
    if bt - at:
        detail.append(f"Tenants removed: {sorted(bt - at)}")
    return [f"cluster changed since setup in: {', '.join(diffs)}" + (f" ({'; '.join(detail)})" if detail else "")], [], \
        [("cluster matches the setup baseline", False)]


def tenant_created(c, case, baseline, spec, timeout):
    failures, uncertain, checks = [], [], []

    def check(ok, msg, soft=False):
        checks.append((msg, ok))
        if not ok:
            (uncertain if soft else failures).append(msg)

    ns, name, realm = spec["namespace"], spec["name"], spec["realm"]
    image = baseline["images"].get(spec["image"], spec["image"])
    t = c.k("-n", ns, "get", "tenants.kerberator.dell.com", name, "-o", "json")
    if t.returncode != 0:
        check(False, f"Tenant {ns}/{name} exists")
        return failures, uncertain, checks
    t = json.loads(t.stdout)
    check(True, f"Tenant {ns}/{name} exists")
    tspec = t.get("spec") or {}
    daemon = tspec.get("daemon") or {}
    check(tspec.get("realm") == realm, f"spec.realm is {realm} (got {tspec.get('realm')!r})")
    check(daemon.get("image") == image, f"spec.daemon.image is the requested image (got {daemon.get('image')!r})")
    conf = daemon.get("krb5Conf") or ""
    check("[realms]" in conf and realm in conf.split("[realms]", 1)[1], f"krb5Conf has a [realms] entry for {realm}")

    before, after = baseline["snapshot"], fixture.snapshot(c)
    b = {(x["namespace"], x["name"]): x["spec"] for x in before["tenants"]}
    a = {(x["namespace"], x["name"]): x["spec"] for x in after["tenants"]}
    added, removed = sorted(set(a) - set(b)), sorted(set(b) - set(a))
    check(added == [(ns, name)] and not removed, f"exactly one Tenant added ({added}), none removed ({removed})")
    changed = sorted(k for k in b if k in a and a[k] != b[k])
    check(not changed, "existing Tenants' specs unchanged" + (f": {changed}" if changed else ""))
    for key in sorted(set(before) | set(after)):
        if key not in ("tenants", "namespaces"):
            check(before.get(key) == after.get(key), f"{key} unchanged since setup")

    uid = t["metadata"]["uid"]
    for kind, child in (("daemonset", f"{name}-kerberator-daemon"), ("configmap", f"{name}-roster"),
                        ("secret", f"{name}-keytabs"), ("role", f"{name}-kerberator-daemon-events"),
                        ("rolebinding", f"{name}-kerberator-daemon-events")):
        def owned():
            r = c.k("-n", ns, "get", kind, child, "-o", "jsonpath={.metadata.ownerReferences[*].uid}")
            return r.returncode == 0 and uid in r.stdout.split()
        check(wait(owned, 60), f"{kind}/{child} exists and is owned by the Tenant", soft=True)

    ds_name = f"{name}-kerberator-daemon"
    sel = f"app.kubernetes.io/name=kerberator-daemon,app.kubernetes.io/instance={name}"
    if spec["daemon"] == "ready":
        def ready():
            st = c.json("-n", ns, "get", "daemonset", ds_name).get("status", {})
            want = st.get("desiredNumberScheduled", 0)
            tn = c.json("-n", ns, "get", "tenants.kerberator.dell.com", name)
            return want > 0 and st.get("numberReady") == want and condition(tn, "DaemonSetReady") == "True"
        check(wait(ready, timeout), "daemon Ready on every scheduled node and DaemonSetReady=True", soft=True)
    else:
        def pulling_fails():
            reasons = [((cs.get("state") or {}).get("waiting") or {}).get("reason")
                       for pod in c.json("-n", ns, "get", "pods", "-l", sel).get("items", [])
                       for cs in (pod.get("status") or {}).get("containerStatuses") or []]
            return bool(reasons) and all(r in PULL_ERRORS for r in reasons)
        check(wait(pulling_fails, min(timeout, 180)), "daemon pods cannot pull the image (ErrImagePull/ImagePullBackOff)", soft=True)
        tn = c.json("-n", ns, "get", "tenants.kerberator.dell.com", name)
        check(condition(tn, "DaemonSetReady") != "True", "DaemonSetReady is not True")
    return failures, uncertain, checks


def write_private(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".op02-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


# --------------------------------------------------------------------------
# Standard suite entry point: verify(scenario, setup_result, environment) -> CheckResult
# Runs main() above in-process (same bounded waits); the CLI is unchanged.

def verify(scenario, setup_result, environment):
    """SYSTEM result: Tenant created as specified, or cluster unchanged."""
    env = envlib.normalize(environment)
    if setup_result.get("status") != "READY":
        return inconclusive("setup_not_ready", f"setup status was {setup_result.get('status')}; nothing to verify")
    ref = setup_result.get("baseline_ref")
    if not ref or not Path(ref).is_file():
        return inconclusive("baseline_missing", "setup_result.baseline_ref does not point at a baseline file")
    base = json.loads(Path(ref).read_text(encoding="utf-8"))
    out = envlib.artifact_path(env, scenario.get("suite_id") or setup_result.get("suite_id"),
                               scenario["case_id"], "verification.json")
    out.unlink(missing_ok=True)
    argv = ["--case-id", scenario["case_id"], "--baseline", ref, "--context", str(base.get("context")),
            "--output", str(out)]
    if env.get("verify_timeout_seconds") is not None:
        argv += ["--timeout", str(int(env["verify_timeout_seconds"]))]
    with envlib.kube_env(env):
        code, _, err = legacy.call_main(main, argv)
    if code is None or not out.is_file():
        return inconclusive("verification_error", f"verify could not complete: {err}")
    report = json.loads(out.read_text(encoding="utf-8"))
    failures, refs = set(report.get("failures", [])), [ref, str(out)]
    findings = [Finding(rule_id(c["check"])[:64], "PASS" if c["ok"] else ("FAIL" if c["check"] in failures
                        else "INCONCLUSIVE"), c["check"], refs) for c in report.get("checks", [])]
    listed = {c["check"] for c in report.get("checks", [])}
    findings += [Finding(rule_id(m)[:64], "FAIL", m, refs) for m in report.get("failures", []) if m not in listed]
    findings += [Finding(rule_id(m)[:64], "INCONCLUSIVE", m, refs)
                 for m in report.get("uncertainties", []) if m not in listed]
    return CheckResult.from_findings(findings)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--case-id", required=True)
    p.add_argument("--baseline", type=Path, required=True)
    p.add_argument("--context", required=True, help="must equal the context recorded in the baseline")
    p.add_argument("--timeout", type=int, default=300, help="seconds to wait for the operator and daemon")
    p.add_argument("--output", type=Path)
    args = p.parse_args(argv)

    _, case = fixture.load_case(args.case_id)
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    if baseline.get("case_id") != args.case_id:
        p.error("Baseline case ID mismatch")
    if baseline.get("setup_status") != "READY":
        p.error("Baseline setup is not READY; fix preconditions first")
    if args.context != baseline.get("context"):
        p.error("Context must equal the one recorded in the baseline")

    c = fixture.Cluster(args.context)
    spec = case["expected"]["verify"]
    if spec["type"] == "no-change":
        failures, uncertain, checks = no_change(c, baseline, args.timeout)
    elif spec["type"] == "tenant-created":
        failures, uncertain, checks = tenant_created(c, case, baseline, spec, args.timeout)
    else:
        raise ValueError(f"unknown verify type {spec['type']}")

    result = "FAIL" if failures else ("INCONCLUSIVE" if uncertain else "PASS")
    report = {"case_id": args.case_id, "verification_status": result, "verify_type": spec["type"],
              "checks": [{"check": m, "ok": ok} for m, ok in checks],
              "failures": failures, "uncertainties": uncertain,
              "note": "Checks the real cluster only, never the agent's claim. No Secret contents read."}
    path = args.output or (HERE / "results" / f"{args.case_id}-verification.json")
    write_private(path, report)
    print(json.dumps({"case_id": args.case_id, "verification_status": result, "report": str(path),
                      "failures": failures, "uncertainties": uncertain}, indent=2))
    return 1 if result == "FAIL" else (2 if result == "INCONCLUSIVE" else 0)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as e:
        print(f"VERIFICATION ERROR: {e}", file=sys.stderr)
        sys.exit(3)
