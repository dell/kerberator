---
name: kerberator-update-tenant-principal
description: Change an existing Kerberator Tenant or Principal spec with kubectl patch, showing a before/after diff and getting approval first, then verify the operator reconciled. Use when the user wants to edit, update, change, or fix a Tenant or Principal setting, such as the realm, the KDC address in krb5Conf, the keytab Secret reference, the renew interval, hostCachePath, or a nodeSelector. Do NOT use for disabling or enabling (OP-05), or for replacing a keytab's contents (OP-04).
---

# Update a Kerberator Tenant or Principal (OP-09)

A **Tenant** describes one Kerberos realm and the daemon that serves it. A
**Principal** maps one Linux UID to a Kerberos identity
(`svc-<uid>@<REALM>`). This skill changes the spec of one that already
exists, using `kubectl patch`. It does not use `kubectl krb edit`, because
an agent cannot drive `$EDITOR`.

**How to read the evidence in this file.** Anything unmarked was observed
on a one-node kind cluster. Anything marked **(code)** comes from reading
the source or docs and has not been observed on a cluster, so report it as
expected behavior, not as fact.

`kubectl patch` skips the warnings `krb edit` prints after saving **(code)**,
so step 2 repeats those checks by hand.

## Inputs to collect

| Input | Example | Notes |
|---|---|---|
| Kind | `tenant` or `principal` | Ask if the request is unclear. |
| CR name | `demo`, `uid-2001` | If the user gives a UID or a Kerberos name, find the CR with `kubectl krb list`: CLI-created ones are `uid-<uid>`, YAML-created ones are often `svc-<uid>`. Never pass the Kerberos name `svc-2001@...`. |
| Field and new value | `spec.daemon.renewMinutes: 60` | Ask for the value. Do not invent one. |
| Namespace | `-n team-a` | Only if not the kubeconfig's default namespace. |

If any input is missing or ambiguous, ask before running anything.

## What each field needs

The Evidence column says where each entry comes from: **Observed**,
**Code**, or **Both**. In a Both row, only the sentences marked **(code)**
are unobserved.

**Tenant**

| Field | Risk | Evidence | What happens |
|---|---|---|---|
| `spec.daemon.renewMinutes` | Low | Observed | Rolls the daemon pods: the args changed and the pod was replaced. The schema refuses -5 and a quoted value. While unset, the daemon ran with `--renew-minutes=30`. |
| `spec.deletionTimeout`, `spec.eventStalenessThreshold` | Low | Code | Used only by the operator, so no pod restart **(code)**. |
| `spec.daemon.krb5Conf` (KDC address) | High | Observed | **Running pods keep the old file until restarted.** See "krb5Conf needs a restart". |
| `spec.daemon.hostCachePath` | High | Code | The daemon reads it only at pod start, and the operator rolls the pods because the args change **(code)**. Consumers such as `rpc.gssd` look in `/tmp`, so tickets may stop being found **(code)**. Ccaches at the old path are not expected to be cleaned up **(code)**. |
| `spec.daemon.pruneStale` | High | Code | Rolls the pods **(code)**. Turning it off means disabled Principals keep their on-node tickets **(code)**. |
| `spec.daemon.imagePullPolicy` | Medium | Both | The schema refuses values other than `Always`, `IfNotPresent`, and `Never`. Rolls the pods **(code)**. |
| `spec.daemon.image`, `resources`, `nodeSelector`, `tolerations`, `hostAliases` | Medium | Code | Roll the pods **(code)**. A bad image or selector can leave nodes with no working daemon **(code)**. |
| `spec.realm` | High | Both | The dry-run accepted a different realm, because the webhook only covers Principals. See "Changing a realm". |

**Principal**

| Field | Risk | Evidence | What happens |
|---|---|---|---|
| `spec.nodeSelector` | Medium | Code | No pod restart, and the docs say it takes effect in 30-90s **(code)**. Tickets are pruned from nodes that stop matching **(code)**. To remove a key, patch it to `null` **(code)**. |
| `spec.keytabSecretRef` | High | Both | The dry-run accepts a Secret that does not exist, and the schema refuses an empty `key`. At reconcile, an unresolved reference drops the Principal from the roster with `KeytabSecretMissing` **(code)**. If `key` stays the same, the roster is unchanged, so daemons may keep the old key until they restart or renew **(code)**. |
| `spec.principal` | High | Both | The webhook refuses a realm that differs from the Tenant's, and a name with no realm. The keytab must also hold the new name **(code)**. |
| `spec.uid` | High | Both | The webhook refuses 0. It also refuses negative UIDs **(code)**. If two Principals claim one UID, the operator rejects both with `DuplicateUID` **(code)**, so check `kubectl krb list` first. |
| `spec.keytab` | Do not patch | Code | Derived from `keytabSecretRef.key`, and normally left empty **(code)**. |

