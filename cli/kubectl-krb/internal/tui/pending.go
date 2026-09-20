// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

package tui

import (
	"fmt"
	"sort"
	"strings"
	"time"

	tea "github.com/charmbracelet/bubbletea"

	v1alpha1 "github.com/dell/kerberator/operator/api/v1alpha1"
)

// pendingAction is one row-level operation the TUI is waiting for
// the operator + daemon pipeline to reflect in status.
//
// The `beforeSig` fingerprint captures the pieces of the target
// object's status that we expect to change after the operator
// reconciles. When the observed signature diverges from beforeSig,
// we consider the action "converged" and clear the pending marker.
//
// Falling back to a 120s timeout guards against unexpected states
// where the signature never changes (e.g., an action that's a
// visual no-op, or a daemon that's stuck).
type pendingAction struct {
	kind        string // "principal" | "tenant"
	ns, name    string
	verb        string // human-readable: "disabling", "enabling", "restricting", ...
	submittedAt time.Time
	beforeSig   string

	// v0.8.13: for tenant actions, resolvePending waits for FULL
	// convergence (all children have finished their per-node
	// reconcile) rather than clearing on the first observed
	// divergence. Two tracked pieces of state make this possible:
	//
	//   - lastSig: most-recently-observed signature; if it hasn't
	//     changed since the last tick, stableTicks increments.
	//   - stableTicks: count of consecutive ticks where the sig
	//     hasn't moved. Used for tenant-enable, where the target
	//     state is a moving up-count and we can't easily compute
	//     it exactly — we wait for the up-count to stabilize
	//     across enough ticks to be sure daemon action has settled.
	//
	// For tenant-disable the terminal state IS knowable ("0/N"),
	// so stableTicks isn't consulted — we clear only when total
	// hits 0.
	//
	// For principal actions we keep the simpler single-divergence
	// clear because principalSig is a set (not a count) and full-set
	// enumeration would be needed to know the target — punted for
	// now. Principals don't yet see the "premature clear" problem on
	// principals because principal sigs move once per daemon action
	// anyway.
	lastSig     string
	stableTicks int
}

// timeoutFor bounds how long a pending action can stay in the
// pending map without observed convergence. Tuned to comfortably
// exceed the typical worst-case propagation of ~90s (Tenant
// reconcile + kubelet CM sync + daemon 10s poll).
const pendingTimeout = 120 * time.Second

// spinnerFrames is the braille-dot cycle. Eight frames at 500ms/
// frame gives a ~4-second full rotation — slow enough to keep
// render pressure low (bubbletea's diff renderer has intermittent
// leaks under rapid re-renders, especially around lipgloss box
// composition) while still visually confirming that something is
// in flight.
var spinnerFrames = []string{"⣾", "⣽", "⣻", "⢿", "⡿", "⣟", "⣯", "⣷"}

// spinnerInterval is how often spinnerTickMsg fires while there
// are pending actions. Bumped from 250ms to 500ms in v0.8.10 to
// reduce the frequency of bubbletea diff-renderer artifacts (leaked
// stale content, off-by-one line shifts, and duplicate row output).
const spinnerInterval = 500 * time.Millisecond

// spinnerTickMsg is fired periodically to advance the spinner
// while there are pending actions. Self-perpetuates only when
// pending is non-empty — no CPU cost when the TUI is idle.
type spinnerTickMsg struct{}

// spinnerTickCmd schedules the next spinner tick.
func spinnerTickCmd() tea.Cmd {
	return tea.Tick(spinnerInterval, func(_ time.Time) tea.Msg {
		return spinnerTickMsg{}
	})
}

// pendingKey builds the pending map key.
func pendingKey(kind, ns, name string) string {
	return kind + "/" + ns + "/" + name
}

