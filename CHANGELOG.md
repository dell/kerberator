# Changelog

All notable changes to Kerberator are documented here. Kerberator
uses unified semver: one tag ships the daemon, the operator, and
the Helm chart together. Container images and chart `appVersion`
all match the tag.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/).

Naming note: the per-identity CRD was called `User` from v0.2.0
through v0.9.0 and is `Principal` from v0.10.0. Entries below v0.10.0
keep the then-current kind name; plugin verbs are written with their
current names. Namespaces, node names, realms, and principals in
sample output have been replaced with example values.

## v0.10.0 (2026-09-20)

**Status: alpha.** First public release. The API is `v1alpha1` and may
change between minor versions; releases are marked pre-release until 1.0.

### Breaking

- **Kind `User` renamed to `Principal`.** The CR describes a Kerberos
  principal bound to a UID, not a person, and the old name collided
  with the everyday meaning of "user" in every sentence about it.
  No conversion webhook; delete and recreate CRs from source
  manifests.

  | Before | After |
  |---|---|
  | kind `User`, plural `users` | kind `Principal`, plural `principals` |
  | short names `krbuser`, `ku` | short names `krbprincipal`, `kp` |
  | `Tenant.status.userCount` | `Tenant.status.principalCount` |
  | `kubectl get tenants` column `USERS` | column `PRINCIPALS` |
  | `kubectl krb add-user` | `kubectl krb add-principal` |
  | `kubectl krb <verb> user <name>` | `kubectl krb <verb> principal <name>` |
  | webhook path `/validate-...-v1alpha1-user` | `/validate-kerberator-dell-com-v1alpha1-principal` |
  | `crd/users.yaml`, `examples/user.yaml` | `crd/principals.yaml`, `examples/quickstart/principal.yaml` |
  | Go files `add_user.go`, `user_*.go` | `add_principal.go`, `principal_*.go` |

  `Tenant` is unchanged (short names `krbtenant`, `kt`), as are
  `tenantRef`, `spec.principal`, `status.tenant`, the Event
  reasons, the `kerberator.dell.com/uid` annotation, and the
  `kerberator.dell.com/ccache-purged` finalizer.

- **Default `hostCachePath` is now `/tmp`.** The code default was
  `/var/lib/kerberator-daemon` while every doc told you to set `/tmp`
  (where stock `rpc.gssd` looks). The default now matches the advice.
  Tenants that relied on the old default without setting the field
  should set `spec.daemon.hostCachePath: /var/lib/kerberator-daemon`
  explicitly.

- **Daemon image default moves to the operator.** New operator flag
  `--daemon-image` (chart value `daemon.image`) supplies the image
  the DaemonSet runs. `Tenant.spec.daemon.image` is now an optional
  per-tenant override rather than a required field.

- **Go module layout.** The four former Go modules (operator, daemon,
  cli/kubectl-krb, shared) are collapsed into the single module
  `github.com/dell/kerberator`; the local `replace` directives are
  gone, so
  `go install github.com/dell/kerberator/cli/kubectl-krb/cmd/kubectl-krb@latest`
  works. Requires Go 1.26.

- **Build.** Container targets are `make images` and `make push`,
  parameterized by `IMAGE_PREFIX` and `CONTAINER_TOOL` (default
  `docker`; set `buildah` to use that instead).

### Added — keytab KVNO visibility

Surfaces the Kerberos key version number (KVNO) and enctypes the
operator observed in each Principal's referenced keytab Secret. Aimed
at catching the "someone regenerated the keytab at the KDC without
rotating the Secret" drift case before it manifests as a
`Preauthentication failed` on every daemon.

- **CRD (`Principal.status`)**: three new fields:
  `kvnoFromSecret` (uint32), `enctypesFromSecret` (string, sorted
  comma-separated), `keytabObservedAt` (RFC3339 time). Populated
  by the operator's Principal reconciler.
- **`kubectl get principals -A`**: new `KVNO` printer column.
- **Operator**: the Principal reconciler now watches `Secret` objects
  and parses each Principal's referenced keytab bytes on reconcile. A
  minimal MIT v0902 keytab parser lives in `shared/keytab` (no
  gokrb5 dep).
- **CLI**: new `kubectl krb kvno [principal]` subcommand: tabular
  view across a namespace, or a focused per-principal view that also
  scans recent `MintFailed` events for kvno-drift signals and
  suggests `kubectl krb rotate-keytab` when it finds them.
- **CLI**: `kubectl krb describe principal` and
  `kubectl krb describe tenant` now include the KVNO alongside
  each Principal's other status.
- **TUI**: Principal detail pane shows `KVNO`, `Enctypes`, and
  `observed X ago` timestamps.

Existing behavior is unchanged when the Secret is missing, empty,
or unparseable: `status.kvnoFromSecret` stays at 0 and the CLI
shows `<not observed>`.

## v0.9.0 (2026-08-10)

### Breaking — API kinds renamed for natural multi-tenant vocabulary

The two CRDs have been renamed to reflect their real semantics
(one Kerberos realm ≡ one tenant; one identity ≡ one user):

- `Realm` → `Tenant` (outer CR; one per Kerberos realm)
- `Tenant` → `User`  (inner CR; one per UID/principal; then called
  User, renamed again to `Principal` in v0.10.0)
- `spec.realmRef` → `spec.tenantRef` (on the new `User` CR)
- `status.tenantCount` → `status.userCount` (on the new `Tenant` CR)
- `status.realm` → `status.tenant` (RealmHealth field renamed to
  `TenantHealth`; surfaces on `User.status.tenant`)
- Short names: `Tenant` is `krbtenant, kt`; the inner CR also gained
  short names (superseded in v0.10.0)
- Package `operator/internal/realm` → `operator/internal/tenant`
- CLI file `add_realm.go` → `add_tenant.go`; the old `add_tenant.go`
  became the per-identity add subcommand

