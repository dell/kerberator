"""lib.py: shared settings and helpers for the DG-06 test scripts.

Imported by the other scripts; not meant to be run directly.

Everything targets the `make quickstart` environment: kind cluster
kind-kerberator, namespace kerberator-demo, Tenant demo, in-cluster MIT KDC
for realm EXAMPLE.COM, Principal svc-2001. Never point these at a real KDC.

These scripts never read Secret data. The only Secret fields they read are
metadata.resourceVersion values, which carry no key material. Keytabs are
written to the Secret by piping `kubectl create --dry-run` into
`kubectl apply`, with output discarded.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

REQUIRED_CONTEXT = "kind-kerberator"
NS = "kerberator-demo"
CR = "svc-2001"                         # Principal CR name
KP = "svc-2001@EXAMPLE.COM"             # Kerberos principal
WRONG_REALM = "WRONG.REALM"             # used only by d3, on the Tenant
DECOY_KP = "svc-2002@EXAMPLE.COM"       # used only by d4
SECRET = "svc-2001-keytab"
SECRET_KEY = "svc-2001.keytab"
TENANT = "demo"
DS = f"{TENANT}-kerberator-daemon"
AGG_SECRET = f"{TENANT}-keytabs"        # the operator's aggregate of keytab Secrets
ROSTER_CM = f"{TENANT}-roster"          # the operator copies krb5Conf into this

HERE = Path(__file__).resolve().parent
REPO = Path(subprocess.run(["git", "-C", str(HERE), "rev-parse", "--show-toplevel"],
                           stdout=subprocess.PIPE, text=True, check=True).stdout.strip())
COLLECT = REPO / ".claude/skills/kerberator-diagnose-auth-failure/scripts/collect.py"
QUICKSTART_TENANT = REPO / "examples/quickstart/tenant.yaml"  # known-good Tenant spec

# Work dir lives outside the repo so the agent under test does not stumble
# on the answer key while exploring the project.
WORK_DIR = Path(os.environ.get("DG06_WORK_DIR", Path.home() / ".dg06-work"))
KEY_DIR = WORK_DIR / "answer-key"
RESULT_DIR = WORK_DIR / "results"
RUN_DIR = WORK_DIR / "runs"


class CommandFailed(Exception):
    def __init__(self, args, returncode, stderr):
        super().__init__(f"command failed ({returncode}): {' '.join(args)}")
        self.cmd = args
        self.returncode = returncode
        self.stderr = stderr


def die(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(2)


def step(msg):
    print(f"== {msg}")


def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sh(args, *, input=None, check=True, env=None, cwd=None, binary=False):
    """Run a command and return its stdout. With check, a non-zero exit raises
    CommandFailed (main() prints its stderr and exits with its status)."""
    if isinstance(input, str):
        input = input.encode()
    p = subprocess.run(args, input=input, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, env=env, cwd=cwd)
    if check and p.returncode != 0:
        raise CommandFailed(args, p.returncode, p.stderr.decode(errors="replace"))
    return p.stdout if binary else p.stdout.decode(errors="replace")


def kc(*args, **kw):
    return sh(["kubectl", "-n", NS, *args], **kw)


def kdc(*args, **kw):
    return kc("exec", "deploy/kdc", "--", *args, **kw)


def kadmin(query, **kw):
    return kdc("kadmin.local", "-q", query, **kw)


def main(fn):
    """Run a script's main function, turning a failed command into its exit
    status, as `set -e` would."""
    try:
        sys.exit(fn(sys.argv[1:]) or 0)
    except CommandFailed as e:
        sys.stderr.write(e.stderr)
        print(f"error: {e}", file=sys.stderr)
        sys.exit(e.returncode or 1)


def guard_env():
    """Safety rail: refuse to run anywhere but the quickstart cluster."""
    if not shutil.which("kubectl"):
        die("kubectl not on PATH")
    ctx = sh(["kubectl", "config", "current-context"]).strip()
    if ctx != REQUIRED_CONTEXT:
        die(f"current context is '{ctx}'; these tests only run on {REQUIRED_CONTEXT}")
    try:
        kc("get", "deploy/kdc")
    except CommandFailed:
        die(f"no demo KDC in {NS}; run 'make quickstart' first")


def kdc_kvno(principal):
    """The KDC's current KVNO, "absent" if the principal does not exist,
    "unavailable" if the KDC can't be reached (d1)."""
    try:
        out = kadmin(f"getprinc {principal}")
    except CommandFailed:
        return "unavailable"
    for line in out.splitlines():
        f = line.split()
        if line.startswith("Key: vno") and len(f) >= 3:
            return f[2].replace(",", "") or "absent"
    return "absent"


def keytab_listing(path):
    """Entries of a keytab inside the KDC pod, without key material."""
    return kdc("klist", "-k", "-t", "-e", path)


def highest_kvno(listing):
    """The highest KVNO in a `klist -k -t -e` listing."""
    nums = [int(line.split()[0]) for line in listing.splitlines()
            if line.split() and line.split()[0].isdigit()]
    return str(max(nums)) if nums else ""


def secret_rv(name):
    return kc("get", "secret", name, "-o", "jsonpath={.metadata.resourceVersion}", check=False)


