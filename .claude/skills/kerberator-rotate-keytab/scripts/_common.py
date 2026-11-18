"""Shared helpers for the kerberator-rotate-keytab scripts.

Imported by precheck.py, rotate.py and verify.py; not meant to be run
directly. Python 3.8+, standard library only.
"""

import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone


def die(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(2)


class Checks:
    """One printed line per check, so a transcript (or a tester) can see
    exactly which evidence passed and which did not."""

    def __init__(self):
        self.failed = False
        self.warned = False

    def ok(self, msg):
        print(f"  OK    {msg}")

    def warn(self, msg):
        print(f"  WARN  {msg}")
        self.warned = True

    def fail(self, msg):
        print(f"  FAIL  {msg}")
        self.failed = True

    def info(self, msg):
        print(f"  INFO  {msg}")


def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ts_norm(t):
    """Make a Kubernetes RFC3339 UTC timestamp sort correctly as a string:
    drop the trailing Z and any fractional seconds."""
    if t.endswith("Z"):
        t = t[:-1]
    return t.split(".", 1)[0]


def ts_ge(a, b):
    """True if timestamp a is at or after timestamp b. An empty or "null" a
    (kubectl jsonpath prints unset fields as null) is never after."""
    return bool(a) and a != "null" and not ts_norm(a) < ts_norm(b)


def env_check():
    kubeconfig = os.environ.get("KUBECONFIG", "")
    if kubeconfig:
        # KUBECONFIG may list several files separated by ':' (kubectl merges
        # them), so it is enough that one of them is readable.
        if not any(f and os.access(f, os.R_OK) for f in kubeconfig.split(":")):
            die(f"none of the files in KUBECONFIG ({kubeconfig}) is readable")
    elif not os.access(os.path.expanduser("~/.kube/config"), os.R_OK):
        die("no kubeconfig: add 'export KUBECONFIG=\"$HOME/<LAST_NAME>.kubeconfig\"' "
            "to ~/.bashrc and start a new session")
    if not shutil.which("kubectl"):
        die("kubectl not found on PATH")
    if not shutil.which("kubectl-krb"):
        die("kubectl-krb not found on PATH; build it with: "
            "go build -o ~/bin/kubectl-krb ./cli/kubectl-krb/cmd/kubectl-krb")


class Args:
    """name (the Principal CR), from_file (new keytab path), ns_args, since,
    expect_kvno, timeout, confirmed. Each script checks the ones it needs."""

    def __init__(self):
        self.name = ""
        self.from_file = ""
        self.ns_args = []
        self.since = ""
        self.expect_kvno = ""
        self.timeout = ""
        self.confirmed = False


_VALUE_FLAGS = {
    "-n": "namespace", "--namespace": "namespace",
    "--from-file": "from_file", "--since": "since",
    "--expect-kvno": "expect_kvno", "--timeout": "timeout",
}


def parse_args(argv):
    a = Args()
    i = 0
    while i < len(argv):
        arg = argv[i]
        key = val = None
        if arg in _VALUE_FLAGS:
            if i + 1 >= len(argv) or not argv[i + 1]:
                die(f"{arg} needs a value")
            key, val = _VALUE_FLAGS[arg], argv[i + 1]
            i += 1
        elif "=" in arg and arg.split("=", 1)[0] in _VALUE_FLAGS and arg.startswith("--"):
            flag, val = arg.split("=", 1)
            key = _VALUE_FLAGS[flag]
        elif arg == "--confirmed":
            a.confirmed = True
        elif arg.startswith("-"):
            die(f"unknown flag: {arg}")
        else:
            if a.name:
                die(f"unexpected argument: {arg}")
            a.name = arg
        if key == "namespace":
            a.ns_args = ["-n", val]
        elif key:
            setattr(a, key, val)
        i += 1
    if not a.name:
        die("missing <principal> (the Principal CR name, e.g. uid-2001)")
    return a


def kc(args, *kubectl_args, merge_stderr=False):
    """Run kubectl in the chosen namespace. Returns (returncode, stdout) with
    trailing newlines stripped, like a shell command substitution."""
    p = subprocess.run(
        ["kubectl", *args.ns_args, *kubectl_args],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT if merge_stderr else subprocess.DEVNULL,
        universal_newlines=True,
    )
    return p.returncode, p.stdout.rstrip("\n")


def can_i(args, *what):
    return kc(args, "auth", "can-i", *what)[1] == "yes"


def principal_field(args, jsonpath):
    """One field of the Principal CR ('' if it can't be read)."""
    return kc(args, "get", "principals.kerberator.dell.com", args.name, "-o", f"jsonpath={jsonpath}")[1]


_ENTRY = re.compile(r"^[ \t]*[0-9]+[ \t]+[^ \t]", re.M)


def has_entries(out):
    return bool(_ENTRY.search(out))


def keytab_entries(path):
    """List a keytab's entries (KVNO, enctype, principal) without key
    material, or None if no tool can read it. MIT krb5 (most Linux) has
    `klist -k -t`; Heimdal (macOS) has `ktutil list` instead. Never pass
    -K / --keys: those print keys. Output only counts if it has at least one
    entry line (a line starting with a KVNO): macOS `klist -k -t` prints
    usage to stderr and exits 0."""
    for tool, cmd in (("klist", ["klist", "-k", "-t", path]),
                      ("ktutil", ["ktutil", "-k", path, "list"])):
        if not shutil.which(tool):
            continue
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           universal_newlines=True)
        out = p.stdout.rstrip("\n")
        if p.returncode == 0 and has_entries(out):
            return out
    return None
