# Kerberator usage guide

Everything is `kubectl apply`; the `kubectl-krb` plugin is optional.
There is no per-cluster bootstrap ritual beyond the Helm install in
the [README](../README.md#install).

For the underlying architecture and the "how does this actually work"
trace, see [design.md](design.md). For the reconciler-level detail
and the daemon↔operator wire contract, see
[architecture.md](architecture.md).

## Create a Tenant

The `Tenant` CR describes one Kerberos realm + how often to renew
tickets + (optionally) which daemon image to run. See
[`examples/quickstart/tenant.yaml`](../examples/quickstart/tenant.yaml)
for a fully-annotated template.

```yaml
apiVersion: kerberator.dell.com/v1alpha1
kind: Tenant
metadata:
  name: demo
  namespace: kerberator-demo
spec:
  realm: EXAMPLE.COM
  daemon:
    renewMinutes: 720
    pruneStale: true
    krb5Conf: |
      [libdefaults]
        default_realm = EXAMPLE.COM
        dns_lookup_kdc = true
      [realms]
        EXAMPLE.COM = { kdc = kdc.example.com }
```

```bash
kubectl apply -f tenant.yaml
kubectl get tenant -n kerberator-demo        # short names: krbtenant, kt
# NAME   REALM         NODES   PRINCIPALS   AGE
# demo   EXAMPLE.COM   3/3     0            30s
```

Two fields worth knowing about that the example leaves at their
defaults:

- `spec.daemon.image` is optional. It defaults to the image the
  operator was installed with (the `daemon.image` chart value, or
  the operator's flag). Set it only to pin a Tenant to a different
  daemon build.
- `spec.daemon.hostCachePath` defaults to `/tmp`, which is where
  `rpc.gssd` and `cifs.upcall` look for `krb5cc_<uid>`. Override it
  only if your node's consumers are configured for another
  directory.

The operator materializes five children ownerRef'd back to the Tenant:
a DaemonSet, a roster ConfigMap, an aggregated keytab Secret, and an
events Role + RoleBinding bound to the namespace's `default`
ServiceAccount (no ServiceAccount is created).
`kubectl delete tenant demo -n kerberator-demo` cleans all of them up.

## Add a Principal

Each `Principal` references its Tenant and pulls its keytab bytes
from a Secret in the same namespace. See
[`examples/quickstart/principal.yaml`](../examples/quickstart/principal.yaml).

```yaml
apiVersion: v1
kind: Secret
metadata: {name: svc-2001-keytab, namespace: kerberator-demo}
type: Opaque
data:
  svc-2001.keytab: <base64-encoded keytab>
---
apiVersion: kerberator.dell.com/v1alpha1
kind: Principal
metadata: {name: uid-2001, namespace: kerberator-demo}
spec:
  uid: 2001
  principal: svc-2001@EXAMPLE.COM
  tenantRef: {name: demo}
  keytabSecretRef: {name: svc-2001-keytab, key: svc-2001.keytab}
```

```bash
kubectl apply -f principal.yaml
kubectl get principal uid-2001 -n kerberator-demo -o wide   # short names: krbprincipal, kp
# NAME       UID    PRINCIPAL              READY   NODES   KVNO   AGE
# uid-2001   2001   svc-2001@EXAMPLE.COM   True    3/3     3      15s
```

The quickstart names the CR after the principal (`svc-2001`);
`kubectl krb add-principal` names it `uid-<uid>`. Either works.

Within one daemon poll interval (default 10s), every worker's
`/tmp/krb5cc_2001` file exists, is owned `2001:2001`, mode `0600`,
and holds a fresh TGT for `svc-2001@EXAMPLE.COM`. Consumers on the
host (`rpc.gssd`, `cifs.upcall`, ...) can now authenticate
operations for that UID.

The admission webhook validates the Principal against five rules on
create/update: tenantRef exists, uid is a positive integer,
principal parses, keytabSecretRef (when set) has both name and key
(Secret existence is checked at reconcile time), and (on OpenShift)
uid falls inside the namespace's SCC uid-range annotation.

## Delete a Principal

Just delete the CR. The operator holds a finalizer until every daemon
pod confirms the on-node ccache is gone (or the configured
`Tenant.spec.deletionTimeout` elapses). On-node ccache removal
requires `spec.daemon.pruneStale: true` on the Tenant. Without it,
deletion waits `deletionTimeout` (default 60s) and then completes
with a `CcachePurgeTimeout` event.

```bash
kubectl delete principal uid-2001 -n kerberator-demo
# principal.kerberator.dell.com "uid-2001" deleted
# (blocks briefly while the operator waits for KerberosCcachePruned events)
```

If the daemon's `--prune-stale` flag is set (it is off unless
`Tenant.spec.daemon.pruneStale` is `true`; `kubectl krb add-tenant`
sets it to true), the daemon removes ccache files whose UIDs were
minted by *this* daemon in a prior sweep but are no longer in the
roster. Foreign UIDs (a human `kinit` on the node) are never touched.

## Use the `kubectl-krb` plugin

Once you're managing more than a handful of Principals, the
[`kubectl-krb`](../cli/kubectl-krb/README.md) plugin folds the
common day-two questions into focused subcommands.

### Read-only (no cluster mutation)

```bash
kubectl krb list -A                             # every Tenant + Principal in the cluster
kubectl krb describe tenant demo                # deep-dive on one Tenant CR
kubectl krb describe principal uid-2001         # per-node roll-up with recent events
kubectl krb events -A                           # tail all daemon events cluster-wide
kubectl krb events --uid 2001                   # filter to one UID
kubectl krb events --reason KerberosMintFailed  # filter to failures
```

### Create / mutate

```bash
# Scaffold + apply a Tenant CR with client-side validation of the
# admission-webhook rules (realm string shape, image ref shape,
# krb5.conf mentions the realm, etc.). Fails locally before the
# server round-trip if anything looks off.
kubectl krb add-tenant demo \
    --realm EXAMPLE.COM \
    --krb5-conf-file ./krb5.conf

# One-shot Principal + Secret creation. <tenant> and <uid> are positional.
# Principal defaults to svc-<uid>@<tenant.spec.realm>; override with
# --principal if you use a different naming scheme.
kubectl krb add-principal demo 2001 --keytab-file ./svc-2001.keytab

# v0.8+: restrict a Principal to a subset of Node-labeled workers.
# Repeatable flag; AND semantics across all key=value pairs.
kubectl krb add-principal demo 2001 --keytab-file ./svc-2001.keytab \
    --node-selector security-zone=prod-a --node-selector tier=gpu

# Rotate a Principal's keytab correctly (see the "Rotate a keytab"
# section below for why this needs its own command).
kubectl krb rotate-keytab uid-2001 --from-file ./fresh.keytab

# Edit a Tenant or Principal in place with $EDITOR. Post-edit runs a
# client-side sanity check on Tenants and warns if the krb5.conf
# lost its [realms] section or the realm was changed without
# touching the config (common footgun).
kubectl krb edit tenant demo
kubectl krb edit principal uid-2001

# Soft-revoke a Principal or an entire Tenant (v0.7+). The operator
# excludes the target from the roster; the daemon prunes on-node
# ccaches on next poll (subject to Tenant.spec.daemon.pruneStale).
# Non-destructive: the CR, keytab Secret, and audit history are
# preserved. Use `enable` to restore.
kubectl krb disable principal uid-2001 --reason compromised
kubectl krb enable  principal uid-2001
kubectl krb disable tenant demo           # whole-tenant maintenance window
kubectl krb enable  tenant demo
```

### Operational

```bash
kubectl krb drain-node worker-1                 # cordon the node + delete its kerberator-daemon pod (--uncordon reverses)
kubectl krb version                             # plugin version

# Show KVNO + enctypes the operator observed in each Principal's
# referenced keytab Secret. Use this to detect drift between the
# KDC and the K8s Secret (a common failure mode after re-exporting
# a keytab on the KDC without running kubectl krb rotate-keytab).
kubectl krb kvno                                # all Principals in current namespace
kubectl krb kvno uid-2001                       # focused view of one Principal + recent mint failures
```

### Interactive TUI

For discoverability or when you're onboarding new operators, launch
the terminal UI:

```bash
kubectl krb tui
```

Two-pane list/detail view of every Tenant + Principal across every
visible namespace. Auto-refreshes every 3 seconds. Full-screen
modal forms for creating Tenants and Principals, rotating keytabs,
and deleting resources.

Every TUI action calls the same shared driver function as its CLI
counterpart, so what you can do in the TUI is exactly what you can
do at the CLI.

| Key | Action |
|---|---|
| `↑ / ↓` (or `k / j`) | Navigate list |
| `n` | Create a Tenant |
| `t` | Create a Principal |
| `r` | Rotate keytab (Principals only) |
| `s` | Edit node selector (Principals only); opens a picker with cluster label discovery |
| `x` | Toggle disable on selection (soft revoke; no confirm) |
| `e` | Edit selection (spawns `kubectl edit`) |
| `d` | Delete selection (with confirm) |
| `R` / `ctrl+r` | Manual refresh |
| `?` | Help overlay |
| `esc` | Back / cancel |
| `q` / `ctrl+c` | Quit |

## Common workflows

### Rotate a Principal's keytab

**The `kubectl krb rotate-keytab` subcommand does this correctly.
Do not rely on "just update the Secret"; see the gotcha below.**

```bash
kubectl krb rotate-keytab uid-2001 --from-file ./fresh.keytab
```

That command performs the three steps that must happen in order:

1. **Update the Secret data** with the new keytab bytes.
2. **Rollout-restart the Tenant's DaemonSet** so kubelet spins new
   daemon pods that pick up the new mounted Secret contents.
3. **Watch `Principal.status.nodes[]`** until every node reports
   `Minted` with a `LastObserved` timestamp *after* the rollout.

**Gotcha:** the daemon's 10-second poll interval hashes the
**roster** file, not the keytab bytes. Updating the Secret alone
does NOT retrigger a mint. The daemon keeps running with the
kubelet-cached old keytab until either (a) the next
`renewMinutes` boundary fires (default 30 minutes) or (b) the pod is
restarted. So a naive `kubectl apply -f new-secret.yaml` leaves
every node's ccache on the OLD key for up to one renew interval,
which is failed I/O the whole time. Use `rotate-keytab`.

### Check for keytab drift

A related failure mode: someone re-exports a keytab on the KDC
(which bumps the principal's KVNO) but forgets to update the K8s
Secret. The KDC now expects KVNO N+1; the Secret still holds KVNO
N; the daemon's next kinit gets `Preauthentication failed`.

`kubectl krb kvno` shows the KVNO the operator observed in the
Secret. Compare to `kvno <principal>` against the KDC to find
drift:

```bash
# Across every Principal in the namespace:
kubectl krb -n kerberator-demo kvno
# PRINCIPAL  UID   PRINCIPAL              KVNO  OBSERVED  ENCTYPES
# uid-2001   2001  svc-2001@EXAMPLE.COM   9     23s       aes128-cts-hmac-sha1-96, ...
# uid-2002   2002  svc-2002@EXAMPLE.COM   9     23s       aes128-cts-hmac-sha1-96, ...
# uid-2003   2003  svc-2003@EXAMPLE.COM   1     23s       aes128-cts-hmac-sha1-96, ...

# Focused view of one Principal with recent mint failures (drift signal):
kubectl krb -n kerberator-demo kvno uid-2001
```

Under the hood, the operator reads each Principal's referenced
keytab Secret and publishes `status.kvnoFromSecret`,
`status.enctypesFromSecret`, and `status.keytabObservedAt`.
`kubectl get principals -A` shows the KVNO column, and the TUI's
Principal detail pane includes it too.

### Soft-revoke a Principal (or a whole Tenant)

If a Principal's credentials are suspected compromised, or you just
want to pause access without destroying state:

```bash
kubectl krb disable principal uid-2001 --reason compromised
```

Effect:

1. Operator flips `Principal.spec.disabled = true`.
2. On the next reconcile (<1s), the roster ConfigMap is
   regenerated WITHOUT this Principal.
3. Kubelet propagates the updated CM to each daemon pod (60-90s
   in practice, kubelet's default `sync-frequency`).
4. Each daemon detects the roster hash change on its 10s poll,
   prunes the on-node `/tmp/krb5cc_<UID>` (if
   `Tenant.spec.daemon.pruneStale` is `true`; it defaults to false,
   so set it, as the quickstart Tenant does).
5. `Principal.status.conditions[Ready]` moves to `Unknown / Disabled`
   with the reason surfaced.
6. Operator emits a `KerberosPrincipalDisabled` Event on the Principal.

The Principal CR, keytab Secret, per-node status history, and audit
trail all stay put. To restore:

```bash
kubectl krb enable principal uid-2001
```

For a whole-tenant kill switch (maintenance windows), use
`Tenant.spec.disabled` via `kubectl krb disable tenant <name>`.

### Restrict a Principal to specific worker nodes (v0.8+)

By default, every Principal's ccache is placed on every worker node
the Tenant's DaemonSet serves. For blast-radius reduction, security-
zone segregation, or hardware-tier gating, restrict a Principal to a
subset of nodes matching Kubernetes labels:

```bash
# Label the nodes that should be eligible
kubectl label node worker-1 security-zone=prod-a
kubectl label node worker-2 security-zone=prod-a

# Restrict the Principal. AND semantics: node must have ALL labels.
kubectl krb add-principal demo 2001 --keytab-file ./svc-2001.keytab \
    --node-selector security-zone=prod-a \
    --node-selector tier=gpu

# Or on an existing Principal, via edit:
kubectl krb edit principal uid-2001
# ... set spec.nodeSelector: {security-zone: prod-a}
```

Effect (usually within 30-90s of the change):

1. Operator writes each Principal's `nodeSelector` and every Node's
   labels into the roster ConfigMap alongside the roster entries.
2. Each daemon pod knows its own node name via the downward API.
3. On the next 10s poll, the daemon detects the CM change, loads
   the selectors, and skips roster entries whose selector doesn't
   match its own node's labels.
4. `pruneStale` removes any ccache the daemon previously minted
   for a now-ineligible Principal. `krb5cc_<UID>` files for foreign
   UIDs (a human's `kinit`) are never touched.

**Fail-closed guarantee**: if the daemon can't resolve its own
node's labels (transient operator/kubelet skew), selector-
restricted Principals are SKIPPED rather than defaulted-in. This
prevents accidental credential placement during config drift.

Node label changes drive fresh reconciles automatically; you do not
have to restart anything. The operator watches Nodes
with a label-change predicate so kubelet's routine status
heartbeats (every ~10s) don't trigger reconcile storms.

### Change the daemon renewal interval

Update `Tenant.spec.daemon.renewMinutes` on the Tenant. The operator
patches the DaemonSet's pod template; kubelet rolls the daemon pods
with the new `--renew-minutes` flag.

```bash
kubectl krb edit tenant demo
# ... change renewMinutes: 720 → renewMinutes: 360, save, quit
```

### Move a Principal to a different Tenant

Delete the old Principal, wait for finalizer completion, apply a new
Principal with the new `tenantRef`. Kerberator does not support
in-place tenant migration (the Kerberos principal has changed by
definition: principals are `<name>@<REALM>` and the realm suffix is
part of the identity).

### Drain a node for maintenance

`kubectl drain <node>` works normally; the daemon pod terminates
cleanly and (as of v0.5.2) the operator watches Pod readiness
transitions and refreshes `Principal.status.nodes[<node>].daemonPodReady`
without waiting for the next daemon Event.

`kubectl krb drain-node <node>` cordons the node and deletes its
kerberator-daemon pod (`--uncordon` reverses). It does not move
Principals or evict other workloads.
