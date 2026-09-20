// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

package tui

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"sort"
	"strings"
	"time"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"github.com/dell/kerberator/cli/kubectl-krb/internal"
	v1alpha1 "github.com/dell/kerberator/operator/api/v1alpha1"
)

// updateList handles keys while on the main list view.
func (m appModel) updateList(msg tea.KeyMsg) (tea.Model, tea.Cmd) {
	rows := m.visibleRows()
	switch msg.String() {
	case "up", "k":
		if m.cursor > 0 {
			m.cursor--
		}
	case "down", "j":
		if m.cursor < len(rows)-1 {
			m.cursor++
		}
	case "n":
		m.tenantForm = newTenantForm()
		m.mode = modeFormTenant
	case "t":
		m.principalForm = newPrincipalForm(m.tenants)
		m.mode = modeFormPrincipal
	case "r":
		if r := m.selectedRow(); r.kind == "principal" {
			if reason := m.checkPendingConflict(r); reason != "" {
				m.toast, m.toastErr = reason, false
				return m, nil
			}
			m.rotate = newRotateForm(r.principal)
			m.mode = modeFormRotate
		} else {
			m.toast = "select a principal first (rotate-keytab only applies to Principals)"
			m.toastErr = true
		}
	case "e":
		if r := m.selectedRow(); r.kind != "" {
			if reason := m.checkPendingConflict(r); reason != "" {
				m.toast, m.toastErr = reason, false
				return m, nil
			}
		}
		return m, m.spawnEdit()
	case "d":
		if r := m.selectedRow(); r.kind != "" {
			if reason := m.checkPendingConflict(r); reason != "" {
				m.toast, m.toastErr = reason, false
				return m, nil
			}
			m.deleteC = newDeleteForm(r)
			m.mode = modeFormDelete
		}
	case "x":
		// Toggle disabled state. No confirmation prompt — this is a
		// soft, reversible op (unlike delete). Calls the same shared
		// driver as the CLI subcommand so behavior is identical.
		//
		// Also marks the row as pending so the spinner reflects
		// "waiting for operator + daemon to converge" instead of
		// leaving the user staring at a static row for 30-90s.
		if r := m.selectedRow(); r.kind != "" {
			if reason := m.checkPendingConflict(r); reason != "" {
				m.toast, m.toastErr = reason, false
				return m, nil
			}
			var verb string
			switch r.kind {
			case "principal":
				if r.principal.Spec.Disabled {
					verb = "enabling"
				} else {
					verb = "disabling"
				}
				m.markPendingPrincipal(r.principal, verb)
			case "tenant":
				if r.tenant.Spec.Disabled {
					verb = "enabling tenant"
				} else {
					verb = "disabling tenant"
				}
				m.markPendingTenant(r.tenant, verb)
			}
			return m, tea.Batch(m.toggleDisabled(r), spinnerTickCmd())
		}
	case "s":
		// v0.8: edit Principal.spec.nodeSelector inline. Only meaningful
		// on Principals; a no-op on Tenant rows (Tenants don't have a
		// per-node selector concept).
		if r := m.selectedRow(); r.kind == "principal" {
			if reason := m.checkPendingConflict(r); reason != "" {
				m.toast, m.toastErr = reason, false
				return m, nil
			}
			m.selector = newSelectorForm(r.principal)
			m.mode = modeFormSelector
			// Kick off the background Node-label fetch so the
			// picker populates while the principal's looking at the
			// form. Result arrives as a nodeLabelsLoadedMsg in
			// the top-level Update.
			// Note: pending marker for this action is set at
			// submit-time in submitSelectorForm(), not here —
			// opening the form isn't itself a mutation.
			return m, m.mkLoadLabelsCmd()
		}
		if r := m.selectedRow(); r.kind == "tenant" {
			m.toast = "node selector applies to Principals, not Tenants"
			m.toastErr = true
		}
	case "R", "ctrl+r":
		return m, m.refresh()
	}
	return m, nil
}

