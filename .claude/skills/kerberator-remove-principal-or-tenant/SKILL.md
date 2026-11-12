---
name: kerberator-remove-principal-or-tenant
description: Safely offboard exactly one Kerberator Principal or Tenant (OP-10) using read-only prechecks, dependency inspection, explicit approval, native kubectl deletion, and independent verification.
---

# OP-10: Remove Principal or Tenant

## Purpose and scope

Remove exactly one explicitly named Kerberator Principal or Tenant custom resource. The `kubectl krb` plugin has no delete operation; after approval, `scripts/remove_resource.py` uses native `kubectl delete` for the specified CR only.

Deletion is destructive and may disrupt Kerberos authentication and running workloads. Never infer permission to delete from a request to inspect, troubleshoot, plan, or explain. **A command-line confirmation string is only a technical guard; it is not evidence of human approval.**

Never directly delete dependent Principals, source keytab Secrets, other Tenants, namespaces, DaemonSets, or unrelated resources. Tenant deletion requests Kubernetes background cascading deletion **only for objects with relevant owner references**; it does not guarantee cleanup of every named child.

## Required inputs

- `kind`: `principal` or `tenant` (lowercase script argument)
- `name`: exact Kubernetes CR name, not a Kerberos principal identity string
- `namespace`: exact Kubernetes namespace
- `context`: exact configured Kubernetes context

Ask for missing inputs. Resolve ambiguous names with read-only queries; do not guess. Run scripts from the directory containing this `SKILL.md`, or use absolute script paths.

## Inspection modes

**Strict mode (default):** Every required Tenant-owned child must be inspectable. Unreadable children block deletion. Always use strict mode for Principals and for Tenants not explicitly established as disposable.

**Limited-inspection mode (optional, Tenants only):** Use `--limited-inspection` only when the user has explicitly identified the *specific Tenant* as a disposable test resource and knowingly accepts the risk of inaccessible controller-owned children. This mode converts only **confirmed RBAC `get` denials** on exact Tenant children into `UNKNOWN` warnings. It must never infer that those children are absent, unused, safe to delete, or guaranteed to be garbage-collected. A timeout, connection failure, unexpected API error, invalid response, or inability to establish authorization remains blocking.

In **both** modes, block if the target cannot be read, the context is not configured, delete permission is missing, Principal inventory cannot be listed, any Principals still reference a Tenant, the target is already deleting, or any other mandatory check fails. `--limited-inspection` is invalid for Principal deletion.

Use the *same mode* in precheck, dependency inspection, deletion, and verification. Never silently fall back to limited mode to bypass a failed strict check: explicitly explain the risk and obtain human approval for limited mode before deletion.

## Step 1 — Precheck (read-only)

```bash
python3 scripts/precheck.py tenant <name> \
  -n <namespace> --context <context>
```

For a disposable test Tenant with approved limited-inspection scope, append `--limited-inspection`.

For a Principal, substitute `principal` and do not use the flag. Precheck validates target existence, context, authorization, Principal inventory, and inspectability of exact Tenant-owned children. **Exit 0** means `READY` or `READY WITH WARNINGS`; **exit 1** means `BLOCKED`; **exit 2** means invalid usage/environment. `READY` is **not** deletion approval. Any `BLOCKED` result stops the workflow.

## Step 2 — Inspect dependencies (read-only)

```bash
python3 scripts/inspect_dependencies.py tenant <name> \
  -n <namespace> --context <context> --json
```

For a disposable test Tenant in limited mode, append `--limited-inspection`.

For a Principal, inspect its `spec.tenantRef.name`, `spec.keytabSecretRef.name` and `.key`, plus other Principals referencing the same Secret. Never delete the referenced Secret. External workload dependency cannot be conclusively proved from CR references alone.

For a Tenant, enumerate Principals whose `spec.tenantRef.name` equals the exact Tenant CR name. **Any member Principal blocks deletion.** Inspect these exact controller-owned child names, without fetching Secret data:

- DaemonSet `<tenant>-kerberator-daemon`
- ConfigMap `<tenant>-roster`
- Secret `<tenant>-keytabs`
- Role `<tenant>-kerberator-daemon-events`
- RoleBinding `<tenant>-kerberator-daemon-events`

In limited mode, children with confirmed RBAC-denied inspection must show `exists: null` / `UNKNOWN` with the error and warning. Never convert UNKNOWN to absent. The report must not contain other blockers. Avoid reading `.data`, `.stringData`, keytabs, kubeconfig contents, or complete Secret JSON/YAML.

## Step 3 — Summarize risks and request explicit approval

