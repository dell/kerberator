# Operator findings and fault domains (DG-08)

Read the section for the `FAULT_DOMAIN` the script printed. Each `FINDING`
code is listed with what it means, how to confirm it from the evidence
sections, and the next step (for the user or a hand-off; this skill changes
nothing).

## controller-runtime: the controller process isn't running or ready

- `operator-crash-loop`: the manager container exits and Kubernetes backs off.
  Confirm with the restart count, last exit reason/code, and the **previous**
  container's error lines (the script reads `--previous` for restarted pods).
  Common causes: bad flags or `extraArgs`, CRDs missing at start ("no matches
  for kind"), webhook certificate files missing, out of memory (`OOMKilled`).
  Quote the line; don't guess past it.
- `operator-probe-failing`: running but `/readyz` fails. Look at `Unhealthy`
  events and the newest log lines.
- `operator-config-error`: `CreateContainerConfigError` and similar. Usually a
  missing ConfigMap/Secret referenced by the pod (e.g. the webhook certificate
  Secret before cert-manager issued it).
- `operator-not-ready` / `operator-scaled-to-zero`: the Deployment has fewer
  ready replicas than desired, or replicas=0 (someone scaled it down).
- `operator-restarted` (warn): ready now, but it restarted before. Quote the
  last exit reason; if restarts keep climbing it is heading for a crash loop.
- `rollout-stuck` (warn): the Deployment's `Progressing` condition hit
  `ProgressDeadlineExceeded`: a new ReplicaSet never became ready. The pod
  findings say why.
- Next step: the user or cluster admin fixes the cause the evidence shows; a
  reinstall or upgrade is OP-11.

## controller-image-distribution: the image can't be pulled

- `operator-image-pull`: `ErrImagePull` / `ImagePullBackOff` / `InvalidImageName`.
  Quote the message and the configured image (`image:` in the Deployment
  section). Common causes: a tag that doesn't exist, a private registry without
  `imagePullSecrets`, an air-gapped node.
- `previous-pod-still-serving` (warn): the new pod can't start, but the old
  ReplicaSet's pod is still ready, so the operator keeps running its old
  version. Say so: it is a blocked rollout, not an outage.
- Never print pull-secret contents. Next step: check the tag exists and the
  registry is reachable from the nodes (cluster admin).

## controller-scheduling: the pod can't be placed on a node

- `operator-unschedulable`: `PodScheduled=False` and `FailedScheduling`
  events. The event message names the blocker (insufficient CPU/memory, taints,
  node selector or affinity). The operator process has not started, so there
  are no logs; don't infer a crash.
- Next step: a human reviews the chart's `nodeSelector`, `tolerations`,
  `affinity` and `resources`, or node capacity. Never cordon, label or taint.

## reconciliation-or-configuration: the controller is up but reconciles fail

- `reconcile-errors`: error log lines in the last 15 minutes, grouped by
  controller and object. The controller process is healthy; specific
  resources fail. Read the `error` field:
  - `... is forbidden`: the operator's ServiceAccount lacks RBAC in that
    namespace (often `installRBAC=false`, or a namespace it can't write).
  - admission or PodSecurity denials: the namespace rejects the DaemonSet
    (hostPath) or the Role.
  - `already exists` / ownerReference errors: an object of the same name exists
    without the Tenant as owner.
- The affected Tenant's own diagnosis is DG-01; quote the object and error so
  the user can hand it over.
- Errors older than 15 minutes, only in the previous container, or about a
  Tenant/Principal that no longer exists (`errors-for-deleted-object`) are
  history; they are listed but not the verdict.

## operator-installation / operator-installation-crds

- `operator-not-found`: no Deployment labelled `app.kubernetes.io/name=kerberator`.
  Either it isn't installed, or it was installed under another name/labels
  (re-run with `-n` and `--deployment`). Don't conclude "not installed" from a
  label search alone if the user says it is.
- `crd-missing`: `tenants.kerberator.dell.com` or
  `principals.kerberator.dell.com` doesn't exist (chart `installCRD=false`, or
  removed). Without it the API rejects that resource kind entirely.
- `crd-not-established`: the CRD exists but the API server hasn't accepted it.
- `crd-version-mismatch`: the CRD doesn't serve the version this release
  expects (chart 0.10.0: `v1alpha1`, served and storage). Show served/storage
  versions; recommend reviewing the upgrade path (OP-11). Never replace or
  delete CRDs: that can delete every Tenant and Principal.
- `multiple-operators`: two controllers reconciling the same CRDs will fight.

## version-evidence

- `cli-version-local-only` (always printed): `kubectl krb version` is the
  plugin on the user's machine.
- `version-difference`: the plugin and the operator (running version, or the
  image tag if the startup line wasn't in the logs read) differ. Not proof of
  breakage; compatibility is unverified. Point to the release notes.
- `dev-build`: a `dev` version string is a local build, not a release.

## Webhook findings (consequences or configuration)

- `webhook-no-endpoints`: the webhook Service has no ready pods behind it.
  Usually a consequence of the operator not being ready; Principal create and
  update fail or hang meanwhile.
- `webhook-ca-missing`: the ValidatingWebhookConfiguration has no caBundle
  (cert-manager not injecting, or a manual certificate not set).
- `webhook-config-missing`: the operator serves a webhook but no configuration
  points at it.
- `webhook-tls-errors`: certificate/TLS errors in the operator logs.

## insufficient-rbac and unknown findings

- `operator-access`, `operator-deployment`, `operator-pods`, `operator-events`,
  `operator-logs`, `crd-access`: that evidence couldn't be read (permissions,
  or another API error quoted in the finding). The operator's
  state there is **unknown**, not healthy. Name the denied resource and the
  minimum access that would settle it (get/list deployments, pods, events and
  pods/log in the operator namespace; get CRDs cluster-wide). Never try other
  kubeconfigs or contexts to get around it.
- `partial-evidence` (info): the verdict is healthy (or compatibility-unverified)
  but some evidence above was unreadable. Report the verdict as limited to what
  was read, and list the unread parts.

## tenant-daemon-out-of-scope

- `tenant-daemons-not-ready`: a Tenant's DaemonSet isn't ready. That is the
  per-Tenant daemon, not the operator. If the operator itself is healthy, say
  so, and hand the Tenant to `kerberator-diagnose-tenant` (DG-01). Don't run
  any Tenant repair.