The Kerberos-protocol vocabulary is unchanged: `Tenant.spec.realm`
still holds the uppercase realm string (e.g. `EXAMPLE.COM`), and
`User.spec.principal` still holds a full Kerberos principal.
Event reasons (`KerberosMintSucceeded`, `KerberosMintFailed`,
`KerberosCcachePruned`, `KerberosKeytabMissing`), metric names, and
the API group / version (`kerberator.dell.com/v1alpha1`) are
unchanged.

No conversion webhook; recreate CRs from your source manifests.

## v0.8.14 (2026-08-10)

### Added — UID range on Tenant detail page

The TUI's Tenant CR detail view now shows the UID range of the users
managed under that tenant, appended in parentheses to the Namespace line:

```
Namespace:       kerberator-demo (UIDs 2001..2004)
```

For a single-user tenant, shows `(UID 2001)`. For an empty tenant,
omits the parenthetical. Computed at render time by scanning the
current users slice for children of the selected tenant — cheap for
the scale the TUI deals with.

## v0.8.13 (2026-08-08)

### Fixed — Tenant-level pending state now cascades to child User rows

Reported: pressing `x` on a Tenant turns the tenant row's badge into
a spinner and shows the pending banner, but the child User rows
stay green throughout the 30-90s reconcile window even though
their ccaches ARE being pruned. Misleading — the user can't tell
the whole subtree is being reconciled.

Root causes (two):

1. **The operator doesn't cascade Ready condition to Users
   when Tenant.spec.Disabled changes.** `UserReconciler` reacts
   to User CR updates, daemon events, and daemon Pod readiness
   — not to Tenant-spec mutations. A child's Ready.reason only
   flips to `TenantDisabled` after each daemon emits a
   `KerberosCcachePruned` event, which is the 30-90s window.
   Fix planned as a follow-up (add a Tenant watch to UserReconciler
   that enqueues all children on spec.Disabled transitions).

2. **The TUI `pendingForRow` didn't cascade the parent's pending
   marker to children.** Fixed here: a User under a Tenant with
   an in-flight action now inherits the tenant's `pendingAction`
   via `pendingForRow`. The user row's badge becomes the
   spinner, and the row-level pending check (`checkPendingConflict`)
   already blocks all mutating actions on those children (v0.8.5).

### Fixed — Tenant pending clears prematurely on partial progress

Reported: the pending banner "disabling tenant ... (Ns)"
disappears as soon as the FIRST child user's ccaches get
pruned, not when the LAST one converges. `tenantSig` is a sum of
per-user ReadyNodes across children, and the earlier
`resolvePending` cleared on the first observed divergence. For
tenant actions that fan out across many children, that's premature.

Fix: `resolvePending` now calls `tenantConverged(r, p, sig)`, which
requires the terminal state (not just any divergence) before
clearing:

  - **Disabling** (`r.Spec.Disabled == true`): converged only
    when `sig` starts with `"0/"` — every child's ccaches have
    been drained across every node.
  - **Enabling / in-place mutation**: converged when `sig !=
    beforeSig` AND has held steady for `tenantStableTicksRequired`
    consecutive dataMsg ticks (2 ticks = ~6s stability). Since
    the target count depends on per-user selectors + DS
    readiness, stability is the honest signal.
  - **No-child tenant**: single divergence is fine — nothing to
    cascade to, only operator-side state matters.

Users keep the simpler first-divergence clear — userSig is
a set of Minted node names and each new node arriving IS
meaningful progress. Punted the "full-set convergence for
users" case; users haven't reported hitting it yet.

## v0.8.12 (2026-08-08)

### Fixed — Flicker every 3s from v0.8.10's overzealous ClearScreen

Reported: the TUI flickers every few seconds. Root cause: v0.8.10
added `tea.ClearScreen` to the dataMsg handler (every 3s) as a
paint-over for bubbletea's diff-renderer artifacts. The clear was
supposed to be imperceptible but on real terminals produces a
brief visible blank + repaint that reads as flicker.

Fix: only issue `tea.ClearScreen` on genuine state transitions,
not on every steady-state refresh:

  - **actionResultMsg**: form closed. Natural transition point;
    the clear coincides with mode-changing (form body → list
    body) so no perceptible flicker. Kept.
  - **dataMsg with a pending action just converged**: the
    spinner-to-final-badge transition should be crisp. Fires
    once per convergence, not on every refresh. Kept.
  - **dataMsg (steady-state, no convergence)**: NO ClearScreen.
    Rely on bubbletea's normal diff render. If the duplicate-row
    bug reappears at steady state, we'll need a more targeted
    trigger (e.g., only on cursor navigation) rather than
    carpet-bombing every refresh.

Trade-off explicit: the duplicate-row bug MAY still surface under
very specific transitions we haven't identified yet. If so, we
handle each case individually rather than papering over with
periodic clears.

## v0.8.11 (2026-08-08)

### Fixed — Spinner/pending banner missing when clearing a nodeSelector

Reported: submitting `s` (edit node selector) with an empty value
completed correctly over ~60s, but during that window neither the
row spinner nor the bottom-bar pending banner showed up.

Root cause is the same class of bug as v0.8.6, at a different
signature. `userSig` used `status.tenant.readySummary`, which was
assumed to only move after the daemon acted. But
`readySummary`'s DENOMINATOR (`DesiredNodes`) is computed by the
operator's `userHealth()` — and it CHANGES immediately on
operator reconcile when a selector is added or removed, because
the eligible-node set changes:

  - Before clearing selector: DesiredNodes=1 (only worker-1 eligible)
  - After operator sees empty selector: DesiredNodes=3 (all nodes)
  - readySummary goes "1/1" → "1/3" within ~1s of the user's write

resolvePending saw the divergence, cleared the pending marker
almost instantly. Spinner never got a chance to show; banner
never appeared.

Fix: `userSig` now hashes the **sorted set of node names with
Reason=Minted** — a signal that only advances when a daemon emits
KerberosMintSucceeded or the operator records a Pruned event.
Operator-side reconciles alone don't change it.

Convergence detection remains correct because when the daemon
DOES re-mint on newly-eligible nodes, the Minted set expands
(gains their names) — signature diverges, pending clears, badge
flips. The 30-90s window during which the daemon reconverges is
now visible to the user via the spinner + banner as intended.