**Refuse and redirect**

| Field | Evidence | Why | Instead |
|---|---|---|---|
| `spec.disabled`, `spec.disabledReason` | Code | Has its own workflow | Principal: OP-05. Tenant: out of scope. |
| Principal `spec.tenantRef` | Both | The dry-run accepted a Tenant that does not exist, so nothing here protects you. In-place moves are not supported **(code)**. | Delete and recreate the Principal, per `docs/usage.md` **(code)**. Out of scope here. |
| Keytab contents | Code | The reference does not change | OP-04. |

## Workflow

**1. Check the current state (read-only).**
```bash
kubectl krb list -n <ns>
kubectl -n <ns> get <tenant|principal> <name> -o yaml | sed -n '/^spec:/,$p'
```
Confirm the target exists, and record the current value for the rollback.
An absent field is a value too: its rollback is `null`, not the default.
If it already has the requested value, say so and stop.

If READY is not True, report it and ask whether to continue, because a
later failure would be hard to attribute. Check "READY can be misleading"
first: READY False has been seen on a working Principal.

**2. Validate the new value (read-only).** Always run these checks
yourself. This skill does not depend on any other skill: do not look for,
wait for, or require a validate-configuration skill (OP-08). If the user
asks you to run one as well, it is an extra check, and it never replaces
this list. If any check fails, report it and stop.

- The target exists (step 1).
- The new value's format: integer for `renewMinutes`; uppercase with dots
  for a realm (`EXAMPLE.COM`); `name@REALM` for `spec.principal`.
- For `krb5Conf`: the new text contains `[realms]` and the Tenant's realm.
  Neither is enforced on a patch **(code)**. The step 3 diff must show only
  the line you meant to change. Save the current value first, so the
  rollback restores it exactly. Write it to a new temporary directory,
  never into the repo or the current folder:
  ```bash
  D=$(mktemp -d) && echo "$D"
  kubectl -n <ns> get tenant <name> -o jsonpath='{.spec.daemon.krb5Conf}' > "$D/krb5conf.orig"
  jq -Rs '{spec:{daemon:{krb5Conf:.}}}' < "$D/krb5conf.orig" > "$D/rollback.json"
  ```
  Record the printed path. Shell variables may not survive between
  commands, so later commands use that literal path. The rollback patch is
  `-p "$(cat <that-dir>/rollback.json)"`, from the same directory. Put it
  in the approval in step 4.
- For `spec.principal`: compare its realm suffix with the Tenant's
  `spec.realm` yourself. Do not rely only on the dry-run (see step 3).
- For a referenced Secret, check that it exists **by name only**:
  ```bash
  kubectl -n <ns> get secret <secret-name> -o name
  ```
  This cannot confirm the `key` exists, and you must not read the Secret to
  find out. Check `KeytabsResolved` in step 7 instead.
- The webhook dry-run, in step 3.

**3. Dry-run and build the diff (no change is persisted).**
```bash
diff -u \
  <(kubectl -n <ns> get <kind> <name> -o yaml | sed -n '/^spec:/,/^status:/p') \
  <(kubectl -n <ns> patch <kind> <name> --type=merge -p '<patch>' --dry-run=server -o yaml | sed -n '/^spec:/,/^status:/p')
```
Examples of `<patch>`:

- Set a value: `{"spec":{"daemon":{"renewMinutes":60}}}`
- Remove a field: `{"spec":{"daemon":{"renewMinutes":null}}}`
- Secret reference (names only, never contents):
  `{"spec":{"keytabSecretRef":{"name":"<secret>","key":"<key>"}}}`

`krb5Conf` is one multi-line string, so build the patch from the current
value, change one line, and let `jq` handle the escaping:
```bash
P=$(kubectl -n <ns> get tenant <name> -o jsonpath='{.spec.daemon.krb5Conf}' \
  | sed 's/<old-kdc>/<new-kdc>/' | jq -Rs '{spec:{daemon:{krb5Conf:.}}}')
```
Then pass `-p "$P"`.

A refusal here is the answer: report the message and stop. Do not try to
get around it.

A passing dry-run is limited:

- It accepted a Secret that does not exist, so it does not prove the
  reference works. It also cannot show that a value works against the KDC
  **(code)**.
