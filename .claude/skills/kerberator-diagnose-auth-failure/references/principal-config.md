# Principal not Ready: configuration and status (was DG-02)

Read this when the Principal itself looks misconfigured or not Ready for a
reason that is not a kinit error: the Tenant, the keytab Secret, the UID,
or the Ready condition point at the problem before Kerberos is involved.

## The Principal's Ready condition

`principal_ready` in SIGNALS is the Principal's `Ready` condition. Its
reason says which kind of problem it is:

| Ready reason | Means | Where to go |
|---|---|---|
| `AllDaemonsReady` | Every eligible node's daemon is ready | Not a Kerberator failure; see "healthy" in SKILL.md |
| `SomeDaemonsNotReady` | Some eligible daemon pods are not ready | **Not proof of a daemon fault.** Check `daemon_not_ready_reason`: `missing-ticket(uid=…)` means the readiness probe failed because a ticket file is missing, which is a consequence of a mint failure. Find whose mint fails (section 7) and diagnose that. `starting` means the pod just restarted (re-run in a minute). `other` means a real daemon problem; see `node-tickets.md` |
| `NoEligibleNodes` | `spec.nodeSelector` matches no node the Tenant serves | Configuration: the selector is too tight or the labels are missing. Node targeting changes are `kerberator-configure-node-targeting` (OP-06) |
| `NoDaemons` | No daemon pods exist for the Tenant | The Tenant's DaemonSet has no pods: Tenant missing, daemon nodeSelector matches nothing, or pods not scheduled. See `node-tickets.md` |
| `Disabled` / `TenantDisabled` | Switched off on purpose | Not a failure. `kerberator-enable-disable-principal` (OP-05) if it should be on |

**Why `SomeDaemonsNotReady` misleads:** the daemon's readiness probe fails if
*any* Principal's ticket file is missing on that node. One Principal that
can't mint therefore makes the daemon pod not ready, and every other
Principal on the same Tenant then shows `Ready=False` too. Use each
Principal's own per-node results (`principals_mint_failing_in_namespace`,
section 7), not their Ready flags, to see who is really failing.

## Configuration checks

| Signal | Problem | Evidence to quote | Next step |
|---|---|---|---|
| `tenant_found=no` | `spec.tenantRef.name` points at a Tenant that doesn't exist in this namespace | Section 2's NotFound error and the Principal's `tenant:` line | List the Tenants (`kubectl krb list`). The fix is editing the Principal's tenantRef, which is a change for the user to make |
| `keytab_secret=missing` | The Secret named in `spec.keytabSecretRef` doesn't exist | Section 1 `keytab secret:` line; usually `KVNO in secret: <not observed>` and `KerberosKeytabMissing` events | A keytab must be supplied: `kerberator-rotate-keytab` (OP-04) writes it. The user provides the keytab file |
| `keytabs_resolved=False` | The Tenant could not resolve at least one Principal's keytab (missing Secret, missing key in it, or unparseable bytes) | The Tenant's `KeytabsResolved` condition in section 2 | If this Principal's Secret is present but `KVNO <not observed>`, the key name (`spec.keytabSecretRef.key`) may be wrong or the bytes are not a keytab. Never read the Secret to check; say what is not observed |
| `duplicate_uid=yes (<name>)` or `roster_consistent=False` | Two Principals in the same Tenant use the same UID. The operator leaves that UID out of the roster, so **neither** is minted: the daemon logs `sweep complete (ok=0, fail=0)` and its pod can look ready, because it expects no ticket for that UID | Section 7 and the Tenant's `RosterConsistent=False DuplicateUID: duplicate uids: [...]` | The user decides which Principal owns the UID. Do not change either |
| `realm_match=no` | The Principal's realm (after `@`) differs from the Tenant's realm | Section 1 vs section 2 | Usually ends in `client-not-found` from the KDC. The Principal's name or Tenant is wrong |
| `kvno_observed=none` with `keytab_secret=present` | The operator hasn't parsed the keytab yet, or the key/bytes are wrong | `KVNO in secret: <not observed>` | Wait for a reconcile (about 15 s) and re-run; if it stays unobserved, as `keytabs_resolved=False` above |

## Reporting

Say which check failed and quote the field or condition. These are
**confirmed by evidence** when the field shows it directly (a missing
Tenant, a missing Secret, a duplicate UID). Don't go on to diagnose kinit
errors for a Principal that can't be configured correctly first.
