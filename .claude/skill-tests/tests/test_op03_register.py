"""Unit tests for OP-03 Register a Principal argument validation.

These tests source the real OP-03 _common.sh parser. They do not contact
Kubernetes, create resources, or read keytab/Secret contents.
"""

import subprocess
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
COMMON_SH = (
    REPO_ROOT
    / ".claude/skills/kerberator-register-principal/scripts/_common.sh"
)


class OP03ArgumentTests(unittest.TestCase):
    def run_parser(self, *args):
        result = subprocess.run(
            [
                "bash",
                "-c",
                'source "$1"; shift; parse_args "$@"; '
                'printf "MODE=%s\\nNAME=%s\\nSECRET=%s\\nSECRET_KEY=%s\\n" '
                '"$MODE" "$NAME" "$SECRET" "$SECRET_KEY"',
                "_",
                str(COMMON_SH),
                *args,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        return result

    def valid_secret_args(self):
        return [
            "--context", "test-context",
            "--namespace", "test-ns",
            "--tenant", "demo",
            "--uid", "2003",
            "--principal", "svc-2003@EXAMPLE.COM",
            "--secret", "svc-2003-keytab",
            "--secret-key", "svc-2003.keytab",
            "--name", "svc-2003",
        ]

    def test_valid_existing_secret_registration_arguments(self):
        result = self.run_parser(*self.valid_secret_args())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("MODE=secret", result.stdout)
        self.assertIn("NAME=svc-2003", result.stdout)
        self.assertIn("SECRET=svc-2003-keytab", result.stdout)
        self.assertIn("SECRET_KEY=svc-2003.keytab", result.stdout)

    def test_valid_local_keytab_arguments_generate_expected_names(self):
        args = [
            "--context", "test-context",
            "--namespace", "test-ns",
            "--tenant", "demo",
            "--uid", "2003",
            "--principal", "svc-2003@EXAMPLE.COM",
            "--from-file", "/tmp/test.keytab",
        ]
        result = self.run_parser(*args)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("MODE=file", result.stdout)
        self.assertIn("NAME=uid-2003", result.stdout)
        self.assertIn("SECRET=uid-2003-keytab", result.stdout)
        self.assertIn("SECRET_KEY=uid-2003.keytab", result.stdout)

    def test_missing_required_context_is_rejected(self):
        args = self.valid_secret_args()
        args.remove("--context")
        args.remove("test-context")
        result = self.run_parser(*args)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("required", result.stderr)

    def test_zero_uid_is_rejected(self):
        args = self.valid_secret_args()
        args[args.index("--uid") + 1] = "0"
        result = self.run_parser(*args)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("positive", result.stderr)

    def test_non_numeric_uid_is_rejected(self):
        args = self.valid_secret_args()
        args[args.index("--uid") + 1] = "abc"
        result = self.run_parser(*args)
        self.assertNotEqual(result.returncode, 0)

    def test_uid_with_leading_zero_is_rejected(self):
        args = self.valid_secret_args()
        args[args.index("--uid") + 1] = "02003"
        result = self.run_parser(*args)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("canonical", result.stderr)

    def test_malformed_kerberos_principal_is_rejected(self):
        args = self.valid_secret_args()
        args[args.index("--principal") + 1] = "svc-2003"
        result = self.run_parser(*args)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("name@REALM", result.stderr)

    def test_file_and_secret_modes_cannot_be_combined(self):
        args = self.valid_secret_args() + ["--from-file", "/tmp/test.keytab"]
        result = self.run_parser(*args)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("choose --from-file OR --secret", result.stderr)

    def test_existing_secret_mode_requires_principal_name(self):
        args = self.valid_secret_args()
        args.remove("--name")
        args.remove("svc-2003")
        result = self.run_parser(*args)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires --secret --secret-key --name", result.stderr)

    def test_invalid_principal_resource_name_is_rejected(self):
        args = self.valid_secret_args()
        args[args.index("--name") + 1] = "Invalid_Name"
        result = self.run_parser(*args)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid Principal or Secret", result.stderr)

    def test_invalid_node_selector_syntax_is_rejected(self):
        args = self.valid_secret_args() + ["--node-selector", "missing-equals"]
        result = self.run_parser(*args)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("expected key=value", result.stderr)

    def test_multiple_valid_node_selectors_are_accepted(self):
        args = (
            self.valid_secret_args()
            + [
                "--node-selector", "security-zone=prod-a",
                "--node-selector", "disk-type=ssd",
            ]
        )
        result = self.run_parser(*args)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
