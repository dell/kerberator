#!/usr/bin/env python3
# tests/test-diagnose.py — exercises scripts/diagnose.py against a stub `kubectl`
# that serves canned JSON from a fixture directory. No cluster needed.
# Run: python3 tests/test-diagnose.py

import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

HERE = pathlib.Path(__file__).resolve().parent
SCRIPT = HERE.parent / "scripts" / "diagnose.py"

# Fake kubectl. Serves files from $FX; a missing file is NotFound, a file
# named <x>.forbidden makes that call fail with an RBAC error. Every call is
# appended to $FX/calls.log so tests can assert what was (not) run.
STUB_BASH = r"""#!/usr/bin/env bash
echo "$*" >> "$FX/calls.log"
serve() { # file kind name
  if [[ -e "$FX/$1.forbidden" ]]; then echo "Error from server (Forbidden): $2 is forbidden: cannot get resource"; exit 1; fi
  if [[ -e "$FX/$1" ]]; then cat "$FX/$1"; exit 0; fi
  echo "Error from server (NotFound): $2 \"$3\" not found"; exit 1
}
if [[ $1 == config ]]; then
  case "$2" in
    current-context) echo dev ;;
    get-contexts) if [[ " $* " == *" -o name "* ]]; then echo dev; else echo "CURRENT NAME"; fi ;;
    view) echo "dev  https://127.0.0.1:6443" ;;
  esac
  exit 0
fi
args=" $* "
if [[ $1 == krb ]]; then
  case "$2" in
    version) echo v0 ;;
    describe)
      if [[ -e "$FX/tenant.json" && $4 == demo && ( $args != *" -n "* || $args == *" -n demo-ns "* ) ]]; then
        echo "Tenant CR:         demo-ns/$4"; exit 0
      fi
      echo "Error: get tenant default/$4: tenants.kerberator.dell.com \"$4\" not found"; exit 1 ;;
    list) serve list.txt tenants all ;;
  esac
  exit 0
fi
if [[ $1 == logs ]]; then
  if [[ $args == *kerberator-daemon* ]]; then serve daemonlogs.txt pods daemon; else serve logs.txt pods operator; fi
fi
kind="$2"
case "$kind" in
  tenants.kerberator.dell.com) serve tenant.json tenants.kerberator.dell.com "$3" ;;
  deployments)                 serve deploy.json deployments x ;;
  daemonset)                   serve ds.json daemonsets.apps "$3" ;;
  nodes)                       serve nodes.json nodes x ;;
  events)                      serve events.json events x ;;
  principals.kerberator.dell.com) serve principals.json principals x ;;
  configmap)                   serve cm.json configmaps "$3" ;;
  secret)                      serve "secret-$3.txt" secrets "$3" ;;
  service)                     serve kdcsvc.json services "$3" ;;
  endpointslices)              serve eps.json endpointslices x ;;
  pods)
    if [[ $args == *"kerberator-daemon"* ]]; then serve pods.json pods x
    elif [[ $args == *"app=kdc"* ]]; then serve kdcpods.json pods x
    else serve oppods.json pods x; fi ;;
esac
echo "stub: unhandled: $*" >&2; exit 1
"""

WORK = pathlib.Path(tempfile.mkdtemp())
(WORK / "bin").mkdir()
kc = WORK / "bin" / "kubectl"
kc.write_text(STUB_BASH)
kc.chmod(0o755)


def cleanup():
    shutil.rmtree(WORK, ignore_errors=True)


# ---- fixture time anchors ----
NOW_DT = datetime.now(timezone.utc).replace(microsecond=0)
OLD_DT = NOW_DT - timedelta(minutes=10)
NOW = NOW_DT.strftime("%Y-%m-%dT%H:%M:%SZ")
OLD = OLD_DT.strftime("%Y-%m-%dT%H:%M:%SZ")

KRB5 = ("[libdefaults]\n  default_realm = EXAMPLE.COM\n"
        "[realms]\n  EXAMPLE.COM = {\n    kdc = kdc.example.com\n  }\n")


def _jwrite(path, obj):
    path.write_text(json.dumps(obj))


def _jread(path):
    return json.loads(path.read_text())


def _jmut(path, fn):
    """Load JSON, apply fn (mutates in place), write back. Any return value is discarded."""
    obj = _jread(path)
    fn(obj)
    _jwrite(path, obj)