// viewList renders the two-pane list+detail body.
func (m appModel) viewList() string {
	rows := m.visibleRows()
	if len(rows) == 0 {
		empty := styleMuted.Render("(no tenants or principals — press 'n' to create a Tenant)")
		return styleBox.Width(m.width - 2).Height(m.height - 4).Render(empty)
	}

	leftW := m.width / 2
	rightW := m.width - leftW - 4
	if leftW < 30 {
		leftW = 30
	}
	if rightW < 30 {
		rightW = m.width - leftW - 4
	}
	// Inner content width = boxW - 2 (border) - 2 (Padding(0, 1)).
	// The selected-row style needs this so its background paints
	// the whole row, not just the printable characters.
	innerW := leftW - 4
	if innerW < 10 {
		innerW = 10
	}

	// Left column: tree.
	//
	// Rendering invariants:
	//
	//  1. Every entry in `left` is EXACTLY one visual line. If
	//     lipgloss's Width() padding on a selected row exceeds
	//     content width, it wraps the content across two lines,
	//     which then look like a duplicate row inside the
	//     bordered box (the specific bug reported in v0.8.7). To
	//     prevent that, we manually clip/pad the row content to
	//     innerW BEFORE applying styleSel, and drop Width() from
	//     the styling call entirely.
	//
	//  2. Selected rows use plain text (no inner styled spans)
	//     so styleSel's Background paints the entire row without
	//     mid-line `\x1b[0m` resets (the v0.6.3 fix).
	//
	//  3. Any accidental \n in a row (from lipgloss re-render or
	//     otherwise) is stripped defensively before joining.
	var left []string
	for i, r := range rows {
		var line string
		pending := m.pendingForRow(r)
		if i == m.cursor {
			raw := formatRow(r, true, pending, m.spinnerFrame)
			line = styleSel.Render(fitLineWidth(raw, innerW))
		} else {
			raw := formatRow(r, false, pending, m.spinnerFrame)
			line = fitAnsiLineWidth(raw, innerW)
		}
		// Defensive: any embedded newline would misalign the
		// vertical join.  Strip.
		line = strings.ReplaceAll(line, "\n", "")
		left = append(left, line)
	}

	// Right column: detail of the selected row.
	right := m.formatDetail(m.selectedRow())

	leftBox := styleActive.Width(leftW).Height(m.height - 5).Render(strings.Join(left, "\n"))
	rightBox := styleBox.Width(rightW).Height(m.height - 5).Render(right)
	return lipgloss.JoinHorizontal(lipgloss.Top, leftBox, rightBox)
}

