"""Unit tests for OP-03 Register a Principal argument validation."""

import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
COMMON_SH = (
    REPO_ROOT
    / ".claude/skills/kerberator-register-principal/scripts/_common.sh"
)


def dns_subdomain(length):
    """Build a valid DNS subdomain of exactly the requested length."""
    labels = []
    remaining = length
    while remaining:
        size = min(63, remaining)
        labels.append("a" * size)
        remaining -= size
        if remaining:
            remaining -= 1
    return ".".join(labels)


class OP03ArgumentTests(unittest.TestCase):
    def run_parser(self, *args):
        harness = r'''
source "$1"
shift
parse_args "$@"
printf 'FIELD\tCONTEXT\t%s\n' "$CONTEXT"
printf 'FIELD\tNS\t%s\n' "$NS"
printf 'FIELD\tTENANT\t%s\n' "$TENANT"
printf 'FIELD\tTARGET_UID\t%s\n' "$TARGET_UID"
printf 'FIELD\tPRINCIPAL\t%s\n' "$PRINCIPAL"
printf 'FIELD\tFROM\t%s\n' "$FROM"
printf 'FIELD\tSECRET\t%s\n' "$SECRET"
printf 'FIELD\tSECRET_KEY\t%s\n' "$SECRET_KEY"
printf 'FIELD\tNAME\t%s\n' "$NAME"
printf 'FIELD\tMODE\t%s\n' "$MODE"
printf 'FIELD\tCONFIRMED\t%s\n' "$CONFIRMED"
for selector in "${SELECTORS[@]}"; do
    printf 'SELECTOR\t%s\n' "$selector"
done
'''
        return subprocess.run(
            ["bash", "-c", harness, "_", str(COMMON_SH), *args],
            capture_output=True,
            text=True,
            check=False,
        )

    def parsed(self, result):
        fields = {}
        selectors = []
        for line in result.stdout.splitlines():
            parts = line.split("\t", 2)
            if parts[0] == "FIELD" and len(parts) == 3:
                fields[parts[1]] = parts[2]
            elif parts[0] == "SELECTOR" and len(parts) == 2:
                selectors.append(parts[1])
        return fields, selectors

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

    def assert_fields_exact(self, actual, expected):
        for key, value in expected.items():
            self.assertIn(key, actual)
            self.assertEqual(actual[key], value, f"Incorrect parsed field: {key}")

    def test_valid_existing_secret_preserves_all_arguments_exactly(self):
        result = self.run_parser(*self.valid_secret_args())
        self.assertEqual(result.returncode, 0, result.stderr)
        fields, selectors = self.parsed(result)

        self.assert_fields_exact(fields, {
            "CONTEXT": "test-context",
            "NS": "test-ns",
            "TENANT": "demo",
            "TARGET_UID": "2003",
            "PRINCIPAL": "svc-2003@EXAMPLE.COM",
            "FROM": "",
            "SECRET": "svc-2003-keytab",
            "SECRET_KEY": "svc-2003.keytab",
            "NAME": "svc-2003",
            "MODE": "secret",
            "CONFIRMED": "false",
        })
        self.assertEqual(selectors, [])

    def test_valid_local_keytab_generates_exact_names_and_preserves_path(self):
        result = self.run_parser(
            "--context", "test-context",
            "--namespace", "test-ns",
            "--tenant", "demo",
            "--uid", "2003",
            "--principal", "svc-2003@EXAMPLE.COM",
            "--from-file", "/tmp/test.keytab",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        fields, selectors = self.parsed(result)

        self.assert_fields_exact(fields, {
            "CONTEXT": "test-context",
            "NS": "test-ns",
            "TENANT": "demo",
            "TARGET_UID": "2003",
            "PRINCIPAL": "svc-2003@EXAMPLE.COM",
            "FROM": "/tmp/test.keytab",
            "SECRET": "uid-2003-keytab",
            "SECRET_KEY": "uid-2003.keytab",
            "NAME": "uid-2003",
            "MODE": "file",
            "CONFIRMED": "false",
        })
        self.assertEqual(selectors, [])

    def test_explicit_confirmed_flag_is_preserved(self):
        result = self.run_parser(*self.valid_secret_args(), "--confirmed")
        self.assertEqual(result.returncode, 0, result.stderr)
        fields, _ = self.parsed(result)
        self.assertEqual(fields["CONFIRMED"], "true")

    def test_multiple_selectors_survive_parsing_in_order(self):
        expected = [
            "security-zone=prod-a",
            "example.com/zone=prod",
        ]
        args = self.valid_secret_args()
        for selector in expected:
            args.extend(["--node-selector", selector])

        result = self.run_parser(*args)
        self.assertEqual(result.returncode, 0, result.stderr)
        fields, selectors = self.parsed(result)
        self.assertEqual(fields["CONFIRMED"], "false")
        self.assertEqual(selectors, expected)

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
        self.assertNotEqual(self.run_parser(*args).returncode, 0)

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

    def test_valid_dotted_dns_subdomain_resource_names_are_accepted(self):
        args = self.valid_secret_args()
        args[args.index("--name") + 1] = "svc.2003-keytab"
        args[args.index("--secret") + 1] = "svc.2003-secret"
        result = self.run_parser(*args)
        self.assertEqual(result.returncode, 0, result.stderr)
        fields, _ = self.parsed(result)
        self.assertEqual(fields["NAME"], "svc.2003-keytab")
        self.assertEqual(fields["SECRET"], "svc.2003-secret")

    def test_resource_name_253_character_boundary_is_accepted(self):
        name = dns_subdomain(253)
        args = self.valid_secret_args()
        args[args.index("--name") + 1] = name
        args[args.index("--secret") + 1] = name
        result = self.run_parser(*args)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_resource_names_over_253_characters_are_rejected(self):
        name = dns_subdomain(254)
        for option in ("--name", "--secret"):
            with self.subTest(option=option):
                args = self.valid_secret_args()
                args[args.index(option) + 1] = name
                self.assertNotEqual(self.run_parser(*args).returncode, 0)

    def test_invalid_resource_name_grammar_is_rejected(self):
        for invalid in ("Invalid_Name", "bad..name", ".leading", "trailing."):
            with self.subTest(name=invalid):
                args = self.valid_secret_args()
                args[args.index("--name") + 1] = invalid
                self.assertNotEqual(self.run_parser(*args).returncode, 0)

    def test_namespace_63_character_boundary_is_accepted(self):
        args = self.valid_secret_args()
        args[args.index("--namespace") + 1] = "a" * 63
        self.assertEqual(self.run_parser(*args).returncode, 0)

    def test_namespace_over_63_characters_is_rejected(self):
        args = self.valid_secret_args()
        args[args.index("--namespace") + 1] = "a" * 64
        result = self.run_parser(*args)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("namespace", result.stderr)

    def test_namespace_dns_label_grammar_is_enforced(self):
        for invalid in ("Uppercase", "bad.name", "-leading", "trailing-"):
            with self.subTest(namespace=invalid):
                args = self.valid_secret_args()
                args[args.index("--namespace") + 1] = invalid
                self.assertNotEqual(self.run_parser(*args).returncode, 0)

    def test_secret_key_253_character_boundary_is_accepted(self):
        args = self.valid_secret_args()
        args[args.index("--secret-key") + 1] = "k" * 253
        self.assertEqual(self.run_parser(*args).returncode, 0)

    def test_secret_key_254_characters_is_accepted(self):
        args = self.valid_secret_args()
        args[args.index("--secret-key") + 1] = "k" * 254
        self.assertEqual(self.run_parser(*args).returncode, 0)

    def test_secret_key_invalid_characters_are_rejected(self):
        args = self.valid_secret_args()
        args[args.index("--secret-key") + 1] = "path/key"
        self.assertNotEqual(self.run_parser(*args).returncode, 0)

    def test_invalid_node_selector_syntax_and_grammar_are_rejected(self):
        invalid_selectors = [
            "missing-equals",
            "a/b/c=x",
            "zone=-prod",
            "zone=prod-",
            "-zone=prod",
            "zone-=prod",
            "Example.com/zone=prod",
            "example.com/zone/extra=prod",
            "zone=prod.",
            "=prod",
            "a" * 64 + "=prod",
            "zone=" + "v" * 64,
            "a" * 254 + "/zone=prod",
        ]
        for selector in invalid_selectors:
            with self.subTest(selector=selector):
                args = self.valid_secret_args() + ["--node-selector", selector]
                result = self.run_parser(*args)
                self.assertNotEqual(result.returncode, 0)

    def test_valid_qualified_selector_and_empty_value_are_accepted(self):
        args = self.valid_secret_args() + [
            "--node-selector", "example.com/zone=prod",
            "--node-selector", "dedicated=",
        ]
        result = self.run_parser(*args)
        self.assertEqual(result.returncode, 0, result.stderr)
        _, selectors = self.parsed(result)
        self.assertEqual(selectors, ["example.com/zone=prod", "dedicated="])

    def test_selector_key_and_value_63_character_boundaries_are_accepted(self):
        args = self.valid_secret_args() + [
            "--node-selector", ("k" * 63) + "=" + ("v" * 63),
        ]
        self.assertEqual(self.run_parser(*args).returncode, 0)

    def test_selector_qualified_prefix_253_character_boundary_is_accepted(self):
        selector = dns_subdomain(253) + "/zone=prod"
        args = self.valid_secret_args() + ["--node-selector", selector]
        self.assertEqual(self.run_parser(*args).returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
