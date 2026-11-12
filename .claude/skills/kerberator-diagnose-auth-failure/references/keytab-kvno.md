# Keytab, KVNO and enctype problems (was DG-03)

Read this when `error_signature` is `key-rejected`, `keytab-no-matching-key`
or `enctype-unsupported`, or when the user asks whether a KVNO mismatch is
the cause. Based on the DG-03 skill by Anvesha Girish.

**A rejected key is not proof of KVNO drift.** `Password incorrect` /
`Preauthentication failed` means the KDC rejected the key the keytab holds.
That happens when the KDC has a newer key (KVNO drift), but also when the
KDC re-created the key at the *same* KVNO (for example, a rebuilt KDC
database), or when the keytab was exported for a different key. Only a
KVNO comparison can tell these apart.

## 1. What Kerberator observed in the Secret

Section 1 of `collect.py` shows it; `kubectl krb kvno <principal> -n <ns>`
shows the same with recent mint failures:

- `KVNO in secret` (`status.kvnoFromSecret`): the highest KVNO in the keytab.
- `enctypes` (`status.enctypesFromSecret`).
- `observed` (`status.keytabObservedAt`): when the operator last read new
  keytab bytes. A keytab that hasn't changed in a long time is not by
  itself stale; note the age, don't conclude from it. A **recent** change
  matters the other way: if failures began right after it, the new keytab is
  the prime suspect, and successes from before it (`[before current keytab]`)
  are not evidence that the key works.

`kubectl krb kvno` ends with "If the KDC has a newer kvno than the Secret,
run: kubectl krb rotate-keytab". That is the CLI's suggestion, not evidence
of drift. Never run it from this skill.

## 2. Identity: is it the right keytab?

Check before comparing KVNOs. Comparing the KVNO of the wrong principal's
keytab means nothing.

- `keytab-no-matching-key` (`Keytab contains no suitable keys for <name>`,
  `Key table entry not found`) is direct evidence that the keytab holds no
  key for this Principal's name: most often the Secret holds another
  principal's keytab. Quote the name in the error and the Principal's
  `kerberos name`.
- Kerberator does not record which principals a keytab contains. If the
  user has the keytab **file** they uploaded, `klist -k <file>` (principals
  and KVNOs only) shows it. Never use `klist -K`, `klist -e -k` with keys, or
  `ktutil`, and never read the Secret.

## 3. The KDC side

The rule: **never `kubectl exec`** (not into the KDC, not into a daemon).
Read the KDC's KVNO only through the user's own environment, and only if it
is already set up:

1. `command -v kvno` exists **and** `klist -s` succeeds (the user already
   holds a Kerberos ticket on this machine).
2. Then `kvno <principal>` (e.g. `kvno svc-2001@EXAMPLE.COM`) prints the
   KDC's current KVNO for that principal. Quote it.

Never run `kinit` yourself, ask for a password, or point at a different
KDC. If either condition fails, or `kvno` errors, the KDC's KVNO is
**not verified**: say so, and name the check that would settle it (someone
with KDC access reads the principal's current KVNO, e.g. `getprinc` in
`kadmin`).

## 4. Interpret

| Evidence | Assessment |
|---|---|
| Secret KVNO N, KDC KVNO higher than N, `key-rejected` | **Confirmed** KVNO mismatch: the KDC has a newer key. Why it happened (a re-export such as `ipa-getkeytab` or `ktadd` without updating the Secret, a password change) is **not** established; offer it as likely causes |
| Secret KVNO N, KDC KVNO N, `key-rejected` | **Not** a KVNO mismatch. The key material differs at the same KVNO (KDC database rebuilt, key re-randomized without a KVNO bump, or a keytab from another KDC). Say the user's KVNO theory is not supported |
| Secret KVNO N, KDC KVNO lower than N | Unusual: a keytab from a different KDC or realm, or a KDC restored from backup. Not established which |
| KDC KVNO not verified, `key-rejected` | **Consistent with** a stale key; a KVNO mismatch is one hypothesis, not a finding. Don't state a KDC KVNO you never saw |
| KDC KVNO not verified, `key-rejected`, and the **current** keytab minted successfully before failing (`events_since_keytab_read` shows a success, then failures) | The keytab didn't change but the KDC's answer did, so **something changed on the KDC side**: the key was re-randomized or re-exported (KVNO drift is the usual cause), or the principal was re-created. Say this is the strongest lead, still not confirmed without the KDC's KVNO |
| `keytab-no-matching-key` | **Strong** evidence the keytab doesn't match this principal (wrong principal, or no usable enctype). Compare the error's name with the Principal's |
| `enctype-unsupported` | **Consistent with** an enctype mismatch. Quote `enctypes`; the KDC's supported enctypes are a question for the KDC admin |

## 5. Fix (a proposal, never run here)

A confirmed stale or wrong key is fixed by supplying a correct keytab with
`kerberator-rotate-keytab` (OP-04): it updates the Secret, restarts the
daemons and waits for a re-mint. The user must provide the new keytab file.
Don't recommend just applying a new Secret: updating the Secret alone may
not trigger a new mint.
