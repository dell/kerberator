#!/usr/bin/env python3
"""Independent regression tests for OP-08's validate.py.

Run from the repository root:
    python -m unittest discover -s .claude/skill-tests/kerberator-validate-config -v

These tests mock kubectl. They do not contact or modify a Kubernetes cluster.
Set VALIDATOR_PATH if validate.py is stored somewhere else.
"""

import contextlib
import importlib.util
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import subprocess


ROOT = Path(__file__).resolve().parents[3]
VALIDATOR = Path(os.environ.get(
    "VALIDATOR_PATH",
    ROOT / ".claude/skills/kerberator-validate-config/scripts/validate.py",
))

spec = importlib.util.spec_from_file_location("op08_validate", VALIDATOR)
if spec is None or spec.loader is None:
    raise RuntimeError(f"Cannot load OP-08 validator at {VALIDATOR}")
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)


CRDS = (
    "tenants.kerberator.dell.com "
    "principals.kerberator.dell.com"
)

VALID_TENANT = """\
apiVersion: kerberator.dell.com/v1alpha1
kind: Tenant
metadata:
  name: demo
  namespace: kerberator-demo
spec:
  realm: EXAMPLE.COM
  daemon:
    krb5Conf: |
      [libdefaults]
        default_realm = EXAMPLE.COM
      [realms]
        EXAMPLE.COM = {
          kdc = kdc.example.com
        }
"""

VALID_PRINCIPAL = """\
apiVersion: kerberator.dell.com/v1alpha1
kind: Principal
metadata:
  name: svc-2001
  namespace: kerberator-demo
spec:
  uid: 2001
  principal: svc-2001@EXAMPLE.COM
  tenantRef:
    name: demo
  keytabSecretRef:
    name: svc-2001-keytab
    key: keytab
"""


