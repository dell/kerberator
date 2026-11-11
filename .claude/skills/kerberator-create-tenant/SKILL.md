---
name: kerberator-create-tenant
description: Safely prepare, precheck, create, and verify one new Kerberator Tenant (OP-02), using bundled Python scripts and explicit human approval before all writes. Never overwrite an existing Tenant.
---

# OP-02: Create a Tenant

A Tenant custom resource (`kerberator.dell.com/v1alpha1`, namespaced) represents a Kerberos realm and an operator-managed daemon installation. `metadata.name` is a Kubernetes name; `spec.realm` is a Kerberos realm. Principals are separately created resources referencing the Tenant by name.

All operational scripts are bundled in `scripts/` relative to this file and need **Python 3 standard library** and installed `kubectl`, but not a source checkout. Run from the skill directory or use an absolute script path. Never silently rely on the default namespace or current context.

## Hard safety rules

1. Ask the user to select the context (and kubeconfig if applicable), namespace, Tenant name, realm, KDC/krb5.conf approach, image/default, node scope, host cache path and prune policy. Multiple contexts require explicit selection; never discover and select a private kubeconfig yourself. Do not substitute placeholder values.
2. **Every local or cluster write requires an exact preview and fresh affirmative approval.** This includes writing a manifest file, installing tools, modifying namespace/ServiceAccount/RBAC, creating the Tenant, or subsequent repairs. Dry-run consent is not create consent. Explain indirect effects of Tenant creation (operator-managed DaemonSet, ConfigMap, aggregated Secret, Role, RoleBinding, image pulls and possible host cache writes/pruning) in the create approval.
3. Do not use `apply`, `patch`, `replace`, or deletion as a creation fallback. `AlreadyExists` always means stop and inspect. Never auto-retry after a timeout; the API might have accepted creation.
4. Keep Secret values, keytab contents, tokens and kubeconfig credentials private. Secret checks must be metadata-only. Do not run pod exec or install/repair tools without separate approval.
5. The script `--confirm` flag is a technical guard, **not** a substitute for user approval. Never manufacture approval. The scripts cannot run against this Kubernetes cluster unless invoked in a real environment with suitable access.

## Configuration facts

- API: `kerberator.dell.com/v1alpha1` kind `Tenant`; CRD schema requires `spec.realm`.
- Realm CLI heuristic: uppercase ASCII A-Z, digits, `.`, `_`, `-`; when none of `.`, `_`, `-` occurs, require length >=3. The script enforces this even for manifest creation.
- `spec.daemon.krb5Conf` (when supplied) must include the literal `[realms]` marker and exact realm. This is a heuristic, **not** a complete Kerberos parser or connectivity test. Missing krb5.conf requires confirmed custom-image handling.
- Explicit `spec.daemon.image` must contain `/` or `:`. Omitting it in a manifest delegates selection to the operator; confirm actual image/version and pullability before executing.
- `renewMinutes` defaults to **30**; must be a positive integer. CLI `pruneStale` defaults **true**, but omitted manifest `pruneStale` is **false**. Explicitly confirm its value. When enabled it may remove previously managed caches on UID roster removal; never assume foreign caches are safe to prune.
- Host cache defaults to `/tmp` on nodes; namespaces do **not** isolate host paths. Review overlapping UIDs, host paths and daemon placements across Tenants before approval. Custom host cache paths can cause misleading daemon `readyz` results if `READYZ_CACHES` still uses `/tmp`.
- `nodeSelector` and `tolerations` determine node scope, not namespace. Empty node selector does not mean worker nodes only. `hostAliases`, `imagePullPolicy`, `resources`, `deletionTimeout`, `eventStalenessThreshold`, and `disabled` are optional according to the installed schema. Explicitly review requested advanced fields; don't invent defaults not specified in the API.
- The implementation expects a namespace `default` ServiceAccount for daemon pods; the Tenant manages its own event RBAC. Check current Pod Security restrictions, image pull permissions, operator installation and reconciliation scope. A running operator need not be in the Tenant namespace. Do not install anything automatically.
- KDC availability, correct realm/KDC DNS, time synchronization, and access from eligible nodes are operational prerequisites for minting, **not** established by an API precheck. Empty Tenants need no Principals or source keytab Secrets, but cannot establish real Kerberos auth success.

## Workflow

### 1. Gather and preview configuration

Establish context, namespace, new Tenant name, realm and krb5.conf source, effective daemon image, target nodes, cache path, prune policy, renewal interval, optional fields, and intended initial disabled state. If multiple contexts exist, show configured names and API servers *without credentials* and ask for the target. Never switch current context or default namespace. Validate any requested advanced field against the **installed** CRD schema. Confirm operator and intended nodes using read-only Kubernetes queries; if RBAC prevents inspection, report the limitation rather than claiming success.

For reliable script validation, create a **JSON** Tenant manifest (JSON is valid YAML), e.g.:

