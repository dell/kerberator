# OP-08 validate-config check catalog

Every check that `scripts/validate.py` can emit. Each entry gives the
`FINDING` shape, why the check matters, the fix the **user** should apply
(the script never writes), and when the check is skipped.

The script emits one of four levels per check: `ok` (passed, listed in
`CHECKED-OK`), `warn` (will apply but may not do what you want),
`fail` (do not apply as-is), `unknown` (could not verify, usually RBAC).

Checks are grouped by what they inspect; order in output follows the
order below.

## Parse / schema

### yaml-parse
- **Signal:** `FINDING: fail yaml-parse | could not parse <file>: <stderr>` — kubectl's client-side decoder rejected the file.
- **Why it matters:** apply would fail with the same error. Stops the pipeline; no later checks run.
- **Fix:** repair the YAML. The decoder's message usually names the line (`error converting YAML to JSON: yaml: line 7: …`).
- **Skipped when:** never. This is always the first check.

### kind-known
- **Signal:** `FINDING: fail kind-known | doc[N] apiVersion=…, want 'kerberator.dell.com/v1alpha1'`
- **Why it matters:** OP-08 only validates Kerberator CRs. A Secret, ConfigMap, or wrong-group CR is out of scope and must be validated with another tool.
- **Fix:** split the doc into a separate file, or fix the typo (`kerberator.dell.io` vs `.com`).
- **Skipped when:** `yaml-parse` failed.

### name-required
- **Signal:** `FINDING: fail name-required | doc[N] metadata.name=… is not a DNS-1123 label`
- **Why it matters:** K8s rejects the apply. The valid shape is lowercase letters, digits, `-`; up to 63 chars.
- **Fix:** rename, e.g. `metadata.name: svc-2001`.
- **Skipped when:** `kind-known` failed.

### namespace-set
- **Signal:** `FINDING: fail namespace-set | doc[N] no namespace in the YAML and no -n passed` **or** `FINDING: warn namespace-set | doc[N] metadata.namespace=… but -n is …` (ref checks will use the namespace-in-YAML).
- **Why it matters:** `kubectl apply -f` on a namespace-less CR falls back to the current context's default namespace, which is usually not what the author meant.
- **Fix:** set `metadata.namespace` or pass `-n`.
- **Skipped when:** `name-required` failed.

## Tenant shape

### realm-shape
- **Signal:** `FINDING: fail realm-shape | doc[N] Tenant spec.realm='example.com' must be uppercase and DNS-domain-shaped (e.g. EXAMPLE.COM)`
- **Why it matters:** Kerberos realms are uppercase by convention. The operator's webhook rejects lowercase; `kinit` accepts lowercase but the daemon's parsing does not.
- **Fix:** `spec.realm: EXAMPLE.COM` (the realm the KDC serves).
- **Skipped when:** the doc is not a Tenant.

### renew-minutes-positive
- **Signal:** `FINDING: fail renew-minutes-positive | doc[N] Tenant spec.daemon.renewMinutes=0, must be an integer > 0`
- **Why it matters:** `renewMinutes <= 0` is treated as "renew now, forever", which hammers the KDC. The operator rejects it.
- **Fix:** omit the field (defaults to 30), or set a sane value like `720` for 12h.
- **Skipped when:** the doc is not a Tenant.

### krb5conf-mentions-realm
- **Signal:** `FINDING: fail krb5conf-mentions-realm | doc[N] Tenant spec.daemon.krb5Conf does not mention realm 'EXAMPLE.COM'`
- **Why it matters:** the daemon writes `krb5Conf` into the container and reads the realm from `spec.realm`. If the realm is not in `[realms]`, every `kinit` fails with `Cannot find KDC`.
- **Fix:** add `EXAMPLE.COM = { kdc = kdc.example.com:88 }` under `[realms]`.
- **Skipped when:** `krb5Conf` is empty (caught by `krb5conf-has-realms`).

### krb5conf-has-realms
- **Signal:** `FINDING: fail krb5conf-has-realms | doc[N] Tenant spec.daemon.krb5Conf has no [realms] section`
- **Why it matters:** `krb5Conf` without `[realms]` has no KDC addresses; the daemon cannot `kinit`.
- **Fix:** add the section. See `examples/quickstart/tenant.yaml` for a full template.
- **Skipped when:** the doc is not a Tenant.

### image-ref-shape
- **Signal:** `FINDING: warn image-ref-shape | doc[N] Tenant spec.daemon.image='…' does not look like a registry/name:tag reference`
- **Why it matters:** a malformed image ref fails at pod-pull time with `InvalidImageName`. This is a warn because the operator's default image is used when the field is empty, and some registries use non-standard shapes.
- **Fix:** write `registry/path:tag` (e.g. `ghcr.io/dell/kerberator-daemon:v1.0.0`) or omit the field.
- **Skipped when:** the doc is not a Tenant, or `image` is unset (defaults are listed as `ok`).

## Principal shape

### uid-positive-int
- **Signal:** `FINDING: fail uid-positive-int | doc[N] Principal spec.uid=0, must be a positive integer`
- **Why it matters:** `uid=0` would mint tickets for root, which Kerberator refuses. Non-integers break the daemon's path formatting (`/tmp/krb5cc_<uid>`).
- **Fix:** use the POSIX UID of the pod/user that will read the ticket, e.g. `2001`.
- **Skipped when:** the doc is not a Principal.

