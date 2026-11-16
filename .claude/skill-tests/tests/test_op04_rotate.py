import os
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[3]
SCRIPTS = (
    REPO
    / ".claude/skills/kerberator-rotate-keytab/scripts"
)


class RotateKeytabTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

        self.work = Path(self.temp.name)
        self.bin = self.work / "bin"
        self.bin.mkdir()

        self.kubeconfig = self.work / "kubeconfig"
        self.kubeconfig.touch()

        self.mutation_log = self.work / "mutations.log"

        self.write_mock(
            "kubectl",
            r'''#!/usr/bin/env bash
case "$*" in
  *"auth can-i "*)
    echo yes
    ;;
  *"get principals.kerberator.dell.com uid-2001 -o jsonpath="*)
    if [[ "$*" == *"status.nodes"* ]]; then
      echo "kerb-w1=Minted kerb-w2=Minted"
    else
      echo "2001|svc-2001@EXAMPLE.COM|demo|uid-2001-keytab|uid-2001.keytab|7|2026-11-03T23:49:27Z|false"
    fi
    ;;
  *"get secret uid-2001-keytab -o name"*)
    echo "secret/uid-2001-keytab"
    ;;
  *"get daemonset demo-kerberator-daemon"*)
    echo "2/2"
    ;;
  *"krb rotate-keytab"*)
    echo "$*" >> "$MOCK_MUTATIONS"
    exit 99
    ;;
  *)
    echo "Unexpected kubectl call: $*" >&2
    exit 1
    ;;
esac
''',
        )

        self.write_mock(
            "kubectl-krb",
            "#!/usr/bin/env bash\nexit 99\n",
        )

        self.write_mock(
            "klist",
            r'''#!/usr/bin/env bash
echo "KVNO Timestamp Principal"
echo "$MOCK_KVNO 11/16/26 10:00:00 $MOCK_PRINCIPAL"
''',
        )

        self.env = os.environ.copy()
        self.env.update({
            "PATH": f"{self.bin}:{self.env['PATH']}",
            "KUBECONFIG": str(self.kubeconfig),
            "MOCK_MUTATIONS": str(self.mutation_log),
            "MOCK_KVNO": "8",
            "MOCK_PRINCIPAL": "svc-2001@EXAMPLE.COM",
        })

    def write_mock(self, name, content):
        path = self.bin / name
        path.write_text(content)
        path.chmod(0o755)

    def run_script(self, script, *args):
        return subprocess.run(
            ["bash", str(SCRIPTS / script), *args],
            capture_output=True,
            text=True,
            env=self.env,
            timeout=15,
        )

    def run_precheck(self, keytab):
        return self.run_script(
            "precheck.sh",
            "uid-2001",
            "--from-file",
            str(keytab),
            "-n",
            "kerberator-project",
        )

    def make_keytab(self, name):
        path = self.work / name
        path.write_text("mock keytab fixture")
        return path

    def test_missing_keytab_is_blocked(self):
        result = self.run_precheck(
            self.work / "missing.keytab"
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("does not exist or is not readable", result.stdout)
        self.assertIn("RESULT: BLOCKED", result.stdout)

    def test_empty_keytab_is_blocked(self):
        keytab = self.make_keytab("empty.keytab")
        keytab.write_bytes(b"")

        result = self.run_precheck(keytab)

        self.assertEqual(result.returncode, 1)
        self.assertIn("is empty", result.stdout)
        self.assertIn("RESULT: BLOCKED", result.stdout)

    def test_wrong_principal_is_blocked(self):
        keytab = self.make_keytab("wrong.keytab")
        self.env["MOCK_PRINCIPAL"] = "svc-2002@EXAMPLE.COM"

        result = self.run_precheck(keytab)

        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "does not contain svc-2001@EXAMPLE.COM",
            result.stdout,
        )
        self.assertIn("RESULT: BLOCKED", result.stdout)

    def test_lower_kvno_produces_warning(self):
        keytab = self.make_keytab("lower.keytab")
        self.env["MOCK_KVNO"] = "6"

        result = self.run_precheck(keytab)

        self.assertEqual(result.returncode, 0)
        self.assertIn("LOWER than current 7", result.stdout)
        self.assertIn("RESULT: READY WITH WARNINGS", result.stdout)

    def test_equal_kvno_produces_warning(self):
        keytab = self.make_keytab("equal.keytab")
        self.env["MOCK_KVNO"] = "7"

        result = self.run_precheck(keytab)

        self.assertEqual(result.returncode, 0)
        self.assertIn("equals current", result.stdout)
        self.assertIn("RESULT: READY WITH WARNINGS", result.stdout)

    def test_rotation_requires_approval(self):
        keytab = self.make_keytab("replacement.keytab")

        result = self.run_script(
            "rotate.sh",
            "uid-2001",
            "--from-file",
            str(keytab),
            "-n",
            "kerberator-project",
        )

        self.assertEqual(result.returncode, 3)
        self.assertIn(
            "NOT RUN: needs the user's approval",
            result.stdout,
        )
        self.assertFalse(self.mutation_log.exists())

    def test_missing_approval_never_invokes_rotation(self):
        keytab = self.make_keytab("replacement.keytab")

        self.run_script(
            "rotate.sh",
            "uid-2001",
            "--from-file",
            str(keytab),
            "-n",
            "kerberator-project",
        )

        if self.mutation_log.exists():
            self.assertEqual(
                self.mutation_log.read_text(),
                "",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