```json
{
  "apiVersion": "kerberator.dell.com/v1alpha1",
  "kind": "Tenant",
  "metadata": {"name": "<approved-name>", "namespace": "<approved-namespace>"},
  "spec": {"realm": "<APPROVED.REALM>", "daemon": {"pruneStale": false, "renewMinutes": 30}}
}
```

This is an **illustrative shape only**, not a working KDC configuration. Replace every placeholder with actual approved data, include the intended krb5.conf/image and any advanced fields. Preview the **exact file contents and path** and obtain separate approval before writing the local manifest; alternatively prepare an already user-provided JSON file. No manifest file should contain credentials or Secret bytes.

### 2. Precheck (read-only)

```bash
python3 scripts/precheck.py <tenant-name> -n <namespace> --context <context>
```

Append `--kubeconfig <approved-path>` when applicable. This checks context, namespace, CRDs, default ServiceAccount, Tenant create/get permissions, verification read permissions, exact absence of target and collision-prone owned children. Result `BLOCKED` is a hard stop; `READY WITH WARNINGS` is **not** permission to ignore warnings. It does **not** establish operator/eligible-node/KDC/image/host-cache health; carry out those additional read-only inspections before creation and explicitly disclose unresolved uncertainties.

### 3. Validate the manifest (no write)

```bash
python3 scripts/create_tenant.py <approved-manifest.json> \
  --name <tenant-name> -n <namespace> --context <context>
```

Use `--kubeconfig` if selected. Local validation checks identity, realm, selected daemon fields, and prints `MANIFEST SHA256`. JSON syntax and these heuristics do **not** replace full live CRD schema validation. For advanced fields, inspect `kubectl explain tenant.spec --recursive` and installed CRD schema. An OPTIONAL Kubernetes server dry-run `kubectl create --dry-run=server -f <manifest>` requires an explicit preview and approval because it sends a create-shaped admission request (though not persistence). It does not prove reconciliation or KDC connectivity.

### 4. Preview, ask approval, create exactly once

Show the exact complete manifest, SHA-256 digest, context/namespace/API server, image choice, intended node coverage, cache/pruning consequences and all operator-managed children. Ask: **“Do you approve creating Tenant `<name>` in namespace `<namespace>` on context `<context>` using SHA-256 `<digest>`, with the described operator-managed resources and host-cache effects?”** Wait for explicit affirmative approval.

After approval only:

```bash
python3 scripts/create_tenant.py <approved-manifest.json> \
  --name <tenant-name> -n <namespace> --context <context> \
  --execute --approved-sha256 <exact-validated-sha256> \
  --confirm "CREATE tenant/<tenant-name> IN <namespace> ON <context>"
```

Add approved `--kubeconfig` if applicable. Execution reruns precheck and performs a single native Kubernetes `create`, never apply/patch/upsert. A changed file hash or target invalidates approval. A create success is *not* a readiness claim. On error/timeout inspect read-only; do not retry, roll back, or repair automatically.

### 5. Independently verify (read-only)

```bash
python3 scripts/verify_creation.py <tenant-name> \
  -n <namespace> --context <context> --wait-seconds 90
```

Use `--json` for structured reporting and `--kubeconfig` if applicable. Verify Tenant conditions `Ready`, `DaemonSetReady`, `RosterConsistent`, and `KeytabsResolved` all equal `True`, with existing child DaemonSet `<name>-kerberator-daemon`, ConfigMap `<name>-roster`, aggregated Secret `<name>-keytabs`, and Role/RoleBinding `<name>-kerberator-daemon-events` owned by the Tenant UID. Require positive DaemonSet desired count with all desired ready; inspect pod readiness and **actual node names**, and compare them to the user's intended placement. The verifier uses metadata-only Secret reads. `Ready=True` by itself is not proof. A missing/unknown condition or inaccessible resource remains **unverified**.

A verified *empty* Tenant has ready Kubernetes infrastructure, **not** verified ticket minting, KDC communication, valid keytabs, or cache correctness. `principalCount` includes Principals referencing the Tenant and is not the active roster size. Host cache isolation across Tenants remains a separate review.

### 6. Report and hand off

Report target identity, approved config and SHA, precheck warnings/limitations, whether creation was accepted, observed condition values, child ownership, ready/desired DaemonSet numbers, actual node placement and verification outcome. On failed verification, gather read-only evidence; do not restart/patch/delete without new authorization. For ongoing Tenant/Principal inspection, hand off to sibling `kerberator-inspect` if present. Register Principals only in a separately approved workflow. A copied `.claude/skills` directory should work without opening the Kerberator source tree.

## Testing

First test scripts with `--help`, `python3 -m py_compile`, malformed JSON/realm, an existing name, missing namespace/context, denied RBAC, missing CRD and child collisions. Run read-only precheck/verification on the **actual selected Kubernetes cluster**, not only against local mocks. Only perform an end-to-end create against an explicitly approved disposable Tenant in the requested namespace. Record script exit codes and read-only observed state; never claim live validation from syntax tests.
