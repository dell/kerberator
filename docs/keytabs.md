# Getting keytabs

Kerberator never talks to your KDC's admin interface. You create one
principal per UID, export its keytab, and store the keytab in a Kubernetes
Secret that a `Principal` references. This page shows the export step for the
common KDCs and the two things that go wrong.

## Naming

Any principal name works. The convention used in this repo is
`svc-<uid>@REALM` (for example `svc-2001@EXAMPLE.COM`), which keeps the UID
visible in `klist` and in Events. The `kubectl krb add-principal` command
defaults to it.

## FreeIPA

```bash
kinit admin
ipa user-add svc-2001 --first=svc --last=2001 --uid=2001 --random
ipa-getkeytab -s ipa.example.com -p svc-2001@EXAMPLE.COM -k svc-2001.keytab
```

`ipa-getkeytab` generates a new key and bumps the KVNO every time it runs.
To fetch the existing key without rotating it, add `-r` (requires the
`retrieve keytab` permission on the principal).

## Active Directory

Create a user (or a service account) for the UID, then export a keytab from a
domain-joined Windows host:

```powershell
ktpass -princ svc-2001@EXAMPLE.COM -mapuser EXAMPLE\svc-2001 -pass * `
  -crypto AES256-SHA1 -ptype KRB5_NT_PRINCIPAL -out svc-2001.keytab
```

Or from Linux with `msktutil`:

```bash
msktutil --create --account-name svc-2001 --keytab svc-2001.keytab \
  --enctypes 0x18 --dont-expire-password
```

Make sure the principal's UID mapping on the file server (SSSD, LDAP, or
RFC 2307 attributes) resolves to the same number as `spec.uid`.

## MIT Kerberos

```bash
kadmin -q "addprinc -randkey svc-2001@EXAMPLE.COM"
kadmin -q "ktadd -k svc-2001.keytab svc-2001@EXAMPLE.COM"
```

## Into a Secret

```bash
kubectl -n kerberator-demo create secret generic svc-2001-keytab \
  --from-file=svc-2001.keytab=./svc-2001.keytab
shred -u svc-2001.keytab
```

Reference it from the `Principal`:

```yaml
spec:
  keytabSecretRef: {name: svc-2001-keytab, key: svc-2001.keytab}
```

## The two things that go wrong

**KVNO drift.** If someone re-exports the keytab on the KDC (which bumps the
key version) and does not update the Secret, every `kinit` fails with
`Preauthentication failed`. Kerberator surfaces the KVNO it sees in the
Secret in `Principal.status.kvnoFromSecret` and in `kubectl krb kvno`. To
rotate cleanly, export the new keytab and run
`kubectl krb rotate-keytab <principal> --from-file ./new.keytab`, which
updates the Secret and lets the daemons re-mint.

**Enctype mismatch.** The keytab must contain an enctype the KDC will issue.
`klist -e -k svc-2001.keytab` shows what is in the file;
`Principal.status.enctypesFromSecret` shows what Kerberator parsed. Modern
KDCs want `aes256-cts-hmac-sha1-96` or the SHA-2 variants.
