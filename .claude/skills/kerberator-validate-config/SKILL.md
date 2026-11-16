---
name: kerberator-validate-config
description: Validate a Kerberator Tenant or Principal YAML against Kerberator's shape and reference rules before anything is applied (OP-08). Use when the user hands over a YAML and asks whether it is safe to apply ("check this YAML", "validate this Tenant before I apply it", "will this Principal work"), or as a precheck other skills run before mutating (OP-02 add-tenant, OP-03 add-principal, OP-06 configure node targeting, OP-09 patch Tenant/Principal). Read-only; reports schema, shape and reference problems with a fix for each. Do NOT use to apply, edit or patch resources — that is the caller's job. Do NOT use to diagnose a Tenant's live readiness (use kerberator-diagnose-tenant) or inspect per-node Principal status (use kerberator-inspect).
---

# Validate a Kerberator Tenant or Principal YAML (OP-08)

Kerberator has two CRDs — **Tenant** (one Kerberos realm) and **Principal**
(one POSIX UID bound to a Kerberos identity + keytab). Each has shape rules
(uppercase realm, positive UID, `principal@REALM` form, a `krb5Conf` that
actually mentions the realm) and reference rules (`tenantRef.name` must
resolve, `keytabSecretRef.name/key` must exist). Applying a YAML that
breaks these produces confusing symptoms hours later (NoEligibleNodes,
Preauthentication failed, dangling Principal). OP-08 catches them
beforehand.

This skill has two roles:

- **Standalone:** the user asks "is this YAML OK to apply?". Run the
  validator, report what fails, what needs attention, and the fix.
- **Shared precheck:** OP-02 (add-tenant), OP-03 (add-principal),
  OP-06 (configure node targeting), and OP-09 (patch Tenant/Principal)
  call this skill before their mutating step and act on `STATUS:`.
  OP-05 (enable/disable) and OP-07 (drain-node) do NOT call it.

`references/checks.md` explains every check code.

## Hard rules

1. **Read-only.** Only run `scripts/validate.py`. Never run
   `kubectl apply|patch|edit|delete|create|rollout|exec|scale`, or any
   mutating `kubectl krb` subcommand. The script never writes files
   either; it only reads the YAML the user named.
2. **No secret contents.** When checking that a keytab Secret exists and
   contains the named key, the script lists key names only (`-o
   jsonpath='{.data}'` then `jq keys`). Never read, print, decode or ask
   for the Secret's values. If the script output contains anything that
   looks like base64 Secret data, treat it as a bug and do not relay it.
3. **Evidence, not inference.** Every reported `fail` or `warn` must
   quote a `FINDING` line. Do not infer problems from file names, YAML
   comments, or the user's wording.
4. **Unknown is not valid.** `FINDING: unknown <code> | <reason>` means
   the check could not be performed (usually RBAC). Say that area is
   unverified; do not report it as passing.
5. **The user chooses the cluster.** Same rule as kerberator-inspect:
   use the context the user named, or the only one configured. If
   several exist, ask. Never use a kubeconfig file you found yourself
   (e.g. one in the repo) unless the user says to.

## Usage

Run from this skill's base directory:

```
scripts/validate.py --from-file <path> [-n NS] [--context C] [--kubeconfig F]
```

- `--from-file <path>` is required. It must be a YAML file on this
  machine; one Tenant, one Principal, or several (a multi-doc file is
  validated doc by doc).
- `-n NS` is the namespace the manifest will be applied to. It is used
  for ref lookups and to catch `metadata.namespace` mismatches. If a
  doc sets `metadata.namespace`, that wins for that doc; `-n` is the
  default for docs that omit it.
- `--context` / `--kubeconfig` work like every other skill. Omit them
  to use the current context.

Output:

```
STATUS: valid | valid-with-warnings | invalid | not-found | forbidden | crd-missing | plugin-missing | error
CONTEXT: <context>
HINT: ...
FINDING: ok|warn|fail|unknown <code> | <evidence>
CHECKED-OK: <codes>
---
== section == ...     # the parsed CR, resolved refs, raw kubectl output
```

Exit codes: `0` valid*, `1` error, `2` the input file is missing/unreadable,
`3` forbidden / crd-missing / plugin-missing, `4` invalid. A `valid-with-warnings`
result still exits 0, matching how other skills treat warnings.

## Workflow (standalone)

0. **Choose the cluster.** Follow kerberator-inspect's rules.
1. **Find the file.** If the user pasted YAML in chat, ask for the file
   path; the script needs a file. Never write it out yourself.
2. **Validate:** `scripts/validate.py --from-file <path> -n <ns>`.
3. **Report.**
   - `invalid`: list every `fail` with the fix from
     `references/checks.md`. Say the YAML should NOT be applied as-is.
   - `valid-with-warnings`: list every `warn` with the fix. Say the
     YAML will apply but call out each warning explicitly; some of them
     (e.g. `nodeselector-matches` matching zero nodes) are "the operator
     will accept this but it will not do what you want".
   - `valid`: say so, name the checks that passed from `CHECKED-OK`,
     and let the user apply.
   - `not-found`, `forbidden`, `crd-missing`, `plugin-missing`,
     `error`: report the status and the raw output. The YAML's validity
     is unknown for the ref checks that depend on the cluster; shape
     checks still ran.

## Workflow (shared precheck)

Other skills call this script before their mutating step. They should:

1. Run `scripts/validate.py --from-file <manifest> -n <ns>` once.
2. Parse `STATUS:` from the first line.
   - `invalid` → stop. Surface every `fail` line and the fix. Do NOT
     proceed to apply/patch. Suggest the user fix the YAML and re-run.
   - `valid-with-warnings` → surface every `warn` and ask the user
     whether to proceed anyway. The original request is not approval.
   - `valid` → proceed to the mutating step.
   - `forbidden` / `crd-missing` / `plugin-missing` / `error` →
     stop and report the status. The precheck did not complete; apply
     could still work or could fail in a worse way, so do not guess.

Callers must never apply the YAML on `invalid`, never skip the warnings
prompt on `valid-with-warnings`, and never hide the raw FINDING lines
from the user.

## Report format

Lead with a one-line verdict naming the file and the cluster. For
example:

> **`tenant.yaml` is invalid for `demo-ns` on `kind-dev`:** the realm is
> not uppercase and `tenantRef.name` points to a Tenant that does not
> exist here.

Then:

1. **Must fix.** Every `fail` finding, one bullet each, with the fix
   from `references/checks.md` as a command the user can run.
2. **Should review.** Every `warn` finding, with the fix. Mark the ones
   that mean "this will apply but do nothing useful" (zero node match,
   duplicate UID) separately from the cosmetic ones.
3. **Unverified.** Every `unknown` finding and the access that would
   settle it.
4. **Ruled out.** A compact list from `CHECKED-OK`.

Keep the report short when all findings point to the same root cause.
Say "no further checks ran" if a parse `fail` stopped the pipeline.

## Out of scope

- Applying the YAML, patching an existing CR, or running
  `kubectl krb edit`. Those belong to OP-02/03/06/09.
- Generating or exporting keytabs. The script never produces or
  modifies a Secret.
- Judging whether a running Tenant is healthy. That is DG-01
  (kerberator-diagnose-tenant). OP-08 only checks the config.
- Per-node Principal status. That is kerberator-inspect.

## Never

- `cat`, `base64 -d`, `kubectl get secret -o yaml/json`, or anything
  that would print the keytab bytes or any other Secret data.
- Edit the YAML file. Suggest the change as a diff for the user to
  apply.
- Run `kubectl krb edit` — agents cannot drive `$EDITOR`. Suggest
  `kubectl patch` or re-applying the fixed YAML.