// formatRow renders one tree entry in the left pane.
//
// When selected==true, the row is emitted as plain text (no inner
// ANSI-styled spans) so that the caller's outer highlight style
// (styleSel with a Background) can paint the entire row uniformly.
// If we let an inner styled span through, its closing `\x1b[0m`
// resets ALL attributes — including the outer background — mid-row.
//
// pending, when non-nil, replaces the row's normal ready-state
// badge with an animated spinner glyph. spinnerFrame drives the
// animation frame.
func formatRow(r row, selected bool, pending *pendingAction, spinnerFrame int) string {
	switch r.kind {
	case "tenant":
		// A ⏸ prefix on disabled tenants so the whole tree collapses
		// visually into "the disabled ones." A spinner prefix
		// overrides both while we're waiting for a reconcile.
		prefix := "●"
		if r.tenant.Spec.Disabled {
			prefix = "⏸"
		}
		if pending != nil {
			prefix = spinnerGlyph(spinnerFrame)
		}
		summary := fmt.Sprintf("%s %s/%s  %s  %s",
			prefix,
			r.tenant.Namespace, r.tenant.Name,
			r.tenant.Spec.Realm,
			r.tenant.Status.ReadySummary)
		if selected {
			return summary
		}
		if pending != nil {
			return styleAccent.Render(summary)
		}
		if r.tenant.Spec.Disabled {
			return styleMuted.Render(summary)
		}
		return styleAccent.Render(summary)
	case "principal":
		ready := readyOf(r.principal.Status.Conditions)
		reason := readyReasonOf(r.principal.Status.Conditions)
		// Effectively-disabled == explicitly disabled on the principal
		// OR the parent Tenant is disabled (cascade in v0.8.3+).
		// Both surface as ⏸ + muted so users don't have to
		// cross-reference the Tenant row to understand why an
		// otherwise-green principal is showing empty NODES.
		disabledCascade := reason == "Disabled" || reason == "TenantDisabled"
		badgeGlyph := "○"
		if ready == "True" || ready == "False" {
			badgeGlyph = "●"
		}
		if r.principal.Spec.Disabled || disabledCascade {
			badgeGlyph = "⏸"
		}
		// Pending overrides everything else; the spinner glyph
		// tells the principal "we're waiting for the operator + daemon
		// to catch up."
		if pending != nil {
			badgeGlyph = spinnerGlyph(spinnerFrame)
		}
		nodes := fmt.Sprintf("%d/%d", r.principal.Status.Tenant.ReadyNodes, r.principal.Status.Tenant.DesiredNodes)
		if selected {
			// Plain-text version — outer styleSel paints everything.
			return fmt.Sprintf("  %s uid-%d  %s  %s",
				badgeGlyph, r.principal.Spec.UID, r.principal.Spec.Principal, nodes)
		}
		// Unselected: color the badge by readiness (or muted if
		// disabled or tenant-disabled) and mute the node-count suffix.
		var badge string
		switch {
		case r.principal.Spec.Disabled || disabledCascade:
			badge = styleMuted.Render(badgeGlyph)
		case ready == "True":
			badge = styleOK.Render(badgeGlyph)
		case ready == "False":
			badge = styleWarn.Render(badgeGlyph)
		default:
			badge = styleMuted.Render(badgeGlyph)
		}
		line := fmt.Sprintf("  %s uid-%d  %s  %s",
			badge, r.principal.Spec.UID, r.principal.Spec.Principal, styleMuted.Render(nodes))
		if r.principal.Spec.Disabled || disabledCascade {
			// Whole line is muted to visually collapse disabled
			// entries (including cascaded from a disabled Tenant).
			// The inner-badge styling above is redundant under a
			// muted wrap but harmless (both wrap in dim).
			return styleMuted.Render(line)
		}
		return line
	}
	return ""
}

// formatDetail renders the right-pane detail for one selection.
// Moved to a method (from a free function) so principal detail can
// enumerate cluster nodes and show which specific ones match a
// nodeSelector.
func (m appModel) formatDetail(r row) string {
	switch r.kind {
	case "tenant":
		return m.formatTenantDetail(r.tenant)
	case "principal":
		return m.formatPrincipalDetail(r.principal)
	}
	return styleMuted.Render("(select a tenant or principal to see details)")
}

