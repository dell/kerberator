---
name: kerberator-diagnose-auth-failure
description: Diagnose why a Kerberator Principal cannot authenticate to Kerberos, by checking realm and KDC configuration, the principal, and ticket evidence (per-node status, events, daemon logs) before proposing a fix. Use when a user reports a Principal or UID that is not Ready, KerberosMintFailed, kinit errors, "can't log in", or asks why Kerberos authentication is failing.
---

# Diagnose a Kerberos authentication failure (DG-06)

Triage for any Kerberos login failure of a Kerberator Principal. Work out
**which kind** of failure it is from evidence, say how sure you are, and
point to the next step. The daemon on each node runs `kinit` with the
Principal's keytab; when that fails, kinit's own error text ends up in the
`KerberosMintFailed` event and the daemon log. That text is the core
evidence.

**This skill is read-only.** It never changes, restarts or rotates
anything. Fixes belong to other skills (see Hand-offs).

**Scope: failures to get the ticket.** It diagnoses why the daemon's
`kinit` fails. It cannot see whether an application on the node then uses
that ticket successfully (where the app looks for the ticket file, its
ownership, or the app's own service-ticket requests), because that needs
node access. If every node shows `Minted` but the user still can't log in,
say the problem is likely on the application side and what to check there.

## Inputs

| Input | Notes |
|---|---|
| Principal CR name | e.g. `uid-2001`. If the user gives a UID (`2001`) or a Kerberos name (`svc-2001@EXAMPLE.COM`), find the CR with `kubectl krb list`. |
| Namespace | Only if not the kubeconfig's default. |
| Cluster context | Use the current one if only one is configured; otherwise ask. |

## Workflow

Run scripts from this skill's base directory: `bash <skill-dir>/scripts/<name>.sh`.

**1. Collect the evidence** (read-only, one command):
```bash
collect.sh <principal> [-n <ns>] [--context <ctx>]
```
It prints eight sections: the Principal, the Tenant's realm and KDC
configuration, per-node status, daemon pods (with the reason any pod isn't
running), Kerberos events (newest first, untruncated), the daemon log lines
for this principal, every Principal in the namespace, and a `SIGNALS`
block. Exit 3 means the Principal was not found: show the list it
prints and ask which one was meant.

**2. Classify**, using `SIGNALS` and `references/kinit-errors.md`, in this order:

1. `principal_disabled=true` or `tenant_disabled=true`: not a failure; it is switched off on purpose.
2. `nodes_failing=0/N` and `error_signature=none`: Kerberator is getting tickets. If the user still reports a failure, it is likely application-side (see Scope); `ticket_cache_dir` is where the ticket files are, as `krb5cc_<uid>`.
3. `daemon_pods_ready` below the node count, or a node with no status: a daemon problem on that node, not Kerberos. Section 4 shows the pod's own events (scheduling, image pull, crash).
4. `node_selector` is set: the Principal is limited to matching nodes on purpose, so other nodes having no ticket is expected, not a failure.
5. `realm_match=no`: the Principal's realm differs from the Tenant's realm. A likely cause of `client-not-found`.
6. Otherwise, look up `error_signature` in `references/kinit-errors.md`. Narrow it with the patterns: all nodes vs one node, and `principals_not_ready_in_namespace` (many Principals failing together points to the KDC, the Tenant or its krb5.conf, not this Principal's key).

**3. Check the evidence agrees.** Quote the actual error text from section
5 or 6. If an event message is truncated (ends in `…`), use the daemon log
line instead. If sources disagree (e.g. the latest event is a success but a
node shows `MintFailed`), say so and weigh the newest evidence.

## Report

Lead with one line, then these parts, kept separate:

1. **Observed failure:** the event reason and the exact kinit error text, which nodes, since when.
2. **Evidence:** realm (Principal vs Tenant), KDC address from krb5.conf, KVNO and enctypes, daemon pod state, failing vs healthy nodes.
3. **Assessment:** the category, labelled **confirmed by evidence**, **consistent with**, **not established**, or **not a failure**.
4. **Not verified:** what this skill cannot see, especially anything on the KDC side (whether the principal exists there, its current KVNO, KDC health).
5. **Next step:** one specific, safe action, or a hand-off.

## Hand-offs

| Finding | Hand off to |
|---|---|
| `key-rejected`, `keytab-no-matching-key` or `enctype-unsupported` | `kerberator-diagnose-keytab-kvno-mismatch` (DG-03), to compare the keytab's principal, KVNO and enctypes |
| A new keytab is needed (confirmed stale or missing key) | `kerberator-rotate-keytab` (OP-04); the user must supply the keytab |
| Disabled on purpose, should be on | `kerberator-enable-disable-principal` (OP-05) |
| General status or listing questions | `kerberator-inspect` (OP-01) |
| KDC-side checks (principal exists, account locked, KDC down) | The KDC admin; say exactly what to ask |

## Never

- Change anything: no rotate, enable/disable, edit, restart, delete or apply.
- Run `kubectl exec` into any pod.
- Read Secret contents: no `get secret -o yaml/json`, `describe secret`, `base64 -d`, `klist -K` or `ktutil --keys`.
- Agree with the user's guess (e.g. "it's the KDC, right?") without evidence for it.
- Present a cause as confirmed when the evidence only says "consistent with".
- Run `git commit` or `git push`.

## Out of scope

Fixing the failure, anything on the KDC itself, and node-level debugging
(clocks, networking) beyond reporting the evidence. Say so and stop.
