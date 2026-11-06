---
name: create-tenant
description: Guide configuration and creation of a Kerberator Tenant, collect the user's cluster, realm, Kerberos configuration, and daemon requirements, preview the exact creation, obtain confirmation before every non-read-only action, then verify reported Tenant readiness and dependent resources. Use when asked to create, configure, or onboard a new Tenant; use kerberator-inspect for read-only status requests about existing resources.
---

# create-tenant

Guide the user from configuration choices to one new Tenant and an evidence-based report of its observed state. Follow the structure and reporting discipline of `../kerberator-inspect/SKILL.md`, but use this skill's explicit creation workflow and approval gates. Do not execute the inspect skill's read-only restrictions as instructions for this creation workflow.

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

The checked-in CRD is `kerberator.dell.com/v1alpha1`, kind `Tenant`, namespaced, plural `tenants`, aliases `krbtenant`/`kt`. Schema requires `spec` and nonempty `spec.realm`; Kubernetes also validates metadata. `daemon` and its image are not schema-required. Select a short Kubernetes-compatible CR name that also fits the generated child names and label values; the `-kerberator-daemon-events` suffix must fit applicable name limits.

| Field | What to establish with the user |
|---|---|
| `metadata.name`, `metadata.namespace` | Exact new CR name and namespace; keep realm separate. |
| `spec.realm` | Actual KDC realm. CLI validation accepts uppercase ASCII letters, digits, `.`, `_`, `-`, with an additional short-name check; it is a heuristic, not KDC validation. Do not silently uppercase a user-supplied realm. |
| `spec.daemon.image` | CLI requires `--image`; its heuristic requires `/` or `:`. A manifest can omit it to use the operator's `--daemon-image`, then the build-version-based fallback. Confirm the selected image and compatibility. |
| `spec.daemon.krb5Conf` | Full intended `/etc/krb5.conf`: default realm, realm/KDC stanza, DNS discovery choices, and any domain mappings or site policy. Stock image needs this in practice. Omission is permitted for a confirmed custom image with its own config. CLI only checks that nonempty content mentions the realm and `[realms]`; it does not parse Kerberos syntax or test connectivity. |
| `spec.daemon.renewMinutes` | Positive integer when set; CLI default and operator rendering default are **30 minutes**. Choose with ticket lifetime, renewal policy, and KDC load in mind. |
| `spec.daemon.pruneStale` | CLI defaults **true**; omitted manifest field is false. Explicitly choose, preferably true for cleanup. It removes caches previously managed by this daemon when UIDs leave the roster; disabling it can leave credentials after disable/delete and force Principal purge timeouts. Do not describe pruning as deleting every foreign cache. |
| `spec.daemon.hostCachePath` | Default `/tmp`; host directory mounted at the same path, with `krb5cc_<uid>` files for consumers. Confirm consumer expectations and overlapping daemons/UIDs across Tenants sharing nodes and paths; namespaces do not isolate host files. |
| `spec.daemon.nodeSelector`, `tolerations` | Intended daemon node scope and relevant taints. Empty selector does not mean only worker nodes; actual eligibility depends on scheduling constraints. Verify expected nodes, not just a count. Distinct from Principal `spec.nodeSelector`, which filters credential placement within the Tenant's daemon nodes. |
| `spec.daemon.hostAliases` | Optional `[{ip: ..., hostnames: [...]}]` for pod hostname overrides when DNS is insufficient; ask for actual mappings. |
| `spec.daemon.imagePullPolicy` | `Always`, `IfNotPresent`, or `Never`; renderer defaults to `IfNotPresent`. |
| `spec.daemon.resources` | Default requests `50m` CPU/`64Mi` memory; limits `200m`/`256Mi` only when neither requests nor limits is supplied. Ask about expected roster size and site requirements; partial settings do not get the full default set. |
| `spec.deletionTimeout` | Principal purge-finalizer timeout, default 60s for nil/zero; e.g. `"60s"`. It is not a Tenant creation timeout or guarantee of credential removal. |
| `spec.eventStalenessThreshold` | Per-UID Event validity duration, nil/zero defaults to `max(5m, 2 × effective renewMinutes)`. Older events fall back to pod-readiness evidence; this does not prove mint success. |
| `spec.disabled` | Default false. True empties the roster while daemon pods remain; cache cleanup depends on pruning. Confirm any intentional initially disabled Tenant and report that limitation. |

**Custom cache-path limitation:** the current DaemonSet passes `--caches=<hostCachePath>` to the daemon, but its `readyz` probe runs without that argument and defaults `READYZ_CACHES` to `/tmp`; the builder does not set that environment variable. A non-default path can therefore misreport readiness. Explain this source-observed limitation before choosing such a path; do not promise support or silently patch the generated DaemonSet.

## Usage and CLI behavior

Read the locally available plugin's `kubectl krb add-tenant --help` and `kubectl krb describe --help` before use. If only the repository binary is available, `./kubectl-krb ... --help` is read-only. Do not install/build it without confirmation. Compare local help with source when versions differ.

