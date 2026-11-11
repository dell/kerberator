---
name: kerberator-create-tenant
description: Discover the Kubernetes/Kerberator environment, propose a new Tenant with no manual Kerberos configuration entry, obtain one creation approval, create once, and verify infrastructure readiness (OP-02).
---

# OP-02: Create a Tenant

Create one new Kerberator Tenant and verify its state. **This is a discovery-first operation, NOT a configuration questionnaire.** The user may supply a kubeconfig file/path. Do not ask them to type a Kerberos realm, KDC address, `krb5.conf`, daemon image, node selector, cache directory, renewal interval, or pruning setting. Discover those from the selected cluster, select safe defaults, and explain any real blocker instead of making the user research Kubernetes. Never invent configuration.

## Interaction contract

1. **Start by inspecting, not asking.** If a kubeconfig was supplied, use it for all requests. Otherwise use an already selected kubeconfig/context; request kubeconfig access only if no working cluster access exists. Do not search private files for credentials or change kubectl's current context/namespace.
2. **Offer selections, not text-entry questions.** If several verified contexts, namespaces, or realms are suitable, show a short numbered list with descriptions and ask the user to *pick an option*. If exactly one is suitable, select it and show it in the final review. Never ask the user to type a realm or Kerberos configuration. Do not present multi-step wizard forms or ask about optional fields.
3. **Suggest a new Tenant name automatically.** Derive a DNS-safe name from the selected realm (e.g. `EXAMPLE.COM` → `example-tenant`); check for an existing Tenant and generated-resource collisions, and propose a unique suffix if necessary. State the proposed name in the final review; the user can change it if desired. Do not assume an arbitrary name is already approved.
4. **One meaningful approval.** Show the proposed Tenant, actual discovered configuration sources, intended effects, and exact create action; obtain explicit approval *before* creating anything. Do not separately ask permission to read the cluster. Do not create manifests or temporary files just for normal CLI creation.
5. If discovery fails, **report exactly what could not be found and why** (including missing RBAC); do not substitute made-up KDC endpoints or turn the failure into a questionnaire. Do not edit security policies, RBAC, existing Tenants, or the operator.

## 1. Discover the existing environment (read-only)

Use `kubectl --kubeconfig PATH` if provided, together with explicit `--context` and `-n` once resolved. Inspect in this order:

- **Context:** `kubectl config get-contexts -o name` (using the supplied kubeconfig). Use the sole context or offer a selection if multiple; do not silently select among multiple clusters.
- **Namespace:** inspect namespaces and existing Tenants (`kubectl get tenants -A -o json`, if authorized). Prefer an existing Kerberator-enabled project namespace with suitable creation RBAC; if multiple, offer a selection. Do not blindly target `default`. If cross-namespace listing is denied, inspect the selected context namespace and accessible namespaces; report the visibility limit.
- **Available realms:** collect actual `spec.realm` values from existing Tenants, and—where readable—non-secret KDC/ConfigMap configuration or operator-managed Kerberos configuration. Deduplicate and display only realms supported by observed evidence. If multiple, offer choices such as `1. EXAMPLE.COM (existing demo Tenant; KDC configuration available)`. A realm name alone does not prove a reachable KDC. **Never ask the user to type a realm.**
- **Kerberos/KDC config:** for the chosen realm, prefer `spec.daemon.krb5Conf` from an existing, compatible Tenant in the chosen environment; it is configuration text, not a keytab. Confirm it references the selected realm and inspect its KDC endpoint(s). If absent, inspect accessible non-secret ConfigMaps and KDC Services for an unambiguous configuration. Construct a minimal `krb5.conf` only if the realm and KDC host/port are established by cluster evidence, and identify that it is a generated proposal, not previously verified configuration. Never copy another realm's configuration blindly; never read Secret `.data`, keytabs, or kubeconfig credentials. **Do not ask for a krb5.conf path or content.**
- **Daemon image:** inspect the Kerberator operator's configured daemon image, then compatible existing Tenant image references. The CLI requires an explicit image: do not assume omission uses the operator default. If multiple valid images are found and their intended compatibility is unclear, offer a short choice with sources. Do not guess image tags.
- **Defaults:** use 30-minute renewal, `--prune-stale=false` for an initial test Tenant, and no custom placement unless observed cluster requirements demand one. The built-in CLI's default for pruning is `true`, so **pass `--prune-stale=false` explicitly**. The default host cache path is `/tmp`; check existing Tenant cache paths and shared-node/UID overlap. Avoid claiming isolation from Tenant namespaces. Do not change cache paths via the basic CLI (it has no host-cache-path flag).

