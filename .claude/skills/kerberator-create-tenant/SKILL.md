---
name: kerberator-create-tenant
description: Guide configuration and creation of a Kerberator Tenant, collect the user's cluster, realm, Kerberos configuration, and daemon requirements, preview the exact creation, obtain confirmation before every non-read-only action, then verify reported Tenant readiness and dependent resources. Use when asked to create, configure, or onboard a new Tenant. Standalone status requests and changes to existing Tenants are outside this creation workflow.
---

# kerberator-create-tenant

Guide the user from configuration choices to one new Tenant and an evidence-based report of its observed state. Gather requirements, preview the exact action and effects, wait for explicit confirmation, create once, then verify without further writes. Hand off diagnosis and subsequent Principal registration as described below.

This package works from a copied `.claude/skills` directory with installed Kubernetes tools and the user's selected cluster access. Resolve bundled reference paths relative to this skill's directory and hand-off paths relative to its sibling skill directories. Load a reference only at the step that needs its details. Do not read or require a Kerberator source checkout, repository documentation, or a repository-local binary at runtime. Use installed CLI help/version and the selected cluster's API schema to check compatibility; report discrepancies before proceeding.

A **Tenant CR** is a namespaced Kubernetes object representing a Kerberos realm and a daemon installation. The **realm** (`spec.realm`, e.g. `EXAMPLE.COM`) is distinct from the **CR name** (`metadata.name`, e.g. `demo`). A **Principal** binds a POSIX UID to a Kerberos principal and references the Tenant CR name through `spec.tenantRef.name` in the same namespace.

## Hard rules

1. **Ask before every non-read-only action.** First show the exact proposed command or file content/diff, target context/namespace/path, and effects; then ask for explicit confirmation and wait. This includes creating directories or local files, redirecting output to a file, installing/building tools, namespace/Secret/RBAC/ServiceAccount changes, creating the Tenant, changing configuration, restarting pods, and cleanup/deletion. A configuration answer, dry-run approval, or earlier broad request to create a Tenant is not confirmation to execute a later write. Each separate write action needs its own confirmation. Changed inputs or another write attempt require fresh confirmation. Do not combine unreviewed writes in a script, pipe, command chain, or TUI. If interaction is unavailable, stop with a chat-only plan and perform no writes.
2. **Explain indirect effects before Tenant creation.** The approved Tenant create triggers operator-managed ConfigMap, Secret, DaemonSet, Role, and RoleBinding creation, daemon pods, image pulls, and potentially host credential-cache writes and pruning. Include these in that approval; no separate approval is needed for each controller consequence of the approved create. A later agent-initiated mutation still needs separate confirmation.
3. **The user chooses the cluster.** Use a user-named context/kubeconfig or the only configured context. With several contexts, show names and API servers and ask which one; never silently use the current context. Do not discover and use repo/home kubeconfigs without the user's choice. Always pass explicit `--context` and `-n` to cluster commands, and `--kubeconfig` if selected. Do not change the current context or default namespace. Create on one confirmed target at a time.
4. **Collect configuration feedback.** Ask for missing choices in small related groups, explain consequences, and let the user correct the proposal. Do not invent KDC addresses, realm, image tag, namespace, node scope, or cleanup policy. Offer defaults as choices and confirm them in the final proposal. Never execute example values or unresolved placeholders.
5. **Read before creating; never overwrite.** An existing Tenant means inspect and explain, then stop this creation path. Do not turn AlreadyExists into edit, apply, replace, deletion, or adoption. Do not take over colliding child objects or patch owner references.
6. **Keep credentials private.** Do not print kubeconfig credentials, tokens, Secret data, or keytab bytes. Use metadata-only Secret queries. A Tenant needs no KDC admin password or keytab. Request configuration and keytab paths/references rather than secret material in chat. Quote shell arguments safely; treat user values and configuration as data, never executable shell fragments.
7. **Report observations, not intentions.** A successful create is not readiness. `Ready=True` alone is insufficient. Never invent output, hide failed checks, call Forbidden “missing,” or repair failures without a new reviewed proposal and confirmation. Pod exec and diagnostic jobs are not automatically read-only; any such operation requires confirmation.

## Ingredients and configuration choices

