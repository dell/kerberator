# Observed status fields

Source: `cli/kubectl-krb/internal/{list,describe}.go`, `operator/api/v1alpha1`.
`spec` is what was asked for; everything below is **status** (what the operator/daemon observed).

## `kubectl krb list`

| Column | Meaning |
|---|---|
| READY | `Ready` condition status (`True`/`False`/`Unknown`; `-` = condition absent) |
| NODES | `readyNodes/desiredNodes` (Tenant: `status.readySummary`) |
| TENANT/UID | Tenant row: Kerberos realm. Principal row: numeric UID |
| AGE | since `creationTimestamp` |

Empty result prints `No Kerberator resources found.` (exit 0), including when `--tenant` matches nothing or the namespace is wrong.

## `describe tenant`

| Field | Meaning |
|---|---|
| Kerberos realm | `spec.realm` |
| Nodes ready | `status.readySummary` |
| Principals | `status.principalCount` |
| Roster hash | hash of the roster the daemons should have; changes when principals change |
| Conditions | `type=status reason: message`. Read all of them: `Ready=True` only means the Tenant reconciled, and it can appear together with `DaemonSetReady=False` and `Nodes ready: 0/1` (see Interpreting) |
| Owned children | DaemonSet, roster ConfigMap, keytab Secret, events Role/RoleBinding (named by convention from the Tenant name) |
| Principals table | UID, principal, keytab Secret/key, KVNO, READY, nodes |

## `describe principal`

| Field | Meaning |
|---|---|
| Tenant CR ref | parent Tenant **CR name** (not the realm) |
| KVNO (observed Ns) | key version read from the keytab Secret, and how long ago |
| `<not observed>` | KVNO 0: Secret missing, empty, or unparseable; usually self-heals on next reconcile |
| Enctypes | enctypes found in the keytab |
| Finalizers | e.g. `kerberator.dell.com/ccache-purged`; a lingering one + `Deleting:` means cleanup is stuck/in progress |
| Deleting: since | `deletionTimestamp` set |
| Per-node status | one row per node: DAEMON READY, REASON (`Minted`, `MintFailed`, …), OBSERVED age, MESSAGE. MESSAGE is cut at 60 characters and ends in `…` when cut |
| Recent daemon Events | up to 10 Events matched by UID annotation, each `[age] Reason on <pod>: message`. Messages are cut at 120 characters. **Not in time order**: use the `[age]` to find the latest. `(none — …)` = not reported yet or GC'd |

## Interpreting

- `Minted` on every node + fresh OBSERVED → healthy.
- `MintFailed` + `KDC unreachable` → node-to-KDC connectivity, not a Kerberator bug.
- READY `True` but NODES `2/3` → a node is missing/not reporting; look at per-node rows.
- **Tenant `Ready=True` does not mean healthy.** Seen on a real cluster: `Ready=True  Reconciled` alongside `DaemonSetReady=False  SomePodsNotReady: 0/1 ready` and `Nodes ready: 0/1`, while its Principal was `Ready False` with `MintFailed`. Judge a Tenant by `Nodes ready` being `n/n` and `DaemonSetReady=True`, not by `Ready` alone.
- **The cause may be cut off.** A truncated MESSAGE or Event (ending `…`, e.g. `kinit: Resource temporar…`) hides the real error. Quote what is visible, say it is truncated, and do not guess the rest; the daemon pod's logs have the full text (outside this skill).
- **Order Events by age, not position.** An older `KerberosMintSucceeded` can be listed above a newer `KerberosMintFailed`. The smallest age is the current state.
- **OBSERVED age alone does not mean stale.** OBSERVED is the time of the daemon's last Event for that UID on that node, and daemons emit on mint/renewal, not continuously. A healthy node can show minutes or hours (seen: `Minted`, 10m, healthy); the upper bound is roughly the Tenant's renew interval (`spec.renewMinutes`, default 720). Treat a node as stale only with a second signal: DAEMON READY `false`, an age far larger than the other nodes', or an age beyond the renew interval. State the age and which signal you used; do not call a node stale on a fixed threshold.
