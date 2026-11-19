#!/usr/bin/env python3
"""DG-02 evaluation setup: put a case's fault in place (optionally) and confirm it.

Fixture mode (default): read a saved collect.py output; never contact Kubernetes.
Live mode: an explicitly selected context; run the skill's collect.py read-only
and confirm the case's fixture_requirements (SIGNALS) hold.

Live mode can also change a DISPOSABLE cluster, only when asked and only on a
kind-* context (the `make quickstart` cluster):
  --reset    restore the quickstart baseline (svc-2001 healthy, KDC up)
  --inject   reset, then create this case's fault (e.g. a wrong keytab)
Neither ever runs against a non-kind context. Keytab bytes move from the KDC
pod into the Secret without being printed or stored.

Examples (from the repository root):
  S=.claude/skill-tests/suites/dg02_auth_failure
  python3 $S/setup.py --case-id dg02-05-wrong-keytab                      # fixture
  python3 $S/setup.py --case-id dg02-05-wrong-keytab --mode live \\
      --context kind-kerberator --inject                                   # create + confirm
  python3 $S/setup.py --case-id dg02-01-healthy --mode live \\
      --context kind-kerberator --reset                                    # back to baseline

Writes a private JSON baseline (default results/<case>-baseline.json). A
scenario that could not be confirmed is an INCONCLUSIVE setup, not a failed
agent evaluation. Exit: 0 READY, 2 INCONCLUSIVE, 1 setup error.
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
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]   # suites/<suite> -> skill-tests -> .claude -> repo root
DEFAULT_SKILL = ROOT / ".claude/skills/kerberator-diagnose-auth-failure/scripts/collect.py"
QUICKSTART = ROOT / "examples" / "quickstart"
REALM = "EXAMPLE.COM"
DECOY = f"svc-2002@{REALM}"
TENANT = "demo"
DS = f"{TENANT}-kerberator-daemon"
SECRET, SECRET_KEY = "svc-2001-keytab", "svc-2001.keytab"
EVAL_PREFIX = "dg02-"          # Principals created by --inject; --reset deletes them


def load_case(case_id):
    data = json.loads((HERE / "evals.json").read_text(encoding="utf-8"))
    case = next((c for c in data["cases"] if c["case_id"] == case_id), None)
    if case is None:
        raise ValueError(f"Unknown case ID: {case_id}")
    return data, case


def parse_output(output):
    """collect.py prints `== 8. SIGNALS ==` followed by `  key=value` lines."""
    signals = {}
    if "== 8. SIGNALS ==" in output:
        for line in output.split("== 8. SIGNALS ==", 1)[1].splitlines():
            m = re.match(r"^\s+([a-z_]+)=(.*)$", line)
            if m:
                signals[m.group(1)] = m.group(2).strip()
    ctx = re.search(r"^CONTEXT:\s*(.*?)\s*$", output, re.M)
    return {"context": ctx.group(1) if ctx else None, "signals": signals}


def requirement_issues(case, parsed):
    """Each fixture requirement is `signal=prefix`: the signal's value must start
    with that prefix (values carry trailing explanations after the number)."""
    issues = []
    if not parsed["signals"]:
        issues.append("No SIGNALS block in collect.py output")
        return issues
    for req in case["fixture_requirements"]:
        key, want = req.split("=", 1)
        got = parsed["signals"].get(key)
        if got is None:
            issues.append(f"Signal {key} missing")
        elif not got.startswith(want):
            issues.append(f"Expected {key}={want}..., got {key}={got}")
    return issues


# ---------------------------------------------------------------- live helpers

class Cluster:
    def __init__(self, context, namespace):
        self.base = ["kubectl", "--context", context, "-n", namespace]

    def k(self, *args, stdin=None, check=False, timeout=180):
        p = subprocess.run([*self.base, *args], input=stdin, capture_output=True,
                           text=isinstance(stdin, str) or stdin is None, timeout=timeout)
        if check and p.returncode != 0:
            raise RuntimeError(f"kubectl {' '.join(args[:3])} failed: {p.stderr.strip()[:300]}")
        return p

    def kadmin(self, query):
        # Evaluator-only: the agent is never allowed to exec. Same environment
        # as the KDC's bootstrap.sh (no KRB5_KDC_PROFILE), so the real database is used.
        return self.k("exec", "deploy/kdc", "--", "kadmin.local", "-q", query)

    def kdc_answers(self):
        return self.kadmin("listprincs").returncode == 0

    def addprinc(self, principal):
        r = self.kadmin(f"addprinc -randkey {principal}")
        text = r.stdout + r.stderr
        if r.returncode != 0 or ("already exists" not in text and "created" not in text):
            raise RuntimeError(f"addprinc {principal} failed: {text.strip()[-200:]}")

    def load_keytab(self, principal):
        """Export `principal`'s keytab in the KDC pod and put it in the Secret.
        The bytes go KDC -> temp file -> Secret; they are never printed."""
        self.k("exec", "deploy/kdc", "--", "rm", "-f", "/tmp/dg02.kt")
        r = self.kadmin(f"ktadd -k /tmp/dg02.kt {principal}")
        if r.returncode != 0:
            raise RuntimeError(f"ktadd {principal} failed")
        raw = subprocess.run([*self.base, "exec", "deploy/kdc", "--", "cat", "/tmp/dg02.kt"],
                             capture_output=True, timeout=60)
        self.k("exec", "deploy/kdc", "--", "rm", "-f", "/tmp/dg02.kt")
        if raw.returncode != 0 or not raw.stdout:
            raise RuntimeError("could not read the exported keytab")
        # A keytab starts with format version 0x05 0x01/0x02 and has at least one entry.
        if len(raw.stdout) < 16 or raw.stdout[0] != 5 or raw.stdout[1] not in (1, 2):
            raise RuntimeError("exported keytab is truncated or not a keytab")
        fd, tmp = tempfile.mkstemp(prefix=".dg02-kt-")
        try:
            try:
                os.write(fd, raw.stdout)
            finally:
                os.close(fd)
            manifest = subprocess.run(
                [*self.base, "create", "secret", "generic", SECRET,
                 f"--from-file={SECRET_KEY}={tmp}", "--dry-run=client", "-o", "json"],
                capture_output=True, text=True, timeout=60)
            if manifest.returncode != 0:
                raise RuntimeError("could not build the Secret manifest")
            self.k("apply", "-f", "-", stdin=manifest.stdout, check=True)
        finally:
            os.path.exists(tmp) and os.unlink(tmp)

    def remint(self):
        """The daemon re-mints on start, on roster change, or every 30 minutes."""
        self.k("rollout", "restart", f"daemonset/{DS}")
        self.k("rollout", "status", f"daemonset/{DS}", "--timeout=120s")

    def replace_quickstart(self, kind):
        manifest = str(QUICKSTART / f"{kind}.yaml")
        for _ in range(5):   # the operator may update the object at the same moment
            r = self.k("replace", "-f", manifest)
            if r.returncode == 0 or "Conflict" not in r.stderr:
                break
            time.sleep(2)
        if r.returncode != 0:
            r = self.k("create", "-f", manifest)
        if r.returncode != 0:
            raise RuntimeError(f"restore {kind} failed: {r.stderr.strip()[:300]}")


def reset(c):
    """svc-2001 healthy on the quickstart Tenant, KDC up, no eval-created objects."""
    c.replace_quickstart("tenant")
    c.replace_quickstart("principal")
    names = c.k("get", "principals.kerberator.dell.com", "-o", "jsonpath={.items[*].metadata.name}").stdout.split()
    for n in names:
        if n.startswith(EVAL_PREFIX):
            c.k("delete", "principals.kerberator.dell.com", n, "--wait=true", "--timeout=60s")
    c.k("scale", "deploy/kdc", "--replicas=1", check=True)
    c.k("rollout", "status", "deploy/kdc", "--timeout=180s", check=True)
    for _ in range(40):
        if c.kdc_answers():
            break
        time.sleep(3)
    else:
        raise RuntimeError("KDC did not come back")
    got = c.kadmin(f"getprinc svc-2001@{REALM}")
    if "does not exist" in got.stdout + got.stderr:
        c.addprinc(f"svc-2001@{REALM}")
    c.kadmin(f"delprinc -force {DECOY}")
    c.load_keytab(f"svc-2001@{REALM}")
    c.remint()


def principal_manifest(name, uid, kprinc):
    return json.dumps({
        "apiVersion": "kerberator.dell.com/v1alpha1", "kind": "Principal",
        "metadata": {"name": name},
        "spec": {"uid": uid, "principal": kprinc, "tenantRef": {"name": TENANT},
                 "keytabSecretRef": {"name": SECRET, "key": SECRET_KEY}}})


def inject(c, scenario):
    """Create the case's fault. Called after reset()."""
    if scenario in ("healthy_principal",):
        return
    if scenario == "kdc_unreachable":
        c.k("scale", "deploy/kdc", "--replicas=0", check=True)
        r = c.k("wait", "pod", "-l", "app=kdc", "--for=delete", "--timeout=120s")
        if r.returncode != 0 and "no matching resources" not in r.stderr.lower():
            raise RuntimeError(f"KDC pod did not go away: {r.stderr.strip()[:200]}")
        c.remint()
    elif scenario == "client_not_found":
        c.kadmin(f"delprinc -force svc-2001@{REALM}")
        c.remint()
    elif scenario == "realm_mismatch_no_failure":
        c.k("patch", "tenants.kerberator.dell.com", TENANT, "--type=merge",
            "-p", '{"spec":{"realm":"WRONG.REALM"}}', check=True)
        c.remint()
    elif scenario == "keytab_no_matching_key":
        c.addprinc(DECOY)
        c.load_keytab(DECOY)               # svc-2001's Secret now holds svc-2002's keytab
        c.remint()
    elif scenario in ("key_rejected_kdc_rekeyed", "leading_kvno_prompt"):
        c.kadmin(f"cpw -randkey svc-2001@{REALM}")   # the KDC moves on; the Secret keeps the old key
        c.remint()
    elif scenario == "keytab_secret_missing":
        c.k("delete", "secret", SECRET, check=True)
        c.remint()
    elif scenario == "duplicate_uid":
        c.k("create", "-f", "-", stdin=principal_manifest(f"{EVAL_PREFIX}dupuid", 2001, f"svc-2001@{REALM}"), check=True)
    elif scenario == "principal_disabled":
        c.k("patch", "principals.kerberator.dell.com", "svc-2001", "--type=merge",
            "-p", '{"spec":{"disabled":true,"disabledReason":"dg02 evaluation"}}', check=True)
    elif scenario == "no_eligible_nodes":
        c.k("patch", "principals.kerberator.dell.com", "svc-2001", "--type=merge",
            "-p", '{"spec":{"nodeSelector":{"dg02/none":"x"}}}', check=True)
    elif scenario == "shared_daemon_symptom":
        # uid 3003 points at svc-2001's keytab, which has no key for svc-3003.
        c.k("create", "-f", "-", stdin=principal_manifest(f"{EVAL_PREFIX}other", 3003, f"svc-3003@{REALM}"), check=True)
    else:
        raise ValueError(f"No injection defined for scenario {scenario}")