Establish these before proposing creation. Separate what the API requires from what makes a usable installation.

### Cluster and dependencies

- A reachable, selected Kubernetes context, an existing intended namespace, and permission to create `tenants.kerberator.dell.com` there. Verification also needs read access to Tenants, Principals, DaemonSets, pods, ConfigMaps, Secret metadata, Roles, RoleBindings, and Events. If access is limited, state exactly what cannot be verified; do not broaden permissions automatically.
- Installed Tenant and Principal CRDs and a running Kerberator operator with rights to reconcile in that namespace. The operator can live in a different namespace; discover its installation rather than assuming `kerberator-system`. CRDs alone do not establish a running controller.
- Existing namespace `default` ServiceAccount. This implementation uses it for daemon pods and grants it `create,patch` on Events through the Tenant-owned Role/RoleBinding. It does not create a dedicated ServiceAccount, and TenantSpec has no serviceAccountName field.
- Nodes able to run the daemon image and permit the rendered pod's hostPath, root credential-cache ownership operations, and `CHOWN`/`FOWNER` capabilities. The pod drops other capabilities, disallows privilege escalation, and uses a read-only root filesystem. Check applicable Pod Security/OpenShift policies; do not weaken them automatically.
- Reachable KDC and working name resolution from the daemon's network on every intended node; correct realm, KDC address/port, time synchronization, and Kerberos policy. Do not claim these are verified merely because the Kubernetes API is reachable. The Tenant does not provision a KDC, Kerberos identities, or external storage consumers.
- A pullable daemon image compatible with the operator and its `readyz` probe. Ask whether to use the installed operator's configured daemon image or an explicit override. Do not copy an old example version. Private image credentials must work in the Tenant namespace: operator chart `imagePullSecrets` do **not** propagate to daemon pods. The namespace's default ServiceAccount can reference a pull Secret; any Secret creation or SA patch requires its own confirmation. There is no Tenant `imagePullSecrets` field.
- Principals and source keytab Secrets are **not prerequisites for creating an empty Tenant**. They are prerequisites for useful ticket minting and constitute separate, explicitly requested and confirmed writes. Create the Tenant before new Principals: the Principal webhook checks that its Tenant exists in the same namespace. The Helm default webhook setup uses cert-manager unless certificates are supplied another way; this webhook validates Principals, not Tenants. Never disable admission to get past a failure.

### Tenant fields and defaults

The API is `kerberator.dell.com/v1alpha1`, kind `Tenant`, namespaced, plural `tenants`, aliases `krbtenant`/`kt`. Schema requires `spec` and nonempty `spec.realm`; Kubernetes also validates metadata. `daemon` and its image are not schema-required. Select a short Kubernetes-compatible CR name that also fits the generated child names and label values; the `-kerberator-daemon-events` suffix must fit applicable name limits.

Keep these defaults explicit in the proposal: effective renewal interval **30 minutes**; CLI `pruneStale=true`, while an omitted manifest field is false; host cache directory `/tmp`. Namespaces do not isolate host cache files, so confirm node/path overlap and pruning intent. Read [references/tenant-fields.md](references/tenant-fields.md) only when choosing or explaining field-specific defaults, placement, resources, or other optional settings.

**Custom cache-path limitation:** the supported DaemonSet passes `--caches=<hostCachePath>` to the daemon, but its `readyz` probe defaults `READYZ_CACHES` to `/tmp` unless the deployment supplies an override. A non-default path can therefore misreport readiness. Explain this limitation before choosing such a path and check the installed pod template; do not promise support or silently patch generated resources.

## Usage and CLI behavior

Read the installed plugin's `kubectl krb add-tenant --help` and `kubectl krb describe --help` before using it. If the plugin is unavailable, a manifest with native `kubectl` can perform creation and status reads; do not install/build tools without confirmation. If installed behavior differs from the facts below, compare help/version and the installed API schema, then revise the proposal before execution.

The supported CLI interface is:

```text
kubectl krb add-tenant <name>
  --realm REALM                         required
  --image IMAGE                         required
  --krb5-conf-file PATH | --krb5-conf CONTENT
  --renew-minutes INTEGER                default 30
  --prune-stale=true|false               default true
  --dry-run                             print YAML; no API create
  --context CONTEXT -n NAMESPACE [--kubeconfig PATH]
kubectl krb describe tenant <name> --context CONTEXT -n NAMESPACE
```

