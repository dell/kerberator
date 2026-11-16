---
name: kerberator-install-upgrade
description: Safely inspect, install, upgrade, and verify the Kerberator operator, CRDs, and kubectl-krb CLI. Use when asked to install Kerberator or upgrade an existing installation.
---

# Install or Upgrade Kerberator (OP-11)

Install or upgrade the operator only after discovering the current cluster state and receiving explicit approval for the exact mutating commands. Report observed facts separately from assumptions. This workflow does not create or modify Tenants, Principals, their Secrets, or application resources.

## Safety rules

- Start with read-only inspection. Confirm the intended Kubernetes context before contacting a cluster. If several contexts exist and the user has not identified one, show the context names and ask which to use. Do not silently choose the current context.
- Never run install, upgrade, apply, or other cluster-mutating commands without explicit user approval of the exact commands and target. First display the commands and their effects. The request to install or upgrade is not itself approval of a proposed command.
- Identify the existing installation method and preserve it. Do not switch between Helm and `install.yaml` without the user's explicit request and a reviewed migration plan.
- Never uninstall or delete resources as troubleshooting. Never read or print Secret data, credentials, kubeconfig contents, or sensitive Helm values. Do not expose them in reports.
- Do not change application resources or Principals as part of operator installation or upgrade. Never run `git commit` or `git push`.
- Do not claim an install/upgrade succeeded from command exit status alone. Verify resources, rollout, CRDs, and versions.

## Repository-verified facts

- The documented prerequisites are Kubernetes 1.24+ and cert-manager for the default admission webhook. The chart can disable its webhook (`webhook.enabled=false`); its values also describe supplying a user-managed webhook certificate and CA. A KDC and per-user keytabs are product prerequisites for managing identities, not operator installation prerequisites.
- The documented OCI chart is `oci://ghcr.io/dell/charts/kerberator`; the README currently shows `--version 0.10.0`. The in-repository chart is `operator/charts/kerberator` and currently declares chart `version: 0.10.0`, `appVersion: "0.10.0"`.
- The documented plain-manifest path is the release asset `https://github.com/dell/kerberator/releases/download/v0.10.0/install.yaml`. The release workflow renders that file from the chart. The checked-in `make manifests` target writes `dist/install.yaml`.
- Chart `installCRD` defaults to `true`; its template includes the `Tenant` and `Principal` CRDs from `operator/charts/kerberator/crd/`. With that value set false, the chart omits them and expects CRDs to be managed out of band.
- Kerberator is alpha. The changelog says the `v1alpha1` API may change between minor versions without a conversion webhook. The v0.10.0 `User` to `Principal` API rename requires recreating affected CRs from source manifests. Never assume arbitrary releases are API-compatible; inspect the relevant release notes and plan any required data migration explicitly.
- `kubectl krb version` reports the local CLI build version; it does not query the cluster. The chart sets `app.kubernetes.io/version` to its appVersion and renders the operator image tag as `v<appVersion>` unless overridden. A live Deployment's configured image tag is evidence of its intended image version, not proof that the image is running successfully. The operator binary supports `--version`, but the repository does not document a routine command that invokes it inside a deployed pod.
- Repository sources do not define a general upgrade policy, supported CLI/operator skew range, release compatibility matrix, or install.yaml-specific upgrade guide. State this limit; do not invent guarantees. Use explicit target versions and inspect the target release's notes before proposing an upgrade.

## 1. Read-only pre-install or pre-upgrade inspection

Use the user's selected kubeconfig/context. The following commands inspect local context configuration or query cluster state; they do not mutate it:

```bash
kubectl config get-contexts
kubectl config current-context
kubectl --context <context> version
helm --kube-context <context> list -A
kubectl --context <context> get deployments,pods -A -l app.kubernetes.io/name=kerberator -o wide
kubectl --context <context> get crd tenants.kerberator.dell.com principals.kerberator.dell.com
kubectl --context <context> get crd tenants.kerberator.dell.com principals.kerberator.dell.com -o jsonpath='{range .items[*]}{.metadata.name}{" served="}{range .spec.versions[*]}{.name}{"="}{.served}{" storage="}{.storage}{" "}{end}{"\n"}{end}'
kubectl --context <context> krb version
```