## v0.8.10 (2026-08-08)

### Fixed — Bubbletea diff-renderer artifacts (aggressive approach)

The v0.8.7-v0.8.8 attempts at fixing the duplicate-row bug worked
partially but didn't eliminate it. Reproducer: submit an action
(e.g. clear a nodeSelector), and the first line of BOTH the left
and right panes shows leaked content from the previous frame —
duplicated tenant row on the left, doubled `User CR:` title on
the right. The mechanism is bubbletea's optimized (diff-based)
renderer occasionally miscounting cursor positioning around
lipgloss box composition, leaving stale content at the top of the
frame.

Root cause is inside bubbletea's `standardRenderer`; not something
we can fully fix from our side. Instead, this release papers over
it with two independent mitigations:

1. **`tea.ClearScreen` on every dataMsg (every 3s)**. Forces a
   full frame redraw at each 3-second data-refresh tick. Bounds
   the maximum lifetime of any stale content to ~3s. The clear
   itself is imperceptible because bubbletea writes the new frame
   within milliseconds.

2. **`tea.ClearScreen` on `actionResultMsg`**. Any user-triggered
   form close (add-tenant, add-principal, rotate, selector, delete)
   now forces a clean re-render, eliminating shadows from the
   form's title/body that were sometimes visible in the top rows
   of the list view.

3. **Slower spinner tick (250ms → 500ms)**. Halves the frequency
   of spinner-driven re-renders. Fewer opportunities for the diff
   renderer to hit its edge cases. Visually the spinner is still
   smooth enough to read as "in progress"; the 4-second full
   rotation matches other TUIs (k9s, lazygit).

### Fixed — Row badge visibly updates on action convergence

Related to the diff-renderer artifacts: when a pending action
converged (e.g. `readyNodes` count changed after a selector edit),
the row's spinner glyph reverted to a normal badge in the model,
but the OLD spinner was still visible on-screen until the user
navigated. That was the diff renderer skipping the badge redraw
because it thought the row content was "the same."

The `tea.ClearScreen` on dataMsg fires whenever new data comes in,
which is when convergence is detected. So the moment
`resolvePending` clears a pending marker, the next frame is a
full clean redraw — the badge visibly flips from spinner to
normal state.

## v0.8.9 (2026-08-08)

### Changed — Matching-nodes reads as a child of the selector row

The v0.8.8 "Matching nodes: worker-1" line sat at the same indent
as "Node selector:", so it looked like just another flat field
rather than the RESOLUTION of the selector above it. Rewrote as
an indented tree-connector row that visually attaches to the
selector value:

```
Node selector:  security-zone=prod-a
                └→ matches: 1 of 3 cluster nodes  worker-1
```

The indent lines up under the selector value column (16 chars
past the "Node selector:  " label), and the └→ glyph makes the
"this is what that resolves to" relationship unambiguous. Also
added an X of Y count so the ratio is obvious.

Zero-match case renders in warn/yellow with a clarifying suffix:

```
Node selector:  security-zone=prod-b
                └→ matches: 0 of 3 cluster nodes  (selector matches nothing)
```

## v0.8.8 (2026-08-08)

### Fixed — Duplicate rows in TUI (actual root cause) + Matching-nodes surface

The v0.8.7 fit-width helpers were a real fix for a real bug, but
they weren't the actual root cause of the duplicate rows. Both
`User CR: uid-2001` and `Tenant CR: demo` were shown at the top of
the right pane at once. That is two `formatDetail` outputs stacked,
not one wrapped row.
Which means the real culprit was bubbletea's diff-based renderer
failing to clear stale content when the total View() output changed
line count between frames.

The footer's height varies between 1 and 3 lines depending on
whether a toast, progress banner, or both are showing. Every
v0.8.4-8.7 change made frame-to-frame deltas more unstable —
enough for bubbletea's optimized renderer to occasionally leak
stale content from a prior frame.

Fix: pad the composed View() output to exactly `(m.width, m.height)`
using `lipgloss.Place()`. Every frame is now the same shape, so
the renderer's line-by-line diff has no ambiguity about which
lines to update. Previously-showing content that would have been
partially preserved is now unambiguously overwritten with blank
padding.

### Added — Matching-nodes surface for User detail

The User detail pane already showed the raw `Node selector:`
line (e.g. `security-zone=prod-a`). It did NOT show which cluster
nodes actually match that selector, so operators had to
cross-reference `kubectl krb node-labels` manually.

Now the detail pane resolves the selector against a fresh Node
labels snapshot and surfaces the exact matching set:

```
Node selector:  security-zone=prod-a
Matching nodes: worker-1
```

When a selector matches zero cluster nodes, we render:

```
Matching nodes: (none — selector matches zero cluster nodes)
```

in warn/yellow, since that's a configuration issue (spec is
declaring a footprint that doesn't exist).

Node labels are refreshed alongside the 3s data poll, so if a
cluster admin changes labels while the TUI is open, the matching
set updates on the next tick. Uses the same
`selectorMatchesLabels` semantics as the daemon-side
`MatchesNode` filter, so what the TUI shows is exactly what the
daemon would decide.

## v0.8.7 (2026-08-08)

### Fixed — Duplicate rows in TUI list on scroll

Reported: duplicate lines appear intermittently when scrolling up
and down. Observed: the Tenant row appeared twice inside the left-pane box
while a child User was pending.

Root cause: `styleSel.Width(innerW).Render(text)` on the selected
row wraps `text` onto two lines when its visible width exceeds
`innerW`. Wrapped output has an embedded `\n` which, joined with
the other rows and rendered inside the bordered box, presents as
a duplicated row. The wrap threshold depends on terminal width AND
row content length, so the bug is intermittent: narrow terminals
+ long principal names + wide tenant/namespace names trigger it.