def load_secret(path):
    """Replace the Principal's keytab Secret with a file from the KDC pod, then
    wait until the operator has re-aggregated it. The daemon mounts the
    aggregate, so re-minting sooner would use the old keytab and show the wrong
    symptom. Setup only; the agent must never do this."""
    agg_rv = secret_rv(AGG_SECRET)
    data = kdc("cat", path, binary=True)
    with tempfile.NamedTemporaryFile() as tmp:
        tmp.write(data)
        tmp.flush()
        manifest = kc("create", "secret", "generic", SECRET,
                      f"--from-file={SECRET_KEY}={tmp.name}",
                      "--dry-run=client", "-o", "yaml")
    kc("apply", "-f", "-", input=manifest)
    wait_for(f"operator to re-aggregate {AGG_SECRET}", 60, lambda: secret_rv(AGG_SECRET) != agg_rv)


def remint():
    """Restart the daemons so every node runs kinit with the current Secret.
    The daemon only re-mints on start, on a roster change, or every 30 minutes.
    A daemon with no ticket file stays unready, so a timeout here is not fatal;
    callers check the outcome themselves."""
    kc("rollout", "restart", f"daemonset/{DS}")
    try:
        kc("rollout", "status", f"daemonset/{DS}", "--timeout=120s")
    except CommandFailed:
        print("  note: daemon rollout not Ready after 120s (expected if no ticket file exists yet)")


def principal_field(jsonpath):
    return kc("get", f"principals.kerberator.dell.com/{CR}", "-o", f"jsonpath={jsonpath}", check=False)


def tenant_field(jsonpath):
    return kc("get", f"tenants.kerberator.dell.com/{TENANT}", "-o", f"jsonpath={jsonpath}", check=False)


def wait_for(what, secs, pred, *args):
    """Poll pred(*args) every 3s until it is true or secs have passed."""
    for _ in range(0, secs, 3):
        try:
            if pred(*args):
                return True
        except (CommandFailed, ValueError, KeyError, TypeError):
            pass
        time.sleep(3)
    print(f"  WARN  timed out after {secs}s waiting for: {what}", file=sys.stderr)
    return False


def nodes_failing():
    return "MintFailed" in principal_field("{.status.nodes[*].reason}")


def secret_kvno_is(kvno):
    return principal_field("{.status.kvnoFromSecret}") == kvno


def ready_status():
    return principal_field('{.status.conditions[?(@.type=="Ready")].status}')


def principal_ready():
    return ready_status() == "True"


def kdc_answers():
    try:
        kadmin("listprincs")
        return True
    except CommandFailed:
        return False


def _node_rows():
    out = principal_field('{range .status.nodes[*]}{.reason}|{.lastObserved}{"\\n"}{end}')
    return [line.split("|", 1) + [""] for line in out.splitlines() if line.split("|", 1)[0]]


def nodes_minted_since(since):
    """Every node reports Minted from a mint at or after the timestamp, so a
    stale Minted from before a restart doesn't count."""
    rows = _node_rows()
    return bool(rows) and all(r == "Minted" and t >= since for r, t, *_ in rows)


def nodes_observed_since(since):
    """Every node reports a mint attempt, of either outcome, from at or after
    the timestamp."""
    rows = _node_rows()
    return bool(rows) and all((r == "Minted" or "MintFailed" in r) and t >= since for r, t, *_ in rows)


def known_good_tenant():
    """The quickstart Tenant as a dict."""
    return json.loads(sh(["kubectl", "create", "--dry-run=client", "-f", str(QUICKSTART_TENANT), "-o", "json"]))


def restore_tenant():
    """Set realm and krb5Conf back to the quickstart values. A merge patch
    leaves other fields alone, and changes nothing if they already match."""
    spec = known_good_tenant()["spec"]
    patch = {"spec": {"realm": spec["realm"], "daemon": {"krb5Conf": spec["daemon"]["krb5Conf"]}}}
    kc("patch", f"tenants.kerberator.dell.com/{TENANT}", "--type=merge", "-p", json.dumps(patch))


def generated_krb5_conf():
    return json.loads(kc("get", "configmap", ROSTER_CM, "-o", "json"))["data"]["krb5.conf"]


def tenant_restored():
    """The Tenant's realm and krb5Conf, and the krb5.conf the operator generated
    into the roster ConfigMap, all match the quickstart values."""
    want = known_good_tenant()["spec"]
    have = json.loads(kc("get", f"tenants.kerberator.dell.com/{TENANT}", "-o", "json"))["spec"]
    want_conf = want["daemon"]["krb5Conf"]
    return (have.get("realm") == want["realm"]
            and have.get("daemon", {}).get("krb5Conf") == want_conf
            and generated_krb5_conf() == want_conf)


def krb_list_shows_1of1():
    """`kubectl krb list` reports the Principal Ready, NODES 1/1."""
    out = sh(["kubectl", "krb", "list", "-n", NS])
    for line in out.splitlines():
        f = line.split()
        if len(f) >= 6 and f[1] == "Principal" and f[2] == CR and f[4] == "True" and f[5] == "1/1":
            return True
    return False


def daemon_counts():
    """"<ready>/<desired>" for the Tenant's DaemonSet."""
    return kc("get", f"daemonset/{DS}", "-o",
              "jsonpath={.status.numberReady}/{.status.desiredNumberScheduled}", check=False)


def healthy_1of1():
    """Ready=True, every node Minted, and all daemon pods ready."""
    if not principal_ready():
        return False
    ready, _, desired = daemon_counts().partition("/")
    if not (ready == desired and ready.isdigit() and int(ready) >= 1):
        return False
    return "MintFailed" not in principal_field("{.status.nodes[*].reason}")


def indent(text, prefix="  "):
    return "".join(prefix + line + "\n" for line in text.splitlines())