def base(d: pathlib.Path):
    """Healthy base fixture: Tenant demo-ns/demo, realm EXAMPLE.COM, 2 nodes,
    Principal svc-2001 (uid 2001) with keytab Secret svc-2001-keytab/keytab."""
    d.mkdir(parents=True, exist_ok=True)
    _jwrite(d / "tenant.json", {
        "metadata": {"name": "demo", "namespace": "demo-ns"},
        "spec": {"realm": "EXAMPLE.COM", "daemon": {"krb5Conf": KRB5}},
        "status": {
            "desiredNodes": 2, "readyNodes": 2, "readySummary": "2/2",
            "principalCount": 1,
            "conditions": [
                {"type": "RosterConsistent", "status": "True", "reason": "AllUIDsUnique", "message": ""},
                {"type": "KeytabsResolved",  "status": "True", "reason": "AllKeytabsResolved", "message": ""},
                {"type": "DaemonSetReady",   "status": "True", "reason": "AllPodsReady", "message": ""},
                {"type": "Ready",            "status": "True", "reason": "Reconciled", "message": ""},
            ],
        },
    })
    _jwrite(d / "deploy.json", {"items": [{
        "metadata": {"namespace": "kerberator-system", "name": "kerberator"},
        "spec": {"replicas": 1},
        "status": {"readyReplicas": 1, "conditions": [{"type": "Available", "status": "True"}]},
    }]})
    _jwrite(d / "ds.json", {
        "spec": {"template": {"spec": {"containers": [{"image": "ghcr.io/dell/kerberator-daemon:v1"}]}}},
        "status": {"desiredNumberScheduled": 2, "numberReady": 2,
                   "updatedNumberScheduled": 2, "numberMisscheduled": 0},
    })
    _jwrite(d / "pods.json", {"items": [
        {"metadata": {"name": "demo-kerberator-daemon-aaaaa"},
         "spec": {"nodeName": "worker-1"},
         "status": {"phase": "Running",
                    "conditions": [{"type": "Ready", "status": "True"}],
                    "containerStatuses": [{"restartCount": 0, "state": {"running": {}}}]}},
        {"metadata": {"name": "demo-kerberator-daemon-bbbbb"},
         "spec": {"nodeName": "worker-2"},
         "status": {"phase": "Running",
                    "conditions": [{"type": "Ready", "status": "True"}],
                    "containerStatuses": [{"restartCount": 0, "state": {"running": {}}}]}},
    ]})
    _jwrite(d / "events.json", {"items": [
        {"involvedObject": {"kind": "Pod", "name": "demo-kerberator-daemon-aaaaa"},
         "reason": "KerberosMintSucceeded", "type": "Normal",
         "message": "minted uid=2001",
         "metadata": {"annotations": {"kerberator.dell.com/uid": "2001"}},
         "lastTimestamp": NOW},
        {"involvedObject": {"kind": "Pod", "name": "demo-kerberator-daemon-bbbbb"},
         "reason": "KerberosMintSucceeded", "type": "Normal",
         "message": "minted uid=2001",
         "metadata": {"annotations": {"kerberator.dell.com/uid": "2001"}},
         "lastTimestamp": NOW},
    ]})
    _jwrite(d / "principals.json", {"items": [{
        "metadata": {"name": "svc-2001"},
        "spec": {"uid": 2001, "tenantRef": {"name": "demo"},
                 "keytabSecretRef": {"name": "svc-2001-keytab", "key": "keytab"}},
        "status": {"conditions": [{"type": "Ready", "status": "True",
                                   "reason": "AllDaemonsReady", "message": "2/2"}]},
    }]})
    (d / "secret-svc-2001-keytab.txt").write_text("keytab 120\n")
    _jwrite(d / "cm.json", {"data": {"users.roster": "2001 svc-2001@EXAMPLE.COM\n",
                                     "krb5.conf": KRB5}})
    (d / "list.txt").write_text(
        "NAMESPACE KIND NAME TENANT/UID READY NODES AGE\n"
        "demo-ns Tenant demo EXAMPLE.COM True 2/2 1h\n")
    (d / "logs.txt").write_text('{"level":"info","msg":"reconciled","kcf":"demo-ns/demo"}\n')
    (d / "daemonlogs.txt").write_text(
        "INFO: Configuration -> Interval: 30m\n"
        "ERROR: Failed to mint cache for svc-2001@EXAMPLE.COM: kinit failed\n")


def fx(name):
    """Create a fresh fixture dir seeded with the healthy base."""
    d = WORK / name
    if d.exists():
        shutil.rmtree(d)
    base(d)
    return d


# ---- fixture mutators ----
def set_cond(d, t, status, reason, message):
    def m(j):
        for c in j["status"]["conditions"]:
            if c.get("type") == t:
                c["status"] = status
                c["reason"] = reason
                c["message"] = message
    _jmut(d / "tenant.json", m)


def ds_ready(d, desired, ready):
    def m(j):
        j["status"]["desiredNumberScheduled"] = desired
        j["status"]["numberReady"] = ready
        j["status"]["updatedNumberScheduled"] = desired
    _jmut(d / "ds.json", m)


def pod0(d, fn):
    """Apply fn to items[0] of pods.json."""
    _jmut(d / "pods.json", lambda j: fn(j["items"][0]))


def add_event(d, evtype, reason, kind, ename, message, ts=None):
    ts = ts or NOW
    _jmut(d / "events.json", lambda j: j["items"].append({
        "involvedObject": {"kind": kind, "name": ename},
        "type": evtype, "reason": reason, "message": message,
        "count": 3, "metadata": {}, "lastTimestamp": ts,
    }))