- File and inline krb5.conf options are mutually exclusive; neither is enforced as required, but omission needs the custom-image discussion above.
- `--dry-run` is a Boolean flag, not `--dry-run=client` or `--dry-run=server`. It validates CLI fields and prints YAML to stdout. It still builds a Kubernetes client and resolves kubeconfig/namespace, so it can fail on configuration; it is not server admission validation or a connectivity test. Keep output in chat/stdout unless a separate file-write approval is obtained.
- Despite “Scaffold + apply” help text, `add-tenant` uses a straight API **Create**, not apply/upsert. AlreadyExists is an error. It does not create the namespace, KDC, source keytabs, or Principals; it does not wait for readiness.
- CLI cannot set placement, resources, hostAliases, hostCachePath, pull policy, duration fields, or disabled state. Use a fully reviewed Tenant manifest and `kubectl create`, rather than create-then-patch or silently omitting requested fields. A manifest also supports intentionally inheriting the operator image.
- Always specify `-n`: this CLI's namespace resolver falls back to the raw kubeconfig current context's namespace when `-n` is omitted, even when another `--context` selects the API connection.

## Workflow

### 1. Choose target and gather requirements

Read this skill's ingredients first. Explain the CR-name/realm distinction and that Tenant creation launches a per-node daemon. Use read-only `kubectl config get-contexts` and a credentials-free API-server/context listing if target selection is needed. Do not dump `kubectl config view --raw`.

If no contexts are configured, ask for the intended kubeconfig/context and stop cluster operations until it is available. If several are configured and none was selected, show their names, API servers, and which is current, then wait for the user's choice. In a non-interactive run, report the unresolved target and provide a chat-only plan; do not fall back to the current context. If several targets are requested, finish this workflow separately for each confirmed target and keep results separate by context.

Ask in conversational groups, reusing answers already provided:

- Which context, namespace, Tenant CR name, and real Kerberos realm? Is this an empty initial Tenant or onboarding existing identities later?
- Which KDC(s), DNS/discovery settings, and krb5.conf source? Which daemon image or installed default should be used?
- Which nodes should receive daemon pods, which host cache directory do consumers use, and could another daemon manage the same UID/path there?
- What renewal and pruning policy is intended? Any hostAliases, private-registry setup, resources, pull policy, maintenance state, or duration overrides?

Explain defaults and consequences when asking. Ask follow-ups for conflicting answers or unknown prerequisites. Summarize decisions and outstanding questions before drafting. Do not ask for creation confirmation until a concrete final proposal is reviewable.

### 2. Read-only preflight

Use selected context/kubeconfig and explicit namespace throughout. Example shapes below use shell variables only after filling them with validated, safely quoted values; they are not ready-to-run commands with invented defaults.

```bash
kubectl --context "$CTX" get namespace "$NS"
kubectl --context "$CTX" api-resources --api-group=kerberator.dell.com
kubectl --context "$CTX" -n "$NS" auth can-i create tenants.kerberator.dell.com
kubectl krb list --context "$CTX" -n "$NS"
kubectl --context "$CTX" -n "$NS" get tenant "$NAME" -o yaml
kubectl --context "$CTX" -n "$NS" get serviceaccount default
```

Append selected `--kubeconfig` to every command when applicable. If authorized read access permits, inspect installed CRD schema and operator Deployment, pods, version, configured daemon image, and reconciliation scope. If cluster-wide discovery is denied, ask for the operator namespace or report the limitation; do not call the operator absent.

Check actual node labels, taints, and eligibility against the proposed selector/tolerations. Inspect existing Tenants/daemon placement for host-cache overlap where access permits. Check for existing children by the names below using metadata-only output, especially Secrets. A NotFound for the exact target namespace/name supports creation there; Forbidden/network errors do not. Do not require proving absence in unrelated namespaces, but explain same-name or same-realm installations discovered elsewhere and confirm intent if overlap matters.