def completed(args, stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(args, returncode, stdout, stderr)


class ValidatorTests(unittest.TestCase):
    def run_validator(self, yaml_text, kubectl_side_effect=None, extra_args=()):
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", encoding="utf-8", delete=False
        )
        try:
            tmp.write(yaml_text)
            tmp.close()

            stdout = io.StringIO()
            side_effect = kubectl_side_effect or self.tenant_kubectl
            with patch.object(validator.shutil, "which", return_value="kubectl"), \
                 patch.object(validator.subprocess, "run", side_effect=side_effect), \
                 patch("sys.argv", [
                     "validate.py", "--from-file", tmp.name,
                     "-n", "kerberator-demo", *extra_args
                 ]), \
                 contextlib.redirect_stdout(stdout):
                try:
                    exit_code = validator.main()
                except Exception as exc:
                    # Mirror the validator's __main__ safety wrapper so tests
                    # exercise the same user-visible CLI result when imported.
                    print("STATUS: error")
                    print("CONTEXT: -")
                    print(f"FINDING: fail internal | {type(exc).__name__}: {exc}")
                    exit_code = 1
            return exit_code, stdout.getvalue()
        finally:
            Path(tmp.name).unlink(missing_ok=True)

    @staticmethod
    def tenant_kubectl(args, **kwargs):
        if "get" in args and "crd" in args:
            return completed(args, CRDS)
        return completed(args, stderr="unexpected kubectl invocation", returncode=1)

    @staticmethod
    def status_from(output):
        lines = [line for line in output.splitlines()
                 if line.startswith("STATUS: ")]
        if len(lines) != 1:
            return None
        return lines[0].removeprefix("STATUS: ")

    def test_valid_tenant_passes_shape_and_crd_checks(self):
        code, output = self.run_validator(VALID_TENANT)
        self.assertEqual(code, 0, output)
        self.assertEqual(self.status_from(output), "valid", output)
        self.assertIn("FINDING: ok realm-shape", output)
        self.assertIn("FINDING: ok crd-installed", output)

    def test_malformed_yaml_is_invalid_without_kubectl_calls(self):
        calls = []

        def no_kubectl(args, **kwargs):
            calls.append(args)
            return self.tenant_kubectl(args, **kwargs)

        code, output = self.run_validator("kind: [Tenant\n", no_kubectl)
        self.assertEqual(code, 4, output)
        self.assertIn("STATUS: invalid", output)
        self.assertIn("FINDING: fail yaml-parse", output)
        self.assertEqual(calls, [], "Malformed YAML should fail before cluster access")

    def test_boolean_renew_minutes_is_rejected(self):
        yaml_text = VALID_TENANT.replace(
            "  daemon:\n", "  daemon:\n    renewMinutes: true\n"
        )
        code, output = self.run_validator(yaml_text)
        self.assertEqual(code, 4, output)
        self.assertIn("FINDING: fail renew-minutes-positive", output)

    def test_empty_node_selector_value_is_rejected(self):
        yaml_text = VALID_PRINCIPAL.replace(
            "  uid: 2001\n",
            "  uid: 2001\n  nodeSelector:\n    kubernetes.io/hostname: \"\"\n"
        )
        code, output = self.run_validator(yaml_text)
        self.assertEqual(code, 4, output)
        self.assertIn("FINDING: fail nodeselector-syntax", output)

    def test_cluster_connection_failure_reports_warning_status(self):
        def unavailable(args, **kwargs):
            return completed(
                args,
                stderr="The connection to the server localhost:6443 was refused",
                returncode=1,
            )

        code, output = self.run_validator(VALID_TENANT, unavailable)
        self.assertEqual(self.status_from(output), "valid-with-warnings", output)
        self.assertIn("unknown crd-installed", output)
        self.assertEqual(code, 0, output)

    def test_kubectl_timeout_is_bounded_and_reported_as_warning(self):
        calls = []

        def timeout_immediately(args, **kwargs):
            calls.append((args, kwargs))
            raise subprocess.TimeoutExpired(args, kwargs["timeout"])

        code, output = self.run_validator(VALID_TENANT, timeout_immediately)

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1].get("timeout"), validator._KUBECTL_TIMEOUT)
        self.assertEqual(validator._KUBECTL_TIMEOUT, 30)
        self.assertEqual(self.status_from(output), "valid-with-warnings", output)
        self.assertIn("unknown crd-installed", output)
        self.assertIn("timed out after 30s", output)
        self.assertEqual(code, 0, output)

    def test_secret_values_never_appear_in_output(self):
        secret_sentinel = "THIS_IS_SECRET_BASE64_SENTINEL"
        secret_queries = []

        def fake_kubectl(args, **kwargs):
            if "get" in args and "crd" in args:
                return completed(args, CRDS)
            if "get" in args and "tenant" in args:
                import json
                return completed(args, json.dumps({
                    "apiVersion": "kerberator.dell.com/v1alpha1",
                    "kind": "Tenant",
                    "metadata": {"name": "demo"},
                    "spec": {"realm": "EXAMPLE.COM"},
                }))
            if "get" in args and "secret" in args:
                secret_queries.append(args)
                if "-o" not in args or not any(
                    arg.startswith("go-template=") and ".data" in arg
                    and "{{$k}}" in arg and "{{$v}}" not in arg
                    for arg in args
                ):
                    raise AssertionError(
                        f"Secret values must not be queried: {args!r}"
                    )
                return completed(args, "keytab ")
            if "get" in args and "principals" in args:
                return completed(args, '{"items":[]}')
            return completed(args, stderr="forbidden", returncode=1)

        code, output = self.run_validator(VALID_PRINCIPAL, fake_kubectl)
        self.assertEqual(code, 0, output)
        self.assertEqual(self.status_from(output), "valid", output)
        self.assertIn("FINDING: ok keytab-secret-exists", output)
        self.assertTrue(secret_queries, "Expected the Secret lookup to run")
        self.assertEqual(len(secret_queries), 1)
        self.assertIn("-o", secret_queries[0])
        self.assertTrue(any("go-template=" in arg for arg in secret_queries[0]))
        self.assertNotIn(secret_sentinel, output)
        self.assertNotIn("keytab:", output)


if __name__ == "__main__":
    unittest.main(verbosity=2)
