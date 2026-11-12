---
name: kerberator-diagnose-auth-failure
description: Diagnose why a Kerberator Principal cannot get or use a Kerberos ticket. Covers a Principal that is not Ready, keytab/KVNO mismatches, missing or stale tickets on nodes, and Kerberos login (kinit) errors, using the Principal, Tenant, per-node status, events and daemon logs before proposing a fix. Use when a user reports a Principal or UID that is not Ready, can't get a ticket, KerberosMintFailed, Preauthentication failed, a suspected KVNO mismatch, a missing or expired ticket on a node, or "can't log in".
---

# Diagnose a Principal or authentication failure (DG-02)

One skill for every "this Principal can't authenticate" symptom (merges
DG-02, DG-03, DG-04 and DG-06). Collect the evidence once, work out
**which kind** of failure it is, say how sure you are, and point to the
next step. Each kind has its own reference file, read only when needed.

**This skill is read-only.** It never changes, restarts or rotates
anything. Fixes belong to other skills (see Hand-offs).

## Inputs

| Input | Notes |
|---|---|
| Principal CR name | e.g. `uid-2001`. If the user gives a UID (`2001`) or a Kerberos name (`svc-2001@EXAMPLE.COM`), find the CR with `kubectl krb list`. |
| Namespace | Only if not the kubeconfig's default. |
| Cluster context | Use the current one if only one is configured; otherwise ask. |

## Workflow

Run scripts from this skill's base directory: `python3 <skill-dir>/scripts/<name>.py` (Python 3.8+, standard library only).

**1. Collect the evidence** (read-only, one command):
```bash
collect.py <principal> [-n <ns>] [--context <ctx>]
```
It prints eight sections (Principal, Tenant with realm and KDC
configuration, per-node status, daemon pods, Kerberos events, daemon log
lines, every Principal in the namespace) and a `SIGNALS` block. Exit 3
means the Principal was not found: show the list it prints and ask which
one was meant. Exit 4 means the cluster didn't answer: say so and stop;
don't guess at the cause of the user's failure.

**2. Classify** with `SIGNALS`, in this order. Stop at the first that
applies, read its reference file, and follow it:

| # | Signals | Kind | Read |
|---|---|---|---|
| 1 | `principal_disabled=true` or `tenant_disabled=true` | Switched off on purpose: **not a failure** | — (OP-05 if it should be on) |
| 2 | `tenant_found=no`, `keytab_secret=missing`, `keytabs_resolved=False` with `kvno_observed=none`, `duplicate_uid=yes`, `realm_match=no`, or Ready reason `NoEligibleNodes` / `NoDaemons` | Principal misconfigured / not Ready | `references/principal-config.md` |
| 3 | `error_signature` is `key-rejected`, `keytab-no-matching-key` or `enctype-unsupported` | Keytab / KVNO / enctype | `references/keytab-kvno.md` |
| 4 | Any other `error_signature` except `none` | Kerberos login error (KDC, clock, revoked, …) | `references/kinit-errors.md` |
| 5 | `daemon_not_ready_reason=other`, `nodes_no_recent_report` or `nodes_stale_report` > 0, or nodes missing | Missing or stale tickets, or a daemon problem | `references/node-tickets.md` |
| 6 | `nodes_minted` = all nodes, `error_signature=none` | Kerberator has tickets on every node: **not a Kerberator failure**. If the user still can't authenticate, it's likely application-side | `references/node-tickets.md` (last pattern) |

Two signals that look like causes but usually aren't:
- **`daemon_pods_ready` below the node count** with
  `daemon_not_ready_reason=missing-ticket(uid=…)`: the daemon's readiness
  probe fails because a ticket file is missing, i.e. *because* a mint
  failed. Diagnose the mint failure; the pod is a symptom.
- **`principals_not_ready_in_namespace` high**: one Principal's missing
  ticket makes the shared daemon pod not ready, which marks every Principal
  on that Tenant not Ready. Use `principals_mint_failing_in_namespace`
  (each Principal's own results) to decide whether the cause is shared.

`node_selector` set: nodes it excludes have no ticket on purpose, and may
show `Pruned`; that's expected, not a failure.

**3. Check the evidence agrees.** Quote the actual error text from section
5 or 6. If an event message is truncated (ends in `…`), use the daemon log
line instead. If sources disagree (the latest event is a success but a node
shows `MintFailed`), say so and weigh the newest evidence.

**Only evidence from after the current keytab counts against it.** Events
marked `[before current keytab]` happened before the operator last read new
keytab bytes (`observed` in section 1). They describe an earlier keytab, so
an old success doesn't make the current key look intermittent, and an old
different error isn't a second current cause. Use `events_since_keytab_read`
for the current keytab's record; mention older events only as history.

## The KDC side

The KDC is outside the cluster's view. **Never `kubectl exec`** into the KDC
or any pod. Read the KDC's state only through the user's environment, and
only if it is already set up: `kvno` is installed and `klist -s` succeeds.
Never run `kinit`, ask for a password, or change KDC settings. Otherwise
report the KDC side (whether the principal exists, its current KVNO, KDC
health) as **not verified** and say exactly what to ask the KDC admin.
Details are in `references/keytab-kvno.md`.

## Report

Lead with one line, then these parts, kept separate:

1. **Observed failure:** the event reason and exact error text (or the
   failed check, e.g. Secret missing), which nodes, since when.
2. **Evidence:** realm (Principal vs Tenant), KDC address from krb5.conf,
   KVNO and enctypes, daemon pod state, failing vs healthy nodes, and what
   the other Principals show.
3. **Assessment:** the kind, labelled **confirmed by evidence**,
   **consistent with**, **not established**, or **not a failure**. Name
   other explanations the evidence doesn't rule out.
4. **Not verified:** what this skill cannot see: the KDC side, the ticket
   file on the node (expiry, owner), the application.
5. **Next step:** one specific, safe action, or a hand-off.

## Hand-offs

| Finding | Hand off to |
|---|---|
| A new keytab is needed (confirmed stale, wrong or missing key) | `kerberator-rotate-keytab` (OP-04); the user must supply the keytab |
| Disabled on purpose, should be on | `kerberator-enable-disable-principal` (OP-05) |
| Selector too tight or labels missing (`NoEligibleNodes`) | `kerberator-configure-node-targeting` (OP-06) |
| Wrong config to fix (realm, tenantRef, keytab Secret reference, krb5.conf KDC address) | `kerberator-update-tenant-principal` (OP-09) |
| The whole Tenant is unhealthy: its daemon isn't running, `NoDaemons`, or every Principal fails for a Tenant-level reason | `kerberator-diagnose-tenant` (DG-01) |
| General status or listing questions | `kerberator-inspect` (OP-01) |
| KDC-side checks (principal exists, current KVNO, account locked, KDC down) | The KDC admin; say exactly what to ask |
| Daemon scheduling, image or node problems | The cluster admin; quote the pod event |

## Never

- Change anything: no rotate, enable/disable, edit, patch, restart, delete or apply.
- Run `kubectl exec` into any pod, including the KDC.
- Run `kinit`, or ask the user for a password.
- Read Secret contents: no `get secret -o yaml/json`, `describe secret`, `base64 -d`, `klist -K` or `ktutil`.
- Agree with the user's guess (e.g. "it's a KVNO mismatch, right?") without evidence for it.
- Present a cause as confirmed when the evidence only says "consistent with".
- Run `git commit` or `git push`.

## Out of scope

Fixing the failure, anything on the KDC itself, and node-level debugging
(clocks, networking, ticket files) beyond reporting the evidence. Say so
and stop.
