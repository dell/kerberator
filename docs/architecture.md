# Kerberator architecture

*Contributor-facing detail on how the operator, daemon, and CRDs
fit together. For user-facing "how does this actually work" prose,
see [design.md](design.md).*

## System diagram

```
┌───────────────────────────────────────────────────────────────────────┐
│  Kubernetes control plane                                             │
│                                                                       │
│    kerberator-operator (Deployment, 1 replica)                        │
│    ├── TenantReconciler     ─── aggregates Principals → owned children│
│    ├── PrincipalReconciler  ─── finalizer + per-UID status            │
│    ├── EventReconciler      ─── in-memory (pod, uid) cache            │
│    └── admission webhook    ─── 5-rule Principal validation           │
│                                                                       │
│    Tenant CR     ─owns→  DaemonSet, roster CM, keytab Secret, RBAC    │
│    Principal CR  ─aggregated by→  Tenant                              │
└───────────────────────────────────────────────────────────────────────┘
                                    │
                                    │  DaemonSet
                                    ▼
┌───────────────────────────────────────────────────────────────────────┐
│  Every worker node                                                    │
│                                                                       │
│    kerberator-daemon (Pod)                                            │
│    ├── reads roster ConfigMap (uid:principal:keytab per line)         │
│    ├── mounts keytab Secret at /etc/kerberator-daemon/keytabs         │
│    ├── kinit -k -t /etc/kerberator-daemon/keytabs/<f> \               │
│    │         -c FILE:/tmp/krb5cc_<uid> <princ>                        │
│    ├── chown <uid>:<uid>, chmod 0600                                  │
│    └── emits per-UID Events on its own Pod (with uid annotation)      │
│                                                                       │
│    /tmp/krb5cc_2001  owned 2001:2001 mode 0600  ← readable by pods    │
│    /tmp/krb5cc_2002  owned 2002:2002 mode 0600     running as those   │
│    /tmp/krb5cc_2003  owned 2003:2003 mode 0600     UIDs               │
└───────────────────────────────────────────────────────────────────────┘
```

Per-Principal per-node status flows back the other way: daemon
Events → operator cache → `Principal.status.nodes[]`.

## Component deep-dives

- [`operator/README.md`](../operator/README.md) — controller design,
  CRDs, admission rules, reconciler responsibilities.
- [`daemon/README.md`](../daemon/README.md) — mint loop, prune
  semantics, kinit contract, roster hash calculation.
- [`shared/events/reasons.go`](../shared/events/reasons.go) — the
  daemon↔operator wire contract. Event reasons, annotation keys, and
  the UID annotation format that lets `ParseUIDFromEvent` recover
  the Principal's UID from a Kubernetes Event.

## Data flow: what happens when you apply a Principal

1. `kubectl apply -f principal.yaml` sends the CR to the API server.
2. The admission webhook validates against five rules (tenantRef,
   uid is a positive integer, principal parse, keytabSecretRef when
   set has both name and key (Secret existence is checked at
   reconcile time), OpenShift SCC uid-range if annotated). Rejects
   invalid CRs synchronously.
3. Admitted CR lands in etcd. `PrincipalReconciler` observes it,
   adds the finalizer, and sets `Ready=Unknown` (reason `NoDaemons`)
   until per-node observations arrive.
4. `TenantReconciler` observes the matching Tenant's Principal set
   changed. It rebuilds:
   - The aggregated roster ConfigMap (one `uid:principal:keytab`
     line per active Principal).
   - The aggregated keytab Secret (one file per active Principal,
     named to match the roster's `keytab` column).
5. Kubelet notices the ConfigMap/Secret changed on nodes running
   the daemon. It updates the mounted files.
6. `kerberator-daemon` on each node reads the new roster on its next
   poll interval (default 10s), sees the new entry, calls `kinit`,
   writes `/tmp/krb5cc_<uid>`, and emits a
   `KerberosMintSucceeded` (or `KerberosMintFailed`) Event on its
   own Pod with the UID in a structured annotation
   (`kerberator.dell.com/uid`).
7. `EventReconciler` observes the Event, decodes the UID from the
   annotation, and updates its in-memory `(pod, uid) → status`
   cache.
8. `PrincipalReconciler` reads that cache and writes
   `Principal.status.nodes[<node>]` with the mint outcome. When all
   nodes report success, the Principal becomes `Ready`.

Delete flow is the same in reverse, gated on the
`kerberator.dell.com/ccache-purged` finalizer: the operator won't
remove the finalizer until every daemon confirms the on-node ccache
is gone (via `KerberosCcachePruned` Events), or
`Tenant.spec.deletionTimeout` elapses.

## Node failure and pod rescheduling

Tickets are pre-distributed, not fetched on demand. Every node that
matches a Principal's `spec.nodeSelector` (by default, every node
the Tenant's DaemonSet runs on) already holds `/tmp/krb5cc_<uid>`
before any workload pod lands there. A pod that gets rescheduled
from `worker-1` to `worker-2` finds its ccache already present on
`worker-2`; there is no cold-start hop through the operator.

Consequences worth knowing:

- **First mint after a Principal is created takes one poll
  interval** (default 10s) plus however long kubelet takes to
  project the updated ConfigMap and Secret into the daemon pod.
  A workload started in that window gets `EACCES` from its
  consumer until the ccache appears.
- **If the daemon on a node is down**, the ccaches it already wrote
  stay on disk and remain valid until their TGTs expire (the KDC's
  ticket lifetime, typically hours). Nothing refreshes them until
  the daemon returns. `Principal.status.nodes[<node>].daemonPodReady`
  flips to `false` so you can see this from the control plane.
- **If a node is lost outright**, its ccaches go with it. Nothing
  needs cleaning up; the replacement node gets a daemon pod from the
  DaemonSet and mints a fresh set on its first poll.
- **Node label changes** are watched by the operator, so adding or
  removing a label that a `nodeSelector` depends on re-evaluates
  placement without a manual restart.

## What Kerberator does not do

Kerberator has one output: a fresh, correctly-owned FILE ccache per
UID on each eligible node. Everything around that is out of scope.

- It does **not mount anything**. NFS, SMB, or any other storage is
  mounted by your CSI driver, autofs, or a plain `mount`, and that
  layer must request Kerberos itself (for NFS, `sec=krb5`).
- It does **not configure `rpc.gssd`**, `cifs.upcall`, or any other
  GSSAPI consumer. They are expected to be running on the node with
  their default ccache search path.
- It does **not run `gssproxy`** or offer a GSSAPI socket to
  applications.
- It does **not write `/etc/krb5.keytab`** or any host machine
  keytab. The node itself gains no Kerberos identity.
- It does **not touch workload pods**. No mutating webhook, no
  injected sidecars or volumes, no environment variables.
- It does **not issue keytabs**. You export them from your KDC and
  store them in Secrets; Kerberator consumes them.

## Testing

Integration tests use controller-runtime's envtest — `make envtest`
downloads the kube-apiserver + etcd binaries once, then `make check`
spins up a real API server in-process and exercises the full
reconciler + webhook stack. Test suites live under
`operator/test/integration/` and cover webhook rejection,
aggregation, ownership, and finalizer lifecycle.