// Newlines in this file are always emitted OUTSIDE style.Render()
// blocks. Wrapping "foo\n" in a lipgloss style causes the ANSI reset
// escape to land after the newline in some terminals, which visually
// shifts the next line's cursor to the right by the width of the
// trailing escape. (Symptom: the first "Per-node status" line had a
// mysterious ~19-column indent while subsequent lines were correct.)
func (m appModel) formatTenantDetail(r *v1alpha1.Tenant) string {
	var b strings.Builder
	// Title = CR name only; put namespace + Kerberos realm as
	// separate fields so nothing collides with "Tenant".
	//
	// The Tenant CRD unfortunately overloads the word "Tenant" —
	// the CRD *kind* is Tenant and the spec has a `tenant` string
	// (e.g. EXAMPLE.COM). Calling both "Tenant:" in the UI
	// makes them ambiguous. We disambiguate: "Tenant CR" for the
	// K8s object, "Kerberos realm" for the string.
	b.WriteString(styleTitle.Render(fmt.Sprintf("Tenant CR: %s", r.Name)))
	b.WriteString("\n\n")
	// UID range: min..max across every Principal that references this Tenant.
	// Cheap scan of m.principals — the TUI holds at most a few hundred, and
	// this is only computed when the detail pane renders.
	uidRange := ""
	var minUID, maxUID int64
	found := false
	for i := range m.principals {
		t := &m.principals[i]
		if t.Namespace != r.Namespace || t.Spec.TenantRef.Name != r.Name {
			continue
		}
		u := t.Spec.UID
		if !found {
			minUID, maxUID = u, u
			found = true
			continue
		}
		if u < minUID {
			minUID = u
		}
		if u > maxUID {
			maxUID = u
		}
	}
	if found {
		if minUID == maxUID {
			uidRange = fmt.Sprintf(" (UID %d)", minUID)
		} else {
			uidRange = fmt.Sprintf(" (UIDs %d..%d)", minUID, maxUID)
		}
	}
	b.WriteString(fmt.Sprintf("Namespace:       %s%s\n", r.Namespace, uidRange))
	b.WriteString(fmt.Sprintf("Kerberos realm:  %s\n", r.Spec.Realm))
	b.WriteString(fmt.Sprintf("Daemon image:    %s\n", r.Spec.Daemon.Image))
	b.WriteString(fmt.Sprintf("Renew minutes:   %d\n", r.Spec.Daemon.RenewMinutes))
	b.WriteString(fmt.Sprintf("Prune stale:     %t\n", r.Spec.Daemon.PruneStale))
	if r.Spec.Disabled {
		b.WriteString(styleWarn.Render("Disabled:        yes (whole-tenant kill switch active)"))
		b.WriteString("\n")
	}
	b.WriteString(fmt.Sprintf("Nodes ready:     %s\n", r.Status.ReadySummary))
	b.WriteString(fmt.Sprintf("Principals:         %d\n", r.Status.PrincipalCount))
	b.WriteString(fmt.Sprintf("Ready condition: %s\n\n", readyOf(r.Status.Conditions)))
	if r.Spec.Daemon.Krb5Conf != "" {
		b.WriteString(styleMuted.Render("krb5.conf:"))
		b.WriteString("\n")
		for _, line := range strings.Split(strings.TrimRight(r.Spec.Daemon.Krb5Conf, "\n"), "\n") {
			b.WriteString("  " + line + "\n")
		}
	}
	return b.String()
}

