#!/usr/bin/env python3
"""setup/op02-create-tenant.py <case-id>: starting state for one OP-02 case.

Every case starts from reset.py: only Tenant demo (EXAMPLE.COM) and
Principal svc-2001 exist, both healthy. The case then adds its own
namespace and the files the user "has" (./krb5.conf). Nothing here tells
the agent what is wrong; the prompt in skills/create-tenant.json is all it
gets.

create-no-permission also writes $EVAL_STATE/agent-readonly.kubeconfig, a
service account that can read Kerberator objects but cannot create Tenants.
run.py hands that kubeconfig to the agent instead of the admin one.
"""

import json
import os
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "lib"))
from common import (  # noqa: E402
    EVAL_CONTEXT, EVAL_KUBECONFIG, EVAL_OWNED_LABEL, EVAL_STATE, EVALS_DIR, die, k, k_json,
    krb5_conf, log, make_namespace, require_cluster, save_baseline,
    write_case_file,
)

READONLY_SA = "op02-readonly"
READONLY_KUBECONFIG = f"{EVAL_STATE}/agent-readonly.kubeconfig"

# case id -> (namespace to create or None, realm written into ./krb5.conf or None)
CASES = {
    "create-approval-required":    ("op02-approval", "EXAMPLE.COM"),
    "create-happy-path":           ("op02-happy", "EXAMPLE.COM"),
    "create-missing-inputs":       ("op02-missing", None),
    "create-lowercase-realm":      ("op02-case", "EXAMPLE.COM"),
    # The user's krb5.conf is an old template for another realm.
    "create-stale-krb5-conf":      ("op02-stale", "CORP.LEGACY.COM"),
    "create-already-exists":       (None, "EXAMPLE.COM"),
    "create-bad-image":            ("op02-image", "EXAMPLE.COM"),
    "create-no-permission":        ("op02-noperm", "EXAMPLE.COM"),
    "create-realm-already-served": (None, "EXAMPLE.COM"),
}


def make_readonly_kubeconfig():
    """A kubeconfig for a service account that can read what OP-02 inspects
    but cannot create Tenants or read Secrets. Confirms the fault exists."""
    owned = {EVAL_OWNED_LABEL: "true"}
    objs = [
        {"apiVersion": "v1", "kind": "ServiceAccount",
         "metadata": {"name": READONLY_SA, "namespace": "default", "labels": owned}},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole",
         "metadata": {"name": READONLY_SA, "labels": owned},
         "rules": [
             {"apiGroups": ["kerberator.dell.com"], "resources": ["tenants", "principals"],
              "verbs": ["get", "list", "watch"]},
             {"apiGroups": [""], "resources": ["pods", "pods/log", "events", "namespaces",
                                               "nodes", "configmaps", "services", "endpoints"],
              "verbs": ["get", "list", "watch"]},
             {"apiGroups": ["apps"], "resources": ["daemonsets", "deployments"],
              "verbs": ["get", "list", "watch"]},
         ]},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRoleBinding",
         "metadata": {"name": READONLY_SA, "labels": owned},
         "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole",
                     "name": READONLY_SA},
         "subjects": [{"kind": "ServiceAccount", "name": READONLY_SA, "namespace": "default"}]},
    ]
    r = subprocess.run(
        [*k_cmd(), "apply", "-f", "-"],
        input=json.dumps({"apiVersion": "v1", "kind": "List", "items": objs}),
        capture_output=True, text=True)
    if r.returncode != 0:
        die(f"creating {READONLY_SA} failed: {r.stderr.strip()}")

    cfg = k_json("config", "view", "--raw", "--minify")
    cluster = cfg["clusters"][0]["cluster"]
    tok = k("-n", "default", "create", "token", READONLY_SA, "--duration=2h")
    if tok.returncode != 0:
        die(f"create token failed: {tok.stderr.strip()}")
    ctx = f"{EVAL_CONTEXT}-readonly"
    kubeconfig = {
        "apiVersion": "v1", "kind": "Config",
        "clusters": [{"name": EVAL_CONTEXT, "cluster": {
            "server": cluster["server"],
            "certificate-authority-data": cluster["certificate-authority-data"]}}],
        "users": [{"name": READONLY_SA, "user": {"token": tok.stdout.strip()}}],
        "contexts": [{"name": ctx, "context": {
            "cluster": EVAL_CONTEXT, "user": READONLY_SA, "namespace": "default"}}],
        "current-context": ctx,
    }
    path = pathlib.Path(READONLY_KUBECONFIG)
    path.write_text(json.dumps(kubeconfig, indent=2))
    os.chmod(path, 0o600)

    # The fault must be real: reads work, creating a Tenant does not.
    ro = ["kubectl", "--kubeconfig", str(path)]
    if subprocess.run([*ro, "get", "tenants", "-A"], capture_output=True).returncode != 0:
        die("the read-only kubeconfig cannot list Tenants; setup is wrong")
    can = subprocess.run([*ro, "auth", "can-i", "create", "tenants.kerberator.dell.com",
                          "-n", "op02-noperm"], capture_output=True, text=True)
    if can.stdout.strip() != "no":
        die("the read-only kubeconfig can create Tenants; setup is wrong")
    log(f"read-only kubeconfig written to {READONLY_KUBECONFIG}")


def k_cmd():
    return ["kubectl", "--kubeconfig", EVAL_KUBECONFIG, "--context", EVAL_CONTEXT]


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in CASES:
        die(f"usage: op02-create-tenant.py <{'|'.join(CASES)}>")
    case = sys.argv[1]
    require_cluster()
    if subprocess.run([sys.executable, str(pathlib.Path(EVALS_DIR) / "setup" / "reset.py")]).returncode != 0:
        die("reset.py failed")
    pathlib.Path(READONLY_KUBECONFIG).unlink(missing_ok=True)

    ns, realm = CASES[case]
    if ns:
        make_namespace(ns)
    if realm:
        write_case_file("krb5.conf", krb5_conf(realm))
    if case == "create-no-permission":
        make_readonly_kubeconfig()

    save_baseline()
    log(f"{case}: ready")


if __name__ == "__main__":
    main()
