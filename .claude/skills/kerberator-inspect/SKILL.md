---
name: kerberator-inspect
description: Read-only inspection of Kerberator Tenants and Principals via kubectl-krb. Use when asked to list, show, describe, check the status or health of, or find a Kerberator Tenant, Principal or UID (e.g. "what tenants exist", "is principal svc-2001 healthy", "why isn't uid 2001 minting", "does tenant demo exist"). Reports observed status and explains missing resources. Do NOT use for creating, rotating, editing, disabling or draining — those are out of scope.
---

# kerberator-inspect

Inspect Kerberator's two CRDs without changing anything:

- **Tenant** — one Kerberos realm; owns a DaemonSet, roster ConfigMap, keytab Secret.
- **Principal** — one UID bound to one Kerberos principal + keytab, referencing a Tenant by `spec.tenantRef.name`.

## Hard rules

1. **Read-only.** Only run `scripts/inspect.sh` (which calls `kubectl krb list|describe` and reads the local kubeconfig). Never run `add-tenant`, `add-principal`, `rotate-keytab`, `edit`, `disable`, `enable`, `drain-node`, or `kubectl apply/delete/patch`. If the user wants a change, say it is outside this skill and stop.
2. **Report what was observed, not what was intended.** Quote values from the CLI output; never infer status from `spec` or from names.
3. **Never invent output.** If the script returns an error status, report that status.
4. **The user chooses the cluster.** Only inspect a cluster (kubeconfig context) the user named, or the only one configured. If several exist and none was named, show them and ask. Never use a kubeconfig file you found yourself (in the repo, home directory, etc.) unless the user says to; you may mention that it exists.

## Usage

```
scripts/inspect.sh contexts [--kubeconfig F]
scripts/inspect.sh list [-A | -n NS] [--tenant NAME] [--context C] [--kubeconfig F]
scripts/inspect.sh describe tenant <name>    [-n NS] [--context C] [--kubeconfig F]
scripts/inspect.sh describe principal <name> [-n NS] [--context C] [--kubeconfig F]
```

Output: line 1 `STATUS: <state>`, line 2 `CONTEXT: <cluster context used>`, optional `HINT:` lines, `---`, then the raw CLI output.
`contexts` lists the configured clusters from the local kubeconfig; it contacts no cluster.
Exit: 0 `ok`/`empty`, 2 `not-found*` (incl. `not-found-by-name`, `not-found-unverified`), 3 `forbidden`/`crd-missing`/`plugin-missing`, 1 `error`.

## Workflow

0. **Choose the cluster.** Run `contexts` unless the user already named a context or kubeconfig.
   - `contexts=1`: use it, and state which cluster you inspected.
   - `contexts>1` and none named: show the list (name, API server, which is current) and ask the user whether to inspect **one**, **a chosen group**, or **all**. Do not default silently to the current one.
   - `contexts=0`: say no cluster is configured and ask for a kubeconfig or context.
   - **Several clusters selected:** run each lookup once per cluster with `--context <name>` and report results under a heading per cluster. Never merge rows from different clusters; a failure on one cluster (`forbidden`, `crd-missing`, `error`) is reported for that cluster and does not stop the others.
   - **Can't ask** (non-interactive run): use the current context only, name it, and list the other contexts you did not inspect.
1. **Discover first.** If the user didn't give an exact name and namespace, run `list -A`. If that is `forbidden` (namespace-scoped users often can't), use `list` in the current namespace or the namespace the user names, and say the search was limited to it. Principal CR names (e.g. `svc-2001`) are not UIDs (`2001`); the list shows both.
2. **Describe the target** with `-n <namespace>` taken from the list output.
3. **Report** using the rules below.

## Reporting observed status

Lead with a one-line verdict, then the evidence. Always name the cluster (the `CONTEXT:` line) the answer applies to.

- **Healthy** only if: Ready is `True`, nodes are `n/n`, and every per-node REASON is `Minted`. Otherwise say which condition fails.
- Always include ages ("KVNO 9, observed 23s ago"). A large OBSERVED age is not a problem on its own: daemons report on mint/renewal, so a healthy node can be minutes or hours old. Flag a node as stale only with a second signal (DAEMON READY `false`, an age far beyond the other nodes', or beyond the Tenant's renew interval), and say which.
- Surface every failing node: node name, REASON, MESSAGE (e.g. `worker-3 MintFailed: kinit failed … KDC unreachable`).
- Call out explicitly, when present: `KVNO: <not observed>`, `Deleting: since …`, `Nodes ready` below desired, `(none — …)` Events.
- Distinguish **Tenant CR name** from the **Kerberos realm** (`spec.realm`) and **Tenant CR ref** (parent Tenant name).
- See `references/status-fields.md` for what each field means.

## Missing resources

Map the script's STATUS to a plain-language answer. Do not just echo the raw error.

| STATUS | What to tell the user | Next step |
|---|---|---|
| `not-found` | No such Tenant/Principal in any namespace. | Show the closest names from `list -A`; suggest it has not been created (creation is out of scope here). |
| `not-found-in-ns(a,b)` | A resource with that CR name exists, but not in the queried namespace; give `ns/name`. | Re-run with `-n`. |
| `not-found-by-name` | The value given is a **UID**, not a CR name; a Principal with that UID exists (e.g. UID `2001` is `kerberator-demo/svc-2001`). | Re-run `describe principal <CR-name> -n <ns>` using the name from the HINT. |
| `not-found-unverified` | Not in the queried namespace, but other namespaces **could not be searched** (cluster-wide list denied). Absence elsewhere is **unknown**; do not say it doesn't exist. | Retry with `-n <ns>` for the namespaces you expect; say what access would settle it. |
| `empty` | Nothing in the queried namespace. | If `-A` was not used, re-run with `-A` before saying the cluster has none. |
| `forbidden` | The caller lacks RBAC; resource state is **unknown**, not missing. | Report the denied verb/resource; do not claim absence. |
| `crd-missing` | Kerberator CRDs aren't installed in this cluster/context. | Check `--context`; operator not deployed. |
| `plugin-missing` | `kubectl-krb` isn't installed. | Point to `cli/kubectl-krb/README.md`. |
| `error` | Unclassified failure (e.g. unreachable API server). | Quote the raw error; state that status is unknown. |

Also check a **dangling reference**: a Principal whose `Tenant CR ref` has no matching Tenant in `list -A` is orphaned — report it as such.
