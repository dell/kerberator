---
name: kerberator-remove-principal-or-tenant
description: Safely offboard exactly one Kerberator Principal or Tenant (OP-10) using read-only prechecks, dependency inspection, explicit approval, native kubectl deletion, and independent verification.
---

# OP-10: Remove Principal or Tenant

## Purpose and scope

Remove exactly one explicitly identified Kerberator Principal or Tenant custom resource. The `kubectl krb` plugin does **not** provide a delete operation; the approved removal uses the native Kubernetes API through `kubectl delete` in `scripts/remove_resource.py`.

Deletion can interrupt Kerberos credential minting and active workloads. This is a **destructive, optional** operation. Never infer authorization to delete from a request to inspect, troubleshoot, plan, or describe deletion.

**Never** directly remove referenced Secrets, keytab contents, dependent Principals, namespaces, or any other unrelated resources. Tenant deletion uses Kubernetes background cascading deletion for its controller-owned children only. Do not use `kubectl krb delete`, which does not exist.

## Required inputs

- `kind`: `principal` or `tenant` (lowercase for script arguments)
- `name`: exact Kubernetes CR name, not a Kerberos identity string
- `namespace`: exact Kubernetes namespace
- `context`: explicit Kubernetes context, checked with `kubectl config get-contexts`

Ask for missing inputs. Do not guess the context or namespace. If the user provides a Kerberos identity rather than a CR name, resolve it read-only and confirm the exact CR name before proceeding.

The scripts reside relative to this `SKILL.md` in `scripts/`. Run them from the skill directory, or use absolute script paths.

## Step 1 — Precheck (read-only)

```bash
python3 scripts/precheck.py principal <name> \
  -n <namespace> --context <context>
```

For a Tenant, replace `principal` with `tenant`. The precheck validates resource existence, explicit context, delete/get permissions, Principal inventory availability, Secret metadata references, and get access to each exact controller-owned child.

**Exit codes:** `0` = READY or READY WITH WARNINGS; `1` = BLOCKED; `2` = invalid usage/environment. A `READY` result **does not** authorize deletion. If BLOCKED, stop; never bypass a failed check with raw `kubectl delete`.

## Step 2 — Inspect dependencies (read-only)

```bash
python3 scripts/inspect_dependencies.py principal <name> \
  -n <namespace> --context <context>
```

For structured results suitable for the evaluation harness, append `--json`.

For a **Principal**, inspect its `spec.tenantRef.name`, `spec.keytabSecretRef.name` and `.key`, and check whether another Principal references the same Secret. Discuss the effect of removing that Kerberos identity on workloads; the scripts cannot conclusively prove no external workload depends on it.

For a **Tenant**, enumerate all Principals with `spec.tenantRef.name` equal to the Tenant's CR name, inspect referenced Secret metadata, and inspect these exact controller-owned children: DaemonSet `<tenant>-kerberator-daemon`, ConfigMap `<tenant>-roster`, Secret `<tenant>-keytabs`, Role `<tenant>-kerberator-daemon-events`, and RoleBinding of the same name. If Principals remain, **block Tenant deletion**. If any child cannot be inspected, stop; inaccessible is not evidence that it is absent. Explain that cascading deletion may stop the DaemonSet and remove the Tenant's generated roster ConfigMap and aggregated keytab Secret. It does not delete the source keytab Secrets referenced by Principals.

Never display or decode `.data`, `.stringData`, keytab contents, credentials, or kubeconfig contents. Never fetch whole Secrets with `-o yaml` or `-o json`.

## Step 3 — Summarize impact and obtain explicit approval

Show the user the exact context, namespace, resource kind/name, discovered dependent Principals, referenced Secret **names**, exact Tenant-owned children and their status, warnings, and anything that cannot be verified. For a Principal, state that only the named CR is deleted and its referenced Secret is not deleted. For a Tenant, state that the named CR and Kubernetes dependents with its owner reference are removed by background cascading deletion; no source keytab Secret or unrelated resource is intentionally deleted. Explain that loss of the identity or Tenant may interrupt credentials or workloads.

If precheck or dependency inspection reports blockers, **stop**. For warnings, explain them and request an explicit decision before proceeding. Any change to target, context, or namespace requires fresh checks and approval.