// principalSig fingerprints the state of a Principal that only moves
// after the DAEMON has acted, not just after the operator has
// reconciled. The signature is the sorted list of per-node status
// entries whose Reason=="Minted".
//
// Why not readySummary? Because it CAN change on operator-side
// reconciles alone. Example: user clears a nodeSelector. The
// operator's principalHealth() runs at next reconcile (~1s) and
// immediately flips the DesiredNodes denominator from 1 (was
// selector-restricted) to N (now unrestricted). readySummary
// goes from "1/1" to "1/3" within a second — long before the
// daemon has re-minted on the newly-eligible nodes. Using
// readySummary as the sig would have resolvePending clear the
// pending marker instantly, defeating the visual progress
// affordance. (v0.8.11 bug.)
//
// The Minted-set signature only advances when a daemon emits a
// KerberosMintSucceeded event or when Kerberator's operator
// records a Pruned reason — both are truly daemon-driven.
//
// Fallback: if the Minted set NEVER changes (misconfigured
// keytab, daemon crash-loop, disconnected KDC), the pending
// entry falls back to the 120s timeout.
func principalSig(t *v1alpha1.Principal) string {
	var minted []string
	for _, n := range t.Status.Nodes {
		if n.Reason == "Minted" {
			minted = append(minted, n.Name)
		}
	}
	sort.Strings(minted)
	return strings.Join(minted, ",")
}

// tenantSig fingerprints the sum of Minted-per-node counts across
// every child Principal of the Tenant. Same rationale as principalSig:
// the operator's rosterHash flips within ~1s of any Tenant.spec
// mutation, so we can't use it as a convergence signal. What
// actually reflects daemon-side convergence is that the children's
// ccaches have been pruned (readyNodes drops to 0) or re-minted
// (rises back to N).
//
// If the tenant has zero child Principals, we fall back to the roster
// hash — with no children there's no daemon convergence to wait
// for, so an operator-side signal is the best we can do.
func (m *appModel) tenantSig(r *v1alpha1.Tenant) string {
	var total, count int32
	for i := range m.principals {
		t := &m.principals[i]
		if t.Namespace == r.Namespace && t.Spec.TenantRef.Name == r.Name {
			count++
			total += t.Status.Tenant.ReadyNodes
		}
	}
	if count == 0 {
		return "empty|" + r.Status.RosterHash
	}
	return fmt.Sprintf("%d/%d", total, count)
}

// tenantConverged decides whether a pending tenant action has
// reached its terminal state. Unlike the simpler principal path
// (first-divergence wins), tenant actions cascade across every
// child Principal's daemons, so partial progress ("one child of
// three has been pruned") shouldn't clear the pending banner —
// users want to see the operation in-flight until the last
// child's ccaches have converged.
//
// Two cases:
//
//	Disabling: the tenant's spec.Disabled is now true. Target
//	    signature is "0/N" — every child fully drained. Clear
//	    only when we observe that terminal.
//
//	Enabling (or any other tenant mutation while spec.Disabled
//	is false): the target is a re-mint of ccaches on eligible
//	nodes. We can't compute the exact target count because it
//	depends on per-principal selectors + DS readiness. Instead we
//	wait for stability — signature has DIVERGED from beforeSig
//	AND has held steady for tenantStableTicksRequired consecutive
//	ticks (roughly 2 dataMsg cycles). That's enough to be sure
//	daemon action has settled.
//
// Falls back to any divergence if the tenant has no children
// (nothing to cascade to — the operator side is all that matters).
func (m *appModel) tenantConverged(r *v1alpha1.Tenant, p *pendingAction, currentSig string) bool {
	if currentSig == "" {
		return false
	}
	// No children → single-divergence is fine.
	if strings.HasPrefix(currentSig, "empty|") {
		return currentSig != p.beforeSig
	}
	if r.Spec.Disabled {
		// Terminal state for disable: everyone drained.
		return strings.HasPrefix(currentSig, "0/")
	}
	// Enable or in-place mutation: wait for stability after divergence.
	if currentSig == p.beforeSig {
		return false
	}
	return p.stableTicks >= tenantStableTicksRequired
}

// tenantStableTicksRequired is how many consecutive dataMsg
// observations of the same sig we require before declaring a
// tenant-enable converged. Data polls every ~3s (data.go tick),
// so 2 ticks = ~6s of steady state. Tuned to be short enough to
// feel responsive but long enough to catch the "climbing count"
// case where daemons are minting one node at a time.
const tenantStableTicksRequired = 2

// spinnerGlyph returns the current spinner-frame glyph for a
// pending row. Deterministic within a single frame, cycled by
// spinnerFrame counter on the model.
func spinnerGlyph(frame int) string {
	return spinnerFrames[((frame%len(spinnerFrames))+len(spinnerFrames))%len(spinnerFrames)]
}