- Tenants have no webhook, only the CRD schema.
- For Principals, the UPDATE webhook has `failurePolicy: Ignore`, so if it
  is down a bad patch passes with no error **(code)**. When you can, confirm
  it has a ready backend. This is the default install; find others with
  `kubectl get validatingwebhookconfigurations`:
  ```bash
  kubectl -n kerberator-system get endpointslices -l kubernetes.io/service-name=kerberator-webhook
  ```

**4. Ask for approval.** Show all of these:

- The exact `kubectl patch` command.
- The diff from step 3.
- The effect, from "What each field needs". For a Tenant field that rolls
  pods, say so: **every Principal of that Tenant re-mints**.
- Any follow-up command, such as the restart for `krb5Conf`.
- The rollback patch from the value recorded in step 1, and its restart if
  the field needs one.

Wait for an explicit yes. The original request is not approval.

**5. Apply (mutating).** Run exactly the approved command, once, without
`--dry-run`. Note the time; step 7 needs it.

**6. Wait for the operator to reconcile.** For a change that rolls the
pods, first confirm the DaemonSet has the new args, then wait for the
rollout. Tenant `demo` has the DaemonSet `demo-kerberator-daemon`.
```bash
kubectl -n <ns> get ds <tenant>-kerberator-daemon -o jsonpath='{.spec.template.spec.containers[0].args}'
kubectl -n <ns> rollout status ds/<tenant>-kerberator-daemon --timeout=120s
```
Observed on one node, in all six rollouts (two patches, four restarts): the
new pod minted 2-4s after the command, and `rollout status` returned after
12-14s. Expect longer with more nodes **(code)**.

For a Principal `nodeSelector`, nothing rolls, so allow the 30-90s from the
docs before verifying **(code)**.

**7. Verify (read-only).** Run this even if step 6 looked fine:
```bash
kubectl krb list -n <ns>
kubectl -n <ns> get <kind> <name> -o yaml | sed -n '/^spec:/,$p'
kubectl -n <ns> get principal <name> -o jsonpath='{range .status.nodes[*]}{.name} {.reason} {.lastObserved}{"\n"}{end}'
kubectl -n <ns> get events --field-selector reason=KerberosMintFailed --sort-by=.lastTimestamp
kubectl -n <ns> logs ds/<tenant>-kerberator-daemon --tail=10
```
All of these must hold:

- The spec shows the new value.
- READY is True and NODES is N/N for the Tenant and its Principals.
- The Tenant's `KeytabsResolved` condition is True.
- If the pods rolled: each node shows `Minted` with `lastObserved` after the
  patch time, the daemon log shows `SUCCESS: Minted ticket cache`, and no
  `KerberosMintFailed` event is newer than the patch. Old failures are
  history, not a result of this change.

With more than one node, `logs ds/...` shows only one pod **(code)**, so
check each pod's log.

## READY can be misleading

`kubectl krb list` READY is a status field, not a live test. Observed on a
Tenant with `renewMinutes` unset:

- **READY False on a working Principal.** svc-2001 showed `False`, NODES
  `0/1`, reason `SomeDaemonsNotReady`, message
  `0/1 eligible daemon pods ready`, and an empty node `reason`. At the same
  time `daemonPodReady` was true, the Tenant showed 1/1, the pod had been
  Ready with 0 restarts, and the daemon log showed a successful renewal 5m
  40s before the status changed. No patch touched the Principal: restarting
  the daemon returned it to True and 1/1.
- **READY True with an old mint.** `Minted` stayed at the same timestamp for
  over 2 hours, and for 9 minutes in a later watch.
- **`lastObserved` can move with no mint.** It changed while the node
  `reason` was empty, so trust it only next to `reason: Minted`.
- **Tenant conditions' `lastTransitionTime` is re-stamped** with no change
  in status, so it does not show when something changed.

The likely cause is that the status counts a node as ready only while a
`Minted` event is recent, and with `renewMinutes` unset that window is 5
minutes against a 30-minute renewal **(code)**. Status also changes only when
the operator reconciles **(code)**. This cause is not proven: the False state
appeared once, and did not return during a 9-minute watch after a restart.

So when READY is False with `daemonPodReady: true`, read the daemon log
before calling it a failure. Report the disagreement. Do not restart or
patch to make the status look right without the user's approval.

## krb5Conf needs a restart

Observed in two tests on a running Tenant on a one-node kind cluster.

**Test 1: added a comment line to `krb5Conf`.**

