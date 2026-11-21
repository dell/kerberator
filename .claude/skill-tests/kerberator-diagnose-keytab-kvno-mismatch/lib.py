"""lib.py: shared settings and helpers for the DG-03 test scripts.

Imported by the other scripts; not meant to be run directly.

Everything targets the `make quickstart` environment: kind cluster,
namespace kerberator-demo, Tenant demo, in-cluster MIT KDC for realm
EXAMPLE.COM, Principal svc-2001. Never point these scripts at a real KDC.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

NS = os.environ.get("DG03_NS", "kerberator-demo")
CR = "svc-2001"                         # Principal CR name
KP = "svc-2001@EXAMPLE.COM"             # Kerberos principal
DECOY_KP = "svc-2002@EXAMPLE.COM"       # used only by T2
SECRET = "svc-2001-keytab"
SECRET_KEY = "svc-2001.keytab"
TENANT = "demo"
DS = f"{TENANT}-kerberator-daemon"

HERE = Path(__file__).resolve().parent

# Work dir lives outside the repo so the agent under test does not stumble
# on the answer key while exploring the project.
WORK_DIR = Path(os.environ.get("DG03_WORK_DIR", Path.home() / ".dg03-work"))
KEY_DIR = WORK_DIR / "answer-key"
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
    """Safety rail: refuse to run anywhere but a kind cluster with the demo KDC."""
    if not shutil.which("kubectl"):
        die("kubectl not on PATH")
    ctx = sh(["kubectl", "config", "current-context"]).strip()
    if not ctx.startswith("kind-"):
        die(f"current context is '{ctx}'; these tests only run on a kind cluster (make quickstart)")
    try:
        kc("get", "deploy/kdc")
    except CommandFailed:
        die(f"no demo KDC in {NS}; run 'make quickstart' first")


def kdc_kvno(principal):
    """The KDC's current KVNO for a principal ("" if it can't be read)."""
    out = kdc("kadmin.local", "-q", f"getprinc {principal}", check=False)
    for line in out.splitlines():
        f = line.split()
        if line.startswith("Key: vno") and len(f) >= 3:
            return f[2].replace(",", "")
    return ""


def keytab_listing(path):
    """Entries of a keytab inside the KDC pod, without key material."""
    return kdc("klist", "-k", "-t", "-e", path)


def highest_kvno(listing):
    """The highest KVNO in a `klist -k -t -e` listing."""
    nums = [int(line.split()[0]) for line in listing.splitlines()
            if line.split() and line.split()[0].isdigit()]
    return str(max(nums)) if nums else ""


def load_secret(path):
    """Replace the Principal's keytab Secret with a file from the KDC pod.
    Setup only; the agent must never do this."""
    data = kdc("cat", path, binary=True)
    with tempfile.NamedTemporaryFile() as tmp:
        tmp.write(data)
        tmp.flush()
        manifest = kc("create", "secret", "generic", SECRET,
                      f"--from-file={SECRET_KEY}={tmp.name}",
                      "--dry-run=client", "-o", "yaml")
    kc("apply", "-f", "-", input=manifest)


def remint():
    """Restart the daemons so every node runs kinit with the current Secret."""
    kc("rollout", "restart", f"daemonset/{DS}")
    kc("rollout", "status", f"daemonset/{DS}", "--timeout=120s")


def principal_field(jsonpath):
    return kc("get", f"principals.kerberator.dell.com/{CR}", "-o", f"jsonpath={jsonpath}", check=False)


def wait_for(what, secs, pred, *args):
    """Poll pred(*args) every 3s until it is true or secs have passed."""
    for _ in range(0, secs, 3):
        try:
            if pred(*args):
                return True
        except CommandFailed:
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


def indent(text, prefix="  "):
    return "".join(prefix + line + "\n" for line in text.splitlines())