// markPendingPrincipal records a pending action against a Principal.
// Captures the current signature so the next dataMsg refresh can
// detect the state change. Idempotent — replacing an existing
// entry restarts the clock (useful if the user re-issues the
// same action).
func (m *appModel) markPendingPrincipal(t *v1alpha1.Principal, verb string) {
	if m.pending == nil {
		m.pending = make(map[string]*pendingAction)
	}
	m.pending[pendingKey("principal", t.Namespace, t.Name)] = &pendingAction{
		kind:        "principal",
		ns:          t.Namespace,
		name:        t.Name,
		verb:        verb,
		submittedAt: time.Now(),
		beforeSig:   principalSig(t),
	}
}

// markPendingTenant records a pending action against a Tenant.
func (m *appModel) markPendingTenant(r *v1alpha1.Tenant, verb string) {
	if m.pending == nil {
		m.pending = make(map[string]*pendingAction)
	}
	m.pending[pendingKey("tenant", r.Namespace, r.Name)] = &pendingAction{
		kind:        "tenant",
		ns:          r.Namespace,
		name:        r.Name,
		verb:        verb,
		submittedAt: time.Now(),
		beforeSig:   m.tenantSig(r),
	}
}

// resolvePending is called from Update after every dataMsg. For
// each pending action, look up the target in the fresh data and
// clear the pending marker if either:
//   - the signature has diverged (state change observed), or
//   - the pendingTimeout has elapsed (fail-safe).
//
// Emits a nudge toast on each transition so users see confirmation
// or a timeout warning. Returns true if ANY pending entry cleared
// so the caller can pair the clear with a screen refresh (see
// v0.8.10 render-cleanliness fix — bubbletea's diff renderer
// leaks stale content on some transitions, and a full clear at
// this moment removes any ambiguity for the user).
func (m *appModel) resolvePending(now time.Time) bool {
	if len(m.pending) == 0 {
		return false
	}
	cleared := false
	for key, p := range m.pending {
		converged := false
		var currentSig string
		switch p.kind {
		case "principal":
			if t := m.findPrincipal(p.ns, p.name); t != nil {
				currentSig = principalSig(t)
				if currentSig != p.beforeSig {
					// Principals: first divergence wins. principalSig is
					// a set of Minted node names; the first change
					// signals daemon action has landed. For now
					// this is close enough to what the user wants;
					// see pendingAction.stableTicks doc for the
					// principal-full-convergence follow-up.
					converged = true
				}
			} else {
				// Object gone (deleted) — treat as converged.
				converged = true
			}
		case "tenant":
			if r := m.findTenant(p.ns, p.name); r != nil {
				currentSig = m.tenantSig(r)
				converged = m.tenantConverged(r, p, currentSig)
			} else {
				converged = true
			}
		}
		// Track stability across ticks so tenantConverged can
		// distinguish "still ramping" from "settled."
		if currentSig == p.lastSig {
			p.stableTicks++
		} else {
			p.lastSig = currentSig
			p.stableTicks = 0
		}
		timedOut := now.Sub(p.submittedAt) > pendingTimeout
		if converged {
			delete(m.pending, key)
			cleared = true
			if m.toast == "" || m.toastErr {
				// Only overwrite an empty/error toast; don't
				// stomp a fresh user-facing message.
				m.toast = fmt.Sprintf("%s converged (%.0fs)", p.verb, now.Sub(p.submittedAt).Seconds())
				m.toastErr = false
			}
			continue
		}
		if timedOut {
			delete(m.pending, key)
			cleared = true
			m.toast = fmt.Sprintf("%s of %s/%s taking longer than %.0fs — check daemon logs",
				p.verb, p.ns, p.name, pendingTimeout.Seconds())
			m.toastErr = true
		}
	}
	return cleared
}

// findPrincipal locates a Principal in the model's snapshot by ns/name.
func (m *appModel) findPrincipal(ns, name string) *v1alpha1.Principal {
	for i := range m.principals {
		if m.principals[i].Namespace == ns && m.principals[i].Name == name {
			return &m.principals[i]
		}
	}
	return nil
}