If the intended context is clear and only one exists, it may be used directly. `helm list -A` identifies Helm releases. Use the Kerberator label selector across namespaces and the named CRDs for targeted discovery; once the namespace is known, inspect only the matching Deployment and pods there. Do not enumerate all deployments and pods across the cluster. A Deployment absent from `helm list -A` is not, by itself, evidence that it came from `install.yaml`.

```bash
kubectl --context <context> -n <operator-namespace> get deployment <deployment-name> -o jsonpath='{.metadata.namespace}{"/"}{.metadata.name}{" image="}{.spec.template.spec.containers[?(@.name=="manager")].image}{" chart-version="}{.metadata.labels.app\.kubernetes\.io/version}{"\n"}'
kubectl --context <context> -n <operator-namespace> get deployment <deployment-name> -o wide
kubectl --context <context> -n <operator-namespace> get pods -l app.kubernetes.io/name=kerberator -o wide
kubectl --context <context> -n <operator-namespace> rollout status deployment/<deployment-name> --timeout=60s
```

If JSONPath escaping or a selector does not work with the installed kubectl, use a non-secret `kubectl get deployment ... -o yaml` only after confirming it contains no sensitive annotations/configuration, or query individual fields. Do not dump all namespace resources indiscriminately.

For a suspected Helm release, inspect its namespace, chart/version, and status:

```bash
helm --kube-context <context> -n <namespace> status <release>
helm --kube-context <context> -n <namespace> list
```

Do not print `helm get values` output: user-supplied values can contain sensitive material. Helm upgrades that omit `--reuse-values` and omit `-f` do not preserve existing custom values; Helm computes values from the chart defaults and values supplied to that upgrade. Before choosing `--reuse-values`, review the effective existing configuration without displaying or exposing sensitive values, then check every retained value against the target chart's values and schema. `--reuse-values` may retain deprecated or incompatible values from the previous chart. If retained values cannot be checked safely, prefer an explicitly reviewed values file or stop and explain the uncertainty. Never assume defaults match the installed release.

Classify the installation consistently. Report **Helm-managed (verified)** only when a release is present and its metadata/ownership links it to the Kerberator resources. Report **manifest-managed (verified)** only when resource metadata or other reliable deployment records identify manifest/GitOps ownership and there is no matching Helm release. A missing Helm release alone does not prove `install.yaml` was used. Otherwise report **unknown installation method**, stating what evidence is missing or inconclusive. Record context, namespace, release/deployment names, configured image/tag, chart label when present, CRDs and served/storage versions, rollout/pod state, CLI version, Kubernetes version, and uncertainty. Check for name collisions, existing CRDs, RBAC/webhook resources, and required cert-manager/certificates before proposing a change. Do not assume the cluster is empty.

## 2. Install

Installation is a cluster mutation. First present the exact command(s), chosen context, namespace, release name, chart/manifest version, expected cluster-scoped CRDs/RBAC/webhook effects, and any relevant collisions. Obtain an explicit yes for those commands before running them.

### Helm OCI chart

The README's documented command is:

```bash
helm install kerberator oci://ghcr.io/dell/charts/kerberator --version 0.10.0 --namespace kerberator-system --create-namespace
```

When a non-default context is selected, include the supported Helm global option before the subcommand:

```bash
helm --kube-context <context> install kerberator oci://ghcr.io/dell/charts/kerberator --version <target-chart-version> --namespace <namespace> --create-namespace
```

Use the exact target requested by the user or verified from documented release information; `0.10.0` is the version in the checked-in docs, not an automatically current target. Default chart behavior enables the validating webhook and cert-manager integration and installs the CRDs. If cert-manager is absent, resolve the webhook certificate prerequisite before installing; do not silently disable validation. Local chart install and image override syntax are documented in `operator/README.md`:

```bash
helm install kerberator ./operator/charts/kerberator --namespace <namespace> --create-namespace --set image.repository=ghcr.io/dell/kerberator-operator --set image.tag=v<version>
```

### Release install.yaml

For the documented v0.10.0 artifact:

```bash
kubectl --context <context> apply -f https://github.com/dell/kerberator/releases/download/v0.10.0/install.yaml
```

For another version, use only that version's verified release asset URL. This method applies rendered Kubernetes manifests (including the rendered operator resources and, by default, CRDs) and does not create a Helm release. Repository docs show this as the plain-manifest alternative; the Helm chart offers release/value management and local chart overrides. Do not apply this over a Helm-managed installation.

After the approved installation command, verify using the read-only checks in **Version and health verification** below. A successful `helm install` or `kubectl apply` response alone is insufficient.