Ask: **“Do you approve deleting `<kind>/<name>` in namespace `<namespace>` on context `<context>`? This may disrupt Kerberos authentication.”**

Wait for an affirmative answer to this specific deletion. Do not treat the original request as execution approval. Never manufacture a confirmation string for an agent without a corresponding human approval.

## Step 4 — Delete exactly one approved CR

After explicit human approval, use the supplied script. It reruns the precheck and dependency inspector before mutation and accepts an exact target-bound confirmation string.

```bash
python3 scripts/remove_resource.py principal <name> \
  -n <namespace> --context <context> \
  --confirm "DELETE principal/<name> IN <namespace> ON <context>"
```

For a Tenant, substitute `tenant` in both places. **This command is destructive.** Never invoke it during skill authoring, exploration, or automated evaluation unless the test explicitly uses a disposable target and an authorized human approves the deletion.

The script uses native `kubectl delete` for the exact CR. For a Tenant, it requests background cascading deletion (`--cascade=background`) so Kubernetes garbage-collects controller-owned children. For a Principal, it preserves the existing orphan propagation; its referenced keytab Secret is not owned by the Principal and is not deleted. The cascade applies only through Kubernetes owner references; it does not authorize direct cleanup or guarantee deletion of unrelated resources. Do not substitute a direct `kubectl delete` as a workaround for a script error. On failure, stop and report the state; do not retry or attempt cleanup without new approval.

Note: the script's `--confirm` argument is a **technical guard**, not human authorization. The conversational approval in Step 3 is mandatory.

## Step 5 — Independently verify (read-only)

```bash
python3 scripts/verify_removal.py principal <name> \
  -n <namespace> --context <context>
```

For a Tenant, replace `principal` with `tenant`. Append `--json` for machine-readable evidence. When known, pass `--expected-uid <original-metadata-uid>` to detect a same-name replacement and `--secret <referenced-secret-name>` to report the Secret's remaining metadata status.

Do not equate a successful delete command with completed deletion. For a Tenant, the verifier polls until the Tenant and its exact DaemonSet, ConfigMap, aggregated Secret, Role, and RoleBinding are absent, and confirms no Principals still reference the Tenant. It reads Secret metadata only. Every exact child must be readable; missing RBAC produces `INCOMPLETE`, not a false absence. Treat only `RESULT: VERIFIED` as confirmed; `PENDING`, `FAILED`, or `INCOMPLETE` require a clear report and a read-only follow-up. Never manually remove remaining children or source Secrets under this workflow.

## Reporting

Report separately: (1) target, namespace, and context; (2) dependency inventory and unresolved risks; (3) the user's explicit approval; (4) whether deletion was accepted; and (5) independent verification outcome. If the CR is still terminating or the API is inaccessible, say that removal is **not yet verified**.

## Safety rules

- Never delete without exact, explicit human approval.
- Never directly delete multiple resources, a namespace, a source Secret, a DaemonSet, or a dependent Principal under this skill. A Tenant's controller-owned children are removed only through the approved Kubernetes cascade.
- Never bypass BLOCKED or incomplete dependency checks.
- Never use `--force`, wildcard deletion, or unapproved cascading cleanup.
- Never expose keytab bytes, Secret values, credentials, or kubeconfig contents.
- Never claim verification success from an agent statement or a delete command alone.
- Never commit, push, or modify the repository automatically as part of offboarding.

## Failure handling

| Condition | Response |
|---|---|
| Wrong context, insufficient permissions, or target missing | Stop; request correction or access. |
| Principal inventory unreadable | Stop; impact cannot be established. |
| Tenant still referenced by Principals | Block; separately offboard members after their own approval. |
| Secret shared by other Principals | Warn; never delete Secret. |
| Dependency impact uncertain | Surface uncertainty; do not claim deletion is safe. |
| No explicit approval | Do not run `remove_resource.py`. |
| Deletion accepted but CR persists | Report pending/incomplete; run read-only verification. |
| API error or unexpected mutation | Stop; report failure; never auto-retry/clean up. |

## Evaluation

Start with read-only scenarios: existing Principal, missing target, insufficient RBAC, shared Secret, and Tenant with dependent Principals. For destructive end-to-end testing, use a disposable Principal with no active workloads and obtain explicit approval. Record script exit codes, report messages, agent approval evidence, and independent cluster state in the existing Python evaluation framework.