func (m *appModel) findTenant(ns, name string) *v1alpha1.Tenant {
	for i := range m.tenants {
		if m.tenants[i].Namespace == ns && m.tenants[i].Name == name {
			return &m.tenants[i]
		}
	}
	return nil
}

// pendingForRow returns the pending entry for a given tree row, or
// nil. Used by the row renderer to decide whether to show the
// spinner glyph in place of the normal badge.
//
// v0.8.13: principals under a tenant with an in-flight action inherit
// the parent's pending marker so their rows visually reflect that
// the whole subtree is being reconciled. Without this cascade,
// children stay green during a tenant-disable/enable — misleading,
// because their ccaches ARE being pruned/minted but their own
// Ready condition only updates as each per-node daemon action
// lands (30-90s later).
func (m appModel) pendingForRow(r row) *pendingAction {
	if len(m.pending) == 0 {
		return nil
	}
	switch r.kind {
	case "tenant":
		return m.pending[pendingKey("tenant", r.tenant.Namespace, r.tenant.Name)]
	case "principal":
		// Own pending first.
		if p := m.pending[pendingKey("principal", r.principal.Namespace, r.principal.Name)]; p != nil {
			return p
		}
		// Cascade: parent Tenant has an in-flight action.
		return m.pending[pendingKey("tenant", r.principal.Namespace, r.principal.Spec.TenantRef.Name)]
	}
	return nil
}

// checkPendingConflict decides whether a mutating action on the
// given row should be BLOCKED because a related pending action is
// still in flight. Returns a human-readable reason when blocking is
// warranted, or "" when the action can proceed.
//
// The block rules encode the parent/child cascade relationship:
//
//	Principal action: blocked if THIS principal has a pending action, OR
//	               its parent Tenant does. Parent-tenant actions
//	               cascade to children (TenantDisabled reason), so
//	               starting a child action mid-cascade produces
//	               redundant writes and confusing "both flags set"
//	               spec state on subsequent enable.
//
//	Tenant action:  blocked if THIS tenant has a pending action, OR
//	               ANY child Principal does. Symmetric argument:
//	               a tenant-level change will move every child's
//	               observed status, which would clash with the
//	               in-flight child action.
//
// Sibling principals (both under the same tenant, no tenant-level
// pending) never conflict with each other and can proceed in
// parallel.
//
// This is a WAIT-AND-EXPLAIN gate, not a hard lock: the toast
// message names exactly what to wait for. Principals who need to force
// through anyway can still `kubectl edit` outside the TUI.
func (m appModel) checkPendingConflict(r row) string {
	if len(m.pending) == 0 {
		return ""
	}
	switch r.kind {
	case "principal":
		// Self conflict.
		if p := m.pending[pendingKey("principal", r.principal.Namespace, r.principal.Name)]; p != nil {
			return fmt.Sprintf("waiting: %s on this principal (%ds elapsed)",
				p.verb, int(time.Since(p.submittedAt).Seconds()))
		}
		// Parent-tenant conflict.
		parentName := r.principal.Spec.TenantRef.Name
		if p := m.pending[pendingKey("tenant", r.principal.Namespace, parentName)]; p != nil {
			return fmt.Sprintf("waiting: parent Tenant %s has %s in flight (%ds) — action would cascade",
				parentName, p.verb, int(time.Since(p.submittedAt).Seconds()))
		}
	case "tenant":
		// Self conflict.
		if p := m.pending[pendingKey("tenant", r.tenant.Namespace, r.tenant.Name)]; p != nil {
			return fmt.Sprintf("waiting: %s on this Tenant (%ds elapsed)",
				p.verb, int(time.Since(p.submittedAt).Seconds()))
		}
		// Any-child conflict.
		for i := range m.principals {
			t := &m.principals[i]
			if t.Namespace != r.tenant.Namespace || t.Spec.TenantRef.Name != r.tenant.Name {
				continue
			}
			if p := m.pending[pendingKey("principal", t.Namespace, t.Name)]; p != nil {
				return fmt.Sprintf("waiting: child Principal %s has %s in flight (%ds) — Tenant-level action would cascade over it",
					t.Name, p.verb, int(time.Since(p.submittedAt).Seconds()))
			}
		}
	}
	return ""
}
