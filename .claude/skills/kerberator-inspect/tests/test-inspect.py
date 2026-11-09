#!/usr/bin/env python3
# tests/test-inspect.py — exercises scripts/inspect.py against a stub `kubectl`.
# No cluster or kubectl-krb needed. Run: python3 tests/test-inspect.py

import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
SCRIPT = HERE.parent / "scripts" / "inspect.py"

# Fake `kubectl`: returns canned output chosen by its arguments. The fake
# cluster has Tenant `demo` and Principal `uid-2001` (UID 2001) in `demo-ns`.
# Env knobs: STUB_CONTEXTS (context names), STUB_CURRENT (current context),
# STUB_ALL_FORBIDDEN (make `list -A` fail with an RBAC error).
STUB_BASH = r"""#!/usr/bin/env bash
# `kubectl config ...`: local kubeconfig reads used by `contexts` and CONTEXT:.
if [[ $1 == config ]]; then
  case "$2" in
    current-context) echo "${STUB_CURRENT:-dev}" ;;
    get-contexts)
      if [[ " $* " == *" -o name "* ]]; then printf '%s\n' ${STUB_CONTEXTS:-dev}
      else echo "CURRENT   NAME   CLUSTER   AUTHINFO   NAMESPACE"; fi ;;
    view) echo "dev  https://127.0.0.1:6443" ;;
  esac
  exit 0
fi
# `kubectl krb <sub> ...`: drop "krb", take the subcommand, keep the rest as one string.
shift; sub=$1; shift; args="$*"
case "$sub" in
  version) echo v0 ;;
  list)
    if [[ $args == *-A* && -n $STUB_ALL_FORBIDDEN ]]; then
      echo 'tenants.kerberator.dell.com is forbidden: cannot list resource "tenants" at the cluster scope'; exit 1
    elif [[ $args == *-A* ]]; then
      printf 'NAMESPACE KIND NAME TENANT/UID READY NODES AGE\ndemo-ns Tenant demo EXAMPLE.COM True 3/3 7m\ndemo-ns   Principal uid-2001 2001 True 3/3 7m\n'
    elif [[ $args == *"-n empty"* || $args == *"--tenant "* ]]; then echo "No Kerberator resources found."
    else printf 'NAMESPACE KIND NAME\nd Tenant demo\n'; fi ;;
  describe)
    if [[ $args == *"-n demo-ns"* ]]; then printf 'Principal CR:  demo-ns/uid-2001\n  KVNO: <not observed>\n'; exit 0; fi
    if [[ $args == *forbid* ]]; then echo 'tenants.kerberator.dell.com "x" is forbidden: cannot get resource'; exit 1; fi
    echo "get $1 default/$2: not found"; exit 1 ;;
esac
"""


def make_stub_dir():
    d = pathlib.Path(tempfile.mkdtemp())
    (d / "bin").mkdir()
    kc = d / "bin" / "kubectl"
    kc.write_text(STUB_BASH)
    kc.chmod(0o755)
    return d


STUB = make_stub_dir()

pass_n = 0
fail_n = 0


def _run(args, env_extra=None):
    env = os.environ.copy()
    env["PATH"] = f"{STUB}/bin:{env['PATH']}"
    if env_extra:
        env.update(env_extra)
    r = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, env=env,
    )
    return r.returncode, r.stdout + r.stderr


def check(name, want_rc, want_status_prefix, *args, env_extra=None):
    """Run the script and compare exit code + first-line prefix."""
    global pass_n, fail_n
    rc, out = _run(list(args), env_extra=env_extra)
    first = (out.splitlines() or [""])[0]
    if rc == want_rc and first.startswith(want_status_prefix):
        pass_n += 1
        print(f"ok   - {name}")
    else:
        fail_n += 1
        print(f"FAIL - {name} (rc={rc} want {want_rc}; got '{first}' want '{want_status_prefix}*')")


