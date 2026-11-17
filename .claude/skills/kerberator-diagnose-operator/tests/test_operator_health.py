#!/usr/bin/env python3
"""Unit tests for scripts/operator_health.py. No cluster needed.

    python3 tests/test_operator_health.py

A fake `kubectl` on PATH answers each call from a scenario: a list of rules,
each a set of substrings the command line must contain and the stdout /
stderr / exit code to return. The first matching rule wins. The fake also
logs every call, so the tests can check the script only ever reads.
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest

sys.dont_write_bytecode = True
HERE = pathlib.Path(__file__).resolve().parent
SCRIPT = HERE.parent / "scripts" / "operator_health.py"

FAKE_KUBECTL = r'''#!/usr/bin/env python3
import json, os, sys
argv = " ".join(sys.argv[1:])
open(os.environ["FAKE_LOG"], "a").write(argv + "\n")
for rule in json.load(open(os.environ["FAKE_SCENARIO"])):
    if all(m in argv for m in rule["match"]):
        sys.stdout.write(rule.get("stdout", ""))
        sys.stderr.write(rule.get("stderr", ""))
        sys.exit(rule.get("rc", 0))
sys.stderr.write("fake kubectl: no rule for: " + argv + "\n")
sys.exit(1)
'''

NS, DEP, IMAGE = "kerberator-system", "kerberator", "ghcr.io/dell/kerberator-operator:v0.10.0"
NOW_TS = time.time()


def J(o):
    return json.dumps(o)


def deployment(ready=1, replicas=1, image=IMAGE, webhook=True):
    args = ["--metrics-addr=:8080", "--health-probe-addr=:8081", "--daemon-image=ghcr.io/dell/kerberator-daemon:v0.10.0"]
    if webhook:
        args.append("--webhook-enabled=true")
    return {"metadata": {"name": DEP, "namespace": NS, "labels": {
                "app.kubernetes.io/name": "kerberator", "app.kubernetes.io/version": "0.10.0",
                "helm.sh/chart": "kerberator-0.10.0"}},
            "spec": {"replicas": replicas, "selector": {"matchLabels": {"app.kubernetes.io/name": "kerberator",
                                                                         "app.kubernetes.io/instance": "kerberator"}},
                     "template": {"spec": {"containers": [{"name": "manager", "image": image, "args": args}]}}},
            "status": {"readyReplicas": ready, "availableReplicas": ready, "updatedReplicas": replicas,
                       "conditions": [{"type": "Available", "status": "True" if ready else "False",
                                       "reason": "MinimumReplicasAvailable" if ready else "MinimumReplicasUnavailable"},
                                      {"type": "Progressing", "status": "True", "reason": "NewReplicaSetAvailable"}]}}


def pod(ready=True, waiting=None, restarts=0, scheduled=True, phase="Running", last=None):
    cs = {"name": "manager", "ready": ready, "restartCount": restarts, "image": IMAGE,
          "state": {"waiting": waiting} if waiting else {"running": {}}}
    if last:
        cs["lastState"] = {"terminated": last}
    conds = [{"type": "PodScheduled", "status": "True" if scheduled else "False",
              "reason": "" if scheduled else "Unschedulable",
              "message": "" if scheduled else "0/3 nodes are available: 3 node(s) had untolerated taint"}]
    return {"metadata": {"name": f"{DEP}-6d9f-abcde"}, "spec": {"nodeName": "node-1" if scheduled else None},
            "status": {"phase": phase, "conditions": conds, "containerStatuses": [] if not scheduled else [cs]}}


def crd(name, served=("v1alpha1",), storage="v1alpha1", established="True"):
    return {"metadata": {"name": name},
            "spec": {"versions": [{"name": v, "served": True, "storage": v == storage} for v in served]},
            "status": {"conditions": [{"type": "Established", "status": established}]}}


def log(level, msg, **kw):
    return J({"level": level, "ts": kw.pop("ts", NOW_TS), "msg": msg, **kw})


STARTUP = log("info", "kerberator-operator", version="v0.10.0", daemonImage="ghcr.io/dell/kerberator-daemon:v0.10.0")
VWC = {"items": [{"metadata": {"name": "kerberator-principal-validate"},
                  "webhooks": [{"failurePolicy": "Fail", "clientConfig": {
                      "caBundle": "Y2E=", "service": {"namespace": NS, "name": "kerberator-webhook"}}}]}]}


def scenario(contexts=("kind-test",), dep=None, deps_rule=None, pods=None, events=None, logs=STARTUP + "\n",
             prev_logs="", crds=None, plugin="kubectl-krb v0.10.0\n", tenants=None, endpoints=1, overrides=()):
    dep = dep if dep is not None else deployment()
    pods = pods if pods is not None else [pod()]
    crds = crds if crds is not None else {n: crd(n) for n in ("tenants.kerberator.dell.com", "principals.kerberator.dell.com")}
    rules = list(overrides) + [
        {"match": ["config get-contexts"], "stdout": "\n".join(contexts) + "\n"},
        {"match": ["config current-context"], "stdout": contexts[0] + "\n"},
        {"match": ["krb version"], "stdout": plugin},
    ]
    if deps_rule:
        rules.append(deps_rule)
    rules += [
        {"match": ["get deployments", "-l app.kubernetes.io/name=kerberator"], "stdout": J({"items": [dep] if dep else []})},
        {"match": ["get pods"], "stdout": J({"items": pods})},
        {"match": ["get events"], "stdout": J({"items": events or []})},
        {"match": ["logs", "--previous"], "stdout": prev_logs},
        {"match": ["logs"], "stdout": logs},
        {"match": ["get validatingwebhookconfigurations"], "stdout": J(VWC)},
        {"match": ["get endpoints"], "stdout": J({"subsets": [{"addresses": [{"ip": "10.0.0.1"}] * endpoints}] if endpoints else []})},
        {"match": ["get tenants.kerberator.dell.com"], "stdout": J({"items": tenants or []})},
    ]
    for name, obj in crds.items():
        rules.append({"match": ["get crd", name], "stdout": J(obj)} if obj else
                     {"match": ["get crd", name], "rc": 1,
                      "stderr": f'Error from server (NotFound): customresourcedefinitions.apiextensions.k8s.io "{name}" not found'})
    return rules


def run(rules, *args):
    with tempfile.TemporaryDirectory() as d:
        k = pathlib.Path(d, "kubectl")
        k.write_text(FAKE_KUBECTL)
        k.chmod(0o755)
        sc, lg = pathlib.Path(d, "scenario.json"), pathlib.Path(d, "calls.log")
        sc.write_text(json.dumps(rules))
        lg.write_text("")
        env = dict(os.environ, PATH=f"{d}:{os.environ['PATH']}", FAKE_SCENARIO=str(sc), FAKE_LOG=str(lg))
        p = subprocess.run([sys.executable, "-B", str(SCRIPT), *args], capture_output=True, text=True, env=env)
        calls = lg.read_text().splitlines()
    head = {}
    for line in p.stdout.splitlines():
        if line == "---":
            break
        if ": " in line and not line.startswith("FINDING"):
            k_, v = line.split(": ", 1)
            head[k_] = v
    findings = [l.split(" ", 3)[2] for l in p.stdout.splitlines() if l.startswith("FINDING:")]
    return p.returncode, p.stdout, head, findings, calls


class OperatorHealth(unittest.TestCase):
    def test_healthy(self):
        rc, out, h, f, _ = run(scenario())
        self.assertEqual((h["STATUS"], h["FAULT_DOMAIN"]), ("healthy", "none-observed"))
        self.assertEqual(rc, 0)
        self.assertEqual(h["OPERATOR"], f"{NS}/{DEP}")
        self.assertIn("operator-ready", out)
        self.assertIn("operator running version (from its startup log line):    v0.10.0", out)

    def test_crash_loop(self):
        p = pod(ready=False, restarts=7, waiting={"reason": "CrashLoopBackOff", "message": "back-off 5m"},
                last={"reason": "Error", "exitCode": 1})
        rc, _, h, f, calls = run(scenario(dep=deployment(ready=0), pods=[p],
                                          prev_logs=log("error", "unable to start manager", error="no matches for kind Tenant")))
        self.assertEqual((h["STATUS"], h["FAULT_DOMAIN"]), ("unhealthy", "controller-runtime"))
        self.assertIn("operator-crash-loop", f)
        self.assertEqual(rc, 4)
        self.assertTrue(any("--previous" in c for c in calls), "previous logs read for a restarted pod")

    def test_image_pull(self):
        p = pod(ready=False, phase="Pending", waiting={"reason": "ImagePullBackOff", "message": "not found"})
        _, out, h, f, _ = run(scenario(dep=deployment(ready=0), pods=[p]))
        self.assertEqual((h["STATUS"], h["FAULT_DOMAIN"]), ("unhealthy", "controller-image-distribution"))
        self.assertIn("operator-image-pull", f)
        self.assertIn(IMAGE, out)

    def test_unschedulable(self):
        p = pod(ready=False, scheduled=False, phase="Pending")
        ev = [{"type": "Warning", "reason": "FailedScheduling", "message": "0/3 nodes are available",
               "involvedObject": {"name": f"{DEP}-6d9f-abcde"}}]
        _, out, h, f, _ = run(scenario(dep=deployment(ready=0), pods=[p], events=ev))
        self.assertEqual((h["STATUS"], h["FAULT_DOMAIN"]), ("unhealthy", "controller-scheduling"))
        self.assertIn("FailedScheduling", out)

    def test_running_with_reconcile_errors(self):
        logs = "\n".join([STARTUP] + [log("error", "Reconciler error", controller="tenant", namespace="team-a",
                                          name="payroll", error='roles.rbac.authorization.k8s.io is forbidden')] * 3)
        rc, out, h, f, _ = run(scenario(logs=logs))
        self.assertEqual((h["STATUS"], h["FAULT_DOMAIN"]), ("running-with-errors", "reconciliation-or-configuration"))
        self.assertIn("reconcile-errors", f)
        self.assertIn("object=team-a/payroll", out)
        self.assertIn("x3", out)

    def test_errors_for_a_deleted_tenant_are_history(self):
        logs = "\n".join([STARTUP, log("error", "Reconciler error", controller="tenant", namespace="team-a",
                                       name="gone", error="refusing to modify ConfigMap")])
        gone = {"match": ["get tenants.kerberator.dell.com gone"], "rc": 1,
                "stderr": 'Error from server (NotFound): tenants.kerberator.dell.com "gone" not found'}
        _, _, h, f, _ = run(scenario(logs=logs, overrides=[gone]))
        self.assertEqual(h["STATUS"], "healthy")
        self.assertIn("errors-for-deleted-object", f)

    def test_failing_rollout_with_old_pod_serving(self):
        new = pod(ready=False, phase="Pending", waiting={"reason": "ErrImagePull", "message": "not found"})
        old = pod(ready=True)
        _, _, h, f, _ = run(scenario(dep=deployment(ready=1), pods=[old, new]))
        self.assertEqual(h["FAULT_DOMAIN"], "controller-image-distribution")
        self.assertIn("previous-pod-still-serving", f)

    def test_old_errors_are_not_current(self):
        logs = "\n".join([STARTUP, log("error", "Reconciler error", controller="tenant", namespace="a", name="b",
                                       error="x", ts=NOW_TS - 3 * 3600)])
        _, _, h, f, _ = run(scenario(logs=logs))
        self.assertEqual(h["STATUS"], "healthy")
        self.assertNotIn("reconcile-errors", f)

    def test_missing_crd(self):
        crds = {"tenants.kerberator.dell.com": crd("tenants.kerberator.dell.com"), "principals.kerberator.dell.com": None}
        _, out, h, f, _ = run(scenario(crds=crds))
        self.assertEqual((h["STATUS"], h["FAULT_DOMAIN"]), ("installation-incomplete", "operator-installation-crds"))
        self.assertIn("principals.kerberator.dell.com: MISSING", out)

    def test_crd_version_mismatch(self):
        crds = {"tenants.kerberator.dell.com": crd("tenants.kerberator.dell.com"),
                "principals.kerberator.dell.com": crd("principals.kerberator.dell.com", served=("v1beta1",), storage="v1beta1")}
        _, _, h, f, _ = run(scenario(crds=crds))
        self.assertEqual((h["STATUS"], h["FAULT_DOMAIN"]), ("installation-mismatch", "operator-installation-crds"))
        self.assertIn("crd-version-mismatch", f)

    def test_version_difference_is_not_breakage(self):
        rc, out, h, f, _ = run(scenario(plugin="kubectl-krb v0.9.0\n"))
        self.assertEqual((h["STATUS"], h["FAULT_DOMAIN"]), ("compatibility-unverified", "version-evidence"))
        self.assertEqual(rc, 0)
        self.assertIn("cli-version-local-only", f)
        self.assertIn("LOCAL machine only", out)

    def test_dev_plugin_version_string(self):
        # Real output of a locally built plugin (seen on a kind quickstart).
        _, out, h, f, _ = run(scenario(plugin="kubectl-krb dev (02fbf45a8444-dirty, go1.27.2, darwin/arm64)\n"))
        self.assertIn("LOCAL machine only, not the cluster): dev", out)
        self.assertIn("dev-build", f)
        self.assertEqual(h["STATUS"], "compatibility-unverified")

    def test_build_info_is_not_a_version_difference(self):
        logs = log("info", "kerberator-operator", version="v0.10.0 (unknown, go1.26.8, linux/amd64)") + "\n"
        _, _, h, f, _ = run(scenario(logs=logs, plugin="kubectl-krb v0.10.0\n"))
        self.assertEqual(h["STATUS"], "healthy")
        self.assertNotIn("version-difference", f)

    def test_runtime_restart_is_explained(self):
        p = pod(ready=True, restarts=5, last={"reason": "Unknown", "exitCode": 255})
        _, out, h, f, _ = run(scenario(pods=[p]))
        self.assertEqual(h["STATUS"], "healthy")
        self.assertIn("container runtime restarted", out)

    def test_rbac_limited(self):
        deny = {"match": ["get deployments"], "rc": 1,
                "stderr": 'Error from server (Forbidden): deployments.apps is forbidden: User "dev" cannot list resource "deployments"'}
        rc, _, h, f, _ = run(scenario(deps_rule=deny))
        self.assertEqual((h["STATUS"], h["FAULT_DOMAIN"]), ("unverified", "insufficient-rbac"))
        self.assertEqual(h["OPERATOR"], "unknown")
        self.assertIn("operator-access", f)
        self.assertEqual(rc, 3)

    def test_operator_not_installed(self):
        _, _, h, f, _ = run(scenario(dep=False))
        self.assertEqual((h["STATUS"], h["FAULT_DOMAIN"]), ("installation-incomplete", "operator-installation"))
        self.assertIn("operator-not-found", f)

    def test_tenant_daemon_is_handed_off(self):
        t = {"metadata": {"namespace": "kerberator-demo", "name": "demo"},
             "status": {"conditions": [{"type": "DaemonSetReady", "status": "False", "message": "0/2 ready"}]}}
        _, out, h, f, _ = run(scenario(tenants=[t]))
        self.assertEqual((h["STATUS"], h["FAULT_DOMAIN"]), ("healthy", "tenant-daemon-out-of-scope"))
        self.assertIn("tenant-daemons-not-ready", f)
        self.assertIn("kerberator-diagnose-tenant", out)

    def test_multiple_contexts_contacts_nothing(self):
        rc, out, h, _, calls = run(scenario(contexts=("kind-a", "prod-b")))
        self.assertEqual((h["STATUS"], h["FAULT_DOMAIN"]), ("requires-user-selection", "context-selection"))
        self.assertEqual(rc, 2)
        self.assertIn("kind-a, prod-b", out)
        self.assertTrue(all(c.startswith("config ") for c in calls), calls)
        # With an explicit context it proceeds.
        _, _, h2, _, calls2 = run(scenario(contexts=("kind-a", "prod-b")), "--context", "prod-b")
        self.assertEqual(h2["STATUS"], "healthy")
        self.assertTrue(any("--context prod-b" in c for c in calls2))

    def test_webhook_without_endpoints(self):
        _, _, h, f, _ = run(scenario(endpoints=0))
        self.assertEqual(f.count("webhook-no-endpoints"), 1, "reported once, not once per webhook rule")

    def test_only_reads(self):
        p = pod(ready=False, restarts=2, waiting={"reason": "CrashLoopBackOff"}, last={"reason": "Error", "exitCode": 1})
        _, _, _, _, calls = run(scenario(dep=deployment(ready=0), pods=[p]))
        for c in calls:
            words, skip = [], False
            for w in c.split():                 # positional words only (skip `-n ns` etc.)
                if skip:
                    skip = False
                elif w.startswith("-"):
                    skip = w in ("-n", "-l", "-o", "-c", "--context", "--kubeconfig")
                else:
                    words.append(w)
            verb = words[0] if words else ""
            self.assertIn(verb, {"config", "get", "logs", "krb"}, c)
            self.assertNotIn("secret", c, c)
            self.assertNotIn(" -f", f" {c}", c)
            self.assertNotIn("--follow", c, c)
            if verb == "krb":
                self.assertEqual(words[:2], ["krb", "version"], c)


if __name__ == "__main__":
    unittest.main(verbosity=1)
