---
name: kerberator-create-tenant
description: Create one new Kerberator Tenant (OP-02) with minimal user input, read-only checks, explicit creation approval, and independent readiness verification.
---

# OP-02: Create a Tenant

Help the user create **one** Kerberator Tenant, then report whether its infrastructure is ready. **Prefer the built-in `kubectl krb add-tenant` CLI.** Only use a JSON manifest and `scripts/create_tenant.py` when the user needs advanced Tenant fields the CLI cannot set. Creating Principals, repairing Kubernetes security, installing operators, and modifying existing Tenants are out of scope.

## Keep the interaction short

**Usually ask only for the new Tenant name and Kerberos realm.** Reuse answers already given. Discover everything else read-only and present **one combined configuration review**, not a questionnaire for every option.

- **Cluster/namespace:** Use the context, kubeconfig and namespace already selected. Otherwise list configured contexts; if more than one is plausible, ask which to use. Never silently discover a private kubeconfig, switch contexts, or change the default namespace. Always pass explicit `--context`, `-n`, and the selected `--kubeconfig` when applicable.
- **Image:** Read the operator's configured daemon image; use that exact image with the CLI. Ask only if the effective image cannot be established. The CLI requires `--image`; a manifest can instead inherit the operator default.
- **Kerberos configuration:** Reuse confirmed non-secret `krb5.conf` text/file for the *same* realm and environment, if available. Otherwise ask once for the KDC address/configuration. Never invent a KDC. The CLI accepts `--krb5-conf-file PATH` or `--krb5-conf CONTENT`.
- **Proposed defaults:** 30-minute renewal; no extra node selector/tolerations (all eligible nodes); `/tmp` cache **only when safe for this cluster**. Explicitly propose `pruneStale=false` for a basic test Tenant, explain that it can leave old caches, and let the user approve or change it. Never silently enable pruning. Host directories are shared between Tenants on the same nodes.
- **Other settings:** Don't ask about resource limits, aliases, placement, disabled mode or custom cache paths unless requested or required by observed conditions. If requested, use the advanced-manifest route.

## Workflow

### 1. Read-only precheck

From this skill directory (or using an absolute script path), run:

```bash
python3 scripts/precheck.py NAME -n NAMESPACE --context CONTEXT
```

Add `--kubeconfig PATH` if selected. `BLOCKED` means stop. Review warnings, but don't turn every warning into a question. Check the operator image and relevant existing Tenant/cache placement read-only. An existing Tenant, inaccessible cluster, missing CRD, or insufficient permission is not a reason to try a different write. **Do not edit Kubernetes admission policies or RBAC.**

### 2. Prepare and review a single creation command

For ordinary creation, verify the installed CLI's syntax with `kubectl krb add-tenant --help`, then construct:

```bash
kubectl krb add-tenant NAME --realm REALM --image IMAGE \
  --krb5-conf-file CONFIG_PATH --renew-minutes 30 \
  --prune-stale=false --context CONTEXT -n NAMESPACE
```

Use `--krb5-conf CONTENT` instead of `--krb5-conf-file` if the approved configuration is inline; include `--kubeconfig PATH` when selected. These are placeholders, **not executable defaults**. Supply the user's approved pruning choice explicitly: the CLI's omitted default is `true`. Optionally run the plugin's `--dry-run` to preview the resource; it does not create a Tenant or prove readiness.

**Advanced fields only:** Use a reviewed JSON manifest and the existing `scripts/create_tenant.py` validation / SHA-256 / `--execute` flow (see `python3 scripts/create_tenant.py --help`). This is *optional*, not the normal path. Review and obtain approval before saving any manifest locally.

Show **one concise review**: target context/namespace, name, realm, image, Kerberos configuration source, intended nodes, cache path, pruning choice, and exact command or manifest. Explain that creating the Tenant causes the operator to create a DaemonSet/pods, ConfigMap, aggregated Secret, Role and RoleBinding; host caches may be written or pruned. Ask for **explicit approval of this exact create**. If inputs change, ask again.

### 3. Create once, then verify

After approval, recheck that the Tenant name is unused. Run **only the approved** CLI create, or the approved `create_tenant.py --execute` manifest creation—not both. No `apply`, patch, replace, delete, unapproved file writes, or automatic retries. If a create times out, check whether the Tenant exists before considering any further action.

Independently verify:

```bash
python3 scripts/verify_creation.py NAME -n NAMESPACE --context CONTEXT --wait-seconds 90
```

Add `--kubeconfig PATH` if selected. Inspect Tenant conditions (`Ready`, `DaemonSetReady`, `RosterConsistent`, `KeytabsResolved`), owned resources, ready/desired DaemonSet pods (desired > 0), and actual nodes. Never read Secret values/keytab bytes.

Report **separate outcomes**:

- **CREATED AND READY:** Tenant exists and infrastructure verification passed.
- **CREATED, NOT READY / UNVERIFIED:** Tenant exists but reconciliation, pods, or visibility are incomplete; explain the observed reason (such as a hostPath admission denial). Do not call this a creation failure.
- **NOT CREATED / UNKNOWN:** The API rejected creation or the result cannot be established; do not guess or retry.

A healthy *empty* Tenant does not establish successful KDC authentication or ticket minting. Registration and credential tests are separate skills.

## Safety and testing

- Explicit approval is required before **any** local or cluster write. Precheck and verification are read-only. The script's `--confirm` flag is not human consent.
- Never expose credentials, keytabs, tokens, kubeconfig secrets, or Secret `.data`.
- Never bypass admission/RBAC, silently overwrite an existing Tenant, or modify cluster security to make the test pass.
- Never commit or push to GitHub as part of this skill.
- Test read-only checks against the **selected real cluster** and use a disposable Tenant for approved end-to-end tests. Report observed results, not assumptions.
