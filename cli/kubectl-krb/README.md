# kubectl-krb

A `kubectl` plugin for Kerberator. Turns the "kubectl get tenants /
kubectl get principals / kubectl describe / kubectl get events
--field-selector..." dance into 14 subcommands that
answer the questions SREs actually ask, plus an interactive `tui`.

## Install

```bash
# From source (from the repo root):
go build -o kubectl-krb ./cli/kubectl-krb/cmd/kubectl-krb
install kubectl-krb ~/.local/bin/          # or /usr/local/bin

# Or straight from the module proxy:
go install github.com/dell/kerberator/cli/kubectl-krb/cmd/kubectl-krb@latest
```

A krew manifest is planned. Until then download a release binary or
use `go install` as above.

`kubectl` auto-discovers any executable named `kubectl-<X>` on `$PATH`,
so `kubectl krb ...` is what you type. All the standard kubectl-style
flags work (`--kubeconfig`, `--context`, `-n / --namespace`, `-A`).

## Subcommands

| Command | Purpose |
|---|---|
| `list` | Every Tenant with its Principals indented beneath |
| `describe tenant\|principal <name>` | Deep-dive on one CR, joined with Events |
| `add-tenant <name>` | Scaffold + apply a Tenant |
| `add-principal <tenant> <uid>` | Create the keytab Secret + Principal in one shot |
| `rotate-keytab <principal>` | Replace keytab bytes, roll the DaemonSet, wait for re-mint |
| `kvno [principal]` | KVNO + enctypes the operator observed in each keytab Secret |
| `edit tenant\|principal <name>` | `kubectl edit` with a post-edit sanity check |
| `disable` / `enable tenant\|principal <name>` | Soft-revoke without deleting the CR |
| `node-labels` | Discover Node labels for `spec.nodeSelector` |
| `drain-node <node>` | Cordon the node and delete its kerberator-daemon pod (`--uncordon` reverses) |
| `events` | Filtered view of the daemon's `Kerberos*` Events |
| `tui` | Interactive terminal UI for all of the above |
| `version` | Print the plugin version |

### `kubectl krb list`

One table, every Tenant, every Principal indented under its parent.
Faster than `kubectl get tenants -A` followed by
`kubectl get principals -A`.

```
$ kubectl krb list -A
NAMESPACE        KIND         NAME      TENANT/UID    READY  NODES  AGE
kerberator-demo  Tenant       demo      EXAMPLE.COM   True   2/3    7m
kerberator-demo    Principal  uid-2001  2001          True   3/3    7m
kerberator-demo    Principal  uid-2002  2002          True   3/3    7m
```

Flags: `--tenant <name>` to focus, `-A` for all namespaces.

### `kubectl krb describe tenant|principal <name>`

Deep-dive on one CR. For Tenants, enumerates the five owned children
(DaemonSet, ConfigMap, Secret, Role, RoleBinding) and lists every
Principal with its KVNO and per-node status. For Principals, joins
`.status.nodes[]` with recent Events filtered on the
`kerberator.dell.com/uid` annotation so you see mint failures with
full context in one place.

```
$ kubectl krb describe principal uid-2001 -n kerberator-demo
Principal CR:      kerberator-demo/uid-2001
  UID:          2001
  Principal:    svc-2001@EXAMPLE.COM
  Tenant CR ref: demo
  Keytab:       svc-2001-keytab/svc-2001.keytab
  KVNO:         9   (observed 23s)
  Enctypes:     aes128-cts-hmac-sha1-96, aes256-cts-hmac-sha1-96
  Age:          7m
  Finalizers:   [kerberator.dell.com/ccache-purged]

Per-node status:
  NODE      DAEMON READY  REASON      OBSERVED  MESSAGE
  worker-1  true          Minted      12s       Minted ccache for uid=2001 principal=...
  worker-3  true          MintFailed  8s        kinit failed for uid=2001: KDC unreachable

Recent daemon Events for this UID:
  [8s] KerberosMintFailed on demo-kerberator-daemon-9qnh9: kinit failed for uid=2001: KDC unreachable
```