The inspected implementation supports:

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
- Despite “Scaffold + apply” help text, `AddTenant` uses a straight API **Create**, not apply/upsert. AlreadyExists is an error. It does not create the namespace, KDC, source keytabs, or Principals; it does not wait for readiness.
- CLI cannot set placement, resources, hostAliases, hostCachePath, pull policy, duration fields, or disabled state. Use a fully reviewed Tenant manifest and `kubectl create`, rather than create-then-patch or silently omitting requested fields. A manifest also supports intentionally inheriting the operator image.
- Always specify `-n`: this CLI's namespace resolver falls back to the raw kubeconfig current context's namespace when `-n` is omitted, even when another `--context` selects the API connection.
- Some README examples claim an optional CLI image or a 720-minute default; checked source/help require an image and use 30. A comment in CLI validation mentions a Tenant webhook, but the actual installed webhook rules target Principals. Prefer executable help, CRD, and implementation over those descriptions.

## Workflow

### 1. Choose target and gather requirements

Read this skill's ingredients first. Explain the CR-name/realm distinction and that Tenant creation launches a per-node daemon. Use read-only `kubectl config get-contexts` and a credentials-free API-server/context listing if target selection is needed. Do not dump `kubectl config view --raw`.

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

If namespace/operator/CRDs/pull setup is missing, describe the dependency. Prepare any requested prerequisite action concretely and obtain separate confirmation before each write, then recheck it. No automatic Helm install, namespace creation, privilege expansion, quickstart KDC deployment, or bypass of ownership checks.

### 3. Prepare a reviewable creation

Choose CLI for supported fields or a manifest for advanced fields. Show the complete desired configuration in chat with explicit defaults and no credentials. Use the CLI's stdout dry-run when appropriate:

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

Fetch the stored Tenant spec and compare it with the reviewed configuration. Run:

```bash
kubectl krb describe tenant "$NAME" --context "$CTX" -n "$NS"
kubectl --context "$CTX" -n "$NS" get tenant "$NAME" -o yaml
```

Use a bounded observation window, proposed as two minutes unless the user chose another duration; re-read roughly every 10–15 seconds while progressing and keep the user informed. `kubectl wait --for=condition=Ready` alone is insufficient. Stop polling at the deadline and report the latest evidence and unresolved checks. Do not restart or patch to make checks pass.

Check all of these actual resources in the Tenant namespace. Use `get` to verify existence and controller ownerReference **UID** equals the new Tenant UID; the describe command's child list is only a naming convention, not an existence/ownership lookup.

| Kind | Expected name / purpose |
|---|---|
| DaemonSet | `<name>-kerberator-daemon`; inspect desired/ready/current/updated scheduling counts, template, and actual pods/nodes. |
| ConfigMap | `<name>-roster`; `users.roster`, optional `krb5.conf`, and selector/node-label JSON when applicable. |
| Secret | `<name>-keytabs`; aggregated keytabs, potentially empty for an empty Tenant. Query metadata only; never dump data. |
| Role and RoleBinding | Both `<name>-kerberator-daemon-events`; Event create/patch permission bound to namespace `default` SA. |

Check the DaemonSet selector and pod owner references before attributing pods; similar labels alone do not prove ownership. Inspect pod placement, readiness, container state, and relevant Events. Distinguish intended eligible nodes from all cluster nodes and explain any inaccessible checks.

Interpret `describe tenant` exactly:

| Observed field / condition | Meaning and required interpretation |
|---|---|
| `Nodes ready` | Tenant `status.readySummary`, built from DaemonSet `numberReady/desiredNumberScheduled`. Require equal counts with **desired > 0**, plus placement matching the user's intent. `0/0` is not a ready installation. |
| `Ready=True Reconciled` | Reconciliation reached its end. This is set independently of the other conditions and is not an overall health guarantee. |
| `DaemonSetReady=True AllPodsReady` | DaemonSet observed, desired > 0, ready equals desired. `False SomePodsNotReady` includes partial readiness and 0/0; `Unknown DaemonSetNotObserved` means not observed. |
| `RosterConsistent=True AllUIDsUnique` | No duplicate eligible UIDs in this Tenant's aggregation. `False DuplicateUID` rejects colliding entries. This does not establish cross-Tenant host-cache isolation. |
| `KeytabsResolved=True AllKeytabsResolved` | Eligible source references resolved. `False SomeKeytabsMissing` means at least one could not resolve. True is not proof of valid keytab content or a successful KDC exchange. |
| `Principals` | `status.principalCount`, normally all referring Principal CRs including disabled/deleting ones, not the active roster size. |
| `Roster hash` | Digest of aggregated roster content, not proof that every daemon has consumed it or that krb5.conf reached every running pod. |
| Principals table | UID, Kerberos principal, keytab Secret/key, observed KVNO, READY, ready/desired nodes. A missing table does not necessarily prove no Principals: this CLI ignores Principal-list errors. Verify with a separate explicit Principal list and report failures. |

A Tenant is **reported ready** only with the expected observed children and placement, `Ready=True`, `DaemonSetReady=True`, `RosterConsistent=True`, `KeytabsResolved=True`, and positive complete node counts. State any verification limitation. Missing/Unknown conditions mean readiness is not established. For a deliberately disabled Tenant, report its disabled state and empty-roster behavior even if those conditions are true.

