---
name: kerberator-drain-node
description: Drain a Kubernetes node used by Kerberator for maintenance, verify destination credential coverage before eviction, register a missing Principal only when needed, and verify relocated workloads. Use for node evacuation or maintenance with Kerberos-dependent pods; not for credential revocation or merely restarting a daemon.
---

# Kerberator node drain

Evacuate ordinary workloads while preserving their Kerberos access. Use installed `kubectl` and `kubectl krb`; this skill does not require a source checkout. Check installed command help and cluster API schema when fields or flags differ. Examples below use resolved shell variables, never literal placeholders.

## Essential behavior

- A Principal maps a workload UID to a Kerberos identity in a Tenant. Moving a pod does **not** require a new Principal per node. Eligible daemons independently mint the existing identity's cache; caches are not migrated.
- Credential coverage is the intersection of Tenant daemon placement and Principal `spec.nodeSelector`. Neither schedules workload pods. Workload scheduling must keep every possible destination inside that coverage.
- Use `kubectl drain --ignore-daemonsets`. The current `kubectl krb drain-node` implementation only cordons and deletes matching daemon pods across namespaces; DaemonSets can recreate them on cordoned nodes. It does not evacuate workloads.
- Keep daemons running during eviction. Their host caches (normally `/tmp/krb5cc_<uid>`) survive pod deletion; stopping a daemon neither purges nor revokes tickets.

## 1. Establish target and evacuation scope

Resolve the intended kubeconfig/context and exact node from the request and session. Ask only for unresolved choices; do not silently select among ambiguous clusters. Pass explicit `--context "$CTX"` to every cluster command, the selected `--kubeconfig` when applicable, and `-n "$NS"` for namespaced operations. `NS` is the Tenant/Principal namespace, which can differ from workload namespaces.

Inspect node readiness, labels, taints and cordon state; all pods on the node; owning controllers; PDBs; storage; and available destination capacity:

```bash
kubectl --context "$CTX" get node "$NODE" -o yaml
kubectl --context "$CTX" get pods -A --field-selector "spec.nodeName=$NODE" -o wide
kubectl --context "$CTX" get nodes -o wide --show-labels
kubectl --context "$CTX" get pdb -A
kubectl --context "$CTX" get tenants.kerberator.dell.com,principals.kerberator.dell.com -A -o yaml
```

Read affected pod/controller specs to determine effective workload UIDs, identities, node selectors/affinity, tolerations, topology constraints and volumes. Do not infer an identity solely from a pod name or assume an unspecified `runAsUser` means a particular UID. Identify unmanaged pods, `emptyDir`/host-local data, local-volume affinity, and static/mirror or DaemonSet pods that will remain. Account for operator/KDC pods if hosted here. Missing access or uncertain identity/placement is a blocker to claiming a safe evacuation.

## 2. Prepare credentials BEFORE eviction

Map each affected Kerberos identity to its existing Principal and Tenant. Confirm both are enabled, the keytab reference resolves, and eligible destination nodes have running Tenant daemons and the required consumer setup (for example NFS mounts and host `rpc.gssd`, or an application cache mount).

- **Existing Principal:** reuse it. If destination coverage is missing, prepare the smallest intended change to Principal placement, Tenant daemon placement, or workload controller scheduling. Explain that credential expansion exposes the identity on more nodes, selector narrowing can prune source caches, and controller template edits can roll pods. Preserve source coverage until workloads leave. Do not blindly clear selectors, relabel nodes, edit operator-owned children, or create duplicate UID identities sharing a host cache path.
- **Missing Principal:** collect the existing KDC identity, positive workload UID, Tenant, trusted keytab path and intended node scope. Kerberator registration creates a Kubernetes Secret and Principal; it does not create a KDC account or issue a keytab. If these inputs are unavailable, stop before draining.

For an authorized new registration, inspect for identity/UID conflicts and existing `uid-<UID>` / `uid-<UID>-keytab` objects first, then use:

```bash
kubectl krb --context "$CTX" -n "$NS" add-principal "$TENANT" "$WORKLOAD_UID" \
  --principal "$KERBEROS_PRINCIPAL" --keytab-file "$KEYTAB_PATH" \
  --node-selector "$PLACEMENT_KEY=$PLACEMENT_VALUE"
```

Repeat `--node-selector` for ANDed labels; omit it only when all Tenant daemon nodes are the intended scope. Never print keytab contents or Secret data. Do not use this command's `--dry-run` for a public preview: it prints the keytab as base64. Preview names, identity, UID and placement without credentials. On failure, inspect partial creation before retrying; do not overwrite or delete pre-existing objects.

Show concrete changes and their effects before executing them. Use authorization already supplied for the maintenance task; ask only for missing authorization, such as an unrequested expansion of credential access or workload scheduling changes.

Verify every required Principal with:

```bash
kubectl krb --context "$CTX" -n "$NS" describe principal "$PRINCIPAL_CR"
```

Require positive eligible-node coverage and successful `Minted` observations on **each possible destination**, with ready daemons and observations applicable to the current configuration. Inspect full Principal status/Events if CLI output is truncated. Pod readiness alone checks cache presence/nonempty size, not ticket validity; keytab KVNO alone does not prove minting. If evidence is absent or stale relative to renewal/configuration, wait within a stated bounded interval or investigate read-only; do not assume success or restart daemons merely to generate evidence. Stop before eviction if coverage cannot be established.

## 3. Drain and observe

State the node, affected workloads, credential evidence, expected remaining pods and chosen timeout. Once authorized, execute one node at a time (example timeout: 10 minutes):

```bash
kubectl --context "$CTX" drain "$NODE" --ignore-daemonsets --timeout=10m
```

Keep normal eviction and termination behavior. Do not automatically add `--force`, `--disable-eviction`, `--delete-emptydir-data`, or zero grace periods, change PDBs, or delete blocking pods. Report the concrete blocker and obtain any additional authorization for data loss or disruption-policy changes.

On timeout/failure, inspect remaining pods and events. A failed drain may already have cordoned the node and evicted some pods: report partial completion, leave its state explicit, and do not blindly retry or uncordon. A successful drain does not prove replacement pods are healthy.

## 4. Verify relocation and hand off maintenance

Track replacement pods through their workload controllers, not old pod names. Confirm desired replicas/Jobs recover as appropriate, actual destination nodes are covered, Principal mint evidence remains healthy, and workload readiness plus an existing safe Kerberos-dependent health check succeeds. Do not invent a write test against production data. If application authentication was not tested, state that limitation.

Report the source's cordon state, remaining DaemonSet/static pods, relocated workloads and destinations, credential changes/evidence, and unresolved blockers. Keep the node cordoned for maintenance; do not reboot, delete it, or uncordon merely because drain finished.

When maintenance is complete and returning the node is requested/authorized, verify node health, Tenant daemons and applicable Principal minting, then run `kubectl --context "$CTX" uncordon "$NODE"`. Uncordon allows scheduling; it does not automatically move workloads back. Restore any temporary configuration only after checking its effect on current workloads and credential coverage.

## Sources

- [Kubernetes drain behavior and flags](https://kubernetes.io/docs/reference/kubectl/generated/kubectl_drain/)
- [Safe node maintenance](https://kubernetes.io/docs/tasks/administer-cluster/safely-drain-node/)
- [Workload node placement](https://kubernetes.io/docs/concepts/scheduling-eviction/assign-pod-node/)

Kerberator-specific behavior was checked against its drain CLI, Principal registration, DaemonSet builder, daemon mint loop and readiness implementation. Recheck installed behavior if a later release changes these contracts.
