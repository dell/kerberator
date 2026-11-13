# Missing or stale tickets on nodes, and daemon problems (was DG-04)

Read this when kinit isn't (only) the problem: a node has no ticket, an old
ticket, or no daemon reporting for this Principal.

## What can and can't be seen

The ticket file is `<ticket_cache_dir>/krb5cc_<uid>` on each node. This
skill **never** execs into pods or reads node files, so it can't see the
file, its owner or its expiry directly. The evidence it has:

- **Per-node status** (section 3): the operator's latest per-UID report from
  each node's daemon, with its age.
- **The daemon's readiness probe** (section 4): it fails with
  `readyz: missing ccache for uid=N at <path>` or `empty ccache` when an
  expected ticket file is missing or empty. That is the direct evidence of a
  missing ticket, visible without exec. The probe does **not** check expiry.
- **Events and daemon logs** (sections 5 and 6): mint successes, failures,
  prunes.

Say plainly that the file itself, its expiry and its permissions are
**not verified**, and that checking them needs someone with node access
(`klist -c <ticket_cache_dir>/krb5cc_<uid>` as that user on the node).

## Per-node states

| Node shows | Means |
|---|---|
| `Minted` | The daemon minted a ticket recently. Healthy for this node |
| `MintFailed` / `KeytabMissing` | kinit failed; diagnose the error (`kinit-errors.md`, `keytab-kvno.md`) |
| `Pruned` | The daemon deleted this UID's ticket on purpose: the Principal is disabled, or its `nodeSelector` no longer matches this node. Expected if so; check `principal_disabled` and `node_selector` |
| reason empty (`-`), daemon ready | No recent per-UID report from this node: the operator fell back to pod readiness. Counted in `nodes_no_recent_report` |
| node missing from the list | No daemon pod on that node (see below) |

`nodes_stale_report` counts nodes whose last report is older than twice the
renew interval: the daemon re-mints every renew interval, so it should have
reported again by then.

## Patterns

| Signals | Points to | Confidence |
|---|---|---|
| `daemon_not_ready_reason=missing-ticket(uid=…)` and that UID's node shows `MintFailed` | The ticket is missing **because** kinit fails. Diagnose the kinit error; the not-ready pod is a symptom | Strong |
| `missing-ticket(uid=…)` naming a **different** UID than this Principal | Another Principal's failure is making the shared daemon pod not ready, which marks this Principal not Ready too. This Principal may be fine: check its own node result | Strong |
| `nodes_no_recent_report` or `nodes_stale_report` > 0, daemon pod ready, no failure events | The daemon stopped reporting for this UID: it may not be renewing (stuck, roster not updated) or events aren't reaching the operator. The ticket may be expiring. **Consistent with** a stale ticket, not proof | Medium |
| `daemon_not_ready_reason=other` | A real daemon problem: image pull, crash loop, scheduling, missing roster. Quote the pod events from section 4 | Strong for "daemon", quote which |
| `daemon_not_ready_reason=starting` | The daemon pod started less than 90 seconds ago and its readiness probe hasn't reported yet. Not a fault by itself: re-run `collect.py` in a minute before concluding anything about the daemon | — |
| `daemon_pods_ready` lower than the Tenant's node count, pod `Pending` | The daemon can't schedule on a node (taints, resources, the Tenant's daemon nodeSelector) | Strong |
| A node with a daemon pod but absent from per-node status | The operator hasn't reported that node yet; re-run after a reconcile (about 15 s) | Low |
| Every node `Minted`, user still can't authenticate | Kerberator has a ticket on the node. The problem is likely on the application side: the app looks in another ccache location, runs as a different UID, or fails a service-ticket request. That needs node or app access | Medium |

## Next steps

- Daemon problems belong to the cluster admin: quote the pod event.
- Stale reports: re-run in one renew interval; if still stale, the daemon
  logs (section 6) and a node-side `klist` are the next checks.
- Never restart, delete or roll out daemon pods from this skill. If a
  restart is the fix, propose it and why.
