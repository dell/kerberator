# kerberator-operator

The Kerberator control plane. A Kubernetes operator that reconciles
`Tenant` and `Principal` CRs into the per-node Kerberos credential
caches (`/tmp/krb5cc_<uid>`) that host-level GSSAPI consumers
(`rpc.gssd` for NFS `sec=krb5`, `cifs.upcall` for SMB, HDFS clients)
use on behalf of processes running as the matching POSIX UID.
Workload containers never see the keytab or the ccache.

Two CRDs, one manager:

- `Tenant`: one per Kerberos realm. Carries `spec.realm`, the daemon
  template (`spec.daemon`), and the tenant's `krb5.conf`. The
  controller materializes it as a DaemonSet running
  [`kerberator-daemon`](../daemon/README.md), an aggregated roster
  ConfigMap, an aggregated keytab Secret, and an events Role +
  RoleBinding. All five children carry a controller `ownerReference`
  back to the Tenant.
- `Principal`: one per (uid, principal, keytab) identity. Points at a
  Tenant via `tenantRef` and at a keytab Secret via
  `keytabSecretRef`. Carries per-node status.

Short names: `krbtenant`/`kt` and `krbprincipal`/`kp`.

For the CRD field reference and end-to-end walkthroughs see
[`docs/usage.md`](../docs/usage.md). For the reconcile loops and
data flow see [`docs/architecture.md`](../docs/architecture.md).

## Install

The operator ships as a Helm chart at `operator/charts/kerberator`.
The validating admission webhook is enabled by default and uses
cert-manager to issue its serving certificate, so install
cert-manager first, or set `webhook.enabled=false` to skip the
webhook entirely.

```bash
helm install kerberator oci://ghcr.io/dell/charts/kerberator --version 0.10.0 \
    --namespace kerberator-system --create-namespace
```

From a checkout:

```bash
helm install kerberator ./operator/charts/kerberator \
    --namespace kerberator-system --create-namespace \
    --set image.repository=ghcr.io/dell/kerberator-operator \
    --set image.tag=v0.10.0
```

Useful values: `daemon.image` (default image the DaemonSet runs;
`Tenant.spec.daemon.image` overrides it per tenant),
`webhook.enabled`, `webhook.certManager.enabled` (set false to supply
your own `webhook.certSecret` + `webhook.caBundle`), `events.enabled`,
`events.cacheTTL`, `events.gcInterval`. The operator may live in a
different namespace from the Tenants it manages.

## Running locally

```bash
make operator                       # builds bin/kerberator-operator
./bin/kerberator-operator --webhook-enabled=false
```

| Flag | Default | Purpose |
|---|---|---|
| `--requeue-interval` | `15s` | Re-reconcile cadence absent an event. |
| `--daemon-image` | `ghcr.io/dell/kerberator-daemon:<version>` | Default DaemonSet image when `Tenant.spec.daemon.image` is empty. |
| `--webhook-enabled` | `false` | Serve the validating webhook. |
| `--webhook-port` | `9443` | Webhook TLS port. |
| `--webhook-cert-dir` | `/etc/webhook/certs` | Directory holding `tls.crt` + `tls.key`. |
| `--events-enabled` | `true` | Ingest per-UID daemon Events into the status cache. |
| `--event-cache-ttl` | `24h` | TTL for cached observations. |
| `--event-gc-interval` | `10m` | Cache GC cadence. |
| `--metrics-addr` | `:8080` | Prometheus metrics endpoint. |
| `--health-probe-addr` | `:8081` | `/healthz` + `/readyz`. |

## Reconciliation details

Finalizer. Each `Principal` carries the
`kerberator.dell.com/ccache-purged` finalizer. On delete, the tenant
reconciler drops the UID from the aggregated roster; the CR is then
held until every daemon pod on the Tenant's desired nodes emits a
`KerberosCcachePruned` Event for that UID, or
`Tenant.spec.deletionTimeout` (default 60s) elapses. On timeout the
finalizer is removed anyway and a `CcachePurgeTimeout` Warning is
posted on the Principal. A stuck delete is worse than a short
purge-lag window.

Per-node status. The daemon posts `KerberosMintSucceeded`,
`KerberosMintFailed`, `KerberosCcachePruned`, and
`KerberosKeytabMissing` Events on its own Pod, each annotated with
`kerberator.dell.com/uid`. The operator's `EventReconciler` keeps
them in an in-memory `(pod, uid)` cache and joins them with daemon
Pod readiness to populate `Principal.status.nodes[]`. Observations
older than `Tenant.spec.eventStalenessThreshold` (floor 5m) fall back
to the plain pod-readiness rollup, as does running with
`--events-enabled=false`.

Keytab introspection. The Principal reconciler parses the referenced
keytab Secret (`shared/keytab`, pure Go) and records
`status.kvnoFromSecret`, `status.enctypesFromSecret`, and
`status.keytabObservedAt`. It watches Secrets, so a rotation surfaces
within seconds. `kvnoFromSecret: 0` means not observed or unparseable.

Webhook. The validating webhook rejects a `Principal` when:

1. `spec.uid` is not a positive integer (0 is reserved for root).
2. The namespace carries the OpenShift `openshift.io/sa.scc.uid-range`
   annotation and `spec.uid` falls outside that range.
3. `spec.principal` is not `<name>@<REALM>`, or its realm does not
   equal the referenced `Tenant.spec.realm`.
4. `tenantRef.name` is empty, or (on Create only) names a Tenant
   that does not exist in the namespace. Updates tolerate a missing
   Tenant so a stranded Principal can still be edited or deleted.
5. `spec.keytabSecretRef` is set but `name` or `key` is empty.

`failurePolicy` is `Fail` for Create and `Ignore` for Update, so a
webhook outage blocks bad admissions without blocking routine status
writes.

## Development

```bash
make test           # unit tests
make envtest        # once: download kube-apiserver + etcd for envtest
make integration    # operator envtest suite (~7s warm)
```

Unit tests sit beside the code under `operator/api/v1alpha1` and
`operator/internal/...`. The envtest suite under
`operator/test/integration/` boots a real kube-apiserver, installs the
chart CRDs and webhook, and exercises the five admission rules,
roster/keytab aggregation with ownerRefs, and both finalizer release
paths (no desired nodes, and per-node purge confirmation). Envtest does
not run the Kubernetes garbage collector, so ownership tests assert
ownerRef structure rather than cascade deletion.