def incluster(d, n, kdc_started_at):
    """Point krb5.conf at an in-cluster KDC with `n` ready endpoints."""
    k = KRB5.replace("kdc.example.com", "kdc.demo-ns.svc.cluster.local:88")
    _jmut(d / "tenant.json", lambda j: j["spec"]["daemon"].update({"krb5Conf": k}))
    _jmut(d / "cm.json", lambda j: j["data"].update({"krb5.conf": k}))
    _jwrite(d / "kdcsvc.json", {"spec": {"selector": {"app": "kdc"}}})
    _jwrite(d / "eps.json", {"items": [{"endpoints": [{"conditions": {"ready": True}} for _ in range(n)]}]})
    _jwrite(d / "kdcpods.json", {"items": [{
        "metadata": {"name": "kdc-abc"},
        "status": {"conditions": [{"type": "Ready", "status": "True" if n > 0 else "False"}],
                   "containerStatuses": [{"restartCount": 4,
                                           "state": {"running": {"startedAt": kdc_started_at}}}]},
    }]})
    # daemon pod container start, used for next-retry estimate
    def m(j):
        for p in j["items"]:
            p["status"]["containerStatuses"][0]["state"] = {"running": {"startedAt": OLD}}
    _jmut(d / "pods.json", m)


def mintfail(d, failure_ts):
    """Newest event for uid 2001 on pod aaaaa is a failure at <ts>."""
    _jmut(d / "events.json", lambda j: j["items"].pop(0))
    add_event(d, "Warning", "KerberosMintFailed", "Pod", "demo-kerberator-daemon-aaaaa",
              "kinit failed for uid=2001: kinit: Resource temporarily unavailable while getting initial credentials",
              failure_ts)
    _jmut(d / "events.json", lambda j: j["items"][-1]["metadata"].update(
        {"annotations": {"kerberator.dell.com/uid": "2001"}}))


# ---- harness ----
pass_n = 0
fail_n = 0
OUT = ""
RC = 0


def run(d, *args):
    global OUT, RC
    (d / "calls.log").write_text("")
    env = os.environ.copy()
    env["PATH"] = f"{WORK}/bin:{env['PATH']}"
    env["FX"] = str(d)
    r = subprocess.run([sys.executable, str(SCRIPT), *args],
                       capture_output=True, text=True, env=env)
    OUT = r.stdout + r.stderr
    RC = r.returncode


def ok(msg):
    global pass_n
    pass_n += 1
    print(f"  ok    {msg}")


def bad(msg):
    global fail_n
    fail_n += 1
    print(f"  FAIL  {msg}")
    for line in OUT.splitlines()[:40]:
        print(f"        | {line}")


def expect(name, want_rc, want_status, want_primary, *patterns):
    show = os.environ.get("SHOW", "")
    if show and show in name:
        for line in OUT.splitlines():
            print(f"        > {line}")
    if RC != want_rc:
        bad(f"{name}: exit {RC}, want {want_rc}"); return
    if not re.search(rf"^STATUS: {want_status}$", OUT, re.M):
        bad(f"{name}: want STATUS: {want_status}"); return
    if want_primary == "-":
        if re.search(r"^PRIMARY:", OUT, re.M):
            bad(f"{name}: unexpected PRIMARY"); return
    else:
        if not re.search(rf"^PRIMARY: {want_primary}$", OUT, re.M):
            bad(f"{name}: want PRIMARY: {want_primary}"); return
    for p in patterns:
        if p.startswith("!"):
            if re.search(p[1:], OUT, re.M):
                bad(f"{name}: output should not match '{p[1:]}'"); return
        else:
            if not re.search(p, OUT, re.M):
                bad(f"{name}: output should match '{p}'"); return
    ok(name)


def grep_calls(d, pattern):
    return any(re.search(pattern, ln) for ln in (d / "calls.log").read_text().splitlines())


print("diagnose.py tests")