After realm, namespace and proposed name are identified, run the bundled precheck (if available):

```bash
python3 scripts/precheck.py PROPOSED_NAME --realm CHOSEN_REALM -n NAMESPACE --context CONTEXT --kubeconfig PATH --json
```

Omit `--kubeconfig` if not applicable. It discovers useful hints and checks basic RBAC/name collisions. It **does not** prove operator readiness or resolve the actual `krb5.conf` contents; do the targeted read-only inspections above as needed. A `BLOCKED` result must not be bypassed. Treat visibility warnings as verification limitations, not prompts for the user to gather details. If the script's accepted flags differ from these, check `--help` and adapt.

## 2. Present one concise proposal

Show: selected cluster/context, namespace, proposed unique Tenant name, realm **and where it was found**, daemon image and source, Kerberos configuration source and KDC endpoint (not credentials), default renewal/pruning, node/cache implications, and any *observed* blockers. Explain that the operator will create the DaemonSet/pods, ConfigMap, aggregated Secret, and event RBAC; host cache writes may occur. **A new Tenant can be created even if Kubernetes admission later blocks its daemon pods; do not call that Ready.**

For standard creation use the installed `kubectl krb add-tenant` command. Confirm actual flags with `kubectl krb add-tenant --help`. It requires `--realm` and `--image`, and accepts `--krb5-conf` (inline text) or `--krb5-conf-file` (existing accessible file). Prefer using already discovered config inline, without writing a new file:

```bash
kubectl --kubeconfig PATH --context CONTEXT krb -n NAMESPACE add-tenant NAME \
  --realm REALM --image IMAGE --krb5-conf 'DISCOVERED_KRB5_CONF' \
  --renew-minutes 30 --prune-stale=false
```

This is an **illustrative command only**: fill in real discovered values, omit the kubeconfig option when unnecessary, safely quote multi-line configuration, and never execute placeholders. An already accessible config file can instead use `--krb5-conf-file`. If the CLI/plugin is absent or advanced Tenant fields are genuinely required, explain the fallback and use a manifest with `scripts/create_tenant.py` **only** after reviewing the exact proposed content and its separate local-write implications. Do not switch to the manifest path merely to collect more user inputs.

Ask one question: **“Create this Tenant with the configuration shown?”** A correction to the proposed name/realm/target requires updating the proposal before approval.

## 3. Create once and verify

Only after explicit approval, recheck the proposed Tenant does not already exist, then execute the approved `kubectl krb add-tenant` command **once**. Do not `apply`, patch, replace, auto-retry, or modify admission/RBAC. On an error or timeout inspect the Tenant read-only before determining whether creation occurred.

Verify using the bundled script:

```bash
python3 scripts/verify_creation.py NAME -n NAMESPACE --context CONTEXT --wait-seconds 90
```

Add `--kubeconfig PATH` if applicable. Inspect Tenant conditions, generated DaemonSet, ready/desired pods (desired > 0), and observed node coverage. If some child-resource metadata cannot be read, say so; never read Secret contents. **Report separately:** `CREATED AND READY`, `CREATED BUT NOT READY/UNVERIFIED` (include observed admission denials or other reasons), or `NOT CREATED/UNKNOWN`. Creating an empty Tenant does not prove KDC authentication or ticket minting.

## Boundaries

- No manual Kerberos configuration questionnaire. If the cluster contains no defensible realm/KDC/image evidence, stop with a precise missing-prerequisite report; do not manufacture answers.
- No local or cluster writes without explicit review and approval. No installation, security changes, deletion, or repair as part of this skill.
- Never disclose credentials, keytabs, kubeconfig values, or Secret data.
- Never commit or push to GitHub.
- Test against the selected cluster; don't claim production readiness based on a CLI success or local syntax test.