For discovery without an exact namespace/name, list within the user's selected scope, using `-A` only for requested cluster-wide discovery. If that is denied, use the user-named namespace and report the limited search. Take the namespace and CR name from observed results before describing a resource. Use native `kubectl get tenants,principals` if the plugin is unavailable; continue to distinguish exact NotFound from denied or failed lookups.

If namespace/operator/CRDs/pull setup is missing, describe the dependency. Prepare any requested prerequisite action concretely and obtain separate confirmation before each write, then recheck it. No automatic Helm install, namespace creation, privilege expansion, quickstart KDC deployment, or bypass of ownership checks.

### 3. Prepare a reviewable creation

Choose CLI for supported fields or a manifest for advanced fields. Show the complete desired configuration in chat with explicit defaults and no credentials.

**Validate in both CLI and manifest mode before requesting creation approval.** A hand-written manifest must pass these same client checks; API schema validation alone does not replace them:

- Realm is nonempty and uppercase, using only ASCII `A–Z`, digits, `.`, `_`, and `-`. A realm without `.`, `_`, or `-` must be at least three characters. Do not silently uppercase it; resolve a mismatch with the user.
- Every supplied nonempty `krb5.conf` must contain the exact realm string and the literal `[realms]` section marker. Stock images need a complete configuration; omission is allowed only for a confirmed custom image that supplies its own config.
- An explicitly supplied image must be nonempty and contain `/` or `:`; confirm the effective image. In manifest mode, omitting the image may intentionally inherit the operator's configured default. Do not insert a dummy image merely to validate.
- The effective `renewMinutes` must be a positive integer; use 30 when omitted and confirm the intended pruning value. Validate all advanced manifest fields against the installed API schema as well.

Either check these explicitly, or validate the CLI-supported fields first with `add-tenant --dry-run` using the exact same realm, image, krb5.conf content, renewal interval, and pruning value as the manifest. When image inheritance or a missing plugin prevents that dry-run, perform the explicit checks. A dry-run's output covers only CLI-supported fields: retain every reviewed advanced field in the final manifest. Neither path validates Kerberos connectivity or replaces creation approval.

CLI stdout dry-run example:

```bash
kubectl krb add-tenant "$NAME" --context "$CTX" -n "$NS" \
  --realm "$REALM" --image "$IMAGE" --krb5-conf-file "$CONF_PATH" \
  --renew-minutes "$RENEW_MINUTES" --prune-stale=true --dry-run
```

The example prune value must be replaced with the user's choice. Confirm the local krb5.conf content corresponds to the reviewed proposal; do not create a config file merely to run this command. For manifest mode, render YAML in chat with `apiVersion`, `kind`, explicit name/namespace, `spec.realm`, and all agreed fields; omit `status`, UID, resourceVersion, and ownerReferences. Prefer streaming reviewed YAML to `kubectl create -f -` to avoid an unnecessary file write. If the user wants a saved manifest or krb5.conf, ask separately with the exact path and full content before writing it.

Optional server dry-run (`kubectl create --dry-run=server -f -`) can check schema/admission without persisting the Tenant. Explain that it contacts the API and invokes admission and obtain confirmation before that create-shaped request; it does not test subsequent reconciliation. Do not equate either dry-run with KDC validation.

Show the final exact creation command and full manifest/configuration, target context/API server/namespace, image and node scope, and expected controller consequences. Invite corrections; resolve them and refresh the preview. Then ask explicitly: “Create Tenant <namespace>/<name> in context <context> with this configuration and the described operator-managed resources and host-cache effects?” Wait for an affirmative response to this exact action.

### 4. Create once after confirmation

Recheck the target identity and absence read-only immediately before creation. If configuration, input-file contents, target, or prerequisites changed since review, stop and obtain fresh confirmation for the revised action.

Run the approved `kubectl krb add-tenant` command without `--dry-run`, or the approved `kubectl --context ... -n ... create -f -` with the exact reviewed manifest. Capture stdout, stderr, and exit status. Do not run both. Do not substitute `apply`, which could update an existing object.

On error, report it and inspect read-only. A timeout may mean the API accepted creation: fetch the exact target and compare the observed spec before considering another attempt. Do not automatically retry any write. AlreadyExists means inspect and stop, even if it appeared between preflight and creation. Do not delete a partially reconciled Tenant as rollback: deletion cascades owned resources and is a separate action requiring a new reviewed approval.