def collect(script, principal, namespace, context):
    run = subprocess.run([sys.executable, str(script), principal, "-n", namespace, "--context", context],
                         text=True, capture_output=True, timeout=120)
    # collect.py exits 3/4/5 for not-found/unreachable/unreadable; keep stdout either way.
    return run.stdout if "COLLECTED:" in run.stdout else run.stdout + "\n" + run.stderr


def atomic_private_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Never write full kubectl output, keytab material, or kubeconfig.
    fd, tmp = tempfile.mkstemp(prefix=".dg02-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(payload, file, indent=2)
            file.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--mode", choices=["fixture", "live"], default="fixture")
    parser.add_argument("--fixture-file", type=Path, help="default: fixtures/<case-id>.txt")
    parser.add_argument("--context", help="REQUIRED in live mode; never auto-select")
    parser.add_argument("--principal", default="svc-2001")
    parser.add_argument("--namespace", default="kerberator-demo")
    parser.add_argument("--skill-script", type=Path, default=DEFAULT_SKILL)
    parser.add_argument("--reset", action="store_true", help="live, kind-* only: restore the quickstart baseline")
    parser.add_argument("--inject", action="store_true", help="live, kind-* only: reset, then create this case's fault")
    parser.add_argument("--wait", type=int, default=240, help="seconds to wait for the fault to show in SIGNALS")
    parser.add_argument("--save-fixture", type=Path, help="live: also save collect.py output here")
    parser.add_argument("--output", type=Path, help="Private JSON baseline path")
    args = parser.parse_args()

    _, case = load_case(args.case_id)
    injected = False
    if args.mode == "fixture":
        if args.reset or args.inject:
            parser.error("--reset/--inject need --mode live")
        path = args.fixture_file or HERE / "fixtures" / f"{args.case_id}.txt"
        output = path.read_text(encoding="utf-8")
        parsed = parse_output(output)
        issues = requirement_issues(case, parsed)
    else:
        if not args.context:
            parser.error("--context required in live mode; no implicit cluster selection")
        if (args.reset or args.inject) and not args.context.startswith("kind-"):
            parser.error("--reset/--inject only run on a disposable kind-* context")
        script = args.skill_script.resolve()
        if not script.is_file():
            parser.error(f"collect.py not found: {script}")
        if args.reset or args.inject:
            c = Cluster(args.context, args.namespace)
            reset(c)
            if args.inject:
                inject(c, case["scenario"])
                injected = True
        # Poll: Kubernetes needs a moment for the daemon to re-mint and report.
        deadline = time.time() + (args.wait if (args.reset or args.inject) else 0)
        while True:
            output = collect(script, args.principal, args.namespace, args.context)
            parsed = parse_output(output)
            issues = requirement_issues(case, parsed)
            if args.reset and not args.inject:
                issues = requirement_issues(load_case("dg02-01-healthy")[1], parsed)
            if not issues or time.time() >= deadline:
                break
            time.sleep(10)
        if parsed["context"] != args.context:
            issues.append("Reported CONTEXT differs from the explicitly chosen context")
        if args.save_fixture:
            args.save_fixture.parent.mkdir(parents=True, exist_ok=True)
            args.save_fixture.write_text(output, encoding="utf-8")

    baseline = {
        "case_id": args.case_id,
        "scenario": case["scenario"],
        "mode": args.mode,
        # A plain --reset only establishes the healthy baseline, not this case's fault,
        # so verify.py (which requires READY) refuses it unless the case is the healthy one.
        "setup_status": ("INCONCLUSIVE" if issues else
                         "RESET-ONLY" if (args.reset and not args.inject and case["scenario"] != "healthy_principal")
                         else "READY"),
        "principal": args.principal,
        "namespace": args.namespace,
        "context": parsed["context"],
        "injected": injected,
        "observed": parsed,
        "output_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
        "issues": issues,
        "note": ("Fault injected on a disposable kind cluster by the evaluator." if injected else
                 "No mutation performed. This is a preflight check, not an agent grade."),
    }
    dest = args.output or (HERE / "results" / f"{args.case_id}-baseline.json")
    atomic_private_json(dest, baseline)
    print(json.dumps({"case_id": args.case_id, "setup_status": baseline["setup_status"],
                      "injected": injected, "baseline_path": str(dest), "issues": issues}, indent=2))
    return 0 if not issues else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        print(f"SETUP ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