## 3. Upgrade

Do not propose or run an upgrade until the following are established:

1. Context, namespace, current operator deployment/image, CLI version, CRD state, and installation method.
2. The user's explicit target version or a target verified from documented release information. Do not infer "latest" from the checked-in README.
3. The target release notes/changelog and any API or CRD changes. Compare current and target chart values using safe, non-secret inspection. Identify custom values, image overrides, and out-of-band CRD ownership; do not overwrite them with defaults by assumption.
4. Compatibility and migration impact. The repository warns that alpha API changes between minor versions may require resource recreation and has no conversion webhook. If the notes require migration/recreation, stop and present a separate, explicit data migration plan. Never delete CRs automatically.

Then show the exact proposed commands and explain whether they update the operator Deployment, CRDs, webhook/RBAC resources, and namespace. Obtain explicit user approval before execution.

### Existing Helm release

Preserve the same release name, namespace, chart source, and reviewed values. Helm does not carry forward custom values by default when an upgrade omits values inputs. After reviewing existing configuration without printing potentially sensitive values, select one strategy: `--reuse-values` to retain existing release values while applying the target chart, or `-f <reviewed-values-file>` to supply an explicitly reviewed values file. `--reuse-values` may retain deprecated or incompatible values from the previous chart. Before proposing it, check retained values against the target chart's values and schema. If that cannot be verified safely, prefer an explicitly reviewed values file or stop and explain the uncertainty. The repository's `make install` target uses `helm upgrade --install` and explicit `--set` overrides; this workflow deliberately does not use `--install` for an upgrade. The command below selects `--reuse-values` only when those checks are complete:

```bash
helm --kube-context <context> upgrade <release> oci://ghcr.io/dell/charts/kerberator --version <target-chart-version> --namespace <namespace> --reuse-values --wait
```

If choosing a reviewed values file instead, replace `--reuse-values` in the proposed command with `-f <reviewed-values-file>`. If the current deployment uses a custom chart source, preserve that source explicitly. Do not use `--install` to conceal a missing release during an upgrade; if the release is absent, reclassify as install only after inspection and approval. The exact displayed command must contain the selected values strategy.

### Existing install.yaml deployment

Keep using the verified target release's install manifest:

```bash
kubectl --context <context> apply -f https://github.com/dell/kerberator/releases/download/v<target-version>/install.yaml
```

Review the target manifest and cluster-specific namespace/image settings before approval. The repo documents `kubectl apply` for install, but no separate install.yaml upgrade guide; applying a new rendered release manifest is an inferred upgrade mechanism, so label it as such and validate its resource names/scope before execution. Never switch this deployment to Helm silently.

The checked-in chart renders CRDs as regular chart template resources when `installCRD: true`; an upgrade will therefore render/apply those CRD definitions along with the chart. The install.yaml release is also rendered from the chart. Verify the target CRD schemas before approval and verify served/storage versions afterward. If CRDs are managed out of band (`installCRD: false`) or by another controller, coordinate with that owner; do not take ownership or apply CRDs unexpectedly.

After the approved upgrade, run the read-only verification workflow. Report a failed rollout, unavailable pods, missing/outdated CRDs, image pull errors, or CLI/operator version difference as unresolved; do not claim success or automatically roll back/retry.

## 4. Version and health verification (read-only)

Run the CLI check locally:

```bash
kubectl krb version
```

The implementation prints `kubectl-krb <version.String()>`; for an uninstrumented build the version component is `dev` (the full string may also include revision, Go version, and OS/architecture). This does not contact or inspect the cluster.

Inspect the live operator Deployment and pods, using the names found in preflight:

```bash
kubectl --context <context> -n <operator-namespace> get deployment <deployment-name> -o jsonpath='{.spec.template.spec.containers[?(@.name=="manager")].image}{"\n"}'
kubectl --context <context> -n <operator-namespace> rollout status deployment/<deployment-name> --timeout=60s
kubectl --context <context> -n <operator-namespace> get pods -l app.kubernetes.io/name=kerberator -o wide
kubectl --context <context> -n <operator-namespace> get pods -l app.kubernetes.io/name=kerberator -o jsonpath='{range .items[*]}{.metadata.name}{" phase="}{.status.phase}{" ready="}{range .status.containerStatuses[*]}{.name}{"="}{.ready}{" image="}{.image}{" imageID="}{.imageID}{" "}{end}{"\n"}{end}'
kubectl --context <context> get crd tenants.kerberator.dell.com principals.kerberator.dell.com
kubectl --context <context> get crd tenants.kerberator.dell.com principals.kerberator.dell.com -o jsonpath='{range .items[*]}{.metadata.name}{" served="}{range .spec.versions[*]}{.name}{"="}{.served}{" storage="}{.storage}{" "}{end}{"\n"}{end}'
```