### 5. Verify reported state without further writes

Fetch the stored Tenant spec and compare it with the reviewed configuration. Use `describe tenant` when the plugin is installed; otherwise read conditions, `readySummary`, and related resource status with native `kubectl`. Always fetch the stored Tenant:

```bash
kubectl krb describe tenant "$NAME" --context "$CTX" -n "$NS"
kubectl --context "$CTX" -n "$NS" get tenant "$NAME" -o yaml
```

Use a bounded observation window, proposed as two minutes unless the user chose another duration; re-read roughly every 10–15 seconds while progressing and keep the user informed. `kubectl wait --for=condition=Ready` alone is insufficient. Stop polling at the deadline and report the latest evidence and unresolved checks. Do not restart or patch to make checks pass.

Quote observed output and capture errors and exit status; never infer health from the requested spec, resource names, or a successful command alone. Interpret list output as follows:

| List column / result | Meaning and reporting limit |
|---|---|
| `TENANT/UID` | Tenant row: Kerberos realm; Principal row: numeric UID. Neither is necessarily the CR name in `NAME`. |
| `READY` | `Ready` condition status: `True`, `False`, or `Unknown`; `-` means the condition is absent. |
| `NODES` | Observed ready/desired counts; Tenant uses `status.readySummary`. Check node details and placement before judging health. |
| `AGE` | Time since resource creation, not time since a successful health check. |
| `No Kerberator resources found.` | No displayed rows in this namespace/filter, even though the command exits successfully. It does not prove cluster-wide absence. The CLI groups Principals under existing Tenants and can omit dangling references; use an explicit Principal list to check them. |

Check all of these actual resources in the Tenant namespace. Use `get` to verify existence and controller ownerReference **UID** equals the new Tenant UID; the describe command's child list is only a naming convention, not an existence/ownership lookup.

| Kind | Expected name / purpose |
|---|---|
| DaemonSet | `<name>-kerberator-daemon`; inspect desired/ready/current/updated scheduling counts, template, and actual pods/nodes. |
| ConfigMap | `<name>-roster`; `users.roster`, optional `krb5.conf`, and selector/node-label JSON when applicable. |
| Secret | `<name>-keytabs`; aggregated keytabs, potentially empty for an empty Tenant. Query metadata only; never dump data. |
| Role and RoleBinding | Both `<name>-kerberator-daemon-events`; Event create/patch permission bound to namespace `default` SA. |

Check the DaemonSet selector and pod owner references before attributing pods; similar labels alone do not prove ownership. Inspect pod placement, readiness, container state, and relevant Events. Distinguish intended eligible nodes from all cluster nodes and explain any inaccessible checks.

Read [references/tenant-status.md](references/tenant-status.md) only when a `describe tenant` field, condition, or unexpected output needs interpretation. The readiness requirements below apply even when that reference is not needed.

A Tenant is **reported ready** only with the expected observed children and placement, `Ready=True`, `DaemonSetReady=True`, `RosterConsistent=True`, `KeytabsResolved=True`, and positive complete node counts. State any verification limitation. Missing/Unknown conditions mean readiness is not established. For a deliberately disabled Tenant, report its disabled state and empty-roster behavior even if those conditions are true.

An empty Tenant can satisfy all those checks without contacting the KDC: `readyz` considers a node with no eligible UIDs ready. Even with entries, the probe checks only that eligible cache files exist and are nonempty; it does not validate TGT freshness or KDC reachability. Report **“Tenant infrastructure reported ready; ticket minting unverified (no active Principals)”** when applicable. Do not create a test identity automatically.

If active Principals already refer to this Tenant, record their Ready and eligible-node summaries and any visible mint failures, node names, observation ages, or missing key-version evidence. Respect Principal nodeSelectors; do not require minting on intentionally excluded nodes. Successful minting requires Ready=True, positive complete eligible node coverage, daemon-ready nodes, and `Minted` on each eligible node. A reported KVNO describes the keytab, not a successful KDC exchange; an old observation or absent Events does not establish current success. Quote truncated text as truncated without guessing the rest. For a failure, pass this initial evidence to the relevant hand-off instead of collecting logs, classifying authentication errors, or attempting repairs here.

