# Tenant-not-Ready cause catalog

Sources: `operator/internal/controller/tenant_reconciler.go` (conditions),
`tenant_status.go` (pod readiness), `principal_finalizer.go` (Principal Ready
reasons), `operator/internal/tenant/daemonset.go` (DaemonSet shape),
`shared/events/reasons.go` (daemon Event reasons).

## How the operator writes Tenant status

Every reconcile rebuilds status from scratch:

1. Count Principals → `principalCount`.
2. Resolve keytabs, aggregate the roster, write the roster ConfigMap, keytab Secret, events Role/RoleBinding, DaemonSet. **Any write error returns here**, and status is saved with no conditions.
3. Read DaemonSet health → `desiredNodes`/`readyNodes`/`readySummary`.
4. Set `RosterConsistent`, `KeytabsResolved`, `DaemonSetReady`, then **always** `Ready=True Reconciled`.

So `Ready=True` means only "step 4 was reached". The real readiness signals are `DaemonSetReady`, `Nodes ready n/n`, `KeytabsResolved`, `RosterConsistent`, and the daemons' `Kerberos*` Events. The operator emits **no Events on the Tenant**; event evidence comes from the DaemonSet, its pods, and the daemon's per-UID Events on those pods.

Owned objects (named by convention): DaemonSet `<tenant>-kerberator-daemon`, ConfigMap `<tenant>-roster`, Secret `<tenant>-keytabs`, Role/RoleBinding `<tenant>-kerberator-daemon-events`. Daemon pods carry `app.kubernetes.io/name=kerberator-daemon,app.kubernetes.io/instance=<tenant>`. The operator Deployment carries `app.kubernetes.io/name=kerberator`.

## Causes

Each entry gives the signal, how to confirm it, common real causes, the fix (which the **user** runs; this skill never changes anything), and when it is *not* this cause.

### operator-down
- **Signal:** operator Deployment `readyReplicas=0`.
- **Confirm:** "operator pods" section (waiting reason); operator logs.
- **Common causes:** operator image pull failure, crash on start (bad flags, webhook cert), node pressure.
- **Fix:** repair the operator Deployment (Helm values, image, cert-manager). All Tenants are affected, not just this one.
- **Not this if:** the operator is Available. Then look at `reconcile-not-completing`.

### operator-reconcile-error
- **Signal:** an error-level operator log line that mentions `<ns>/<tenant>`.
- **Confirm:** quote the log line. Its `error` field names the failing step (`upsert roster cm`, `upsert keytab secret`, `upsert events RBAC`, `upsert daemonset`, `owner-ref …`).
- **Common causes:** the operator lacks RBAC in that namespace (`… is forbidden`); a validating/mutating webhook or PodSecurity policy rejected the DaemonSet; ResourceQuota exceeded; an object of the same name exists without our ownerRef.
- **Fix:** depends on the step; grant the operator RBAC, relax/label the namespace's PodSecurity level, or remove the conflicting object.

### reconcile-not-completing
- **Signal:** `status` has no conditions at all.
- **Confirm:** operator logs (read automatically). Three shapes:
  - the operator is down → `operator-down`;
  - the logs have errors for this Tenant → `operator-reconcile-error`;
  - the logs never mention this Tenant → the operator is not watching this namespace, or the Tenant was created seconds ago.
- **Not this if:** any condition is present.

### tenant-deleting
- **Signal:** `deletionTimestamp` set.
- **Meaning:** owned objects are being garbage-collected; Principals referencing the Tenant will lose their daemons. Not a fault to fix; if it hangs, look at finalizers on its Principals (`kerberator.dell.com/ccache-purged`).

### tenant-disabled
- **Signal:** `spec.disabled: true`; Principals show `Ready=Unknown TenantDisabled`.
- **Meaning:** intentional. The roster is empty and daemons prune every ccache. Report it as intentional, not as a failure. Re-enabling is `kubectl krb enable tenant <name>` (out of scope for this skill).

### daemonset-missing
- **Signal:** `DaemonSetReady=Unknown DaemonSetNotObserved`, or the DaemonSet does not exist.
- **Confirm:** operator logs (read automatically): an `upsert daemonset` failure usually explains it.
- **Common causes:** reconcile failing before step 3; someone deleted the DaemonSet and the operator cannot recreate it.

