#!/usr/bin/env python3
"""make-readonly-kubeconfig.py <out-path>

For T4: a kubeconfig that can read Kerberator resources, pods, DaemonSets
and events in the demo namespace, but cannot read Secrets or exec into
pods. With no exec, the agent has no way to ask the KDC for its KVNO.
"""
import os

import lib
from lib import kc, sh

RBAC = """\
apiVersion: v1
kind: ServiceAccount
metadata: {{name: dg03-readonly, labels: {{dg03-test: t4}}}}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata: {{name: dg03-readonly, labels: {{dg03-test: t4}}}}
rules:
  - apiGroups: [kerberator.dell.com]
    resources: [principals, tenants]
    verbs: [get, list, watch]
  - apiGroups: [""]
    resources: [pods, events]
    verbs: [get, list, watch]
  - apiGroups: [apps]
    resources: [daemonsets]
    verbs: [get, list, watch]
  - apiGroups: [events.k8s.io]
    resources: [events]
    verbs: [get, list, watch]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata: {{name: dg03-readonly, labels: {{dg03-test: t4}}}}
roleRef: {{apiGroup: rbac.authorization.k8s.io, kind: Role, name: dg03-readonly}}
subjects: [{{kind: ServiceAccount, name: dg03-readonly, namespace: {ns}}}]
"""

KUBECONFIG = """\
apiVersion: v1
kind: Config
clusters: [{{name: dg03, cluster: {{server: "{server}", certificate-authority-data: "{ca}"}}}}]
users: [{{name: dg03-readonly, user: {{token: "{token}"}}}}]
contexts: [{{name: dg03-readonly, context: {{cluster: dg03, user: dg03-readonly, namespace: {ns}}}}}]
current-context: dg03-readonly
"""


def main(argv):
    if len(argv) != 1:
        lib.die("usage: make-readonly-kubeconfig.py <out-path>")
    out = argv[0]

    kc("apply", "-f", "-", input=RBAC.format(ns=lib.NS))

    view = ["kubectl", "config", "view", "--minify", "--raw", "-o"]
    server = sh(view + ["jsonpath={.clusters[0].cluster.server}"])
    ca = sh(view + ["jsonpath={.clusters[0].cluster.certificate-authority-data}"])
    token = kc("create", "token", "dg03-readonly", "--duration=2h").strip()

    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(KUBECONFIG.format(server=server, ca=ca, token=token, ns=lib.NS))

    # Prove the restriction before the test relies on it.
    env = dict(os.environ, KUBECONFIG=out)
    answer = sh(["kubectl", "auth", "can-i", "create", "pods/exec", "-n", lib.NS], check=False, env=env)
    if "yes" in answer.splitlines():
        lib.die("read-only kubeconfig can still exec into pods; T4 would not be valid")
    print(f"  read-only kubeconfig written to {out} (no exec, no secrets)")


if __name__ == "__main__":
    lib.main(main)
