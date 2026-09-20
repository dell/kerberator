// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

package tui

import (
	"context"
	"time"

	tea "github.com/charmbracelet/bubbletea"
	corev1 "k8s.io/api/core/v1"

	v1alpha1 "github.com/dell/kerberator/operator/api/v1alpha1"
)

// dataMsg is the bubbletea message shape for a completed list of
// Tenants + Principals. Sent as the result of a `refresh` command.
type dataMsg struct {
	tenants    []v1alpha1.Tenant
	principals []v1alpha1.Principal
	nodeLabels map[string]map[string]string
	err        error
}

// tickMsg is emitted by the periodic refresh ticker.
type tickMsg time.Time

// dataMsg + refresh() also fetches the cluster's Node labels
// (v0.8.8) so the Principal detail pane can surface which specific
// nodes currently match a spec.nodeSelector. The fetch is fire-
// and-forget; a nil map on a fetch error just means the
// "Matching nodes" line isn't shown for that render.
//
// actionResultMsg carries the outcome of a form submission (add,
// rotate, delete). The main view uses it to display a transient
// success/error toast and trigger a data refresh.
//
// pendingKey, when non-empty, identifies a pending row (see
// pending.go) that should be cleared on error. Successful actions
// leave the pending marker in place; it clears on the next data
// refresh when the target's status signature changes.
type actionResultMsg struct {
	summary    string
	err        error
	pendingKey string
}

// refresh reads every Tenant + every Principal across every namespace
// the caller can see and returns them as a dataMsg. Uses cross-
// namespace listing because the TUI's list pane is designed to be a
// cluster-wide surface — mirrors `kubectl krb list -A`.
//
// The client is pre-built and stored in m.kc (see KubeClientProvider
// docs) so this goroutine only ever calls thread-safe methods on it.
// controller-runtime's client.Client is safe for concurrent use.
func (m appModel) refresh() tea.Cmd {
	cl := m.kc.Client
	return func() tea.Msg {
		ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
		defer cancel()
		var tenants v1alpha1.TenantList
		if err := cl.List(ctx, &tenants); err != nil {
			return dataMsg{err: err}
		}
		var principals v1alpha1.PrincipalList
		if err := cl.List(ctx, &principals); err != nil {
			return dataMsg{err: err}
		}
		// v0.8.8: also snapshot Node labels so the detail pane can
		// show which specific nodes match a Principal nodeSelector.
		// Fire-and-forget: a failure here doesn't prevent the
		// tenant/principal refresh from succeeding.
		var nodes corev1.NodeList
		labels := map[string]map[string]string{}
		if err := cl.List(ctx, &nodes); err == nil {
			for i := range nodes.Items {
				n := &nodes.Items[i]
				cp := make(map[string]string, len(n.Labels))
				for k, v := range n.Labels {
					cp[k] = v
				}
				labels[n.Name] = cp
			}
		}
		return dataMsg{tenants: tenants.Items, principals: principals.Items, nodeLabels: labels}
	}
}

// tick returns a Cmd that emits a tickMsg after the given interval.
// Called from Update to schedule the next refresh.
func tick(d time.Duration) tea.Cmd {
	return tea.Tick(d, func(t time.Time) tea.Msg { return tickMsg(t) })
}
