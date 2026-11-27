#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: Copyright (c) 2026 Dell Technologies
"""OP-10 trusted fixture preparation and baseline snapshot.

Evaluator-only: never copy this file, evals.json or the baseline into the
agent's workspace.

prepare(scenario, environment) -> SetupResult (dict)

Every case needs a fixture, so prepare() only changes the cluster when the
environment passes the mutation gate (allow_fixture_mutation, is_disposable,
fixture in allowed_fixture_ids, explicit kind-* context). Otherwise BLOCKED.

What it does:
  1. reset: delete the eval-owned namespaces op10-eval / op10-eval-b (refuses a
     namespace that is not labelled eval-owned) and the op10-no-delete RBAC;
  2. build the case fixture (Tenants, Principals, placeholder keytab Secrets);
  3. preflight: the fixture is really there (for empty_tenant, the operator
     created the Tenant's children);
  4. baseline: metadata.uid of every fixture object, written to
     <artifact_dir>/<suite>/<case>/baseline.json. Secrets are read by
     metadata only, never their data.

For op10-09 the agent must run as ServiceAccount op10-no-delete: its kubeconfig
is written to the artifact dir and returned in agent_inputs; the runner hands
that file (only) to the agent.

CLI (manual use):
  python3 setup.py --case-id op10-01-principal-approve --context kind-kerberator-eval \\
      --artifact-dir /private/op10 --workspace-dir /tmp/agent-ws --allow-mutation
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

_SKILL_TESTS = Path(__file__).resolve().parents[2]
if str(_SKILL_TESTS) not in sys.path:
    sys.path.insert(0, str(_SKILL_TESTS))
from harness import environment as envlib  # noqa: E402
from harness import scenario as sc  # noqa: E402

SUITE_ID = "op10_remove_principal_or_tenant"
NS_A, NS_B = "op10-eval", "op10-eval-b"
OWNED = "kerberator.dell.com/eval-owned"
NO_DELETE_SA = "op10-no-delete"
GROUP = "kerberator.dell.com"
API = f"{GROUP}/v1alpha1"

RESOURCE = {
    "Principal": f"principals.{GROUP}",
    "Tenant": f"tenants.{GROUP}",
    "Secret": "secrets",
    "DaemonSet": "daemonsets.apps",
    "ConfigMap": "configmaps",
    "Role": "roles.rbac.authorization.k8s.io",
    "RoleBinding": "rolebindings.rbac.authorization.k8s.io",
    "ServiceAccount": "serviceaccounts",
}
TENANT_CHILDREN = ("DaemonSet", "{t}-kerberator-daemon"), ("ConfigMap", "{t}-roster"), \
    ("Secret", "{t}-keytabs"), ("Role", "{t}-kerberator-daemon-events"), \
    ("RoleBinding", "{t}-kerberator-daemon-events")


class SetupError(Exception):
    """A precondition definitively failed -> BLOCKED."""


class SetupUnknown(Exception):
    """Evidence is missing or a wait timed out -> UNKNOWN."""


# ------------------------------------------------------------------ kubectl
class Kube:
    """kubectl pinned to one kubeconfig + context (never the ambient default)."""

    def __init__(self, context: str, kubeconfig: str | None = None, timeout: int = 60):
        self.context, self.kubeconfig, self.timeout = context, kubeconfig, timeout

    def run(self, *args: str, stdin: str | None = None, timeout: int | None = None) -> subprocess.CompletedProcess:
        cmd = ["kubectl", "--context", self.context]
        if self.kubeconfig:
            cmd += ["--kubeconfig", str(self.kubeconfig)]
        try:
            return subprocess.run(cmd + list(args), input=stdin, capture_output=True, text=True,
                                  timeout=timeout or self.timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            raise SetupUnknown(f"kubectl {' '.join(args[:3])} timed out") from exc
        except OSError as exc:
            raise SetupUnknown(f"cannot run kubectl: {exc}") from exc

    def meta(self, kind: str, name: str, namespace: str | None) -> dict | None:
        """{uid, deleting} or None if NotFound. Metadata only (safe for Secrets)."""
        args = ["get", RESOURCE.get(kind, kind), name, "--no-headers",
                "-o", "custom-columns=UID:.metadata.uid,DEL:.metadata.deletionTimestamp"]
        if namespace:
            args += ["-n", namespace]
        r = self.run(*args)
        if r.returncode != 0:
            if "NotFound" in r.stderr or "not found" in r.stderr.lower():
                return None
            raise SetupUnknown(f"cannot read {kind}/{name}: {r.stderr.strip()[:200]}")
        parts = r.stdout.split()
        if not parts:
            raise SetupUnknown(f"empty metadata for {kind}/{name}")
        return {"uid": parts[0], "deleting": len(parts) > 1 and parts[1] != "<none>"}


def wait_until(what: str, fn, timeout: int, interval: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while True:
        if fn():
            return
        if time.monotonic() >= deadline:
            raise SetupUnknown(f"timed out after {timeout}s waiting for {what}")
        time.sleep(min(interval, max(0.0, deadline - time.monotonic())))


# ------------------------------------------------------------------ fixture objects
def _labels() -> dict:
    return {OWNED: "true"}


def tenant(name: str, ns: str, realm: str) -> dict:
    return {"apiVersion": API, "kind": "Tenant",
            "metadata": {"name": name, "namespace": ns, "labels": _labels()},
            "spec": {"realm": realm, "daemon": {
                "pruneStale": True,
                # Own cache dir per fixture Tenant so fixtures never share tickets.
                "hostCachePath": f"/tmp/{ns}-{name}",
                "krb5Conf": (f"[libdefaults]\n  default_realm = {realm}\n  dns_lookup_kdc = false\n"
                             f"[realms]\n  {realm} = {{\n    kdc = kdc.{ns}.svc.cluster.local:88\n  }}\n")}}}


def keytab_secret(name: str, ns: str, key: str) -> dict:
    # Placeholder bytes, not a keytab: OP-10 only needs the reference to exist.
    data = base64.b64encode(b"op10-eval placeholder, not a keytab").decode()
    return {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": {"name": name, "namespace": ns, "labels": _labels()},
            "data": {key: data}}


def principal(name: str, ns: str, uid: int, tenant_name: str, realm: str, secret: str, key: str) -> dict:
    return {"apiVersion": API, "kind": "Principal",
            "metadata": {"name": name, "namespace": ns, "labels": _labels()},
            "spec": {"uid": uid, "principal": f"{name}@{realm}", "tenantRef": {"name": tenant_name},
                     "keytabSecretRef": {"name": secret, "key": key}}}


def _principal_set(name: str, ns: str, uid: int, tenant_name: str = "op10", realm: str = "OP10.EXAMPLE.COM",
                   secret: str | None = None, key: str | None = None) -> list[dict]:
    secret = secret or f"op10-{name}-keytab"
    key = key or f"{name}.keytab"
    return [keytab_secret(secret, ns, key), principal(name, ns, uid, tenant_name, realm, secret, key)]


def fixture_objects(fixture: str) -> list[dict]:
    """The objects a fixture creates. Pure function (no cluster access)."""
    base = [tenant("op10", NS_A, "OP10.EXAMPLE.COM")]
    if fixture in ("principal_with_secret", "no_delete_permission"):
        return base + _principal_set("uid-1001", NS_A, 61001) + _principal_set("uid-1002", NS_A, 61002)
    if fixture == "empty_tenant":
        return base + [tenant("op10-empty", NS_A, "OP10EMPTY.EXAMPLE.COM")] + _principal_set("uid-1002", NS_A, 61002)
    if fixture == "decoy_only":
        return base + _principal_set("uid-10070", NS_A, 61070)
    if fixture == "shared_secret":
        shared = keytab_secret("op10-shared-keytab", NS_A, "shared.keytab")
        return base + [shared,
                       principal("uid-1003", NS_A, 61003, "op10", "OP10.EXAMPLE.COM", "op10-shared-keytab", "shared.keytab"),
                       principal("uid-1004", NS_A, 61004, "op10", "OP10.EXAMPLE.COM", "op10-shared-keytab", "shared.keytab")]
    if fixture == "two_namespaces":
        return (base + _principal_set("uid-1001", NS_A, 61001)
                + [tenant("op10b", NS_B, "OP10B.EXAMPLE.COM")]
                + _principal_set("uid-1001", NS_B, 61011, "op10b", "OP10B.EXAMPLE.COM"))
    raise SetupError(f"unknown fixture {fixture!r}")


FIXTURES = ("principal_with_secret", "empty_tenant", "decoy_only", "shared_secret",
            "two_namespaces", "no_delete_permission")


def _key(kind: str, ns: str | None, name: str) -> str:
    return f"{kind}/{ns or '-'}/{name}"


# ------------------------------------------------------------------ cluster steps
def reset(k: Kube) -> None:
    """Remove everything a previous OP-10 run created. Never touches unlabelled objects."""
    for ns in (NS_A, NS_B):
        r = k.run("get", "namespace", ns, "-o", "json")
        if r.returncode != 0:
            if "NotFound" in r.stderr:
                continue
            raise SetupUnknown(f"cannot read namespace {ns}: {r.stderr.strip()[:200]}")
        labels = (json.loads(r.stdout).get("metadata") or {}).get("labels") or {}
        if labels.get(OWNED) != "true":
            raise SetupError(f"namespace {ns} exists but is not labelled {OWNED}; refusing to delete it")
        r = k.run("delete", "namespace", ns, "--wait=false")
        if r.returncode != 0 and "NotFound" not in r.stderr:
            raise SetupError(f"delete namespace {ns} failed: {r.stderr.strip()[:200]}")
    for kind in ("clusterrolebinding", "clusterrole"):
        k.run("delete", kind, NO_DELETE_SA, "--ignore-not-found", "-l", f"{OWNED}=true")
    for ns in (NS_A, NS_B):
        wait_until(f"namespace {ns} to be deleted (Tenant finalizers need the operator running)",
                   lambda ns=ns: k.meta("namespace", ns, None) is None, timeout=180)


def build(k: Kube, fixture: str) -> list[dict]:
    objs = fixture_objects(fixture)
    for ns in sorted({o["metadata"]["namespace"] for o in objs}):
        r = k.run("create", "namespace", ns)
        if r.returncode != 0:
            raise SetupError(f"create namespace {ns} failed: {r.stderr.strip()[:200]}")
        k.run("label", "namespace", ns, f"{OWNED}=true", "--overwrite")
    r = k.run("apply", "-f", "-", stdin=json.dumps({"apiVersion": "v1", "kind": "List", "items": objs}))
    if r.returncode != 0:
        raise SetupError(f"apply fixture {fixture} failed (are the Kerberator CRDs installed?): {r.stderr.strip()[:300]}")
    return objs


def make_no_delete_kubeconfig(k: Kube, path: Path) -> str:
    """ServiceAccount that can read what OP-10 inspects but cannot delete anything.

    No Secret access at all (get/list on Secrets would expose their data).
    """
    read = ["get", "list", "watch"]
    objs = [
        {"apiVersion": "v1", "kind": "ServiceAccount",
         "metadata": {"name": NO_DELETE_SA, "namespace": NS_A, "labels": _labels()}},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole",
         "metadata": {"name": NO_DELETE_SA, "labels": _labels()},
         "rules": [{"apiGroups": [GROUP], "resources": ["tenants", "principals"], "verbs": read},
                   {"apiGroups": [""], "resources": ["namespaces", "configmaps", "events", "pods"], "verbs": read},
                   {"apiGroups": ["apps"], "resources": ["daemonsets"], "verbs": read},
                   {"apiGroups": ["rbac.authorization.k8s.io"], "resources": ["roles", "rolebindings"],
                    "verbs": read}]},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRoleBinding",
         "metadata": {"name": NO_DELETE_SA, "labels": _labels()},
         "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": NO_DELETE_SA},
         "subjects": [{"kind": "ServiceAccount", "name": NO_DELETE_SA, "namespace": NS_A}]},
    ]
    r = k.run("apply", "-f", "-", stdin=json.dumps({"apiVersion": "v1", "kind": "List", "items": objs}))
    if r.returncode != 0:
        raise SetupError(f"create {NO_DELETE_SA} RBAC failed: {r.stderr.strip()[:200]}")
    r = k.run("config", "view", "--raw", "--minify", "-o", "json")
    if r.returncode != 0:
        raise SetupUnknown("cannot read cluster endpoint from the evaluator kubeconfig")
    cluster = json.loads(r.stdout)["clusters"][0]["cluster"]
    r = k.run("-n", NS_A, "create", "token", NO_DELETE_SA, "--duration=2h")
    if r.returncode != 0:
        raise SetupError(f"create token failed: {r.stderr.strip()[:200]}")
    ctx = f"{k.context}-{NO_DELETE_SA}"
    cfg = {"apiVersion": "v1", "kind": "Config",
           "clusters": [{"name": k.context, "cluster": {
               "server": cluster["server"],
               "certificate-authority-data": cluster.get("certificate-authority-data", "")}}],
           "users": [{"name": NO_DELETE_SA, "user": {"token": r.stdout.strip()}}],
           "contexts": [{"name": ctx, "context": {"cluster": k.context, "user": NO_DELETE_SA, "namespace": NS_A}}],
           "current-context": ctx}
    path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    os.chmod(path, 0o600)
    return ctx


def snapshot(k: Kube, objs: list[dict], extra: list[tuple[str, str, str]] = ()) -> dict:
    out = {}
    for o in objs:
        m = o["metadata"]
        out[_key(o["kind"], m["namespace"], m["name"])] = k.meta(o["kind"], m["name"], m["namespace"])
    for kind, ns, name in extra:
        out[_key(kind, ns, name)] = k.meta(kind, name, ns)
    return out


def tenant_children(tenant_name: str, ns: str) -> list[tuple[str, str, str]]:
    return [(kind, ns, pattern.format(t=tenant_name)) for kind, pattern in TENANT_CHILDREN]


def target_should_exist(scenario: dict) -> bool:
    """Before the run: the target exists, except in the not-found case."""
    exp = scenario["expected"]
    return not (exp.get("target_state") == "absent" and exp.get("result") != "REMOVED")


def preflight(scenario: dict, objects: dict, target_meta: dict | None) -> list[str]:
    issues = []
    missing = [key for key, m in objects.items() if m is None or m["deleting"]]
    if missing:
        issues.append(f"fixture objects missing or deleting: {missing}")
    target = scenario["setup"]["target"]
    if target.get("namespace"):
        present = target_meta is not None and not target_meta["deleting"]
        if present != target_should_exist(scenario):
            issues.append(f"target {target['kind']}/{target['name']} "
                          f"{'exists' if present else 'is missing'} before the run")
    return issues


# ------------------------------------------------------------------ standard entry point
def prepare(scenario: dict, environment: dict) -> dict:
    env = envlib.normalize(environment)
    suite_id = scenario.get("suite_id") or SUITE_ID
    fixture = (scenario.get("setup") or {}).get("fixture")
    target = scenario["setup"].get("target") or {}
    namespace = target.get("namespace") or NS_A

    def result(status, issues, ref=None, agent_inputs=None):
        return envlib.setup_result(status, {**scenario, "suite_id": suite_id}, env, baseline_ref=ref,
                                   issues=issues, namespace=namespace, agent_inputs=agent_inputs)

    issues = envlib.problems(env) + envlib.mutation_gate(env, fixture)
    ctx = env.get("context")
    if ctx and not ctx.startswith("kind-"):
        issues.append("OP-10 fixtures only run on a disposable kind-* context")
    if fixture not in FIXTURES:
        issues.append(f"unknown fixture {fixture!r}")
    if issues:
        return result("BLOCKED", issues)

    k = Kube(ctx, env.get("kubeconfig_path"), timeout=int(env.get("timeout_seconds") or 60))
    agent_inputs = {"prompt": scenario["prompt"]}
    try:
        reset(k)
        objs = build(k, fixture)
        extra: list[tuple[str, str, str]] = []
        if fixture == "empty_tenant":
            extra = tenant_children("op10-empty", NS_A)
            wait_until("the operator to create op10-empty's children",
                       lambda: all(k.meta(kind, name, ns) for kind, ns, name in extra), timeout=180)
        if fixture == "no_delete_permission":
            kc = envlib.artifact_path(env, suite_id, scenario["case_id"], "agent.kubeconfig")
            agent_inputs.update({"kubeconfig": str(kc), "context": make_no_delete_kubeconfig(k, kc)})
        objects = snapshot(k, objs, extra)
        target_meta = (k.meta(target["kind"], target["name"], target["namespace"])
                       if target.get("namespace") else None)
        problems = preflight(scenario, objects, target_meta)
        if problems:
            return result("BLOCKED", problems)
    except SetupError as exc:
        return result("BLOCKED", [str(exc)])
    except SetupUnknown as exc:
        return result("UNKNOWN", [str(exc)])

    baseline = envlib.artifact_path(env, suite_id, scenario["case_id"], "baseline.json")
    baseline.write_text(json.dumps({
        "suite_id": suite_id, "case_id": scenario["case_id"], "context": ctx, "fixture": fixture,
        "target": target, "target_meta": target_meta, "objects": objects,
        "tenant_children": [list(c) for c in extra] if fixture == "empty_tenant" else [],
    }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result("READY", [], str(baseline), agent_inputs)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="OP-10 fixture setup (evaluator only)")
    p.add_argument("--case-id", required=True)
    p.add_argument("--context", required=True)
    p.add_argument("--kubeconfig")
    p.add_argument("--artifact-dir", required=True)
    p.add_argument("--workspace-dir", required=True)
    p.add_argument("--allow-mutation", action="store_true",
                   help="confirm this is a disposable kind cluster you may reset")
    a = p.parse_args(argv)
    scenario = sc.load_case(Path(__file__).resolve().parent, a.case_id)
    env = {"workspace_dir": a.workspace_dir, "artifact_dir": a.artifact_dir, "context": a.context,
           "kubeconfig_path": a.kubeconfig, "is_disposable": a.allow_mutation,
           "allow_fixture_mutation": a.allow_mutation,
           "allowed_fixture_ids": [scenario["setup"]["fixture"]] if a.allow_mutation else []}
    res = prepare(scenario, env)
    print(json.dumps(res, indent=2))
    return {"READY": 0, "BLOCKED": 1}.get(res["status"], 2)


if __name__ == "__main__":
    raise SystemExit(main())
