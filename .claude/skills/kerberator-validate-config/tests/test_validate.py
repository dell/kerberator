#!/usr/bin/env python3
"""test_validate.py — exercises scripts/validate.py against a stub `kubectl`.
No live cluster needed. Run: python3 tests/test_validate.py
"""

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

HERE = pathlib.Path(__file__).resolve().parent
SCRIPT = HERE.parent / "scripts" / "validate.py"

KRB5 = textwrap.dedent("""\
    [libdefaults]
      default_realm = EXAMPLE.COM
    [realms]
      EXAMPLE.COM = {
        kdc = kdc.example.com
      }
""")

# Stub kubectl.  Env vars:
#   STUB_LOG           — path to append every invocation (set by the test harness)
#   STUB_CRD_ERROR=1   — make the CRD check return a connection-refused error
# Handles enough surface area for all test cases:
#   get crd ...        → return both CRD names
#   get tenant/tenants → NotFound (Principals refer to Tenants in a separate ns)
#   get principals...  → empty list (no UID conflicts)
#   get nodes ...      → empty (no nodes in stub cluster)
#   get secret ...     → fake Secret with one key "keytab"
STUB_BASH = r"""#!/usr/bin/env bash
echo "$*" >> "${STUB_LOG:-/dev/null}"
ARGS="$*"

# --- CRD error simulation --------------------------------------------------
if [[ "${STUB_CRD_ERROR:-}" == "1" && "$ARGS" == *" crd "* ]]; then
  echo "The connection to the server 127.0.0.1:6443 was refused" >&2
  exit 1
fi

# --- CRD check -------------------------------------------------------------
if [[ "$ARGS" == *" crd "* ]]; then
  echo "principals.kerberator.dell.com tenants.kerberator.dell.com"
  exit 0
fi

# --- Tenant lookup (Principal -> tenantRef, not in-file) --------------------
if [[ "$ARGS" == *" tenant "* || "$ARGS" == *" tenants.kerberator.dell.com "* ]]; then
  echo 'Error from server (NotFound): tenants.kerberator.dell.com not found' >&2
  exit 1
fi

# --- Principals list (duplicate-uid check) ----------------------------------
if [[ "$ARGS" == *" principals"* ]]; then
  echo '{"items":[]}'
  exit 0
fi

# --- Nodes (nodeselector-matches and label-typo-suggest) --------------------
if [[ "$ARGS" == *" nodes"* ]]; then
  if [[ "$ARGS" == *"-o json"* && "$ARGS" != *"-o jsonpath"* ]]; then
    echo '{"items":[]}'
  fi
  # jsonpath call for node names: return empty stdout (0 nodes match)
  exit 0
fi

# --- Secret (keytab presence, keys only — never bytes) ----------------------
if [[ "$ARGS" == *" secret "* ]]; then
  if [[ "$ARGS" == *"-o go-template"* ]]; then
    # go-template call: return only key names (no values)
    echo 'keytab '
  elif [[ "$ARGS" == *"-o json"* && "$ARGS" != *"-o jsonpath"* ]]; then
    echo '{"data":{"keytab":"ZmFrZWtleXRhYg=="}}'
  else
    echo 'map[keytab:ZmFrZWtleXRhYg==]'
  fi
  exit 0
fi

echo "stub: unhandled: $*" >&2
exit 1
"""

WORK = pathlib.Path(tempfile.mkdtemp())
(WORK / "bin").mkdir()
_kc = WORK / "bin" / "kubectl"
_kc.write_text(STUB_BASH)
_kc.chmod(0o755)


def teardown():
    shutil.rmtree(WORK, ignore_errors=True)


def _run(yaml_text: str, *, extra_args: list | None = None,
         env_overrides: dict | None = None) -> tuple[str, int]:
    """Write yaml_text to a temp file, invoke validate.py, return (stdout, rc)."""
    f = tempfile.NamedTemporaryFile(suffix=".yaml", delete=False, mode="w")
    f.write(yaml_text)
    f.close()
    stub_log = tempfile.mktemp()
    env = {**os.environ, "PATH": f"{WORK / 'bin'}:{os.environ['PATH']}",
           "STUB_LOG": stub_log}
    if env_overrides:
        env.update(env_overrides)
    cmd = [sys.executable, str(SCRIPT), "--from-file", f.name, "-n", "demo-ns"]
    if extra_args:
        cmd += extra_args
    r = subprocess.run(cmd, capture_output=True, text=True, env=env)
    os.unlink(f.name)
    return r.stdout + r.stderr, r.returncode


def _status(out: str) -> str:
    for line in out.splitlines():
        if line.startswith("STATUS:"):
            return line.split(":", 1)[1].strip()
    return ""


def _has_finding(out: str, level: str, code: str) -> bool:
    prefix = f"FINDING: {level} {code}"
    return any(ln.startswith(prefix) for ln in out.splitlines())