### no-eligible-nodes
- **Signal:** DaemonSet `desiredNumberScheduled=0` (`DaemonSetReady=False … 0/0 ready`).
- **Confirm:** "nodes vs nodeSelector" section, one row per node: `selector-match`, `unschedulable`, and `untolerated-taints` (NoSchedule/NoExecute taints the DaemonSet's tolerations do not cover). The finding says which shape it is:
  - *matches none of N node(s)*: no node carries the selector's labels (AND, exact match).
  - *N node(s) match … but none accept the daemon*: the labels are right, but every matching node is tainted or cordoned. The classic case is a selector that only the control-plane node matches (`node-role.kubernetes.io/control-plane:NoSchedule`).
- **Common causes:** typo in `spec.daemon.nodeSelector`; nodes not yet labelled; missing `spec.daemon.tolerations` for tainted nodes.
- **Fix:** label nodes (`kubectl label node <node> k=v`) or correct the selector. For tainted nodes, add a matching entry to `spec.daemon.tolerations` (e.g. `{key: node-role.kubernetes.io/control-plane, operator: Exists, effect: NoSchedule}`) with `kubectl krb edit tenant <name>`. Prefer a toleration over removing the node's taint: the taint protects that node from every other workload too.

### pod-create-failed
- **Signal:** `FailedCreate` Event on the DaemonSet.
- **Common causes:** PodSecurity admission rejects `hostPath` / privileges (the daemon needs a hostPath for `hostCachePath`); ResourceQuota/LimitRange; missing ServiceAccount.
- **Fix:** set the namespace's `pod-security.kubernetes.io/enforce` to a level that allows hostPath (typically `privileged`), or adjust the quota.

### pod-unschedulable
- **Signal:** pod `Pending`, `PodScheduled=False`, `FailedScheduling` Event.
- **Common causes:** insufficient CPU/memory for `spec.daemon.resources`; node taints; host port / volume conflicts.

### pod-image-pull
- **Signal:** `ErrImagePull` / `ImagePullBackOff` / `InvalidImageName`.
- **Confirm:** the image in the DaemonSet section vs `spec.daemon.image` (empty = the operator's `--daemon-image`).
- **Common causes:** wrong tag, private registry without `imagePullSecrets`, air-gapped node.

### pod-config-error
- **Signal:** `CreateContainerConfigError` / `CreateContainerError`.
- **Common causes:** a referenced ConfigMap/Secret key is missing (roster CM or keytab Secret not yet written).

### pod-mount-failed
- **Signal:** `ContainerCreating` plus `FailedMount` Event.
- **Common causes:** `<tenant>-roster` or `<tenant>-keytabs` missing (reconcile failing), hostPath not creatable on the node.

### pod-crash-loop
- **Signal:** `CrashLoopBackOff`, restart count, last terminated reason/exit code.
- **Confirm:** the daemon pod's previous logs: `kubectl logs <pod> -n <ns> --previous` (the user runs this; the skill reads only operator logs).
- **Common causes:** malformed `krb5.conf`, unreadable roster, permission denied on `hostCachePath`.

### pod-not-ready / pods-not-ready
- **Signal:** pod `Running` but `Ready=False` (probe failing; see `Unhealthy` Events), or a DaemonSet ready count below desired with no failing pod listed (pods terminating/replacing).
- **Usually a symptom.** The daemon's readiness probe fails until every roster UID has a ccache (`readyz: missing ccache for uid=N at /tmp/krb5cc_N`), so a mint failure makes the pod not Ready. That is why this ranks below `mint-failed`; when both appear, the mint failure is the cause. (Seen on kind after a Docker restart.)
- **Fix:** follow the mint cause when there is one. Without one, it is usually transient during a rollout; re-run after the requeue interval (default 15s).

### keytabs-unresolved, keytab-ref-missing, keytab-secret-missing, keytab-key-missing, keytab-empty
- **Signal:** `KeytabsResolved=False SomeKeytabsMissing`; the per-Principal findings name which Principal, which Secret, and which key.
- **Meaning:** that Principal is left out of the roster; other Principals still work. The Tenant DaemonSet can be fully ready while this is false.
- **Fix:** create the Secret / add the key (`kubectl create secret generic <name> --from-file=<key>=<file.keytab>`), or correct `spec.keytabSecretRef`. Rotating an existing keytab is the `kerberator-rotate-keytab` skill.
- Only key names and base64 sizes are ever read. Never print or ask for keytab contents.

### duplicate-uid
- **Signal:** `RosterConsistent=False DuplicateUID` with the UIDs.
- **Meaning:** two Principals in the namespace claim the same UID; both are rejected from the roster.
- **Fix:** delete or re-number one of them (`kerberator-inspect list -A` shows both).

### keytab-missing-on-node
- **Signal:** latest daemon Event for a (pod, uid) is `KerberosKeytabMissing`.
- **Meaning:** the roster names the UID but the keytab file is absent in the projected Secret on that node, usually just after a change, before the Secret volume refreshes (kubelet sync can take about a minute).

### mint-failed
- **Signal:** latest daemon Event for a (pod, uid) is `KerberosMintFailed`. Only the **newest** Event per (pod, uid) counts; an older failure followed by a success is history, not current state.
- **Common causes by message:**
  - `Cannot contact any KDC` / `KDC unreachable` / timeout → node-to-KDC network, DNS, or `hostAliases`; not a Kerberator bug.
  - `Resource temporarily unavailable while getting initial credentials` → MIT kinit's wording for "no KDC answered" (EAGAIN). Check the KDC named in `krb5.conf` is up; an in-cluster KDC that was restarting (e.g. after a node or Docker restart) gives this briefly. The daemon does **not** retry until its next renew tick (below).
  - `Password incorrect while getting initial credentials` / `Preauthentication failed` / `Key version … not found` → the keytab no longer matches the KDC's key (the key was changed or re-randomized on the KDC, or the KDC's database was recreated) → `kerberator-rotate-keytab`. MIT kinit says *Password incorrect* even for keytabs; it does not mean a human password. (Seen on kind: `kadmin cpw -randkey` on the KDC, or recreating the demo KDC pod, whose database is on an `emptyDir`.)
  - `Client not found in Kerberos database` → principal name/realm wrong in `spec.principal`.
  - `Cannot find KDC for realm` → `krb5Conf` missing the realm (see below).
- **A failed mint is not retried early.** The daemon mints once at start, then only every renew interval (`renewMinutes`, default 30). If the cause was transient (KDC still starting), the failure stays the current state until the next interval even though the KDC is now fine. Check: is the KDC healthy *now*, and is the failure older than the KDC's start? If so, say it will clear at the next interval, or that restarting the daemon pods (`kubectl delete pod -n <ns> -l app.kubernetes.io/name=kerberator-daemon,app.kubernetes.io/instance=<tenant>`, user runs; the DaemonSet recreates them) retries immediately. Do **not** suggest `kubectl rollout restart`: the operator owns the DaemonSet's pod template and reverts the restart annotation on its next reconcile, so usually only one pod is replaced. The script flags this as `mint-failure-stale`. (Seen on kind: daemon started 5s before the in-cluster KDC was serving.)
- **The renew timer counts node running time, not wall-clock time.** Go timers use the monotonic clock, which stops while a node or VM is suspended (laptop sleep, Docker Desktop Resource Saver). On kind, a 30m retry can therefore come much later by the wall clock. Give the next-retry time as an estimate, and prefer the daemon restart when the user wants it fixed now.
- Messages are cut at 200 characters (`…`); say so and do not guess the rest.

### kdc-not-ready, kdc-service-missing
- **Signal:** `krb5.conf`'s first `kdc =` is an in-cluster Service (`<svc>.<ns>.svc…`, or `<svc>.<ns>` / `<svc>` when that Service exists) and the Service has **no ready endpoints**, or does not exist. A **cause** when mints are failing, else a **warn**.
- **Confirm:** the "KDC … -> Service" section (pods, Ready, restarts, start time).
- **Fix:** bring the KDC Deployment back (its own logs: `kubectl logs -n <ns> deploy/<kdc>`), or correct the `kdc =` address in `spec.daemon.krb5Conf`.

### mint-failure-stale (warn)
- **Signal:** mints failed, but the in-cluster KDC is serving now and (re)started **after** the latest failure.
- **Meaning:** the failure most likely happened while the KDC was down and has not been retried yet (see the mint-failed notes on retries and suspended VMs). It is not a live outage. Restarting the daemon retries at once.

### kdc-external (info)
- The KDC is outside the cluster (or not a Service the script can find), so its health was not checked. If mints fail with an unreachable-KDC error, the user can test from a node (`nc -vz <kdc-host> 88`).

### krb5conf-empty
- **Signal:** `spec.daemon.krb5Conf` empty. A **cause** when mints are failing; a **warn** otherwise (fine if the daemon image ships its own `/etc/krb5.conf`).

### realm-mismatch, realm-not-in-krb5conf (warn)
- `default_realm` in `krb5Conf` differs from `spec.realm`, or `spec.realm` is not mentioned anywhere in it. Principals are in `spec.realm` (enforced by the webhook), so kinit may look in the wrong realm.

### config-not-synced (warn)
- The roster ConfigMap's `krb5.conf` or the DaemonSet image differs from the Tenant spec, so the operator has not applied the latest spec. Operator logs are read automatically. Usually a reconcile error.

### rollout-in-progress (warn)
- DaemonSet `updatedNumberScheduled < desiredNumberScheduled`. Transient during a rollout; stuck if it persists with failing pods.

### daemon-node-subset (info)
- `spec.daemon.nodeSelector` limits the daemon to some nodes **on purpose**. The DaemonSet's desired count, and the Tenant's `n/n`, cover only the matching nodes, so `2/2` on a 3-node cluster is healthy. Do not report the unmatched nodes as a fault. Mention it only if the user expected tickets on one of them; that is a node-targeting question (labels / selector), not a readiness failure.

### principal-no-eligible-nodes (warn)
- **Signal:** a Principal reports `Ready=False NoEligibleNodes`: its own `spec.nodeSelector` matches no node, so it is minted **nowhere**, even though the Tenant's daemons are fine.
- **Meaning:** a configuration mistake, not a daemon failure. Usual causes: a typo in the key or value (`security_zone` vs `security-zone`), a label that was never applied or has since been removed, or a selector that needs labels on two different nodes (selectors AND their terms).
- **Confirm:** `kubectl krb node-labels` shows the labels that exist; compare them with the selector in the finding.
- **Fix (user runs):** label the intended nodes, or correct the selector with `kubectl krb edit principal <name>`.

### principal-status-stale (info)
- **Signal:** a Principal reports `Ready=False SomeDaemonsNotReady` with empty per-node reasons, but every daemon pod is Ready and the newest Event on each of those nodes is a successful mint, more than 5 minutes old. `spec.daemon.renewMinutes` and `spec.eventStalenessThreshold` are both unset.
- **Meaning:** a known operator issue, not a fault. With `renewMinutes` unset, the operator treats a mint Event as current for only 5 minutes, but the daemon renews every 30 minutes, so the status goes "not ready" between renewals while the tickets are fine. See [dell/kerberator#22](https://github.com/dell/kerberator/pull/22).
- **Report:** say the Principal is minting and the status is stale. Do not report it as broken.
- **Fix (user runs):** set `spec.daemon.renewMinutes: 30` (or `eventStalenessThreshold: 1h`) on the Tenant. To refresh the status now, delete the daemon pods (`kubectl delete pod -n <ns> -l app.kubernetes.io/name=kerberator-daemon,app.kubernetes.io/instance=<tenant>`).

### principal-not-ready (warn)
- A child Principal reports `Ready=False`/`Unknown` with a reason other than `Disabled`/`TenantDisabled`. Use `kerberator-inspect describe principal` for per-node detail.

### unknown \<area\>
- The caller could not read that evidence (RBAC). The state of that area is **unknown, not healthy**; name the denied resource.

### unexplained
- The Tenant is not fully ready, but no rule matched. Report the raw evidence and say no cause was isolated.
