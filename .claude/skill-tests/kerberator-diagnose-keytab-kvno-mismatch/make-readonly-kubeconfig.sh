#!/usr/bin/env bash
# make-readonly-kubeconfig.sh <out-path>
#
# For T4: a kubeconfig that can read Kerberator resources, pods, DaemonSets
# and events in the demo namespace, but cannot read Secrets or exec into
# pods. With no exec, the agent has no way to ask the KDC for its KVNO.
source "$(dirname "$0")/lib.sh"
[[ $# -eq 1 ]] || die "usage: make-readonly-kubeconfig.sh <out-path>"
OUT="$1"

kc apply -f - >/dev/null <<EOF
apiVersion: v1
kind: ServiceAccount
metadata: {name: dg03-readonly, labels: {dg03-test: t4}}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata: {name: dg03-readonly, labels: {dg03-test: t4}}
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
metadata: {name: dg03-readonly, labels: {dg03-test: t4}}
roleRef: {apiGroup: rbac.authorization.k8s.io, kind: Role, name: dg03-readonly}
subjects: [{kind: ServiceAccount, name: dg03-readonly, namespace: $NS}]
EOF

server="$(kubectl config view --minify --raw -o jsonpath='{.clusters[0].cluster.server}')"
ca="$(kubectl config view --minify --raw -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')"
token="$(kc create token dg03-readonly --duration=2h)"

umask 077
cat >"$OUT" <<EOF
apiVersion: v1
kind: Config
clusters: [{name: dg03, cluster: {server: "$server", certificate-authority-data: "$ca"}}]
users: [{name: dg03-readonly, user: {token: "$token"}}]
contexts: [{name: dg03-readonly, context: {cluster: dg03, user: dg03-readonly, namespace: $NS}}]
current-context: dg03-readonly
EOF

# Prove the restriction before the test relies on it.
if KUBECONFIG="$OUT" kubectl auth can-i create pods/exec -n "$NS" | grep -qx yes; then
  die "read-only kubeconfig can still exec into pods; T4 would not be valid"
fi
echo "  read-only kubeconfig written to $OUT (no exec, no secrets)"
