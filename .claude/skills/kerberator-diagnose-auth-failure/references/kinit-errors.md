# Kerberos login errors: what they mean (was DG-06)

The daemon on each node runs `kinit` with the Principal's keytab. When it
fails, the daemon puts kinit's own error text in the `KerberosMintFailed`
event and its log:

```
kinit failed for uid=2001 principal=svc-2001@EXAMPLE.COM: kinit failed: exit status 1 (output: kinit: <ERROR TEXT>)
```

`collect.py` matches that text and prints `error_signature=<name>`. The
texts below are MIT Kerberos messages (what the daemon image uses). Always
quote the actual text you saw; it is the evidence.

| `error_signature` | Typical error text | What it usually means | On its own, it is | Next step |
|---|---|---|---|---|
| `key-rejected` | `Password incorrect while getting initial credentials`, `Preauthentication failed` | The KDC rejected the key in the keytab: the KDC has a newer key (KVNO drift) or the keytab holds a different key | **Consistent with** a stale key, not proof | Go to `keytab-kvno.md`: check identity, then compare KVNOs |
| `client-not-found` | `Client '<name>@<REALM>' not found in Kerberos database` | The principal does not exist on the KDC: a typo, the wrong realm, or it was deleted | **Strong** evidence | Compare the Principal's Kerberos name and realm with the Tenant's realm; ask the KDC admin whether the principal exists |
| `kdc-unreachable` | `Cannot contact any KDC for realm`, `Cannot resolve network address for KDC`, `Cannot find KDC for realm`, `Resource temporarily unavailable` (no KDC answered in time, e.g. the KDC is still starting) | The node cannot reach the KDC: KDC down, network/DNS, or a wrong `kdc =` address in the Tenant's krb5.conf | **Strong** for "network or config", not which one | All nodes failing: KDC down or wrong address. One node: that node's network. Quote the `kdc =` line |
| `clock-skew` | `Clock skew too great` | The node's clock is too far from the KDC's (default limit 5 minutes) | **Strong** | Ask a cluster admin to check time sync on the failing node(s) |
| `keytab-no-matching-key` | `Keytab contains no suitable keys for <name>@<REALM>`, `Key table entry not found` | The keytab holds no usable key for this principal: most often it is a keytab for a **different** principal (the wrong file was uploaded), or none of its keys have an encryption type kinit will use | **Strong** that the keytab doesn't match this principal | Go to `keytab-kvno.md` (identity check); a correct keytab is then supplied with OP-04 |
| `enctype-unsupported` | `KDC has no support for encryption type` | The keytab has a key for this principal, but in an encryption type the KDC doesn't accept | **Consistent with** an enctype mismatch | Go to `keytab-kvno.md`; compare the enctypes in section 1 |
| `credentials-revoked` | `Client's credentials have been revoked` | The account is locked or disabled on the KDC | **Strong** | Ask the KDC admin to check the account |
| `keytab-file-missing`, `keytab-missing` | `Key table file ... not found`, or a `KerberosKeytabMissing` event | The daemon has no keytab file for this principal: the Secret is missing, empty, or not mounted yet | **Strong** | Check `keytab_secret` and `keytabs_resolved` (see `principal-config.md`); `KVNO <not observed>` in section 1 agrees. A keytab can be supplied with `kerberator-rotate-keytab` (OP-04) |
| `unrecognized` | anything else | Not in this table | Unknown | Quote the full text from the daemon log, say it is unrecognized, and do not guess |
| `none` | no current failure | Nothing is failing now | — | Check `nodes_failing` and `latest_event`; it may be healthy, disabled, or not yet reported |

## Patterns across nodes

| Pattern | Points to |
|---|---|
| Every node fails with the same error | Something shared: the key, the KDC, the realm or krb5.conf |
| One node fails, others mint | That node: its network to the KDC, its clock, or its daemon pod |
| A node shows no status and its daemon pod isn't Running | The daemon, not Kerberos: it never tried (`node-tickets.md`) |
| Many Principals fail at once (`principals_mint_failing_in_namespace`, their own node results, **not** their Ready flags) | Something they share: the KDC, the Tenant, or its krb5.conf, not one Principal's key |
| Only one Principal fails, others in the same Tenant mint | That Principal: its key, its name or realm, or its account on the KDC |
| The Principal has a `nodeSelector` | Nodes that don't match are skipped on purpose; no ticket there is expected |

## Not failures

- `principal_disabled=true` or `tenant_disabled=true`: the Principal is switched off on purpose, so no tickets are minted. Point to `kerberator-enable-disable-principal` (OP-05) if it should be on.
- Events older than about an hour expire. No events with healthy `Minted` nodes means healthy, not "unknown".