Keep these version facts distinct: `kubectl krb version` reports the local CLI build version; the Deployment's configured manager image reference/tag expresses the intended image; `imageID` reports the digest pulled by the container; and the operator's startup version log is runtime version evidence when safely available. A digest does not establish a semantic version. The chart label `app.kubernetes.io/version` is chart appVersion, not proof of the running binary's version. If a safe startup version cannot be obtained, state that the actual runtime version cannot be verified. Do not expose unrelated logs or sensitive configuration. The repository provides no `kubectl krb` subcommand that queries the operator's version.

Check CLI/operator compatibility against the target release notes. The repository defines no supported skew range, so matching strings do not establish compatibility or health, and differing strings alone do not prove a fault if no policy is documented. Report both observed versions and the missing compatibility guarantee. If different, recommend aligning the CLI with the operator release after verifying the correct release binary, but do not replace binaries or mutate the cluster without approval.

Healthy operator installation evidence requires: expected operator Deployment exists; rollout completes; desired pods are Ready; configured image/tag and available runtime version evidence are consistent with the intended release (if runtime version is unavailable, say so); and both Kerberator CRDs exist with expected served/storage versions. Report local CLI availability and CLI version separately as client-side checks; CLI availability is not a requirement for a healthy operator installation. These checks verify control-plane installation, not that any Tenant/Principal workloads or application integrations are healthy. Never print or inspect Secret data.

## 5. Troubleshooting

Use read-only inspection first. Do not delete, uninstall, restart, or reapply resources as an automatic troubleshooting action.

| Symptom | Read-only checks and response |
|---|---|
| Operator pods not Ready | `kubectl ... get pods -o wide` and `kubectl ... describe pod <pod>`; inspect events and readiness state without exposing Secret contents. Report the actual scheduling, probe, or mount error. |
| CRDs missing/outdated | `kubectl get crd tenants.kerberator.dell.com principals.kerberator.dell.com` and inspect served/storage versions. Check context and `installCRD` ownership. Do not apply CRDs until source/method/approval are clear. |
| Helm release failed | `helm ... status <release>` and `helm ... list`; inspect Deployment/pod events and cert-manager readiness. Do not uninstall or retry an upgrade blindly. |
| Image pull failure | Inspect pod events and the Deployment's configured image/tag and `imagePullSecrets` references without reading Secret contents. Check registry reachability/access and configured mirror. Never print credentials. |
| CLI/operator version mismatch | Record `kubectl krb version`, live Deployment image/tag, chart label, and (if safe) startup version. Consult release notes; repository does not define a skew policy. Recommend aligning compatible release artifacts, subject to approval. |
| Upgrade rollout failure | Capture `rollout status`, Deployment conditions, pod readiness and events, image IDs, and CRD versions. Identify which checks failed. Do not retry, roll back, or uninstall without an approved plan. |

## 6. Safe testing and final report

The repository documents `make quickstart` as a kind-based end-to-end exercise; it creates a kind cluster, installs cert-manager and Kerberator, and creates demo KDC/Principal resources. It is mutating and must only be run with explicit authorization in a dedicated disposable local kind environment. `make quickstart-clean` deletes that cluster; never run it unless explicitly authorized and the cluster identity is confirmed. Repository test coverage also includes chart lint/render and envtest, but those do not prove a real installation/upgrade succeeds.

For this workflow, a read-only verification run is the preferred safe check: run the commands in **1** and **4**, then report actual output. Do not claim that kind, Helm, envtest, or any test ran unless it actually did.

Summarize:

- Installation method (verified Helm-managed, verified manifest-managed, or unknown, with supporting evidence) and release/deployment name.
- Context and namespace.
- `kubectl krb version` output and distinct operator version evidence (configured image tag, chart label, running image digest, and runtime version if obtained; state explicitly if runtime version cannot be verified).
- CRD presence and served/storage versions.
- Rollout status and pod readiness/image pull state.
- Verified facts, assumptions, and unresolved issues, including compatibility information absent from repository sources.
