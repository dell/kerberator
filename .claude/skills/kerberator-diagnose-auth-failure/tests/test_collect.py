#!/usr/bin/env python3
"""Unit tests for scripts/collect.py. No cluster needed.

    python3 tests/test_collect.py

A fake `kubectl` on PATH answers each call from a scenario: a list of
rules, each a set of substrings the command line must contain and the
stdout / stderr / exit code to return. The first matching rule wins.
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
HERE = pathlib.Path(__file__).resolve().parent
COLLECT = HERE.parent / "scripts" / "collect.py"
sys.path.insert(0, str(COLLECT.parent))
from collect import classify  # noqa: E402

FAKE_KUBECTL = r'''#!/usr/bin/env python3
import json, os, sys
argv = " ".join(sys.argv[1:])
for rule in json.load(open(os.environ["FAKE_SCENARIO"])):
    if all(m in argv for m in rule["match"]):
        sys.stdout.write(rule.get("stdout", ""))
        sys.stderr.write(rule.get("stderr", ""))
        sys.exit(rule.get("rc", 0))
sys.stderr.write("fake kubectl: no rule for: " + argv + "\n")
sys.exit(1)
'''

NOW = "2099-01-01T00:00:00Z"   # far future, so "now - lastObserved" is never negative
PRINC = "svc-2001@EXAMPLE.COM"
KINIT_FAIL = ("kinit failed for uid=2001 principal=svc-2001@EXAMPLE.COM: kinit failed: exit status 1 "
              "(output: kinit: Password incorrect while getting initial credentials\n)")


def principal(name="svc-2001", uid=2001, reason="Minted", ready=True, msg="Minted ccache",
              seen=NOW, ready_cond="True", ready_reason="AllDaemonsReady", **spec_extra):
    spec = {"uid": uid, "principal": PRINC, "tenantRef": {"name": "demo"},
            "keytabSecretRef": {"name": "svc-2001-keytab", "key": "svc-2001.keytab"}, **spec_extra}
    return {"metadata": {"name": name, "namespace": "kerberator-demo"}, "spec": spec,
            "status": {"kvnoFromSecret": 2, "enctypesFromSecret": "aes256-cts-hmac-sha1-96",
                       "keytabObservedAt": NOW,
                       "conditions": [{"type": "Ready", "status": ready_cond, "reason": ready_reason}],
                       "nodes": [{"name": "node-1", "daemonPodReady": ready, "reason": reason,
                                  "lastObserved": seen, "message": msg}]}}


TENANT = {"metadata": {"name": "demo"},
          "spec": {"realm": "EXAMPLE.COM", "daemon": {"renewMinutes": 30, "krb5Conf":
                   "[libdefaults]\n  default_realm = EXAMPLE.COM\n[realms]\n  EXAMPLE.COM = {\n    kdc = kdc.example:88\n  }\n"}},
          "status": {"conditions": [{"type": "KeytabsResolved", "status": "True"},
                                    {"type": "RosterConsistent", "status": "True"}]}}


def pods(ready=True, started="2000-01-01T00:00:00Z"):
    return {"items": [{"metadata": {"name": "demo-daemon-abc"}, "spec": {"nodeName": "node-1"},
                       "status": {"phase": "Running", "startTime": started,
                                  "containerStatuses": [{"ready": ready, "restartCount": 0,
                                                         "state": {"running": {"startedAt": started}}}]}}]}


def uid_event(reason, msg):
    return {"items": [{"metadata": {"annotations": {"kerberator.dell.com/uid": "2001"}},
                       "lastTimestamp": NOW, "reason": reason, "count": 3, "message": msg,
                       "involvedObject": {"name": "demo-daemon-abc"}}]}


def J(obj):
    return json.dumps(obj)


def scenario(p=None, others=None, tenant=TENANT, pod_ready=True, pod_events=None, events=None,
             logs="", secret_rc=0, overrides=(), pod_started="2000-01-01T00:00:00Z"):
    """Rules for one cluster state. `overrides` come first, so they win."""
    p = p or principal()
    rules = list(overrides) + [
        {"match": ["config current-context"], "stdout": "kind-test\n"},
        {"match": ["get principals.kerberator.dell.com svc-2001"], "stdout": J(p)},
        {"match": ["get principals.kerberator.dell.com", "-o json"],
         "stdout": J({"items": [p] + (others or [])})},
        {"match": ["get secret"], "stdout": "secret/svc-2001-keytab\n" if not secret_rc else "",
         "stderr": "" if not secret_rc else 'Error from server (NotFound): secrets "svc-2001-keytab" not found',
         "rc": secret_rc},
        {"match": ["get tenants.kerberator.dell.com demo"], "stdout": J(tenant)},
        {"match": ["get pods"], "stdout": J(pods(pod_ready, pod_started))},
        {"match": ["get events --field-selector"], "stdout": J(pod_events or {"items": []})},
        {"match": ["get events"], "stdout": J(events or {"items": []})},
        {"match": ["logs demo-daemon-abc"], "stdout": logs},
        {"match": ["krb list"], "stdout": "NAMESPACE KIND NAME\nkerberator-demo Principal svc-2001\n"},
    ]
    return rules


def run(rules, name="svc-2001"):
    with tempfile.TemporaryDirectory() as d:
        k = pathlib.Path(d, "kubectl")
        k.write_text(FAKE_KUBECTL)
        k.chmod(0o755)
        sc = pathlib.Path(d, "scenario.json")
        sc.write_text(json.dumps(rules))
        env = dict(os.environ, PATH=f"{d}:{os.environ['PATH']}", FAKE_SCENARIO=str(sc))
        p = subprocess.run([sys.executable, "-B", str(COLLECT), name, "-n", "kerberator-demo"],
                           capture_output=True, text=True, env=env)
    signals = {}
    if "== 8. SIGNALS ==" in p.stdout:
        for line in p.stdout.split("== 8. SIGNALS ==", 1)[1].splitlines():
            if "=" in line:
                k_, v = line.strip().split("=", 1)
                signals[k_] = v.split("   ")[0].split("  (")[0].strip()
    return p.returncode, p.stdout, signals


class Classify(unittest.TestCase):
    def test_signatures(self):
        cases = {
            "kinit: Password incorrect while getting initial credentials": "key-rejected",
            "Preauthentication failed": "key-rejected",
            "Client 'svc-1@EXAMPLE.COM' not found in Kerberos database": "client-not-found",
            "Cannot contact any KDC for realm 'EXAMPLE.COM'": "kdc-unreachable",
            "Cannot resolve network address for KDC in realm": "kdc-unreachable",
            "kinit: Resource temporarily unavailable while getting initial credentials": "kdc-unreachable",
            "Clock skew too great while getting initial credentials": "clock-skew",
            "Keytab contains no suitable keys for svc-2001@EXAMPLE.COM": "keytab-no-matching-key",
            "Key table entry not found": "keytab-no-matching-key",
            "KDC has no support for encryption type": "enctype-unsupported",
            "Client's credentials have been revoked": "credentials-revoked",
            "Key table file '/k/svc.keytab' not found": "keytab-file-missing",
            "something else entirely": "unrecognized",
            "": "none",
        }
        for text, want in cases.items():
            self.assertEqual(classify(text), want, text)


class Collect(unittest.TestCase):
    def test_healthy(self):
        rc, out, s = run(scenario())
        self.assertEqual(rc, 0)
        self.assertEqual(s["error_signature"], "none")
        self.assertEqual(s["nodes_minted"], "1/1")
        self.assertEqual(s["realm_match"], "yes")
        self.assertEqual(s["keytab_secret"], "present")
        self.assertIn("kdc = kdc.example:88", out)

    def test_key_rejected_with_probe_failure(self):
        p = principal(reason="MintFailed", ready=False, msg=KINIT_FAIL, ready_cond="False",
                      ready_reason="SomeDaemonsNotReady")
        rc, out, s = run(scenario(
            p=p, pod_ready=False,
            pod_events={"items": [{"lastTimestamp": NOW, "type": "Warning", "reason": "Unhealthy",
                                   "message": "Readiness probe failed: readyz: missing ccache for uid=2001 at /tmp/krb5cc_2001"}]},
            events=uid_event("KerberosMintFailed", KINIT_FAIL),
            logs="2026/11/05 ERROR: Failed to mint cache for svc-2001@EXAMPLE.COM: kinit: Password incorrect\n)\n"))
        self.assertEqual(rc, 0)
        self.assertEqual(s["error_signature"], "key-rejected")
        self.assertEqual(s["nodes_failing"], "1/1")
        self.assertEqual(s["daemon_not_ready_reason"], "missing-ticket(uid=2001)")
        self.assertEqual(s["principals_mint_failing_in_namespace"], "1/1")
        self.assertEqual(s["principal_ready"], "False")

    def test_multiline_kinit_message_counts_one_node(self):
        # The bash version counted the wrapped ")" line as a second node.
        p = principal(reason="MintFailed", msg=KINIT_FAIL)
        _, _, s = run(scenario(p=p))
        self.assertEqual(s["nodes_failing"], "1/1")

    def test_signature_falls_back_to_node_message(self):
        # Events and logs unreadable: the per-node message still classifies.
        p = principal(reason="MintFailed", msg="kinit: Clock skew too great")
        forbidden = 'Error from server (Forbidden): events is forbidden'
        rc, out, s = run(scenario(p=p, overrides=[
            {"match": ["get events"], "stderr": forbidden, "rc": 1},
            {"match": ["get pods"], "stderr": 'Error from server (Forbidden): pods is forbidden', "rc": 1},
            {"match": ["get secret"], "stderr": 'Error from server (Forbidden): secrets is forbidden', "rc": 1}]))
        self.assertEqual(rc, 0)
        self.assertEqual(s["error_signature"], "clock-skew")
        self.assertEqual(s["keytab_secret"], "unknown")
        self.assertIn("UNAVAILABLE: Error from server (Forbidden): events is forbidden", out)

    def test_missing_secret_and_duplicate_uid(self):
        dup = principal(name="dg02-dupuid")
        t = json.loads(J(TENANT))
        t["status"]["conditions"] = [{"type": "KeytabsResolved", "status": "False"},
                                     {"type": "RosterConsistent", "status": "False"}]
        p = principal()
        p["status"]["kvnoFromSecret"] = 0
        _, _, s = run(scenario(p=p, others=[dup], tenant=t, secret_rc=1))
        self.assertEqual(s["keytab_secret"], "missing")
        self.assertEqual(s["kvno_observed"], "none")
        self.assertEqual(s["keytabs_resolved"], "False")
        self.assertEqual(s["roster_consistent"], "False")
        self.assertEqual(s["duplicate_uid"], "yes (dg02-dupuid)")

    def test_tenant_missing_and_realm(self):
        _, _, s = run(scenario(overrides=[{"match": ["get tenants"], "rc": 1,
                     "stderr": 'Error from server (NotFound): tenants.kerberator.dell.com "demo" not found'}]))
        self.assertEqual(s["tenant_found"], "no")
        self.assertEqual(s["realm_match"], "unknown")

    def test_realm_mismatch(self):
        t = json.loads(J(TENANT))
        t["spec"]["realm"] = "WRONG.REALM"
        _, _, s = run(scenario(tenant=t))
        self.assertEqual(s["realm_match"], "no")

    def test_stale_and_no_report(self):
        p = principal(reason="", seen="2000-01-01T00:00:00Z", msg="")
        _, _, s = run(scenario(p=p))
        self.assertEqual(s["nodes_no_recent_report"], "1/1")
        self.assertEqual(s["nodes_failing"], "0/1")

    def test_keytab_missing_event(self):
        p = principal(reason="KeytabMissing", msg="keytab missing")
        _, _, s = run(scenario(p=p, events=uid_event("KerberosKeytabMissing", "keytab missing for uid=2001")))
        self.assertEqual(s["error_signature"], "keytab-missing")

    def test_daemon_not_ready_reasons(self):
        # Just started, no probe result yet -> starting, not a daemon fault.
        import datetime as _dt
        just_now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _, _, s = run(scenario(pod_ready=False, pod_started=just_now))
        self.assertEqual(s["daemon_not_ready_reason"], "starting")
        # Long-running and not ready with no probe message -> other.
        _, _, s = run(scenario(pod_ready=False))
        self.assertEqual(s["daemon_not_ready_reason"], "other")

    def test_events_before_current_keytab_are_marked(self):
        # Keytab re-read at 10:00. A success at 09:00 is about the OLD keytab;
        # only the 10:05 failure counts against the current one.
        p = principal(reason="MintFailed", msg=KINIT_FAIL)
        p["status"]["keytabObservedAt"] = "2030-01-01T10:00:00Z"
        events = {"items": [
            {"metadata": {"annotations": {"kerberator.dell.com/uid": "2001"}}, "lastTimestamp": "2030-01-01T10:05:00Z",
             "reason": "KerberosMintFailed", "message": KINIT_FAIL, "involvedObject": {"name": "pod-new"}},
            {"metadata": {"annotations": {"kerberator.dell.com/uid": "2001"}}, "lastTimestamp": "2030-01-01T09:00:00Z",
             "reason": "KerberosMintSucceeded", "message": "Minted", "involvedObject": {"name": "pod-old"}}]}
        _, out, s = run(scenario(p=p, events=events))
        self.assertTrue(s["events_since_keytab_read"].startswith("1 failed, 0 succeeded"), s["events_since_keytab_read"])
        self.assertIn("(x1)  [before current keytab]: Minted", out)
        self.assertNotIn("pod-new (x1)  [before", out)

    def test_old_error_then_success_is_resolved(self):
        logs = ("ERROR: Failed to mint cache for svc-2001@EXAMPLE.COM: Password incorrect\n)\n"
                "SUCCESS: Minted ticket cache svc-2001@EXAMPLE.COM -> /tmp/krb5cc_2001\n")
        _, _, s = run(scenario(logs=logs))
        self.assertEqual(s["error_signature"], "none")

    def test_not_found_lists_principals(self):
        rc, out, _ = run(scenario(overrides=[{"match": ["get principals.kerberator.dell.com 2001"], "rc": 1,
                         "stderr": 'Error from server (NotFound): principals.kerberator.dell.com "2001" not found'}]),
                         name="2001")
        self.assertEqual(rc, 3)
        self.assertIn("kubectl krb list", out)
        self.assertIn("kerberator-demo Principal svc-2001", out)

    def test_unreachable(self):
        rc, out, _ = run([{"match": ["config"], "rc": 1, "stderr": "x"},
                          {"match": ["get principals"], "rc": 1,
                           "stderr": "E1105 memcache.go:265 couldn't get\nThe connection to the server localhost:6443 was refused"}])
        self.assertEqual(rc, 4)
        self.assertIn("UNREACHABLE", out)
        self.assertNotIn("krb list", out)

    def test_never_reads_secret_data(self):
        # Every secret call must be metadata-only (-o name).
        with tempfile.TemporaryDirectory() as d:
            log = pathlib.Path(d, "calls")
            rules = scenario()
            k = pathlib.Path(d, "kubectl")
            k.write_text(FAKE_KUBECTL.replace("argv = ", f"open({str(log)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\nargv = "))
            k.chmod(0o755)
            sc = pathlib.Path(d, "s.json")
            sc.write_text(json.dumps(rules))
            subprocess.run([sys.executable, "-B", str(COLLECT), "svc-2001"], capture_output=True,
                           env=dict(os.environ, PATH=f"{d}:{os.environ['PATH']}", FAKE_SCENARIO=str(sc)))
            calls = log.read_text().splitlines()
        secret_calls = [c for c in calls if " secret" in f" {c}"]
        self.assertTrue(secret_calls)
        for c in secret_calls:
            self.assertIn("-o name", c, c)
        self.assertFalse([c for c in calls if c.startswith("exec") or " exec " in c])


if __name__ == "__main__":
    unittest.main(verbosity=1)