Fix: replaced the auto-wrap Width-and-Render pattern with manual
fit-to-width helpers that clip with an ellipsis (if the row is too
long) or pad with trailing spaces (if too short) BEFORE applying
the style. Two variants:

  - `fitLineWidth`: plain-text rows (selected). No ANSI to
    preserve; simple rune truncation and pad.
  - `fitAnsiLineWidth`: unselected rows with inner ANSI-styled
    spans (badges, muted node counts). Walks the string keeping
    escape sequences intact while truncating visible cells.

Also strips any accidental `\n` from row output before joining,
as defense-in-depth against future style-with-Width regressions.

## v0.8.6 (2026-08-08)

### Fixed — Pending gate cleared prematurely, letting conflicting actions through

Reported: after disabling a Tenant, pressing `x` on a child User
while the spinner was still running was not blocked.

Root cause: the pending-convergence signatures in v0.8.4/v0.8.5
were built from fields that flip on the OPERATOR's next reconcile
(~1s after the user's write) rather than after the DAEMON has
actually acted (30-90s):

  - `userSig` included `Ready.status` and `Ready.reason`, both
    of which the operator sets immediately when it observes
    spec.disabled=true (or tenant-disabled cascade). Signature
    diverged from beforeSig within a second, `resolvePending`
    cleared the pending entry, and the gate stopped blocking.
  - `tenantSig` included the Roster's `rosterHash`, which the
    operator recomputes to `sha256("")` within a second of a
    Tenant.spec.disabled flip. Same premature-clear bug.

Fix: signatures now include ONLY fields that require the daemon
to have acted:

  - `userSig` = `status.tenant.readySummary` (Minted-per-node
    count, per the v0.8.3 userHealth fix). Only moves when the
    daemon prunes or mints.
  - `tenantSig` = sum of children's `readyNodes` (plus child count
    denominator). Only moves when daemons have converged across
    the tenant. Falls back to `rosterHash` for childless tenants
    since there's no daemon convergence to wait for.

Live-verified: v0.8.6 now correctly blocks a child-User `x` for
the full 60-90s daemon-convergence window while a parent Tenant
`x` is pending. When the daemons finish (readyNodes → 0), the
pending entry naturally clears and further actions unblock.

## v0.8.5 (2026-08-08)

### Added — Pending-conflict gate on TUI mutations

Building on v0.8.4's in-flight progress affordance: when a row has
a pending action, related mutations are now blocked with an
explanatory toast rather than silently queuing racy writes.

The block rules encode the parent/child cascade relationship:

- **User action** blocked when THIS user, OR its parent Tenant,
  has a pending action. Parent-tenant actions cascade to children
  (TenantDisabled reason), so starting a child action mid-cascade
  produces redundant writes and confusing "both flags set" spec
  state on subsequent enable.

- **Tenant action** blocked when THIS tenant, OR ANY child User,
  has a pending action. Symmetric argument: tenant-level changes
  will move every child's observed status, which would clash with
  an in-flight child action.

- **Sibling Users** (both under the same tenant, no tenant-level
  pending) never conflict with each other and proceed in parallel.

The block is a **wait-and-explain**, not a hard lock: the toast
names exactly what to wait for and how long it's been waiting.
Users who need to force through anyway can still `kubectl edit`
outside the TUI.

Applied at both keypress time (x, s, r, e, d on a list row) and at
form-submit time (rotate-keytab, selector edit, delete confirm) —
handles the case where the user opens a form before a pending
action starts, then submits after.

## v0.8.4 (2026-08-08)

### Added — TUI in-flight progress affordance

Actions in the TUI (`x` disable/enable, `s` set nodeSelector, and
their form counterparts) trigger a chain of asynchronous reconciles
that typically take 30-90s to fully converge: operator reconcile →
ConfigMap regeneration → kubelet CM sync (dominant, ~60s) → daemon
poll detects hash change → mint/prune → operator observes → status
updates. Previously the TUI just returned to the list view with a
transient toast, giving no visible signal that anything was in
progress. Users who hadn't read the docs would think it was broken.

Now:

- **Per-row spinner glyph.** When you submit an action, the target
  row's status badge is replaced with an animated braille-dot
  spinner (`⣾⣽⣻⢿⡿⣟⣯⣷`, 250ms/frame) until the observed status
  signature diverges from what it was at submission time. At that
  point the spinner clears and the row snaps to its new state
  (green ●, yellow ●, ⏸, etc.).

- **Bottom-bar progress banner.** A muted line above the toast
  shows what we're waiting on: `⣾ disabling kerberator-demo/uid-2001 (23s — reconcile typically ~30-90s)`.
  Multiple concurrent actions collapse into `⣾ 3 actions pending
  — oldest: ...`. This is the "explain the wait" affordance —
  users don't have to know about kubelet CM sync intervals; they
  see it happening.

- **Educational submission toast.** The toast that fires when the
  K8s API accepts an update now includes the "waiting for
  reconcile (~30-90s)" hint so users don't confuse "API accepted"
  with "daemon has applied it."

- **120s timeout with warning.** If a pending action doesn't
  converge in 120s (well past the expected worst case), the
  spinner clears and the toast turns red with a "taking longer
  than expected — check daemon logs" nudge. Prevents a stuck
  spinner from misleading indefinitely.

- **Zero-cost when idle.** The 250ms spinner-tick only self-
  schedules while `len(pending) > 0`. No animation, no re-renders,
  no CPU cost when the TUI is sitting at the list view with
  nothing in flight.

Signature-based convergence detection is action-agnostic — the same
mechanism catches disable/enable landing, selector edits taking
effect, and could be extended to rotate-keytab and add/delete
without additional bookkeeping.

## v0.8.3 (2026-08-08)

### Fixed — Tenant-disable cascade to User status + TUI

Two related bugs surfaced when disabling a whole Tenant:

- **User status was lying.** Unrestricted Users under a disabled
  Tenant continued to show `Ready=True 3/3` because
  `userHealth()`'s unrestricted path returned the DS-level
  daemon-pod-ready count. The daemons were running (empty roster),
  but no ccaches existed anywhere. The status did not reflect that.

  Fix: `userHealth()` now counts per-node `Reason=Minted`
  entries for BOTH restricted and unrestricted users —
  unifying the two paths. Ready is now "eligible nodes with a
  Minted status," never "daemon pods that are up." A user whose
  ccaches were all pruned (tenant-disabled, or a fresh install
  awaiting mints) correctly reports `0/N` until real mints happen.

