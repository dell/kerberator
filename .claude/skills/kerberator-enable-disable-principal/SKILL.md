---
name: kerberator-enable-disable-principal
description: Disable (soft-revoke) or re-enable a Kerberator Principal with kubectl krb disable / enable, then verify the new state, without exposing secret contents. Use when the user wants to suspend, revoke, pause, block, disable, restore, re-enable, or bring back a Kerberator Principal or UID, for example while investigating a suspected key leak or during maintenance.
---

# Enable or disable a Kerberator Principal (OP-05)

A **Principal** maps one Linux UID to a Kerberos identity
(`svc-<uid>@<REALM>`). **Disabling** it is a soft revoke: Kerberator
excludes the Principal from the roster, and the ticket cache on each node
is pruned, so workloads running as that UID lose their ticket. **Enabling**
restores it. Nothing is deleted. The Principal CR and its keytab Secret
stay as they are.

## Inputs to collect

| Input | Example | Notes |
|---|---|---|
| Principal CR name | `uid-2001` | If the user gives a UID or a Kerberos name, find the CR with `kubectl krb list`: CLI-created ones are `uid-<uid>`, YAML-created ones are often `svc-<uid>`. Never pass the Kerberos name `svc-2001@...`. |
| Action | disable or enable | Ask if the request is unclear. |
| Reason (disable only) | `compromised` | Recommended labels: `compromised`, `maintenance`, `audit-hold`. Ask the user for the reason. Do not invent one. |
| Namespace | `-n team-a` | Only if not the kubeconfig's default namespace. |

If any input is missing or ambiguous, ask before running anything.

## Workflow

**1. Check the current state (read-only).**
```bash
kubectl krb list -n <ns>
kubectl -n <ns> get principal <name> -o yaml | sed -n '/^spec:/,$p'
```
Confirm the Principal exists and note whether it is already enabled or
disabled (`spec.disabled: true` means disabled), and its Ready condition.
If it is already in the requested state, say so and stop. Do not run the
command again.

`kubectl krb describe principal` does not show the disabled state, so do
not use it to decide whether a Principal is disabled.

**2. Ask for approval.** Show the exact command and what it does:

- Disable: `kubectl krb disable principal <name> --reason <label> [-n <ns>]`
  removes the Principal from the roster and prunes its ticket cache on the
  nodes. Workloads using that UID lose Kerberos access until it is enabled.
- Enable: `kubectl krb enable principal <name> [-n <ns>]` restores it, and
  the nodes mint tickets again.

Wait for an explicit yes. The original request is not approval.

**3. Run the command (mutating).** Run exactly the approved command, once.

**4. Verify (read-only).** The operator applies both disable and enable
asynchronously, so wait about 20 seconds after the command before
verifying. Then run:
```bash
kubectl krb list -n <ns>
kubectl -n <ns> get principal <name> -o yaml | sed -n '/^spec:/,$p'
```
- After disable: `spec.disabled: true`, `spec.disabledReason` is the
  reason you gave, and the Ready condition has `reason: Disabled`
  (observed: status `Unknown`, message
  `principal is disabled (<reason>)`). `kubectl krb list` shows
  READY=Unknown for the Principal.
- After enable: the Ready condition is `True`, and `kubectl krb list`
  shows READY=True and NODES 1/1 for the Principal.

`kubectl krb describe principal` and the Tenant's PRINCIPALS count do not
show the disabled state. Do not use them to verify.

## Reporting the outcome

End with a short report in two parts, and keep them separate:

- **Confirmed:** only what the CLI printed, such as "Principal uid-2001
  shows disabled with reason maintenance".
- **Not confirmed / next steps:** anything that failed, was skipped, or is
  a hypothesis. Label hypotheses as such.

## When something fails

| Symptom | Meaning | What to do |
|---|---|---|
| `not found` | Wrong name or namespace | Run `kubectl krb list` and ask which Principal was meant. |
| `Forbidden` | Account lacks permission | Stop; name the missing permission. Do not try other commands to get around it. |
| Already in the requested state | Nothing to change | Report it and stop. |
| After enable, nodes show `MintFailed` | The keytab may be stale or wrong, not an enable problem | Report the failing nodes and messages. Diagnosing key problems is DG-03, and replacing the keytab is OP-04. |

## Never

- Disable or enable a Tenant. This skill covers Principals only.
- Disable without a reason the user gave, or without their approval of
  that exact command.
- Edit or patch the Principal CR directly, or delete it.
- Print, `cat`, copy, or summarize keytab files or the kubeconfig, or read
  Secrets (`kubectl get secret ... -o yaml/json`, `describe secret`,
  `base64 -d`, `klist -K`).
- Run `git commit` or `git push`.

## Out of scope

Tenants (disable/enable), creating Principals (OP-03), replacing keytabs
(OP-04), and diagnosing why a Principal is not Ready (DG-03, DG-06). Say so
and stop when a request needs one of these.
