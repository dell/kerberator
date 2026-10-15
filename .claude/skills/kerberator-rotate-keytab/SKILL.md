---
name: kerberator-rotate-keytab
description: Rotate a Kerberator Principal's keytab with kubectl krb rotate-keytab and verify every node re-minted, without exposing secret contents. Use when the user wants to rotate, replace, renew, or update a keytab for a Kerberator Principal or UID, for example after a password change or a keytab re-export on the KDC.
---

# Rotate a Kerberator keytab (OP-04)

A **Principal** maps one Linux UID to a Kerberos identity
(`svc-<uid>@<REALM>`). Its keytab (the identity's key) lives in a
Kubernetes Secret. The Tenant's daemon on each node uses it to mint a
ticket into `/tmp/krb5cc_<uid>`.

Rotating means swapping in a new keytab **and** making every node re-mint
with it. `kubectl krb rotate-keytab` does this in the documented order:
update the Secret, rollout-restart the Tenant's daemon DaemonSet, then
wait until every node reports `Minted`. Updating the Secret alone is not
enough: daemons keep the old key until their next renew interval.

## Inputs to collect

| Input | Example | Notes |
|---|---|---|
| Principal CR name | `uid-2001` | If the user gives a UID or a Kerberos name, find the CR with `kubectl krb list`: CLI-created ones are `uid-<uid>`, YAML-created ones are often `svc-<uid>`. Never pass the Kerberos name `svc-2001@...`. |
| New keytab path | `./svc-2001.keytab` | A file on this machine. Ask for it; never create or fetch one. |
| Namespace | `-n team-a` | Only if not the kubeconfig's default namespace. |

If any input is missing or ambiguous, ask before running anything.

## Workflow

Run scripts from this skill's base directory: `bash <skill-dir>/scripts/<name>.sh`.

**1. Precheck (read-only).**
```bash
precheck.sh <principal> --from-file <path> [-n <ns>]
```
Checks permissions, the Principal and its keytab Secret, the daemon
DaemonSet, and the new keytab file (principal name and KVNO, via
`klist -k -t` or `ktutil list`, which never show key bytes). Ends with `RESULT: READY`, `READY WITH WARNINGS`, or
`BLOCKED`, and prints `expected_kvno=`.

- `BLOCKED`: stop. Report each `FAIL` line and what would fix it. Do not
  rotate.
- `READY WITH WARNINGS`: show every `WARN` line to the user. Treat
  "KVNO is LOWER than current" as a reason to recommend not proceeding.
  For "KVNO equals current", ask whether the KDC was re-keyed since this
  file was exported: precheck cannot see the KDC's current KVNO, and a
  stale file fails exactly like this.

**2. Ask for approval.** Show the exact command from step 3 and say what
it does: overwrites the keytab Secret, restarts the Tenant's daemon pod
on every node (all Principals of that Tenant briefly re-mint), and waits
up to 2 minutes. Wait for an explicit yes. The original request is not
approval.

**3. Rotate (mutating).**
```bash
rotate.sh <principal> --from-file <path> [-n <ns>] --confirmed
```
Only add `--confirmed` after the user approved this exact command. Note
the `started_at=` line; step 4 needs it.

**4. Verify (read-only).** Run this even if step 3 reported success:
```bash
verify.sh <principal> --since <started_at> --expect-kvno <expected_kvno> [-n <ns>]
```
Omit `--expect-kvno` if precheck printed `expected_kvno=unknown`.
`RESULT: VERIFIED` requires all of: every node `Minted` after
`started_at`, Ready=True, the operator observed the expected KVNO, and no
`KerberosMintFailed` events for the UID since `started_at`.

If it is `NOT VERIFIED` only because the KVNO has not updated yet, wait
about 30 seconds and run `verify.sh` once more. Do not loop beyond that.

## Reporting the outcome

End with a short report in two parts, and keep them separate:

- **Confirmed:** only what a script printed as `OK` or as CLI output,
  such as "3/3 nodes Minted at or after 14:02:10Z" or "operator observes
  KVNO 4".
- **Not confirmed / next steps:** anything that failed, was skipped, or
  is a hypothesis. Label hypotheses as such ("possibly the KDC still has
  an older KVNO").

## When something fails

| Symptom | Meaning | What to do |
|---|---|---|
| `principal ... not found` | Wrong name or namespace | Run `kubectl krb list` and ask which Principal was meant. |
| `has no keytabSecretRef` | Nothing to rotate | Stop; report it. |
| `Forbidden` / precheck `cannot ...` | Account lacks permission | Stop; name the missing permission. Do not try other commands to get around it. |
| `timed out waiting for re-mint (still: X/Y minted)` | Rotation applied, some nodes did not re-mint | Do not re-run rotate. Run `verify.sh`, then `kubectl krb describe principal <name>` and `kubectl krb events --uid <uid>`, and report the failing nodes and their messages. |
| `KerberosMintFailed` with `Password incorrect while getting initial credentials` or `Preauthentication failed` | The KDC rejected the key: the file is older than the KDC's current key (KVNO mismatch) | Report the evidence and ask for a fresh export from the KDC; rotating that fresh file fixes it. Diagnosing further is DG-03. |
| `KerberosMintFailed` with `KDC unreachable` / `Cannot contact any KDC` | Network or KDC problem, not the keytab | Report it; do not rotate again. |
| verify: KVNO 0 | Operator cannot parse the Secret | Report it; the uploaded file is probably not a keytab. |

There is no automatic rollback. Rotating back to an old keytab only helps
if the KDC still accepts that key, so never suggest it without saying so.

## Never

- Print, `cat`, copy, upload, or summarize keytab files or the kubeconfig.
- Run `klist -K`, `ktutil list --keys`, `kubectl get secret ... -o
  yaml/json`, `kubectl describe secret`, or `base64 -d` on Secret data.
- Edit or apply the Secret yourself, or run `kubectl rollout restart`
  directly. `rotate-keytab` does both in the right order.
- Add `--confirmed` without the user's approval of that exact command.
- Run `git commit` or `git push`.

## Out of scope

Generating or re-exporting keytabs on the KDC, creating Principals
(OP-03), and diagnosing why a KDC rejects a key (DG-03). Say so and stop
when a request needs one of these.