VALID_TENANT = textwrap.dedent(f"""\
    apiVersion: kerberator.dell.com/v1alpha1
    kind: Tenant
    metadata:
      name: demo
      namespace: demo-ns
    spec:
      realm: EXAMPLE.COM
      daemon:
        renewMinutes: 60
        krb5Conf: |
{textwrap.indent(KRB5, "          ")}
""")

VALID_PRINCIPAL = textwrap.dedent("""\
    apiVersion: kerberator.dell.com/v1alpha1
    kind: Principal
    metadata:
      name: svc-2001
      namespace: demo-ns
    spec:
      uid: 2001
      principal: svc@EXAMPLE.COM
      tenantRef:
        name: demo
      keytabSecretRef:
        name: svc-2001-keytab
        key: keytab
""")


class TestValidateTenant(unittest.TestCase):

    def test_valid_tenant_status(self):
        """Reviewer-confirmed: a well-formed Tenant YAML → STATUS: valid."""
        out, rc = _run(VALID_TENANT)
        self.assertEqual(_status(out), "valid", out)
        self.assertEqual(rc, 0, out)

    def test_malformed_yaml_invalid(self):
        """Reviewer-confirmed: broken YAML → STATUS: invalid."""
        bad = "apiVersion: kerberator.dell.com/v1alpha1\nkind: Tenant\nspec:\n  bad: [\n"
        out, rc = _run(bad)
        self.assertEqual(_status(out), "invalid", out)
        self.assertEqual(rc, 4, out)

    # ---- renewMinutes boolean regression (Fix 1) ----------------------------

    def test_renew_minutes_true_invalid(self):
        """renewMinutes: true must be STATUS: invalid — bool is subclass of int."""
        yaml = VALID_TENANT.replace("renewMinutes: 60", "renewMinutes: true")
        out, rc = _run(yaml)
        self.assertEqual(_status(out), "invalid", out)
        self.assertTrue(_has_finding(out, "fail", "renew-minutes-positive"), out)

    def test_renew_minutes_false_invalid(self):
        """renewMinutes: false must be STATUS: invalid."""
        yaml = VALID_TENANT.replace("renewMinutes: 60", "renewMinutes: false")
        out, rc = _run(yaml)
        self.assertEqual(_status(out), "invalid", out)
        self.assertTrue(_has_finding(out, "fail", "renew-minutes-positive"), out)

    def test_renew_minutes_zero_invalid(self):
        """renewMinutes: 0 must be STATUS: invalid (not > 0)."""
        yaml = VALID_TENANT.replace("renewMinutes: 60", "renewMinutes: 0")
        out, rc = _run(yaml)
        self.assertEqual(_status(out), "invalid", out)
        self.assertTrue(_has_finding(out, "fail", "renew-minutes-positive"), out)

    def test_renew_minutes_negative_invalid(self):
        """renewMinutes: -1 must be STATUS: invalid."""
        yaml = VALID_TENANT.replace("renewMinutes: 60", "renewMinutes: -1")
        out, rc = _run(yaml)
        self.assertEqual(_status(out), "invalid", out)
        self.assertTrue(_has_finding(out, "fail", "renew-minutes-positive"), out)

    def test_renew_minutes_unset_valid(self):
        """renewMinutes unset → operator default → ok finding, not a failure."""
        yaml = VALID_TENANT.replace("        renewMinutes: 60\n", "")
        out, rc = _run(yaml)
        self.assertEqual(_status(out), "valid", out)
        self.assertTrue(_has_finding(out, "ok", "renew-minutes-positive"), out)

    # ---- unreachable API regression (Fix 2) ---------------------------------

    def test_unreachable_api_is_valid_with_warnings_not_valid(self):
        """When the API server is unreachable the CRD check emits an 'unknown'
        finding.  STATUS must be valid-with-warnings, not valid."""
        out, rc = _run(VALID_TENANT, env_overrides={"STUB_CRD_ERROR": "1"})
        status = _status(out)
        self.assertNotEqual(status, "valid",
                            f"STATUS must not be 'valid' when API is unreachable; got {status!r}\n{out}")
        self.assertEqual(status, "valid-with-warnings", out)


class TestValidatePrincipal(unittest.TestCase):

    def test_empty_nodeselector_invalid(self):
        """Reviewer-confirmed: nodeSelector: {} → STATUS: invalid."""
        yaml = VALID_PRINCIPAL + "  nodeSelector: {}\n"
        out, rc = _run(yaml)
        self.assertEqual(_status(out), "invalid", out)
        self.assertTrue(_has_finding(out, "fail", "nodeselector-syntax"), out)

    def test_secret_values_not_in_output(self):
        """Reviewer-confirmed: keytab bytes must never appear in output."""
        out, rc = _run(VALID_PRINCIPAL)
        # The stub encodes a known value; make sure it is not echoed.
        self.assertNotIn("ZmFrZWtleXRhYg==", out,
                         "Secret value appeared in validate output")
        # The key name may appear (that is intentional); just not the value.
        self.assertIn("keytab", out, "Expected key name 'keytab' in output")


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        teardown()