### 6. Handle failures and report

| Observation | Action within this creation workflow |
|---|---|
| Exact target NotFound | State absent in the queried context/namespace only. An existing CR with the same name elsewhere is a different target; report its namespace and obtain the user's choice before changing targets. |
| Empty or filtered list | Report the exact namespace/filter searched; inspect the intended target explicitly. Do not infer cluster-wide absence or a missing Tenant from an empty display alone. |
| Cross-namespace discovery denied | Absence elsewhere is unknown. Continue only in the selected namespace where permitted, and report the search limitation. |
| CRD missing | Confirm API discovery in the selected context and name the missing API resource; do not infer that no operator is deployed. Installation needs a separate proposal and confirmation. |
| Plugin missing | Report that `kubectl-krb` is unavailable; use native `kubectl` for the manifest workflow and observation if available. Installation/build needs separate confirmation. |
| Forbidden | Quote the denied verb/resource and target; resource state is unknown, not missing. Do not broaden permissions or change credentials/context. |
| API/config or unclassified failure | Quote the raw error and exit status, and state which observations remain unknown. Do not silently change target or retry a write. |
| Tenant exists but conditions absent | Report reconciliation pending/unknown and hand off to DG-01 if unresolved within the observation window. |
| Child name collision / ownership refusal | Show conflicting kind/name and owner metadata. Do not delete/adopt or patch ownerRefs. |
| Tenant not ready: incomplete nodes, `SomePodsNotReady`, `SomeKeytabsMissing`, `DuplicateUID`, or child creation failure | Capture observed conditions, counts, and affected resource names, then hand off to DG-01. Do not diagnose or repair here. |
| `MintFailed`, `KerberosMintFailed`, or an authentication/keytab failure on a Principal | Capture the Principal/UID, failing nodes, visible reasons/messages and ages, then hand off to DG-02. Do not diagnose or repair here. |
| Observation deadline reached | Report current conditions, counts, and unknown checks; hand off unresolved Tenant readiness to DG-01. Do not restart polling or retry creation automatically. |

Lead the final report with **created and reported ready**, **created but not ready**, **created with readiness unknown**, **creation request timed out; matching Tenant observed**, **already existed; no creation performed**, or **not created**, as supported by evidence. A matching read-back after a timeout does not prove that this request created the resource; state its observed readiness separately. Include context, namespace/CR name, realm, actual creation result, observed conditions/reasons, ready/desired nodes and scope, child checks, Principal/minting evidence or its absence with observation ages, and remaining limitations. Surface every failed or inaccessible check, and distinguish intended configuration from observed state. Never call the whole installation healthy based only on the create message or Ready condition. State which further actions would require fresh confirmation.

## Hand-offs

Use these destinations only when the matching case occurs; do not preload their instructions during creation. Resolve a destination as `../<skill-name>/SKILL.md` within the copied `.claude/skills` tree and check that it exists before reading it. If unavailable, name the missing skill/ID, provide the observed evidence and intended next step, and stop that branch. Do not substitute source inspection or inline diagnosis, and do not create a replacement skill automatically.

| Case | Hand off to |
|---|---|
| Tenant remains not ready or readiness cannot be established | `kerberator-diagnose-tenant` (DG-01). Pass the Tenant conditions, child/owner checks, node counts and unverified checks. |
| Principal mint/authentication failure | `kerberator-diagnose-auth-failure` (DG-02). Pass the Principal CR name/UID, Tenant reference, affected nodes, visible errors and observation ages. |
| Tenant creation completed; next step is to register identities | Offer `kerberator-register-principal` (OP-03). Pass the Tenant CR name, realm, readiness result and intended UID/principal/keytab references, without credential bytes. Do not start registration unless the user requested that follow-on work. |

Every hand-off carries the exact context/kubeconfig choice, namespace, reviewed configuration, actual creation result, latest observed state, limitations, and actions already approved/performed. The creation approval does not authorize a repair, test identity, Secret write, or Principal registration. Apply the destination's instructions within the existing scope and approval requirements. If both Tenant readiness and minting fail, route the infrastructure observations to DG-01 first and retain the auth evidence for DG-02; do not infer a common cause.