| Check | Result |
|---|---|
| The ConfigMap, after the patch | Updated. `<tenant>-roster` key `krb5.conf` had the comment. |
| Same pod, 189s after the patch | **`/etc/krb5.conf` still had the old text.** Pod not replaced, DaemonSet generation unchanged. |
| Control: the same key via the non-`subPath` mount, `/etc/kerberator-daemon/roster/krb5.conf` | Had the comment, so the kubelet had synced. |
| READY during that time | The Tenant stayed True, 1/1, so READY does not reveal this. |
| After `kubectl rollout restart` | The new pod's `/etc/krb5.conf` had the comment. The pod minted 2s after the restart. |

**Test 2: changed the KDC address, then reverted it.** The `kdc =` line
went from `kdc.kerberator-demo.svc.cluster.local:88` to the ClusterIP of
the same KDC Service, `<cluster-ip>:88`. The dry-run diff showed only that
line.

| Check | Result |
|---|---|
| The ConfigMap, after the patch | Had the new address. |
| Same pod, 103s after the patch | **`/etc/krb5.conf` still had the old address.** Pod not replaced, DaemonSet generation unchanged (89), daemon 1/1. |
| READY during that time | Tenant and svc-2001 stayed True, 1/1. |
| After `kubectl rollout restart` | New pod in 12s. Its `/etc/krb5.conf` had the new address, and the log showed `SUCCESS: Minted ticket cache svc-2001@EXAMPLE.COM` 2s after the restart. svc-2001 node `Minted` with `lastObserved` after the restart. No `KerberosMintFailed` newer than the patch. |
| Rollback: patch with the saved original, then restart | `spec.daemon.krb5Conf` matched the saved file byte for byte. The new pod had the original address and minted 3s after the restart. Tenant conditions `RosterConsistent`, `KeytabsResolved`, `DaemonSetReady`, `Ready` all True. svc-2001 Ready True, 1/1. |
| DaemonSet generation | 89, then 91 after the first restart, 93 after the second: up by 2 per restart, none for the patches. |

The file is mounted with `subPath`, and the pod template has no hash of the
config, so a patch alone never reaches a running daemon. **After any
`krb5Conf` patch, including a rollback, a restart is required.** Include it
in the approval in step 4, then run it and continue from step 6:
```bash
kubectl -n <ns> rollout restart ds/<tenant>-kerberator-daemon
```
Then confirm the pod has the new file. This is the only `kubectl exec` this
skill allows:
```bash
kubectl -n <ns> get pods -l app.kubernetes.io/name=kerberator-daemon,app.kubernetes.io/instance=<tenant>
kubectl -n <ns> exec <daemon-pod> -- cat /etc/krb5.conf
```
Step 6 counts four restarts: the one in test 1, the two in test 2, and
the one in "READY can be misleading". The test 1 restart was watched for
63 seconds and replaced the pod exactly once. In the two test 2 restarts,
`rollout status` reported one new pod each. After the test 1 restart and
both test 2 restarts, the operator removed the `restartedAt` annotation
from the DaemonSet and the generation went up by 2. The fourth restart was
not checked for this. So do not expect to find the annotation, and do not
treat its absence as a failed restart.

Test 2 pointed at the same KDC by another address, so it shows that a new
`kdc =` line reaches the daemon and is used after a restart. Moving to a
different KDC server was not tested: whether it works depends on that KDC
holding the same keys **(code)**. A `Minted` result after the restart, newer
than the patch, is the evidence to look for. With an unreachable KDC, expect
`KerberosMintFailed` with `Cannot contact any KDC` after the restart, while
the old pod would have kept working until replaced **(code)**.

## Changing a realm

This is a migration, not a one-field edit. Only the first fact below was
observed, and the sequence has not been run on a cluster.

The dry-run accepted a Tenant with a different realm. Every Principal would
keep its old `@REALM` suffix, and later updates to it would be refused until
`spec.principal` is fixed **(code)**. Principals cannot go first either,
because the webhook compares them with the Tenant's current realm: a patch
to a realm other than the Tenant's was refused.

Before asking for approval, list every Principal of the Tenant, and confirm
with the user that keytabs for the new realm already exist as Secrets. Then
approve the whole sequence at once **(code)**:

1. Patch the Tenant's `spec.realm` and `spec.daemon.krb5Conf` in one patch.
2. Patch each Principal's `spec.principal`, and `spec.keytabSecretRef` if
   needed, in one patch per Principal.
3. Restart the DaemonSet, as for `krb5Conf`, then verify every Principal.