`Tenant CR ref` is the name of the parent Tenant CR
(`tenantRef.name`), not the Kerberos realm. The realm lives on
the Tenant (`spec.realm`) and shows up in `describe tenant`.

### `kubectl krb add-tenant <name>`

Scaffold + apply a Tenant CR. Runs client-side validation (realm
string shape, image ref shape, `krb5.conf` mentions the realm and has
a `[realms]` section) so common typos surface before the admission
webhook rejects the round-trip.

```
$ kubectl krb add-tenant demo -n kerberator-demo \
    --realm EXAMPLE.COM \
    --krb5-conf-file ./krb5.conf
tenant.kerberator.dell.com/demo created
```

Flags: `--realm` (required; the Kerberos realm string), `--image`
(optional; overrides the operator's default daemon image),
`--renew-minutes` (default 720), `--krb5-conf-file` OR `--krb5-conf`
(inline), `--dry-run`.

### `kubectl krb add-principal <tenant> <uid> --keytab-file <path>`

One-shot Principal registration. Reads the keytab bytes, creates the
keytab Secret, applies the Principal CR. The principal defaults to
`svc-<uid>@<Tenant.spec.realm>`; override with `--principal`.

```
$ kubectl krb add-principal demo 2003 --keytab-file ~/keytabs/svc-2003.keytab -n kerberator-demo
secret/uid-2003-keytab created
principal.kerberator.dell.com/uid-2003 created
```

Use `--dry-run` to see the manifests it would apply. Pipe a keytab via
`--keytab-file -` for stdin input.

Per-node targeting: add `--node-selector key=value` (repeatable, AND
semantics) to restrict a Principal's on-node ccache to workers
matching Kubernetes Node labels. Empty means every node the Tenant
serves.

```
$ kubectl label node worker-1 security-zone=prod-a
$ kubectl krb add-principal demo 2001 --keytab-file ./svc-2001.keytab \
      --node-selector security-zone=prod-a
```

Within roughly 30-90s the daemon on `worker-1` mints `krb5cc_2001`;
every other daemon skips it and (if `pruneStale=true` on the Tenant)
removes any pre-existing `krb5cc_2001`. Fail-closed semantics: if a
daemon cannot resolve its own node's labels, restricted Principals are
dropped rather than defaulted in. To restrict an existing Principal,
use `kubectl krb edit principal` and set `spec.nodeSelector`.

### `kubectl krb rotate-keytab <principal> --from-file <path>`

Replaces a Principal's keytab Secret data and forces every daemon
across the Tenant to re-mint. Does the three things you have to do in
exactly the right order to avoid the "why is my ccache still on the
old key for 30 minutes" foot-gun:

1. Update the keytab Secret in place (preserves ownerRefs + labels).
2. `rollout restart` the Tenant's DaemonSet so kubelet spins new
   daemon pods that pick up the new mounted keytab bytes.
3. Watch `Principal.status.nodes[]` until every node reports `Minted`
   with a `lastObserved` after the rollout timestamp.

```
$ kubectl krb rotate-keytab uid-2001 --from-file ./fresh.keytab -n kerberator-demo
==> rotating principal kerberator-demo/uid-2001 (secret=uid-2001-keytab key=uid-2001.keytab tenant=demo)
    secret/uid-2001-keytab updated (409 bytes at key uid-2001.keytab)
    daemonset/demo-kerberator-daemon rollout restarted
==> waiting up to 2m0s for every node to re-mint
    all 3 nodes re-minted successfully
```

Flags: `--from-file` (required, `-` for stdin), `--wait` (default true),
`--timeout` (default 2m).

### `kubectl krb kvno [principal]`

Shows the KVNO and enctypes the operator observed in each Principal's
referenced keytab Secret on the last successful reconcile. Use this to
detect drift between the KDC and the Kubernetes Secret, a common
failure mode after a keytab was regenerated at the KDC without a
matching `kubectl krb rotate-keytab`.

```
$ kubectl krb -n kerberator-demo kvno
NAME      UID   PRINCIPAL             KVNO  OBSERVED  ENCTYPES
uid-2001  2001  svc-2001@EXAMPLE.COM  9     23s       aes128-cts-hmac-sha1-96, ...
uid-2002  2002  svc-2002@EXAMPLE.COM  9     23s       aes128-cts-hmac-sha1-96, ...
uid-2003  2003  svc-2003@EXAMPLE.COM  1     23s       aes128-cts-hmac-sha1-96, ...

$ kubectl krb -n kerberator-demo kvno uid-2001
Principal: kerberator-demo/uid-2001
UID:       2001
Principal: svc-2001@EXAMPLE.COM
Secret:    kerberator-demo/svc-2001-keytab  (key: svc-2001.keytab)

KVNO:      9
Enctypes:  aes128-cts-hmac-sha1-96, aes128-cts-hmac-sha256-128, ...
Observed:  23s ago
```

With one argument, also scans recent `MintFailed` events for the UID
and, if any are found, suggests `kubectl krb rotate-keytab`.

No CLI-side Secret access is needed: the operator parses each
Secret's keytab bytes on reconcile and publishes
`status.kvnoFromSecret` / `status.enctypesFromSecret` /
`status.keytabObservedAt` for consumers.

### `kubectl krb edit tenant|principal <name>`

Thin wrapper on `kubectl edit` scoped to the two Kerberator CRDs.
Uses `$KUBE_EDITOR` or `$EDITOR` (falls back to vi). Post-edit, runs
a client-side sanity check on the resulting Tenant and warns if the
`krb5.conf` block references the wrong realm or is missing its
`[realms]` section.

### `kubectl krb node-labels`

Discover what Node labels are available in the cluster. Useful before
setting a `Principal.spec.nodeSelector`.

```
$ kubectl krb node-labels
CUSTOM LABELS (operator-set; use these in Principal.spec.nodeSelector)
  security-zone                             prod-a    (1 node)
  tier                                      gpu, cpu  (3 nodes)

$ kubectl krb node-labels --show-system   # include kubernetes.io/*, CSI labels, etc.
$ kubectl krb node-labels --script        # one key=value per line, no headers
```

By default only operator-set labels are shown. Kubelet, cloud
provider, and vendor CSI driver label prefixes
(`beta.kubernetes.io/*`, `node.kubernetes.io/*`,
`kubernetes.io/{arch,os,hostname}`, ...) are hidden so the initial
view is short and relevant. Use `--show-system` when debugging or when
you actually want to target a system label.

### `kubectl krb disable tenant|principal <name>`

Soft-revoke a Principal or an entire Tenant without deleting the CR.
Non-destructive; use `enable` to restore.

```
$ kubectl krb disable principal uid-2001 -n kerberator-demo --reason compromised
principal kerberator-demo/uid-2001 disabled (reason: compromised)
       operator will prune on-node ccaches on next reconcile

$ kubectl krb list -A
NAMESPACE        KIND         NAME      TENANT/UID  READY    NODES  AGE
kerberator-demo    Principal  uid-2001  2001        Unknown  3/3    2h
kerberator-demo    Principal  uid-2002  2002        True     3/3    2h

$ kubectl krb enable principal uid-2001 -n kerberator-demo
principal kerberator-demo/uid-2001 enabled; operator will re-add to Tenant roster on next reconcile
```

Whole-tenant variant for maintenance windows:

```
$ kubectl krb disable tenant demo -n kerberator-demo
tenant kerberator-demo/demo disabled; operator will aggregate an empty roster and daemon will prune every ccache on next poll
```

Effect: the operator excludes the target from the aggregated roster;
on the next daemon poll (10s) the on-node ccache is pruned (respecting
`Tenant.spec.daemon.pruneStale`). Kubelet's ConfigMap sync interval
(about 60-90s) dominates the total round-trip. Emits
`KerberosPrincipalDisabled` / `KerberosPrincipalEnabled` Events on transition
for audit trails.

Flags: `--reason` (Principals only; free-form, surfaced on
`Ready.message`). Idempotent; no-op if the target is already in the
requested state.

### `kubectl krb enable tenant|principal <name>`

Counterpart to `disable`. Clears `spec.disabled` and (for Principals)
also clears `spec.disabledReason` so stale reasons do not linger.
Idempotent.

### `kubectl krb tui`

Interactive terminal UI for everything above. Two-pane layout: a tree
of Tenants with Principals indented beneath, and a detail pane for the
selection. Full-screen modal forms for creating Tenants and
Principals, rotating keytabs, editing node selectors, and deleting
resources (with confirmation). Auto-refreshes every 3 seconds and
shows a per-row spinner while an action is waiting on the daemons to
converge.

Every TUI action calls the same shared function as its CLI
counterpart, so what you can do here is exactly what you can do at
the CLI.

```
kubectl krb tui           # requires a real TTY
```

Keybindings (top-level list view):

| Key | Action |
|---|---|
| `up / down` (or `k / j`) | Navigate list |
| `n` | Create a Tenant |
| `t` | Create a Principal |
| `r` | Rotate keytab (Principals only) |
| `s` | Edit node selector (Principals only); opens a picker with cluster label discovery |
| `x` | Toggle disable on selection (soft revoke; no confirm) |
| `e` | Edit selection (spawns `kubectl edit`) |
| `d` | Delete selection (with confirm) |
| `R` / `ctrl+r` | Manual refresh |
| `?` | Help overlay |
| `esc` | Back / cancel |
| `q` / `ctrl+c` | Quit |

Inside the selector form (`s`):

| Key | Action |
|---|---|
| `tab` / `down` | Focus the discovered-labels picker |
| `j` / `k` (or arrows) | Navigate the picker |
| `enter` (in picker) | Insert `key=value` at the input cursor |
| `S` (in picker) | Toggle inclusion of system labels |
| `tab` (in picker) | Back to the input |
| `enter` (in input) | Submit |
| `esc` | Cancel |

### `kubectl krb drain-node <node> [--uncordon]`

Cordons the node and deletes its kerberator-daemon pod. `--uncordon`
reverses; the DaemonSet controller re-creates the pod on uncordon.
This is a temporary quiesce for KDC maintenance windows or per-node
canary rollouts, not a permanent removal.

Unlike `kubectl drain`, it only touches Kerberator's own DaemonSet.
It does not move Principals or evict other workloads.

### `kubectl krb events [--uid <n>] [--reason <r>]`

Filtered view of the `Kerberos*` Events emitted by daemons. Uses the
structured `kerberator.dell.com/uid` annotation for the `--uid`
filter (not message grepping, which is only the operator's fallback
path).

```
$ kubectl krb events --uid 2001 -A
AGE  NS               POD                           UID   REASON              MESSAGE
6m   kerberator-demo  demo-kerberator-daemon-bcd5m  2001  KerberosMintFailed  kinit failed for uid=2001 ...
6m   kerberator-demo  demo-kerberator-daemon-9qnh9  2001  KerberosMintFailed  kinit failed for uid=2001 ...
```

### `kubectl krb version`

```
$ kubectl krb version
kubectl-krb dev
```

Version is stamped via
`-ldflags "-X github.com/dell/kerberator/shared/version.Version=<tag>"`
at build time; falls through to `dev` for uninstrumented builds.

## Design notes

- No new state. Every subcommand is a stateless CR mutation on the
  cluster. Uninstalling the plugin tomorrow does not strand any data.
- Composes with kubectl. We use `cli-runtime`'s `ConfigFlags` for
  flag parsing, so `--kubeconfig` / `--context` / `-n` behave exactly
  the same as with `kubectl` itself.
- Uncached client. Short-lived CLI operations do not pay the informer
  cache warmup tax; we use controller-runtime's direct client.
- Client-side filtering (for now). The `--uid` filter on `events`
  walks the returned list rather than field-selecting on the
  apiserver, because Event annotations are not indexed. Fine at
  hundreds of daemons.

## Not (yet) implemented

- `kubectl krb migrate <old-tenant> <new-tenant>`: bulk-patch a set
  of Principals' `tenantRef.name`. One-liner via `kubectl patch`; the
  CLI can wrap it.
- `kubectl krb events -f`: a persistent event watcher. Use
  `kubectl get events --watch --field-selector reason=...` today.
- Colored output. Deliberately kept plain text; pipe into `bat` or
  `awk` if you want ANSI.