### principal-at-realm
- **Signal (shape):** `FINDING: fail principal-at-realm | doc[N] Principal spec.principal='svc-2001' must be name@REALM`
- **Signal (realm mismatch):** `FINDING: fail principal-at-realm | doc[N] Principal principal realm 'OTHER.COM' != tenant realm 'EXAMPLE.COM'`
- **Why it matters:** the daemon uses this exact string with `kinit`. Without `@REALM`, the KDC lookup is ambiguous; a mismatch means the daemon asks the wrong KDC.
- **Fix:** write `svc-2001@EXAMPLE.COM`, matching the Tenant's `spec.realm`.
- **Skipped when:** the doc is not a Principal, or the Tenant ref did not resolve (realm comparison needs the Tenant's realm).

### tenantref-name-required
- **Signal:** `FINDING: fail tenantref-name-required | doc[N] Principal spec.tenantRef.name is missing`
- **Why it matters:** a Principal without a Tenant is orphaned and will stay `Ready=Unknown` forever. The operator does nothing with it.
- **Fix:** set `spec.tenantRef: {name: <tenant>}`.
- **Skipped when:** the doc is not a Principal.

### keytabref-required
- **Signal:** `FINDING: fail keytabref-required | doc[N] Principal spec.keytabSecretRef needs both .name and .key`
- **Why it matters:** with no keytab reference the daemon has nothing to kinit with; the Principal is a dangling CR.
- **Fix:** set `spec.keytabSecretRef: {name: <secret>, key: <filename inside the Secret>}`.
- **Skipped when:** the doc is not a Principal.

### nodeselector-syntax
- **Signal:** `FINDING: fail nodeselector-syntax | doc[N] Principal spec.nodeSelector keys with empty value: ['env']`
- **Why it matters:** a nodeSelector with empty keys or values matches nothing; the Principal never mints. The operator's webhook accepts the shape but behaviour is surprising.
- **Fix:** either remove the empty entry or give it a value (`env: prod`).
- **Skipped when:** the doc is not a Principal, or `nodeSelector` is unset.

## Cluster references

These checks need cluster access. If the plugin/CRD/RBAC prechecks fail, the whole group is skipped; the result's STATUS becomes `crd-missing` or the shape checks are reported with a note that cluster-side verification was skipped.

### crd-installed
- **Signal (missing):** `FINDING: fail crd-installed | kerberator CRDs are not installed in this context`
- **Signal (unreadable):** `FINDING: unknown crd-installed | cannot list CRDs (RBAC); cluster-side checks are skipped`
- **Why it matters:** apply would fail with `no matches for kind "Tenant"`. Also the sentinel that OP-08 uses to decide whether to attempt ref checks at all.
- **Fix:** install the Helm chart (see repo README). Exit code 3 and STATUS `crd-missing` propagate to the caller.
- **Skipped when:** `plugin-missing`.

### tenant-exists
- **Signal (missing):** `FINDING: fail tenant-exists | tenantRef.name=foo not found in namespace demo-ns`
- **Signal (in-file):** `FINDING: ok tenant-exists | tenantRef.name=foo is defined in the same file`
- **Why it matters:** a Principal with a dangling `tenantRef` is orphaned forever (kerberator-inspect flags it).
- **Fix:** create the Tenant first, or correct the ref. If both CRs are being applied together in the same file the script treats the in-file Tenant as resolved, since apply will create them in order.
- **Skipped when:** `tenantref-name-required` failed; `crd-missing`; `forbidden`.

### keytab-secret-exists
- **Signal (missing secret):** `FINDING: fail keytab-secret-exists | keytabSecretRef ns/name not found`
- **Signal (missing key):** `FINDING: fail keytab-secret-exists | secret ns/name exists but has no key 'x.keytab' (present: ['y.keytab'])`
- **Why it matters:** no Secret → operator parks the Principal with `KeytabsResolved=False`. Wrong key → the daemon mints nothing and emits `KerberosMintFailed`.
- **Fix:** create the Secret first (`kubectl create secret generic … --from-file=<key>=<path>`), or fix `keytabSecretRef.key`.
- **Never:** the check only reads the Secret's `.data` map KEYS (never the base64 values). If raw Secret bytes ever appear in the script output, that is a bug — do not relay it.
- **Skipped when:** `keytabref-required` failed; `crd-missing`; `forbidden`.

### duplicate-uid
- **Signal:** `FINDING: warn duplicate-uid | uid=2001 is already used by Principal(s) ['other-p'] in demo-ns; two Principals writing the same ccache will overwrite each other`
- **Why it matters:** two Principals with the same UID race for `/tmp/krb5cc_<uid>`; the daemon can only hold one ticket per UID. The CR is accepted but behaviour is non-deterministic.
- **Fix:** pick a different UID, or disable one of the Principals.
- **Skipped when:** `uid-positive-int` failed; `crd-missing`; `forbidden`.

### nodeselector-matches
- **Signal (ok):** `FINDING: ok nodeselector-matches | selector {…} matches 2 node(s): […]`
- **Signal (zero):** `FINDING: warn nodeselector-matches | selector {…} matches 0 nodes; the daemon will skip this Principal on every node until a node is labelled`
- **Why it matters:** a zero-match selector is one of the most common causes of "my Principal is not minting" bugs (see OP-06 cases `zero-match-guard`, `key-typo`).
- **Fix:** label a node (`kubectl label node <n> key=value`) or change the selector. If this is intentional (e.g. a label will be added later), say so and apply anyway.
- **Skipped when:** `nodeselector-syntax` failed; `crd-missing`; `forbidden`.

### label-typo-suggest
- **Signal:** `FINDING: warn label-typo-suggest | selector keys may be typos: {'security_zone': 'security-zone'}`
- **Why it matters:** `security_zone` vs `security-zone` matches zero nodes but looks right. The fuzzy match (close-match cutoff 0.8) catches the frequent underscore-vs-hyphen case.
- **Fix:** rename the selector key to the suggested one, or the node label to the one the YAML uses.
- **Skipped when:** `nodeselector-matches` was skipped; `crd-missing`; `forbidden`.