func (m appModel) formatPrincipalDetail(t *v1alpha1.Principal) string {
	var b strings.Builder
	// Title = CR name only. Namespace goes to a field so ns/name
	// isn't jammed against the label "Principal:" (which — unlike
	// Tenant — isn't overloaded, but stay consistent with the
	// Tenant detail layout for a uniform look).
	b.WriteString(styleTitle.Render(fmt.Sprintf("Principal CR: %s", t.Name)))
	b.WriteString("\n\n")
	b.WriteString(fmt.Sprintf("Namespace:      %s\n", t.Namespace))
	b.WriteString(fmt.Sprintf("UID:            %d\n", t.Spec.UID))
	b.WriteString(fmt.Sprintf("Principal:      %s\n", t.Spec.Principal))
	b.WriteString(fmt.Sprintf("Tenant CR ref:   %s\n", t.Spec.TenantRef.Name))
	if t.Spec.KeytabSecretRef != nil {
		b.WriteString(fmt.Sprintf("Keytab secret:  %s (key %s)\n",
			t.Spec.KeytabSecretRef.Name, t.Spec.KeytabSecretRef.Key))
	}
	// KVNO + enctypes derived by the operator from the referenced
	// Keytab Secret. This is what the daemon's next kinit will
	// see; the drift signal is `Ready:False` with reason
	// `Preauthentication failed` in the node table below.
	if t.Status.KvnoFromSecret != 0 {
		obs := ""
		if t.Status.KeytabObservedAt != nil {
			obs = fmt.Sprintf("  (observed %s ago)", shortAge(t.Status.KeytabObservedAt.Time))
		}
		b.WriteString(fmt.Sprintf("KVNO:           %d%s\n", t.Status.KvnoFromSecret, obs))
		if t.Status.EnctypesFromSecret != "" {
			b.WriteString(fmt.Sprintf("Enctypes:       %s\n", t.Status.EnctypesFromSecret))
		}
	} else if t.Spec.KeytabSecretRef != nil {
		b.WriteString(styleMuted.Render("KVNO:           <not observed>"))
		b.WriteString("\n")
	}
	b.WriteString(fmt.Sprintf("Ready:          %s (%d/%d)\n",
		readyOf(t.Status.Conditions),
		t.Status.Tenant.ReadyNodes,
		t.Status.Tenant.DesiredNodes))
	if t.Spec.Disabled {
		line := "Disabled:       yes"
		if t.Spec.DisabledReason != "" {
			line = fmt.Sprintf("Disabled:       yes (reason: %s)", t.Spec.DisabledReason)
		}
		b.WriteString(styleWarn.Render(line))
		b.WriteString("\n")
	}
	if len(t.Spec.NodeSelector) > 0 {
		// Render as `key=value key=value` in alphabetical key
		// order for stable output.
		keys := make([]string, 0, len(t.Spec.NodeSelector))
		for k := range t.Spec.NodeSelector {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		var pairs []string
		for _, k := range keys {
			pairs = append(pairs, fmt.Sprintf("%s=%s", k, t.Spec.NodeSelector[k]))
		}
		b.WriteString(fmt.Sprintf("Node selector:  %s\n", strings.Join(pairs, " ")))
		// v0.8.9: indent + tree-connector glyph so the resolved
		// node set visually reads as a CHILD of the selector row
		// above it, not another flat field. Answers the "which
		// nodes should I expect to see this principal active on?"
		// question the user asked.
		//
		// When m.nodeLabels is empty (fetch failed, or first
		// refresh not landed yet), we skip the resolution line
		// rather than misleadingly say "0 nodes match."
		if len(m.nodeLabels) > 0 {
			var matching []string
			for name, labels := range m.nodeLabels {
				if selectorMatchesLabels(t.Spec.NodeSelector, labels) {
					matching = append(matching, name)
				}
			}
			sort.Strings(matching)
			total := len(m.nodeLabels)
			// Indent under "Node selector:" (16 chars label + 2
			// space padding) so the └→ visually points into the
			// same column as the selector value.
			const indent = "                "
			if len(matching) == 0 {
				b.WriteString(indent)
				b.WriteString(styleWarn.Render(fmt.Sprintf("└→ matches: 0 of %d cluster nodes  (selector matches nothing)", total)))
				b.WriteString("\n")
			} else {
				b.WriteString(indent)
				b.WriteString(styleMuted.Render(fmt.Sprintf("└→ matches: %d of %d cluster nodes  ",
					len(matching), total)))
				b.WriteString(strings.Join(matching, ", "))
				b.WriteString("\n")
			}
		}
	}
	b.WriteString("\n")
	b.WriteString(styleMuted.Render("Per-node status:"))
	b.WriteString("\n")
	if len(t.Status.Nodes) == 0 {
		b.WriteString("  (no nodes reporting)\n")
	} else {
		for _, n := range t.Status.Nodes {
			since := ""
			if !n.LastObserved.IsZero() {
				since = "  " + shortAge(n.LastObserved.Time)
			}
			state := "?"
			style := styleMuted
			switch n.Reason {
			case "Minted":
				state = "✓"
				style = styleOK
			case "MintFailed":
				state = "✗"
				style = styleErr
			}
			// Assemble the styled parts, then append the newline
			// OUTSIDE any Render() call.
			line := fmt.Sprintf("  %s %s  %s%s",
				style.Render(state), n.Name, n.Reason, styleMuted.Render(since))
			b.WriteString(line)
			b.WriteString("\n")
		}
	}
	return b.String()
}

// selectorMatchesLabels reports whether ALL selector key=value
// pairs are present in the node labels. Duplicates the daemon-side
// MatchesNode logic (pkg/watcher/nodefilter.go) so the TUI shows
// exactly the same answer the daemon would give. Zero selector
// matches everything.
func selectorMatchesLabels(selector, labels map[string]string) bool {
	for k, v := range selector {
		if got, ok := labels[k]; !ok || got != v {
			return false
		}
	}
	return true
}

// fitLineWidth forces a plain-text (no ANSI) row to exactly `w`
// visible cells: pads with trailing spaces if shorter, truncates
// with an ellipsis if longer. The caller wraps the result in a
// style whose Background paints the whole row uniformly — the
// invariant is that the input has visible-width == w so lipgloss
// never has cause to wrap. See v0.8.7 double-row bug fix.
func fitLineWidth(s string, w int) string {
	cur := lipgloss.Width(s)
	if cur == w {
		return s
	}
	if cur < w {
		return s + strings.Repeat(" ", w-cur)
	}
	// Truncate rune-by-rune (approx cell width; our rows contain
	// only single-cell characters — ASCII + a handful of
	// single-cell glyphs like ⏸ ● ⣾ — so len(rune) == visible width).
	runes := []rune(s)
	for lipgloss.Width(string(runes)) > w-1 {
		if len(runes) == 0 {
			break
		}
		runes = runes[:len(runes)-1]
	}
	out := string(runes) + "…"
	// Pad if the ellipsis made it short.
	if pad := w - lipgloss.Width(out); pad > 0 {
		out += strings.Repeat(" ", pad)
	}
	return out
}

// fitAnsiLineWidth is fitLineWidth for strings that may already
// contain ANSI escape sequences (from inner styleOK/Warn/Muted
// spans on unselected rows). Preserves escape sequences intact
// while truncating visible content. Pads with plain spaces after
// a trailing `\x1b[0m` reset so trailing colors don't bleed.
func fitAnsiLineWidth(s string, w int) string {
	cur := lipgloss.Width(s)
	if cur == w {
		return s
	}
	if cur < w {
		return s + strings.Repeat(" ", w-cur)
	}
	// Walk runes; if we're inside an ANSI escape sequence,
	// copy it verbatim without counting visible cells. Stop
	// accumulating visible cells at w-1, then append "…" +
	// a reset so any inner style doesn't leak past the row.
	runes := []rune(s)
	var out []rune
	visible := 0
	i := 0
	for i < len(runes) {
		r := runes[i]
		if r == 0x1b {
			// Copy the escape sequence intact until we hit a
			// terminator byte (0x40-0x7e). Includes CSI, SGR,
			// etc. — the only ones lipgloss emits.
			out = append(out, r)
			i++
			for ; i < len(runes); i++ {
				out = append(out, runes[i])
				if runes[i] >= 0x40 && runes[i] <= 0x7e {
					i++
					break
				}
			}
			continue
		}
		if visible >= w-1 {
			break
		}
		out = append(out, r)
		visible++
		i++
	}
	res := string(out) + "\x1b[0m…"
	if pad := w - lipgloss.Width(res); pad > 0 {
		res += strings.Repeat(" ", pad)
	}
	return res
}

// readyOf digests a []metav1.Condition into the value of the "Ready"
// condition status ("True" / "False" / "Unknown"), or "-" if absent.
// Duplicates the internal.readyOf helper to avoid a cross-package
// import cycle (tui depends on internal only via KubeClientProvider).
func readyOf(conds []metav1.Condition) string {
	for _, c := range conds {
		if c.Type == "Ready" {
			return string(c.Status)
		}
	}
	return "-"
}

// readyReasonOf returns the machine-readable Reason on the "Ready"
// condition, empty if absent. Used by the principal-row renderer to
// tell "intentionally disabled" (Disabled, TenantDisabled) apart
// from actually-broken reasons (SomeDaemonsNotReady, NoEligibleNodes)
// so we can render the disabled cascade with the ⏸ badge.
func readyReasonOf(conds []metav1.Condition) string {
	for _, c := range conds {
		if c.Type == "Ready" {
			return c.Reason
		}
	}
	return ""
}

// shortAge renders a duration like "12s ago", "3m ago", "2h ago" — the
// familiar kubectl idiom. Zero time returns empty (caller decides).
func shortAge(t time.Time) string {
	if t.IsZero() {
		return ""
	}
	d := time.Since(t)
	switch {
	case d < time.Minute:
		return fmt.Sprintf("%ds ago", int(d.Seconds()))
	case d < time.Hour:
		return fmt.Sprintf("%dm ago", int(d.Minutes()))
	case d < 24*time.Hour:
		return fmt.Sprintf("%dh ago", int(d.Hours()))
	default:
		return fmt.Sprintf("%dd ago", int(d.Hours()/24))
	}
}

// toggleDisabled flips spec.disabled on the selected Tenant or
// Principal via the shared internal.ToggleDisabled driver. Same code
// path as `kubectl krb disable/enable`.
func (m appModel) toggleDisabled(r row) tea.Cmd {
	cl := m.kc.Client
	var kind, ns, name string
	var currentlyDisabled bool
	switch r.kind {
	case "tenant":
		kind, ns, name = "tenant", r.tenant.Namespace, r.tenant.Name
		currentlyDisabled = r.tenant.Spec.Disabled
	case "principal":
		kind, ns, name = "principal", r.principal.Namespace, r.principal.Name
		currentlyDisabled = r.principal.Spec.Disabled
	default:
		return func() tea.Msg { return actionResultMsg{summary: "nothing selected"} }
	}
	disable := !currentlyDisabled
	pk := pendingKey(kind, ns, name)
	return func() tea.Msg {
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		var sink strings.Builder
		if err := internal.ToggleDisabled(ctx, cl, &sink, ns, kind, name, disable, ""); err != nil {
			return actionResultMsg{summary: err.Error(), err: err, pendingKey: pk}
		}
		verb := "enabled"
		if disable {
			verb = "disabled"
		}
		// Note: this "toast" only means the update was ACCEPTED by
		// the K8s API. The pending marker will hang around until
		// the operator + daemon actually reconcile (~30-90s), and
		// the pending-in-progress footer keeps the user informed.
		return actionResultMsg{
			summary: fmt.Sprintf("%s %s/%s %s — waiting for reconcile (~30-90s)", kind, ns, name, verb),
		}
	}
}

// spawnEdit shells out to `kubectl edit` on the selected resource.
// Bubbletea's ExecProcess primitive handles the terminal handoff
// (suspending the TUI, restoring alt-screen on return). If the
// selection is empty, we no-op with a toast.
func (m appModel) spawnEdit() tea.Cmd {
	r := m.selectedRow()
	if r.kind == "" {
		return func() tea.Msg { return actionResultMsg{summary: "nothing selected", err: fmt.Errorf("no row")} }
	}
	var resource, ns, name string
	switch r.kind {
	case "tenant":
		resource, ns, name = "tenant.kerberator.dell.com", r.tenant.Namespace, r.tenant.Name
	case "principal":
		resource, ns, name = "principal.kerberator.dell.com", r.principal.Namespace, r.principal.Name
	}
	c := exec.Command("kubectl", "edit", resource, name, "-n", ns)
	c.Stdin = os.Stdin
	c.Stdout = os.Stdout
	c.Stderr = os.Stderr
	return tea.ExecProcess(c, func(err error) tea.Msg {
		if err != nil {
			return actionResultMsg{summary: "edit failed: " + err.Error(), err: err}
		}
		return actionResultMsg{summary: "edit complete"}
	})
}
