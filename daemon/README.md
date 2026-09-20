# kerberator-daemon

Per-node Kerberos credential-cache minter. Its sole job is to keep a
set of `krb5cc_<UID>` files on the node's filesystem fresh, correctly
owned, and mode 0600, so that host-level GSSAPI consumers (`rpc.gssd`
for NFS `sec=krb5`, `cifs.upcall` for SMB, HDFS clients) can find and
use them when a process running as the matching UID (typically the
main process of a container in a pod) makes a Kerberos-authenticated
I/O call. The daemon never touches the pod itself, has no knowledge
of workload containers, and is not on any application's request path.
See [`docs/design.md`](../docs/design.md) for the full flow,
including the gotchas around UID sourcing (`runAsUser` vs image
`USER` vs in-container `su`, and user namespace remapping).

Deployed as a DaemonSet by the Kerberator operator. You should not
install this on its own; the operator manages every aspect of its
lifecycle from a `Tenant` CR. This document exists so people debugging
on-node issues have somewhere to look.

## What it does

On every worker:

1. Reads the roster file, a plaintext table with one
   `uid:principal:keytab-filename` per line, mounted from the
   operator-owned aggregated roster ConfigMap.
2. For each entry, invokes:
   ```
   kinit -k -t <keytab-dir>/<filename> -c FILE:<cache-dir>/krb5cc_<uid> <principal>
   ```
3. `chown <uid>:<uid>` and `chmod 0600` the resulting ccache file.
4. Loops on a poll interval (default 10s):
   - If the hash of the roster and its companion files
     (`selectors.json`, `node-labels.json`) changed, re-sweeps
     immediately.
   - Otherwise, if `renewMinutes` elapsed, re-sweeps.
5. When a Principal carries `spec.nodeSelector`, the operator projects
   the selector and a snapshot of Node labels alongside the roster;
   the daemon skips entries whose selector does not match this node's
   labels (fail closed if the labels cannot be resolved).
6. When `--prune-stale` is set, removes ccache files whose UIDs were
   minted by this process in a prior sweep (or adopted at startup
   because they were in the roster) but are no longer eligible.
   Foreign UIDs (a human `kinit` on the node) are never touched.

Every mint outcome is posted as a Kubernetes `Event` on the daemon's
own Pod, with the UID in a structured annotation
(`kerberator.dell.com/uid`) that the operator's `EventReconciler` uses
to populate `Principal.status.nodes[]`.

`kerberator-daemon readyz` is the readiness probe. It reuses the same
roster and selector filter to decide which ccaches should exist on
this node, so a node that correctly skips a selector-restricted
Principal still reports ready.

## Configuration

Binary defaults are shown; the operator's DaemonSet builder overrides
the paths as noted.

| Flag | Binary default | Set by the operator to |
|---|---|---|
| `--roster` | `/etc/roster/users.roster` | `/etc/kerberator-daemon/roster/users.roster` |
| `--keytabs` | `/keytabs` | `/etc/kerberator-daemon/keytabs` |
| `--caches` | `/host-tmp` | `Tenant.spec.daemon.hostCachePath` (default `/tmp`), mounted from the host at the same path |
| `--renew-minutes` | `0` (falls back to env `RENEW_MINUTES`, then 720) | `Tenant.spec.daemon.renewMinutes` (30 when unset) |
| `--prune-stale` | `false` | present when `Tenant.spec.daemon.pruneStale` is true |
| `--selectors` | `<roster-dir>/selectors.json` | (default) |
| `--node-labels` | `<roster-dir>/node-labels.json` | (default) |

Env (set by the operator via the downward API):

| Variable | Purpose |
|---|---|
| `POD_NAME` | Which pod owns the emitted Events. |
| `POD_NAMESPACE` | Namespace of that pod. |
| `NODE_NAME` | Key into `node-labels.json` for selector filtering. |

If `POD_NAME` or `POD_NAMESPACE` is unset, event emission is disabled
(no-op). The mint loop continues either way; a broken API server
never blocks a ticket mint.

## Security posture

- Runs as root with only `CHOWN` + `FOWNER` capabilities (all others
  dropped). Needed to write ccache files owned by arbitrary UIDs.
- `readOnlyRootFilesystem: true`; the only writable mount is the
  host cache directory.
- No hostNetwork. KDC reachability is handled via `hostAliases` on the
  Tenant's daemon spec when the KDC is not in cluster DNS.
- Keytabs live in a projected Secret mounted read-only, never on
  writable disk.

## Testing

```bash
cd daemon && go test ./...
```

Unit tests cover the mint loop, prune and adoption logic, selector
filtering, and event emission (including annotation shape). There is
no integration harness; the daemon has no reconciliation logic to
exercise, just signal-driven mint sweeps.