- **TUI showed child Users as green when parent Tenant was
  disabled.** The badge/color rendering only checked
  `User.spec.disabled`, not the cascade from a disabled Tenant.

  Fix: operator now writes a distinct `TenantDisabled` Ready
  condition reason on every child User when the parent Tenant
  has `spec.disabled=true`. TUI's row renderer treats `Reason ==
  "TenantDisabled"` the same as `Spec.Disabled=true`: `⏸` badge,
  muted line, unambiguous visual state.

Live-verified with a full disable/re-enable cycle in testing:

```
disable tenant -> both Users: Ready=Unknown reason=TenantDisabled 0/N
enable tenant  -> both Users: Ready=True reason=AllDaemonsReady N/N
```

## v0.8.2 (2026-08-07)

### Added — Node-label discovery + TUI editor

Answers the follow-up UX question from v0.8's rollout: "how do I
know what nodeSelector values are available?"

- **`kubectl krb node-labels`** subcommand. Lists Node labels
  aggregated across the cluster. By default hides well-known
  system labels (`beta.kubernetes.io/*`, `node.kubernetes.io/*`,
  vendor CSI label prefixes, `kubernetes.io/{arch,os,hostname}`)
  so first-time viewers see only operator-set labels — the ones
  a User.spec.nodeSelector is safe to key off of.
  - `--show-system` includes the auto-set labels for completeness.
  - `--script` emits one `key=value` per line for grep/awk.
  - Values elided at 3 with `+N more` when a key has a long tail.

- **TUI: `s` key on a User opens a NodeSelector editor** with an
  interactive **discovery picker** underneath the input.
  - Async fetch of Node labels on form open (5s timeout, error
    surfaced inline).
  - `tab` or `↓` from the input focuses the picker; `j/k` or
    `↑/↓` navigate; `enter` inserts `key=value` at the input's
    cursor; `S` toggles system labels; `tab` back to the input
    for edits; `enter` in the input submits.
  - Pre-fills with the user's current selector so it's edit-in-
    place rather than blank-and-retype.
  - Shared driver (`internal.SetUserNodeSelector`) — TUI, CLI,
    and any future integration use the same code path.

### Fixed

- **User status accounting was selector-blind.** A
  selector-restricted user showed `1/3` because
  `status.tenant.readyNodes` was the DS-level ready count,
  ignoring per-user eligibility. New `userHealth()` method on
  the UserReconciler computes eligible + minted node counts by
  listing Nodes and applying the selector; unrestricted users
  still use DS-level counts (backward compatible).
- **New Ready condition reason: `NoEligibleNodes`** —
  distinguishes "your selector matches zero cluster nodes"
  (config error) from "some daemons unready" (transient).
- **Daemon readiness probe was selector-blind.** Pre-v0.8 shell
  probe (`awk` + roster + ccache presence check) failed on any
  node correctly skipping a selector-restricted user. Replaced
  with a `kerberator-daemon readyz` subcommand that reuses the
  daemon's own filter logic — one source of truth for "what
  should be minted on this node."

## v0.8.0 (2026-08-07)

### Added — Per-node targeting via `User.spec.nodeSelector`

A user's on-node ccache can now be restricted to a subset of
worker nodes by matching Kubernetes Node labels. Same semantics as
`DaemonSet.spec.template.spec.nodeSelector` — AND across all
key=value pairs, empty means every node. Answers the "why are
credentials on nodes that don't host this user's workload"
security-review concern directly.

- **`User.spec.nodeSelector: map[string]string`** (default empty).
  Non-empty restricts on-node ccache to matching Nodes.

- **Operator side** (all inside the existing TenantReconciler; no
  new RBAC bindings, just `nodes:[get,list,watch]` added to the
  operator's cluster role):
  - Writes `selectors.json` into the aggregated roster ConfigMap
    when any included User has a non-empty `nodeSelector`:
    `{"<uid>": {"k": "v", ...}, ...}`
  - Writes `node-labels.json` alongside it — a snapshot of every
    Node's labels: `{"<nodeName>": {"k": "v", ...}, ...}`
  - Watches Nodes with a **label-change predicate** so Node status
    heartbeats (kubelet, every ~10s) don't trigger reconcile
    storms — only actual label transitions do.
  - Backward compatible: absent when no selectors exist, so pre-
    v0.8 daemons and clusters without per-node targeting see the
    same minimal ConfigMap shape they always did.

- **Daemon side** (zero new K8s API dependencies — everything
  through the mounted ConfigMap):
  - `NODE_NAME` env populated via downward API (`spec.nodeName`).
  - New flags `--selectors` and `--node-labels` (default to files
    alongside the roster in the same mount). Absent files = no
    filtering (matches pre-v0.8 behavior).
  - Filter runs on every sweep: for each roster entry, look up the
    user's selector; if present, match against this node's
    labels from `node-labels.json`. Skip mismatches. **Fail closed
    for restricted users when node-labels can't be resolved**
    (transient operator/kubelet skew shouldn't leak ccaches).
  - Roster hash detector now includes selectors.json and
    node-labels.json — changes to either drive a re-sweep even
    when `users.roster` is byte-identical.

- **Startup ccache adoption**: on daemon (re)start, `krb5cc_<uid>`
  files present on disk whose UID is currently in the roster are
  claimed as "managed" so `pruneStale` can clean them up if the
  user becomes ineligible on this node. Foreign UIDs (a human's
  `kinit` on the node) are NEVER adopted because they aren't in
  the roster — same safety property as the original design.

- **`kubectl krb add-principal --node-selector k=v`** (repeatable).
  AND semantics across multiple `--node-selector` flags.

- **TUI**: User detail pane surfaces the `Node selector` line
  when non-empty. Row-level indicator via natural badge muting
  when a user is filtered out on this listing node (same UX
  pattern as disabled).

### Live-verified on the test cluster

```
$ kubectl label node worker-1 security-zone=prod-a
$ kubectl -n kerberator-demo patch user uid-2001 --type=merge \
    -p '{"spec":{"nodeSelector":{"security-zone":"prod-a"}}}'
```

Daemon logs on nodes without the label:

```
INFO: Adopted 2 pre-existing ccache(s) as managed: uids=[2001 2002]
INFO: uid=2001 skipped on this node (selector security-zone=prod-a does not match node label (got "", present=false))
SUCCESS: Minted ticket cache svc-2002@EXAMPLE.COM -> /tmp/krb5cc_2002
INFO: Pruned stale ticket cache for uid=2001 (no longer in roster)
```

Result: `krb5cc_2001` present ONLY on worker-1 (which carries the
label); pruned on worker-2 and worker-3. `krb5cc_2002` (no
selector) present on all three. Blast-radius reduction achieved
without any workload-side changes.

### Changed

- **Operator ClusterRole**: adds `nodes:[get,list,watch]`.
  Read-only, narrow. Required by the label-projection feature.

- **Daemon roster-change detector**: `hashInputs()` now composes
  MD5 across `users.roster` + `selectors.json` + `node-labels.json`
  (companion files contribute nothing if absent). Previous
  behavior only hashed the roster, which meant selector changes
  wouldn't fire a re-sweep until the roster itself changed. Fixed.

- **`Processor.adoptPreExisting()`** at daemon startup: reclaims
  ccaches from previous processes IFF their UID is currently in
  the roster. Prevents the "restart lost my managed set → can't
  prune stale ccaches" edge case.

## v0.7.0 (2026-08-07)

### Added — Soft-revoke primitive

Kerberator now supports disabling a User or an entire Tenant without
deleting the CR. This is the "pause" / "kill switch" primitive every
mature operator eventually needs — used for incident response
(suspected credential compromise), maintenance windows, blast-radius
reduction, and testing.

- **`User.spec.disabled: bool`** (default `false`). When true, the
  operator excludes the User from the parent Tenant's aggregated
  roster on the next reconcile. The daemon's poll loop (10s)
  observes the roster hash change and prunes the on-node ccache
  (subject to `Tenant.spec.daemon.pruneStale`). Set back to `false`
  to restore — the operator re-adds the user and the daemon
  re-mints. The User CR, keytab Secret, per-node status history,
  and audit trail are all preserved. Round-trip time on the test
  cluster: ~80s from `disable` to on-node ccache pruned (bounded by
  kubelet's ConfigMap sync interval, not by anything we control).

- **`User.spec.disabledReason: string`** (optional). Free-form
  human label surfaced on `User.status.conditions[Ready].message`.
  Recommended values (not enforced): `compromised`, `maintenance`,
  `audit-hold`. Ignored when `disabled` is `false`.

- **`Tenant.spec.disabled: bool`** (default `false`). Whole-tenant
  kill switch. When true, the operator aggregates an EMPTY roster
  for this Tenant; every child User is effectively disabled at
  once. DaemonSet keeps running (ownerRef graph preserved) so re-
  enabling doesn't have to re-establish state.

- **`User.status.conditions[Ready]`** — new condition state:
  `Unknown / Disabled` (message: `user is disabled (<reason>)`)
  distinguishes "operator intentionally quiesced this" from
  "something broke."

- **Kubernetes Events** — `KerberosUserDisabled` and
  `KerberosUserEnabled` are emitted on the User on transition
  (not on every reconcile). Includes the reason if set. Consumers:
  audit trails, dashboards, on-call alerts distinguishing
  intentional-quiesce from crashloop.

- **`kubectl krb disable <tenant|user> <name>`** — subcommand,
  optionally takes `--reason`. Idempotent (no-op if already
  disabled) and prints a friendly summary.

- **`kubectl krb enable <tenant|user> <name>`** — counterpart.
  Clears `DisabledReason` on the way back (so stale reasons don't
  linger).

- **TUI: `x` key toggle** on the selected row. No confirmation
  modal — this is a soft, reversible op unlike delete. Row badge
  changes to `⏸` when disabled, and the whole line dims via muted
  style. Tenant and User detail panes surface the disabled state
  with the reason.

### Changed

- **`Tenant.status.userCount`** is now the TOTAL number of Users
  pointing at this Tenant (including disabled + deleting), not just
  the roster-eligible count. Matches what a human counting
  `kubectl get users` would see. Roster aggregation still uses
  the filtered subset.

### Live-verified on the test cluster

Full disable/enable round-trip:

```
$ kubectl krb disable user uid-2001 -n kerberator-demo --reason smoke-test
user kerberator-demo/uid-2001 disabled (reason: smoke-test)
                                      — operator will prune on-node ccaches on next reconcile
```

About 80s later, on every node:

```
INFO: Dynamic ConfigMap event detected! Refreshing roster immediately...
INFO: Pruned stale ticket cache for uid=2001 (no longer in roster)
```

```
$ kubectl krb list -A
NAMESPACE        KIND    NAME       TENANT/UID     READY    NODES
kerberator-demo    User  uid-2001   2001          Unknown  3/3    ← was: True
kerberator-demo    User  uid-2002   2002          True     3/3

$ kubectl krb enable user uid-2001 -n kerberator-demo
```

About 80s later: `SUCCESS: Minted ticket cache svc-2001@EXAMPLE.COM
-> /tmp/krb5cc_2001`, and Ready flips back to True.

## v0.6.5 (2026-08-07)

### Changed

- **Disambiguated "Tenant" labels across the TUI and CLI.** The
  CRD *kind* is `Tenant` while the realm string lives in
  `spec.realm` (e.g. `EXAMPLE.COM`). Displaying `Tenant: <ns>/<name>`
  right above `Kerberos realm: EXAMPLE.COM` made readers guess
  which was which.
  - TUI tenant detail title: `Tenant: ns/name` → `Tenant CR: name`,
    with `Namespace` as a separate field
  - TUI user detail title: `User: ns/name` → `User CR: name`,
    with `Namespace` as a separate field
  - TUI user `Tenant ref:` field → `Tenant CR ref:`
  - CLI `describe tenant`: `Tenant:` → `Tenant CR:`
  - CLI `describe user`: `Tenant:` → `Tenant CR ref:` (matches
    the spec field name `spec.tenantRef.name`)
  Tree-view rows unchanged; `● ns/name  EXAMPLE.COM  3/3` already
  had enough context.

## v0.6.4 (2026-08-07)

### Fixed

- **TUI: selected user row STILL not fully highlighted after
  v0.6.3.** The v0.6.3 fix addressed the row-width padding but not
  the inner-span reset problem. When `formatRow` returns a string
  like `"  " + styleOK.Render("●") + " uid-2002  ..." +
  styleMuted.Render("3/3")`, each inner styled span closes with
  `\x1b[0m` — which the terminal treats as "reset ALL attributes,
  including the outer background." So the highlight painted OK on
  either side of an inner span but went blank across the span's
  interior.

  Real fix: teach `formatRow` about the selected state. When
  selected, emit plain text (no inner `Render()` calls at all)
  and let the outer `styleSel.Width(innerW).Render(...)` paint the
  entire row uniformly. When not selected, keep the per-badge and
  per-suffix coloring. Verified live — selected user rows now
  highlight fully, badge included.

## v0.6.3 (2026-08-07)

### Added

- **TUI: startup splash screen.** Big block-glyph "KERBERATOR" logo,
  tagline, and version, centered in the terminal. Any key dismisses
  and drops into the main list view. Uses `lipgloss.Place()` for
  centering; degrades to a plain-text version in terminals smaller
  than 20×10.

### Fixed

- **TUI: selected user row only partially highlighted** (the
  colored badge + muted node count stayed unhighlighted). Root
  cause: `styleSel = ...Reverse(true)` doesn't compose cleanly with
  rows that already contain inner ANSI-styled spans — the terminal's
  per-token attribute reset breaks reverse-video after the first
  inner span. Fix: replace Reverse with an explicit
  `Foreground(white) + Background(blue) + Bold` style, applied with
  `.Width(innerW)` so the highlight paints the entire row, not just
  the printable characters. Tenant rows still highlight identically
  (they've always been single-span).

## v0.6.2 (2026-08-07)

### Fixed

- **TUI: same styled-newline layout bug still visible in the
  rotate-keytab form and the help overlay.** v0.6.1 fixed
  `list.go`'s user detail pane but missed four other call sites in
  `forms.go`. All Render() calls in the TUI package now have
  newlines emitted OUTSIDE the styled span. Verified by grepping
  the whole package for `Render\(.*\\n` — clean sweep.

## v0.6.1 (2026-08-07)

### Fixed

- **TUI: nil-pointer panic on refresh** — the previous shape stored
  factory closures (`func() (client.Client, error)`) in
  `KubeClientProvider` and called them from every `tea.Cmd`. When an
  in-flight refresh from `Init()` overlapped with the 3-second tick's
  refresh, both goroutines concurrently mutated cli-runtime's
  `ConfigFlags` (which is not documented as goroutine-safe) and one
  eventually nil-deref'd inside controller-runtime's `client.New`.
  Fix: `KubeClientProvider` now holds a **pre-built** `client.Client`
  and a **resolved** namespace string, materialized once in
  `NewTUICmd`'s `RunE` before bubbletea starts. Every `tea.Cmd` uses
  the shared client, which controller-runtime documents as safe for
  concurrent use. Bonus effect: kubeconfig-loading errors now surface
  as a plain CLI error at startup instead of a mid-session panic.

- **TUI: mysterious ~19-column indent on the first "Per-node status"
  entry** — caused by embedding `"\n"` inside `styleMuted.Render()`
  calls. The trailing ANSI reset landed after the newline in some
  terminals, shifting the cursor for the next line. Rewrote every
  styled block in `list.go` to emit newlines OUTSIDE the `Render()`
  calls. Layout is now flush across all node lines.

- **TUI: noisy `[controller-runtime] log.SetLogger(...) was never
  called` banner printed on top of the alt-screen** — controller-
  runtime's default logger warns once on first use. Silenced via
  `ctrllog.SetLogger(logr.Discard())` inside the TUI subcommand's
  `RunE`, before the first client operation.

## v0.6.0 (2026-08-06)

### Added

- **`kubectl krb add-tenant`** — imperative counterpart to hand-writing a
  Tenant YAML. Takes `--tenant`, `--image`, `--renew-minutes`,
  `--krb5-conf-file`/`--krb5-conf`. Runs a client-side subset of the
  admission webhook rules (tenant string shape, image ref shape, renew
  bounds, krb5.conf mentions the tenant and has a `[realms]` section)
  so common typos surface before the round-trip. `--dry-run` prints
  the manifest without touching the API.

- **`kubectl krb rotate-keytab <user>`** — one-command wrapper for
  the "here's a fresh keytab" workflow that used to be a foot-gun.
  Reads new keytab bytes, updates the User's Secret, rolls the
  Tenant's DaemonSet, and waits until every node re-mints. Fixes a
  recurring gotcha: updating the Secret alone does NOT
  retrigger a mint until the 12h renewal boundary — you need the
  DaemonSet rollout too. `--wait=false` for hands-off use.

- **`kubectl krb edit <tenant|user> <name>`** — thin wrapper on
  `kubectl edit` scoped to the Kerberator CRDs. Post-edit, runs a
  client-side sanity check on the resulting object and warns if the
  krb5.conf lost its `[realms]` section or the tenant string was
  changed without touching the config (common footgun).

- **`kubectl krb tui`** — first-pass interactive terminal UI built on
  charmbracelet's bubbletea. MVP scope: two-pane list/detail view of
  every Tenant + User across every visible namespace; keyboard-
  driven forms for creating Tenants and Users, rotating keytabs,
  editing (spawns `kubectl edit`), and deleting with confirmation.
  Auto-refreshes every 3 seconds; per-action toasts on success/error.
  Every TUI action calls the same shared functions as its CLI
  counterpart (`AddTenant`, `RotateKeytab`, `CreateUserWithKeytab`,
  ...) — CLI/TUI parity by construction.

  Keybindings: `↑/↓` navigate, `n` new Tenant, `t` new User, `r`
  rotate keytab (Users only), `e` edit, `d` delete, `?` help,
  `esc` cancel, `q` quit.

### Changed

- Extracted the CLI's User-creation code path into a public
  `internal.CreateUserWithKeytab` (returning a small `ActionResult`
  struct) so the TUI form and the `kubectl krb add-principal` subcommand
  drive identical behavior.

## v0.5.2 (2026-08-06)

### Fixed

- **User status now catches up automatically after a DaemonSet
  rollout.** Filed in v0.5.1 as a known limitation. The user
  reconciler now watches daemon Pods (filtered by the operator-
  stamped `app.kubernetes.io/name=kerberator-daemon` label) and
  enqueues affected Users whenever a Pod's Ready condition
  transitions. Previously the user reconciler only fired on
  User CR changes and daemon-emitted Kerberos* Events, both of
  which lagged behind Pod-level state on a rollout — the
  workaround was to `kubectl annotate user ...` to force a
  refresh.

  Predicate is deliberately narrow to avoid noisy reconciles:
  - **Create** of any daemon Pod (fresh scheduling always affects
    node roll-up).
  - **Update** only when the Pod's `Ready` condition status
    actually changed (filters out the kubelet's per-second status
    heartbeats that don't move Ready).
  - **Delete** of any daemon Pod (missing pod affects the user's
    per-node status view).
  - **Generic** events are ignored.

  Live-verified in testing: `kubectl rollout
  restart daemonset/...` followed by watching `kubectl krb list
  -A` — Users now transition from `False 2/3` back to
  `True 3/3` within ~20s (previously stayed at 2/3 indefinitely).

## v0.5.1 (2026-08-06)

### Added

- **User `Ready` condition.** The User CRD schema has always had
  `status.conditions[]` but the operator never wrote a top-level
  `Ready` condition — only per-node status in `nodes[]` and the
  aggregated `readyNodes/desiredNodes` roll-up. Callers (including
  `kubectl krb list` and generic `kubectl get`-style tooling) that
  looked for `Ready` correctly found nothing. Now the user reconciler
  publishes a `Ready` condition derived from the same
  `ReadyNodes == DesiredNodes` rule the plugin's display was hoping
  to find:
  - `True / AllDaemonsReady` — every desired daemon pod is ready and
    at least one daemon exists.
  - `False / SomeDaemonsNotReady` — some fraction of the desired
    daemons haven't reported ready yet.
  - `Unknown / NoDaemons` — no daemon pods observed at all (Tenant
    hasn't fully reconciled).

### Fixed

- **Retired CHANGELOG entry: "v0.4.1 known regression: Events not
  landing."** The regression was never real. What we were actually
  seeing was: (a) daemon events for failed mints did land in the API
  server correctly, (b) Kubernetes' event GC removed them from the
  cluster after ~1 hour, and (c) `kubectl krb events` without `-A`
  operated on the current kubectl default namespace (`default`),
  where no Kerberator events exist. All three factors combined to
  make it look like events were missing when they were actually just
  aged out or in the wrong namespace scope. No code change; the
  CHANGELOG note is corrected retroactively below.

### Known limitation (fixed in v0.5.2)

- User status was stale after a daemon rollout — the user
  reconciler didn't watch Pod objects directly. Fixed in v0.5.2
  with a Pod watch on daemon Pods.

## v0.5.0 (2026-08-06)

### Changed

- **Documentation restructure.** The root README shrank from 532 to
  ~150 lines by moving deep material into supporting docs under
  `docs/`. New layout:
  - `docs/design.md` — how the process ↔ ccache path works, UID
    gotchas, non-goals.
  - `docs/alternatives.md` — full design-space survey and
    decision-matrix table (Kerbernetes, gssproxy, in-container
    Kerberos, Windows gMSA, CSI-level, KEYRING/KCM, kdcproxy,
    SPIFFE, ticket-sidecar pattern).
  - `docs/usage.md` — complete Create Tenant / Add User / Delete
    User walkthrough with keytab rotation, node drain, and
    plugin workflows.
  - `docs/architecture.md` — contributor-facing system diagram and
    data-flow trace for User reconciliation.
  - `CHANGELOG.md` (this file) — pulled the Versioning section out
    of the README.
- No functional changes; container images retagged from v0.4.6.

## Earlier development (v0.1.0 - v0.4.x, 2026-08-06)

Pre-history, collapsed. The tags still exist for archaeology.

- v0.1.0: initial two-CRD API (an outer per-realm CR and an inner
  per-UID CR), validating admission webhook, per-UID Event ingestion
  keyed on the structured `kerberator.dell.com/uid` annotation, and
  finalizer-driven deletion. Consolidated the daemon and operator
  prototypes into one repository.
- v0.2.0: outer CR renamed `Factory` to `Tenant`; the inner
  DaemonTemplate became `spec.daemon`; `factoryPodReady` became
  `daemonPodReady`.
- v0.2.1: chart fullname and imageRef helper fixes.
- v0.3.0: first `kubectl-krb` plugin with `list`, `describe`, an add
  subcommand for principals, `drain-node`, and `events`.
- v0.4.0 (retired) / v0.4.1: API group settled on
  `kerberator.dell.com`; same rename applied to the Event UID
  annotation and the ccache finalizer. v0.4.0 never reached a live
  cluster because a stale image cache shipped the pre-rename daemon
  binary; v0.4.1 is the real cut.
- v0.4.2 - v0.4.6 (docs only): reframed the README around node-level
  ccache management for `rpc.gssd`-style consumers, added the
  process-to-kernel-to-ccache trace, the UID gotchas section
  (`runAsUser` vs image `USER` vs in-container `su`, user namespace
  remapping, OpenShift SCC ranges), and the survey of adjacent
  approaches (in-container Kerberos, gssproxy, gMSA, CSI-level
  Kerberos, KEYRING/KCM, kdcproxy, SPIFFE).