def _assert(cond, name):
    global pass_n, fail_n
    if cond:
        pass_n += 1
        print(f"ok   - {name}")
    else:
        fail_n += 1
        print(f"FAIL - {name}")


try:
    # Status for each main path: list, describe, missing resources, RBAC, bad usage.
    check("list ok",                    0, "STATUS: ok",                        "list")
    check("list empty namespace",       0, "STATUS: empty",                     "list", "-n", "empty")
    check("list -A ok",                 0, "STATUS: ok",                        "list", "-A")
    check("list --tenant in other ns",  2, "STATUS: not-found-in-ns(demo-ns)",  "list", "-n", "x", "--tenant", "demo")
    check("list --tenant nowhere",      2, "STATUS: not-found",                 "list", "-n", "x", "--tenant", "ghost")
    check("describe principal ok",      0, "STATUS: ok",                        "describe", "principal", "uid-2001", "-n", "demo-ns")
    check("describe by UID -> by-name", 2, "STATUS: not-found-by-name",         "describe", "principal", "2001")
    check("describe by CR name elsewhere", 2, "STATUS: not-found-in-ns(demo-ns)", "describe", "principal", "uid-2001")
    check("describe tenant in other ns",   2, "STATUS: not-found-in-ns(demo-ns)", "describe", "tenant", "demo")
    check("describe tenant missing",    2, "STATUS: not-found",                 "describe", "tenant", "nope")
    check("describe forbidden",         3, "STATUS: forbidden",                 "describe", "tenant", "forbid")
    check("describe bad kind",         64, "unknown kind",                      "describe", "foo", "x")
    check("no args prints usage",      64, "inspect.py")

    # cluster (context) selection
    check("contexts, one cluster",      0, "STATUS: ok",                        "contexts")

    _, out = _run(["contexts"])
    _assert(re.search(r"(?m)^HINT: contexts=1:", out) is not None,
            "one context -> use it")

    _, out = _run(["contexts"], env_extra={"STUB_CONTEXTS": "dev staging prod"})
    _assert(re.search(r"(?m)^HINT: contexts=3: .*ask which to inspect", out) is not None,
            "several contexts -> ask")

    _, out = _run(["list"])
    second = out.splitlines()[1] if len(out.splitlines()) > 1 else ""
    _assert(second == "CONTEXT: dev",
            "CONTEXT line shows current context")

    _, out = _run(["list", "--context", "prod"])
    second = out.splitlines()[1] if len(out.splitlines()) > 1 else ""
    _assert(second == "CONTEXT: prod",
            "CONTEXT line shows --context")

    # cluster-wide listing denied: absence elsewhere must be reported as unknown
    check("describe missing, -A forbidden", 2, "STATUS: not-found-unverified",
          "describe", "tenant", "nope",
          env_extra={"STUB_ALL_FORBIDDEN": "1"})
    check("list --tenant, -A forbidden",    2, "STATUS: not-found-unverified",
          "list", "-n", "x", "--tenant", "ghost",
          env_extra={"STUB_ALL_FORBIDDEN": "1"})
    _, out = _run(["describe", "tenant", "nope"],
                  env_extra={"STUB_ALL_FORBIDDEN": "1"})
    _assert(re.search(r"(?m)^HINT: .*could NOT search other namespaces \(list -A: forbidden\)", out) is not None,
            "unverified hint says why")

    # Hint content: warning markers in describe output become HINTs, and the
    # by-name hint names the real CR so Claude knows what to describe next.
    _, out = _run(["describe", "principal", "uid-2001", "-n", "demo-ns"])
    _assert(re.search(r"(?m)^HINT: KVNO not observed", out) is not None,
            "<not observed> hint")

    _, out = _run(["describe", "principal", "2001"])
    _assert(re.search(r"(?m)^HINT: .*UID 2001: demo-ns/uid-2001", out) is not None,
            "by-name hint names the CR")
finally:
    shutil.rmtree(STUB)

print()
print(f"passed={pass_n} failed={fail_n}")
sys.exit(0 if fail_n == 0 else 1)