An empty Tenant can satisfy all those checks without contacting the KDC: `readyz` considers a node with no eligible UIDs ready. Even with entries, the probe checks only that eligible cache files exist and are nonempty; it does not validate TGT freshness or KDC reachability. Report **“Tenant infrastructure reported ready; ticket minting unverified (no active Principals)”** when applicable. Do not create a test identity automatically.

For existing active Principals, use `kubectl krb describe principal <CR-name>` on each relevant identity with the same explicit target. Report Ready, eligible node counts, observed KVNO and its age, and every per-node DAEMON READY / REASON / OBSERVED / MESSAGE. Strong mint evidence is Ready=True, positive complete eligible node coverage, daemon-ready nodes and `Minted` on each eligible node; absence or pod-readiness fallback is weaker evidence. Respect Principal nodeSelectors; do not require Minted on intentionally excluded nodes. Do not treat an old observation as stale solely by a fixed age: compare renewal policy, other observations, daemon readiness, and Event staleness settings. Events may be unordered or garbage-collected; compare timestamps/ages. Messages may be truncated; quote what is visible and use read-only logs/full Events for more evidence when permitted.

### 6. Handle failures and report

| Observation | Read-only follow-up; do not automatically mutate |
|---|---|
| CRD/plugin missing | Report missing dependency and relevant install docs; installation needs a separate proposal and confirmation. |
| Forbidden or API/config failure | Quote the denied resource/verb or error; state unknown status and scope. Never infer absence or change credentials/context. |
| Tenant exists but conditions absent | Inspect controller availability, namespace scope, logs and Events; report reconciliation pending/unknown. |
| Child name collision / ownership refusal | Show conflicting kind/name and owner metadata. Do not delete/adopt or patch ownerRefs. |
| `SomePodsNotReady`, Pending, image pull or mount error | Inspect pod Events, node constraints, namespace policy, image access, mounts, and resource fit. |
| `SomeKeytabsMissing` / `DuplicateUID` | Inspect source reference metadata and referring Principal specs; identify exact conflicts without exposing Secret bytes. |
| `MintFailed`, `KeytabMissing`, missing KVNO | Inspect Principal status, full Events/logs; distinguish KDC/DNS/time/config/keytab failures from rollout issues. No automatic rotate, kinit, KDC change, or pod restart. |
| Observation deadline reached | Report current counts/conditions and blockers; ask about next diagnostic or a separately reviewed corrective action. |

Lead the final report with **created and reported ready**, **created but not ready**, **created with readiness unknown**, **already existed; no creation performed**, or **not created**, as supported by evidence. Include context, namespace/CR name, realm, actual creation result, observed conditions/reasons, ready/desired nodes and scope, child checks, Principal/minting evidence or its absence, and remaining limitations. Never call the whole installation healthy based only on the create message or Ready condition. State which further actions would require fresh confirmation.

## Repository references and maintenance

Resolve these paths from the repository root. When behavior differs from this skill, inspect the installed CRD, local CLI help/version, and matching source; disclose discrepancies and revise the proposal before execution.

- `.claude/skills/kerberator-inspect/SKILL.md`, `scripts/inspect.sh`, `references/status-fields.md`: template for target selection, observed reporting, and error distinctions. The helper permits read-only list/describe only; it is not a creation helper. Some older status prose quotes daemon standalone defaults; Tenant rendering uses 30 minutes.
- `cli/kubectl-krb/internal/add_tenant.go`, `add_tenant_test.go`: flags, heuristics, dry-run serialization, straight Create and AlreadyExists handling.
- `cli/kubectl-krb/internal/client.go`, `describe.go`, `list.go`: targeting, actual describe output and its limitations.
- `operator/charts/kerberator/crd/tenants.yaml`, `operator/api/v1alpha1/tenant_types.go`: API schema, complete spec/status fields.
- `operator/internal/controller/tenant_reconciler.go`, `tenant_helpers.go`, `tenant_status.go` and related tests: aggregation, child ownership guard, Event RBAC, keytab resolution, rollup conditions.
- `operator/internal/tenant/daemonset.go`, `daemonset_test.go`, `aggregate.go`, `aggregate_test.go`: effective image/resources/interval/path defaults, security/probes, eligible roster and duplicate handling.
- `daemon/cmd/kerberator-daemon/readyz.go`, `daemon/pkg/watcher/`, `daemon/README.md`: probe limits, mint/prune loop and node filtering.
- `operator/internal/webhook/principal_validation.go`, `operator/charts/kerberator/templates/webhook.yaml`: Principal admission dependencies (verify chart filename in the checkout if renamed).
- `README.md`, `operator/README.md`, `docs/usage.md`, `docs/architecture.md`, `docs/security-model.md`, `docs/private-registry.md`, `docs/keytabs.md`, `examples/quickstart/tenant.yaml`, `examples/quickstart/principal.yaml`: installation, security boundaries, source keytabs, registry behavior, and examples. Quickstart KDC addresses are development examples, never production defaults.