try:
    # ===== healthy base =====
    d = fx("healthy"); run(d, "tenant", "demo", "-n", "demo-ns")
    expect("healthy tenant", 0, "healthy", "-",
           r"^CONTEXT: dev$", r"^CHECKED-OK: .*pod-image-pull", r"!^FINDING: cause")
    if grep_calls(d, r"^logs "):
        bad("healthy: operator logs must not be read")
    else:
        ok("healthy: operator logs not read")
    log = (d / "calls.log").read_text().splitlines()
    bad_secret = [ln for ln in log if ln.startswith("get secret") and "go-template=" not in ln]
    if bad_secret:
        bad("secrets read with a value-revealing format")
    else:
        ok("secrets only read via key-name/size go-template")

    # ===== image pull =====
    d = fx("image"); ds_ready(d, 2, 1)
    set_cond(d, "DaemonSetReady", "False", "SomePodsNotReady", "1/2 ready")
    def _im(p):
        p["status"]["conditions"][0]["status"] = "False"
        p["status"]["phase"] = "Pending"
        p["status"]["containerStatuses"][0]["state"] = {"waiting": {"reason": "ImagePullBackOff",
                                                                     "message": 'Back-off pulling image "bad:tag"'}}
    pod0(d, _im)
    run(d, "tenant", "demo", "-n", "demo-ns")
    expect("image pull back-off", 4, "not-ready", "pod-image-pull",
           r"FINDING: cause pod-image-pull \| demo-kerberator-daemon-aaaaa on worker-1",
           r"HINT: Tenant Ready=True Reconciled only means")

    # ===== crash loop =====
    d = fx("crash"); ds_ready(d, 2, 1)
    def _cr(p):
        p["status"]["conditions"][0]["status"] = "False"
        p["status"]["containerStatuses"][0] = {
            "restartCount": 7,
            "state": {"waiting": {"reason": "CrashLoopBackOff"}},
            "lastState": {"terminated": {"reason": "Error", "exitCode": 1}},
        }
    pod0(d, _cr)
    run(d, "tenant", "demo", "-n", "demo-ns")
    expect("crash loop", 4, "not-ready", "pod-crash-loop",
           r"restarts=7, last=Error\(exit 1\)")

    # ===== unschedulable =====
    d = fx("sched"); ds_ready(d, 2, 1)
    def _sc(p):
        p["spec"]["nodeName"] = None
        p["status"] = {"phase": "Pending",
                       "conditions": [{"type": "PodScheduled", "status": "False",
                                        "message": "0/3 nodes are available: insufficient memory"}]}
    pod0(d, _sc)
    add_event(d, "Warning", "FailedScheduling", "Pod", "demo-kerberator-daemon-aaaaa",
              "0/3 nodes are available: insufficient memory")
    run(d, "tenant", "demo", "-n", "demo-ns")
    expect("unschedulable pod", 4, "not-ready", "pod-unschedulable", r"insufficient memory")

    # ===== mount failure =====
    d = fx("mount"); ds_ready(d, 2, 1)
    def _mf(p):
        p["status"]["conditions"][0]["status"] = "False"
        p["status"]["containerStatuses"][0]["state"] = {"waiting": {"reason": "ContainerCreating"}}
    pod0(d, _mf)
    add_event(d, "Warning", "FailedMount", "Pod", "demo-kerberator-daemon-aaaaa",
              'MountVolume.SetUp failed for volume "keytabs": secret "demo-keytabs" not found')
    run(d, "tenant", "demo", "-n", "demo-ns")
    expect("mount failure", 4, "not-ready", "pod-mount-failed",
           r'secret "demo-keytabs" not found')

    # ===== FailedCreate =====
    d = fx("create"); ds_ready(d, 2, 0)
    _jmut(d / "pods.json", lambda j: j.__setitem__("items", []))
    add_event(d, "Warning", "FailedCreate", "DaemonSet", "demo-kerberator-daemon",
              'pods "x" is forbidden: violates PodSecurity "restricted:latest": hostPath volumes')
    run(d, "tenant", "demo", "-n", "demo-ns")
    expect("DaemonSet FailedCreate", 4, "not-ready", "pod-create-failed", r"PodSecurity")

    # ===== no eligible nodes =====
    d = fx("nonodes"); ds_ready(d, 0, 0)
    _jmut(d / "pods.json", lambda j: j.__setitem__("items", []))
    _jmut(d / "tenant.json", lambda j: j["spec"]["daemon"].update({"nodeSelector": {"kerberos": "yes"}}))
    set_cond(d, "DaemonSetReady", "False", "SomePodsNotReady", "0/0 ready")
    _jwrite(d / "nodes.json", {"items": [{"metadata": {"name": "worker-1", "labels": {}}, "spec": {}}]})
    run(d, "tenant", "demo", "-n", "demo-ns")
    expect("no eligible nodes", 4, "not-ready", "no-eligible-nodes", r"selector-match=false")

    # ===== DaemonSet missing (reads operator logs) =====
    d = fx("nods"); (d / "ds.json").unlink()
    _jmut(d / "pods.json", lambda j: j.__setitem__("items", []))
    set_cond(d, "DaemonSetReady", "Unknown", "DaemonSetNotObserved", "")
    run(d, "tenant", "demo", "-n", "demo-ns")
    expect("DaemonSet missing (reads operator logs)", 4, "not-ready", "daemonset-missing",
           r"== operator logs")

    # ===== keytab Secret missing =====
    d = fx("nosecret"); (d / "secret-svc-2001-keytab.txt").unlink()
    set_cond(d, "KeytabsResolved", "False", "SomeKeytabsMissing",
             "one or more principal KeytabSecretRefs could not be resolved")
    run(d, "tenant", "demo", "-n", "demo-ns")
    expect("keytab Secret missing", 4, "not-ready", "keytab-secret-missing",
           r"Secret demo-ns/svc-2001-keytab does not exist",
           r"FINDING: cause keytabs-unresolved")

    # ===== keytab key missing =====
    d = fx("nokey"); (d / "secret-svc-2001-keytab.txt").write_text("svc.keytab 120\n")
    run(d, "tenant", "demo", "-n", "demo-ns")
    expect("keytab key missing", 4, "not-ready", "keytab-key-missing",
           r"no key 'keytab' \(keys: svc.keytab\)")

    # ===== duplicate UID =====
    d = fx("dup"); set_cond(d, "RosterConsistent", "False", "DuplicateUID", "duplicate uids: [2001]")
    run(d, "tenant", "demo", "-n", "demo-ns")
    expect("duplicate UID", 4, "not-ready", "duplicate-uid", r"duplicate uids: \[2001\]")

    # ===== mint failed (latest event wins) =====
    d = fx("mint")
    add_event(d, "Warning", "KerberosMintFailed", "Pod", "demo-kerberator-daemon-aaaaa",
              "kinit failed uid=2001: Cannot contact any KDC for realm 'EXAMPLE.COM'")
    _jmut(d / "events.json", lambda j: j["items"][0].__setitem__("lastTimestamp", OLD))
    run(d, "tenant", "demo", "-n", "demo-ns")
    expect("mint failed (latest event wins)", 4, "not-ready", "mint-failed",
           r"uid=2001 on demo-kerberator-daemon-aaaaa", r"Cannot contact any KDC")

    # ===== probe failing because mint failed =====
    d = fx("probe"); ds_ready(d, 2, 1); set_cond(d, "DaemonSetReady", "False", "SomePodsNotReady", "1/2 ready")
    pod0(d, lambda p: p["status"]["conditions"][0].__setitem__("status", "False"))
    add_event(d, "Warning", "Unhealthy", "Pod", "demo-kerberator-daemon-aaaaa",
              "Readiness probe failed: readyz: missing ccache for uid=2001 at /tmp/krb5cc_2001")
    add_event(d, "Warning", "KerberosMintFailed", "Pod", "demo-kerberator-daemon-aaaaa",
              "kinit failed for uid=2001: kinit: Resource temporarily unavailable while getting initial credentials")
    _jmut(d / "events.json", lambda j: j["items"][0].__setitem__("lastTimestamp", OLD))
    run(d, "tenant", "demo", "-n", "demo-ns")
    expect("probe failing because mint failed -> mint-failed is primary",
           4, "not-ready", "mint-failed",
           r"FINDING: cause pod-not-ready .*missing ccache")

    # ===== in-cluster KDC no ready endpoints =====
    d = fx("kdcdown"); incluster(d, 0, OLD); mintfail(d, NOW)
    run(d, "tenant", "demo", "-n", "demo-ns")
    expect("in-cluster KDC with no ready endpoints", 4, "not-ready", "kdc-not-ready",
           r"FINDING: cause mint-failed",
           r"== KDC kdc.demo-ns.svc.cluster.local:88 -> Service demo-ns/kdc, ready endpoints: 0")

    # ===== in-cluster KDC Service missing =====
    d = fx("kdcgone"); incluster(d, 1, OLD); (d / "kdcsvc.json").unlink(); mintfail(d, NOW)
    run(d, "tenant", "demo", "-n", "demo-ns")
    expect("in-cluster KDC Service missing", 4, "not-ready", "kdc-service-missing",
           r"Service demo-ns/kdc does not exist")

    # ===== KDC came up after failure =====
    d = fx("kdcstale"); incluster(d, 1, NOW); mintfail(d, OLD)
    run(d, "tenant", "demo", "-n", "demo-ns")
    expect("KDC came up after the failure -> stale-failure warning",
           4, "not-ready", "mint-failed",
           r"FINDING: warn mint-failure-stale .*retries only every 30m",
           r"== daemon logs: demo-ns/demo-kerberator-daemon-aaaaa",
           r"Interval: 30m")

    # ===== healthy in-cluster KDC =====
    d = fx("kdcok"); incluster(d, 1, OLD)
    run(d, "tenant", "demo", "-n", "demo-ns")
    expect("healthy in-cluster KDC", 0, "healthy", "-",
           r"!FINDING: .* kdc-", r"ready endpoints: 1")
    if grep_calls(d, r"logs demo-kerberator"):
        bad("healthy: daemon logs must not be read")
    else:
        ok("healthy: daemon logs not read")

    # ===== external KDC info only =====
    d = fx("kdcext"); run(d, "tenant", "demo", "-n", "demo-ns")
    expect("external KDC is info only", 0, "healthy", "-",
           r"FINDING: info kdc-external \| KDC kdc.example.com is outside the cluster")

    # ===== crash loop reads previous logs =====
    d = fx("crashlogs"); ds_ready(d, 2, 1)
    def _cl(p):
        p["status"]["conditions"][0]["status"] = "False"
        p["status"]["containerStatuses"][0] = {
            "restartCount": 3, "state": {"waiting": {"reason": "CrashLoopBackOff"}}}
    pod0(d, _cl)
    run(d, "tenant", "demo", "-n", "demo-ns")
    if grep_calls(d, r"logs demo-kerberator-daemon-aaaaa -n demo-ns --tail=30 --previous"):
        ok("crash loop reads the previous container's logs")
    else:
        bad("crash loop: previous logs not read")
    run(d, "tenant", "demo", "-n", "demo-ns", "--no-logs")
    if grep_calls(d, r"^logs "):
        bad("--no-logs still read daemon logs")
    else:
        ok("--no-logs skips daemon logs too")

    # ===== old failure superseded by newer success =====
    d = fx("recovered")
    add_event(d, "Warning", "KerberosMintFailed", "Pod", "demo-kerberator-daemon-aaaaa",
              "kinit failed uid=2001: timeout", OLD)
    run(d, "tenant", "demo", "-n", "demo-ns")
    expect("old failure superseded by newer success", 0, "healthy", "-",
           r"!FINDING: cause mint-failed")

    # ===== empty krb5Conf alone =====
    d = fx("emptykrb5")
    _jmut(d / "tenant.json", lambda j: j["spec"]["daemon"].update({"krb5Conf": ""}))
    _jmut(d / "cm.json", lambda j: j["data"].pop("krb5.conf"))
    run(d, "tenant", "demo", "-n", "demo-ns")
    expect("empty krb5Conf alone is a warning", 0, "healthy-with-warnings", "-",
           r"FINDING: warn krb5conf-empty")
    add_event(d, "Warning", "KerberosMintFailed", "Pod", "demo-kerberator-daemon-aaaaa",
              "kinit failed uid=2001: Cannot find KDC")
    _jmut(d / "events.json", lambda j: j["items"][0].__setitem__("lastTimestamp", OLD))
    run(d, "tenant", "demo", "-n", "demo-ns")
    expect("empty krb5Conf + mint failure is a cause", 4, "not-ready", "krb5conf-empty",
           r"FINDING: cause mint-failed")

    # ===== realm mismatch =====
    d = fx("realm")
    k2 = KRB5.replace("default_realm = EXAMPLE.COM", "default_realm = OTHER.ORG")
    _jmut(d / "tenant.json", lambda j: j["spec"]["daemon"].update({"krb5Conf": k2}))
    _jmut(d / "cm.json", lambda j: j["data"].update({"krb5.conf": k2}))
    run(d, "tenant", "demo", "-n", "demo-ns")
    expect("realm mismatch", 0, "healthy-with-warnings", "-",
           r"FINDING: warn realm-mismatch \| krb5Conf default_realm=OTHER.ORG but spec.realm=EXAMPLE.COM")

    # ===== config not synced -> operator logs read =====
    d = fx("unsynced")
    _jmut(d / "cm.json", lambda j: j["data"].update({"krb5.conf": "stale"}))
    with (d / "logs.txt").open("a") as f:
        f.write('{"level":"error","msg":"reconcile tenant","kcf":"demo-ns/demo","error":"upsert daemonset: admission webhook denied"}\n')
    run(d, "tenant", "demo", "-n", "demo-ns")
    expect("spec not applied -> logs read, error surfaced",
           4, "not-ready", "operator-reconcile-error",
           r"FINDING: warn config-not-synced", r"admission webhook denied")

    # ===== no conditions =====
    d = fx("noconds")
    _jmut(d / "tenant.json", lambda j: j.__setitem__("status", {}))
    with (d / "logs.txt").open("a") as f:
        f.write('{"level":"error","msg":"reconcile tenant","kcf":"demo-ns/demo","error":"upsert roster cm: configmaps is forbidden"}\n')
    run(d, "tenant", "demo", "-n", "demo-ns")
    expect("no conditions -> reconcile not completing",
           4, "not-ready", "operator-reconcile-error",
           r"FINDING: cause reconcile-not-completing",
           r"HINT: Tenant Ready condition is absent",
           r"FINDING: cause operator-reconcile-error .*configmaps is forbidden")
    run(d, "tenant", "demo", "-n", "demo-ns", "--no-logs")
    if grep_calls(d, r"^logs "):
        bad("--no-logs still read logs")
    else:
        ok("--no-logs skips operator logs")

    # ===== operator down =====
    d = fx("opdown"); _jmut(d / "tenant.json", lambda j: j.__setitem__("status", {}))
    _jmut(d / "deploy.json", lambda j: j["items"][0].update({
        "status": {"readyReplicas": 0,
                   "conditions": [{"type": "Available", "status": "False"}]}}))
    _jwrite(d / "oppods.json", {"items": [{
        "metadata": {"name": "kerberator-abc"},
        "status": {"phase": "Pending",
                   "containerStatuses": [{"state": {"waiting": {"reason": "ImagePullBackOff",
                                                                 "message": "no such image"}}}]},
    }]})
    run(d, "tenant", "demo", "-n", "demo-ns")
    expect("operator down", 4, "not-ready", "operator-down",
           r"kerberator-system/kerberator ready=0/1",
           r"kerberator-abc Pending ImagePullBackOff")

    # ===== disabled tenant =====
    d = fx("disabled")
    _jmut(d / "tenant.json", lambda j: j["spec"].update({"disabled": True}))
    run(d, "tenant", "demo", "-n", "demo-ns")
    expect("disabled tenant", 4, "not-ready", "tenant-disabled", r"intentionally empty")

    # ===== events forbidden =====
    d = fx("evforbid")
    (d / "events.json").rename(d / "events.json.forbidden")
    run(d, "tenant", "demo", "-n", "demo-ns")
    expect("events forbidden -> unknown, not absent", 0, "healthy-with-warnings", "-",
           r"FINDING: unknown events \| could not list events in demo-ns \(forbidden\)")

    # ===== operator list forbidden =====
    d = fx("opforbid")
    (d / "deploy.json").rename(d / "deploy.json.forbidden")
    run(d, "tenant", "demo", "-n", "demo-ns")
    expect("operator list forbidden -> unknown", 0, "healthy-with-warnings", "-",
           r"FINDING: unknown operator-health")

    # ===== tenant not found anywhere =====
    d = fx("missing"); run(d, "tenant", "nope", "-n", "demo-ns")
    expect("tenant not found anywhere", 2, "not-found", "-",
           r"no Tenant .nope. in any namespace")

    # ===== tenant in another namespace =====
    d = fx("otherns"); run(d, "tenant", "demo", "-n", "wrong-ns")
    expect("tenant in another namespace", 2, r"not-found-in-ns\(demo-ns\)", "-")

    # ===== unverified =====
    d = fx("unverified")
    (d / "list.txt").rename(d / "list.txt.forbidden")
    run(d, "tenant", "demo", "-n", "wrong-ns")
    expect("not found, other namespaces unsearchable", 2, "not-found-unverified", "-",
           r"could NOT search other namespaces")

    # ===== missing tenant name -> usage =====
    d = fx("usage"); run(d, "tenant")
    if RC == 64:
        ok("missing tenant name -> usage (exit 64)")
    else:
        bad(f"missing tenant name: exit {RC}, want 64")

    # ===== node targeting: taint =====
    d = fx("taint"); ds_ready(d, 0, 0)
    _jmut(d / "pods.json", lambda j: j.__setitem__("items", []))
    _jmut(d / "tenant.json", lambda j: j["spec"]["daemon"].update({"nodeSelector": {"kerberos": "yes"}}))
    set_cond(d, "DaemonSetReady", "False", "SomePodsNotReady", "0/0 ready")
    _jwrite(d / "nodes.json", {"items": [
        {"metadata": {"name": "cp", "labels": {"kerberos": "yes"}},
         "spec": {"taints": [{"key": "node-role.kubernetes.io/control-plane", "effect": "NoSchedule"}]}},
        {"metadata": {"name": "worker-1", "labels": {}}, "spec": {}},
    ]})
    run(d, "tenant", "demo", "-n", "demo-ns")
    expect("only matching node is tainted -> names the taint, says tolerations",
           4, "not-ready", "no-eligible-nodes",
           r"none accept the daemon \(cp tainted node-role.kubernetes.io/control-plane=:NoSchedule\): add spec.daemon.tolerations")

    _jmut(d / "ds.json", lambda j: j["spec"]["template"]["spec"].update(
        {"tolerations": [{"key": "node-role.kubernetes.io/control-plane",
                          "operator": "Exists", "effect": "NoSchedule"}]}))
    run(d, "tenant", "demo", "-n", "demo-ns")
    expect("tolerated taint is not blamed", 4, "not-ready", "no-eligible-nodes",
           r"cp\tselector-match=true\tunschedulable=false\tuntolerated-taints=$",
           r"!none accept the daemon")

    # ===== subset =====
    d = fx("subset")
    _jmut(d / "tenant.json", lambda j: j["spec"]["daemon"].update({"nodeSelector": {"kerberos": "yes"}}))
    _jwrite(d / "nodes.json", {"items": [
        {"metadata": {"name": "worker-1", "labels": {"kerberos": "yes"}}, "spec": {}},
        {"metadata": {"name": "worker-2", "labels": {"kerberos": "yes"}}, "spec": {}},
        {"metadata": {"name": "worker-3", "labels": {}}, "spec": {}},
    ]})
    run(d, "tenant", "demo", "-n", "demo-ns")
    expect("Tenant covers a node subset by design -> healthy, info",
           0, "healthy", "-",
           r"info daemon-node-subset .*matches 2 of 3 node")

    (d / "nodes.json").rename(d / "nodes.json.forbidden")
    run(d, "tenant", "demo", "-n", "demo-ns")
    expect("subset selector, nodes unreadable -> no extra unknown",
           0, "healthy", "-", r"!unknown nodes")

    # ===== Principal NoEligibleNodes =====
    d = fx("noelig")
    def _ne(j):
        j["items"][0]["spec"]["nodeSelector"] = {"zone": "eu-west"}
        j["items"][0]["status"]["conditions"][0] = {
            "type": "Ready", "status": "False", "reason": "NoEligibleNodes",
            "message": "spec.nodeSelector matches zero cluster nodes; principal will not be minted anywhere"}
    _jmut(d / "principals.json", _ne)
    run(d, "tenant", "demo", "-n", "demo-ns")
    expect("Principal NoEligibleNodes -> its own warning naming the selector",
           0, "healthy-with-warnings", "-",
           r'warn principal-no-eligible-nodes \| svc-2001 \(uid 2001\): spec.nodeSelector \{"zone":"eu-west"\}',
           r"!principal-not-ready")

    # ===== stale Principal status =====
    def stale_fx(d):
        _jmut(d / "principals.json", lambda j: j["items"][0].__setitem__("status", {
            "nodes": [{"name": "worker-1", "daemonPodReady": True},
                      {"name": "worker-2", "daemonPodReady": True}],
            "conditions": [{"type": "Ready", "status": "False",
                             "reason": "SomeDaemonsNotReady",
                             "message": "0/2 eligible daemon pods ready"}],
        }))
        def m(j):
            for ev in j["items"]:
                ev["lastTimestamp"] = OLD
        _jmut(d / "events.json", m)

    d = fx("stale"); stale_fx(d); run(d, "tenant", "demo", "-n", "demo-ns")
    expect("stale Principal status -> info, Tenant healthy",
           0, "healthy", "-",
           r"info principal-status-stale \| svc-2001 \(uid 2001\): Ready=False SomeDaemonsNotReady, but every daemon pod is Ready",
           r"!principal-not-ready")

    d = fx("stalefail"); stale_fx(d)
    _jmut(d / "events.json", lambda j: j["items"][1].__setitem__("reason", "KerberosMintFailed"))
    run(d, "tenant", "demo", "-n", "demo-ns")
    expect("last Event is a failure -> not excused as stale",
           4, "not-ready", "mint-failed", r"!principal-status-stale")

    d = fx("stalerenew"); stale_fx(d)
    _jmut(d / "tenant.json", lambda j: j["spec"]["daemon"].update({"renewMinutes": 30}))
    run(d, "tenant", "demo", "-n", "demo-ns")
    expect("renewMinutes set -> a real warning",
           0, "healthy-with-warnings", "-",
           r"warn principal-not-ready", r"!principal-status-stale")

    d = fx("stalefresh"); stale_fx(d)
    _jmut(d / "events.json", lambda j: j["items"][0].__setitem__("lastTimestamp", NOW))
    run(d, "tenant", "demo", "-n", "demo-ns")
    expect("a mint under 5 minutes old -> not explained as stale",
           0, "healthy-with-warnings", "-",
           r"warn principal-not-ready", r"!principal-status-stale")

    # ===== leftovers that outlive the problem =====
    d = fx("deadpod")
    add_event(d, "Warning", "KerberosMintFailed", "Pod", "demo-kerberator-daemon-gone1",
              "kinit failed for uid=2001: Password incorrect", NOW)
    _jmut(d / "events.json", lambda j: j["items"][-1]["metadata"].update(
        {"annotations": {"kerberator.dell.com/uid": "2001"}}))
    run(d, "tenant", "demo", "-n", "demo-ns")
    expect("mint failure from a deleted pod is history, not a cause",
           0, "healthy", "-", r"!cause mint-failed", r"demo-kerberator-daemon-gone1")

    d = fx("oldcreate")
    _jmut(d / "ds.json", lambda j: j["status"].update({"currentNumberScheduled": 2}))
    add_event(d, "Warning", "FailedCreate", "DaemonSet", "demo-kerberator-daemon",
              'pods "x" is forbidden: violates PodSecurity', OLD)
    run(d, "tenant", "demo", "-n", "demo-ns")
    expect("old FailedCreate with all pods present is ignored",
           0, "healthy", "-", r"!cause pod-create-failed", r"CHECKED-OK: .*pod-create-failed")

    d = fx("terminating")
    def _tr(j):
        j["items"][1]["metadata"]["deletionTimestamp"] = "2026-01-01T00:00:00Z"
        j["items"][1]["status"]["phase"] = "Succeeded"
        j["items"][1]["status"]["conditions"][0]["status"] = "False"
    _jmut(d / "pods.json", _tr)
    run(d, "tenant", "demo", "-n", "demo-ns")
    expect("terminating pod is shown, not reported as a fault",
           0, "healthy", "-", r"!cause pod-not-ready",
           r"demo-kerberator-daemon-bbbbb\tworker-2\tTerminating")

    # ===== unreadable != ruled out =====
    d = fx("opunverified")
    (d / "deploy.json").rename(d / "deploy.json.forbidden")
    run(d, "tenant", "demo", "-n", "demo-ns")
    expect("operator unreadable -> operator-down not in CHECKED-OK",
           0, "healthy-with-warnings", "-",
           r"FINDING: unknown operator-health",
           r"!CHECKED-OK: .*operator-down",
           r"CHECKED-OK: .*daemonset-missing")

    d = fx("kdcunverified"); incluster(d, 1, OLD)
    (d / "kdcsvc.json").rename(d / "kdcsvc.json.forbidden")
    run(d, "tenant", "demo", "-n", "demo-ns")
    expect("KDC Service unreadable -> kdc checks not in CHECKED-OK",
           0, "healthy-with-warnings", "-",
           r"FINDING: unknown kdc",
           r"!CHECKED-OK: .*kdc-not-ready",
           r"!CHECKED-OK: .*kdc-service-missing")

finally:
    cleanup()

print()
print(f"passed: {pass_n}  failed: {fail_n}")
sys.exit(0 if fail_n == 0 else 1)