Before *any* deletion, show the user the exact kind/name, namespace, context, Principal dependencies, discovered child resource names and status, and all warnings. In limited mode, **explicitly list all UNKNOWN children** and explain that child cleanup cannot be independently verified with current RBAC permissions. Note that Tenant deletion may affect node credentials and workloads, and Kubernetes garbage collection only acts on objects with matching owner references.

Ask the user to approve deleting this exact resource on this context and namespace. For limited inspection, explicitly state that approval includes proceeding **despite listed RBAC inspection gaps**, and only for the identified disposable test Tenant. Wait for the user's affirmative approval **in the conversation**; a prior request to explain or plan deletion is not sufficient. If kind/name/context/namespace or inspection mode changes, rerun checks and obtain new approval. Do not synthesize human approval or a confirmation string on behalf of the user.

## Step 4 — Delete exactly one approved custom resource

**Only after explicit human approval**, run the supplied script. It reruns read-only checks before any mutation. A Tenant uses background cascading deletion; a Principal uses orphan propagation. Neither permits manual cleanup of dependent or source resources.

Strict Principal example:

```bash
python3 scripts/remove_resource.py principal <name> \
  -n <namespace> --context <context> \
  --confirm "DELETE principal/<name> IN <namespace> ON <context>"
```

Strict Tenant example:

```bash
python3 scripts/remove_resource.py tenant <name> \
  -n <namespace> --context <context> \
  --confirm "DELETE tenant/<name> IN <namespace> ON <context>"
```

For an explicitly approved disposable Tenant in limited mode, add `--limited-inspection` to the Tenant command. Do not use this flag for Principals. The script's `--confirm` value is an exact-target technical guard, **not** a substitute for Step 3 approval.

Do not use raw `kubectl delete` as a workaround for script failure. Do not force, retry, or manually clean up child objects without new authorization. A deletion request accepted by Kubernetes is not proof of full removal.

## Step 5 — Independently verify (read-only)

```bash
python3 scripts/verify_removal.py tenant <name> \
  -n <namespace> --context <context> --json
```

For limited mode, append `--limited-inspection`. For Principals, substitute `principal` and omit the flag. When available, pass `--expected-uid <original-metadata-uid>` to detect same-name replacement; `--secret <referenced-secret-name>` only checks related Secret metadata.

The verifier polls for bounded periods; it separately reports `target_status` and `cleanup_status` for Tenant deletions. **`target_status: VERIFIED_REMOVED` with `cleanup_status: INCOMPLETE` means the Tenant CR is absent, but full removal of its children is not verified.** Overall `VERIFIED` requires independently established target removal and complete applicable child checks; UNKNOWN children produce overall `INCOMPLETE`. Report `PENDING`, `FAILED`, and `INCOMPLETE` accurately. Never equate `kubectl delete` success, an absent Tenant alone, or the agent's assertion with complete cleanup.

## Final report

Report exact target/context/namespace, chosen inspection mode, Principal inventory outcome, list of observed and UNKNOWN children, explicit human approval, whether the deletion request was accepted, and both target and cleanup verification results. If children remain unreadable, say **Tenant removed; child cleanup unverified**, not “all resources removed.” No additional mutation is authorized by a verification result.

## Safety rules

- Never delete without affirmative human approval for the exact target and inspection mode.
- Never delete a Tenant with member Principals; never use limited inspection for Principals.
- Never bypass unknown failures, unreachable API, unverified target identity, missing delete permission, or principal inventory failure.
- Never fetch Secret contents, expose credentials, delete source Secrets, force deletion, or manually remove controller-owned children under this skill.
- Never infer controller ownership solely from resource names; background garbage collection depends on Kubernetes owner references.
- Never claim full cleanup when RBAC hides a child; preserve UNKNOWN in results.
- Never commit, push, or modify repository files as part of an offboarding run.

## Failure handling and evaluation

Wrong context, missing target, unreadable target, missing permissions to delete, unreadable Principal inventory, or member Principals: **BLOCKED**. RBAC-denied Tenant child inspection: **BLOCKED** in strict mode, **UNKNOWN warning** only in explicitly opted-in limited mode. Unexpected errors remain blockers. Deletion accepted but CR or children remain: report pending/incomplete and follow with read-only verification; never automatically retry cleanup.

For evaluation, compare strict versus limited-inspection behavior on a *known disposable* Tenant with mocked RBAC denial and no member Principals. Capture mode, read-only check exit codes, the UNKNOWN resource list, human approval evidence, deletion result, `target_status`, `cleanup_status`, and overall verdict. No live mutation during skill authoring or dry-run tests.