Principals are expected to fail to mint until the sequence finishes
**(code)**, so say so in the approval.

## Reporting the outcome

End with a short report in two parts, and keep them separate:

- **Confirmed:** only what the CLI printed, such as "`renewMinutes` is 60,
  daemon pod replaced, svc-2001 Ready True, NODES 1/1, Minted at
  21:48:59Z".
- **Not confirmed / next steps:** anything that failed, was skipped, or is
  a hypothesis, including any **(code)** behavior you relied on. Label
  hypotheses as such. Include the rollback patch.

## When something fails

| Symptom | Source | Meaning | What to do |
|---|---|---|---|
| `tenants.kerberator.dell.com "<name>" not found` | Observed | Wrong name or namespace | Run `kubectl krb list` and ask which one was meant. |
| `secrets "<name>" not found` | Observed | The referenced Secret is not there | Stop. Creating it is the user's job, and a patch now is expected to drop the Principal from the roster **(code)**. |
| `denied the request: spec.principal ... principal tenant "<X>" does not match the referenced Tenant` | Observed | The realm differs from the Tenant's | Report it and stop. If a realm move is the goal, see "Changing a realm". |
| `denied the request: spec.principal ... must have the shape <name>@<TENANT>` | Observed | Malformed name | Ask for the full `name@REALM`. |
| `denied the request: spec.uid ... must be a positive integer` | Observed | UID is invalid | Ask for a valid UID. |
| `spec.daemon.renewMinutes in body should be greater than or equal to 1` or `must be of type integer` | Observed | Rejected by the CRD schema | Ask for a positive integer, unquoted. |
| `spec.daemon.imagePullPolicy: Unsupported value` | Observed | Rejected by the CRD schema | Use `Always`, `IfNotPresent`, or `Never`. |
| `spec.keytabSecretRef.key in body should be at least 1 chars long` | Observed | Empty key | Ask for the key name. Do not read the Secret to find it. |
| `patched (no change)` | Observed | Already has that value | Report it and stop. |
| READY False, `SomeDaemonsNotReady`, with `daemonPodReady: true` | Observed | May be a status problem, not a ticket failure | See "READY can be misleading". Check the daemon log and report what it shows. |
| Everything is Ready but the old KDC is still in use | Observed (old file in the pod) | `krb5Conf` was patched with no restart | See "krb5Conf needs a restart". |
| `Forbidden` with no webhook named | **(code)** | Account lacks permission | Stop; name the missing permission. Do not try other commands to get around it. |
| `rollout status` times out | **(code)** | New pods are not becoming ready | Do not re-patch. Run `kubectl -n <ns> get pods -o wide` and `kubectl krb describe principal <name>`, report them, and offer the rollback. |
| `KeytabsResolved` False, `KeytabSecretMissing` | **(code)** | The Secret or key did not resolve, and the Principal left the roster | Report it and offer the rollback. |
| `KerberosMintFailed` newer than the patch | **(code)** | KDC or key problem | Report the nodes and messages, and offer the rollback. Diagnosing is DG-06 or DG-03. Do not patch again to experiment. |

To roll back, repeat steps 3 to 7 with the rollback patch, including
approval. It is a new change, so do not run it on your own. A rollback of a
field that rolls pods re-mints too, as it did for `renewMinutes`.

## Never

- Use `kubectl krb edit`, `kubectl edit`, `kubectl apply`, or `kubectl
  replace` for these changes.
- Apply a patch without the diff and the user's approval of that exact
  command.
- Touch, patch, delete, disable, or bypass the admission webhook, or wait
  for it to be down so a patch gets through.
- Patch the DaemonSet, roster ConfigMap, or aggregated keytab Secret. The
  operator owns them and overwrites them **(code)**.
- Put a Secret's contents in any command, or read Secrets (`kubectl get
  secret ... -o yaml/json`, `describe secret`, `base64 -d`, `klist -K`).
  `kubectl get secret <name> -o name` is the only allowed Secret read.
- Use `kubectl exec` for anything except `cat /etc/krb5.conf`. In particular,
  never read the mounted keytabs or ticket caches.
- Print, `cat`, copy, or summarize keytab files or the kubeconfig.
- Run `git add`, `git commit`, or `git push`.

## Out of scope

Creating Tenants or Principals (OP-03), replacing keytab contents (OP-04),
disabling or enabling (OP-05), moving a Principal to another Tenant,
generating keytabs on the KDC, and diagnosing why a Principal is not Ready
(DG-03, DG-06). Say so and stop when a request needs one of these.
