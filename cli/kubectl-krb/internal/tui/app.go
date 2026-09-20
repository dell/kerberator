// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

package tui

import (
	"fmt"
	"strings"
	"time"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"

	v1alpha1 "github.com/dell/kerberator/operator/api/v1alpha1"
)

// mode enumerates which view the app is currently rendering. Only
// one mode is active at a time; forms are full-screen modals over
// the list view.
type mode int

const (
	modeSplash mode = iota
	modeList
	modeFormTenant
	modeFormPrincipal
	modeFormRotate
	modeFormSelector
	modeFormDelete
	modeHelp
)

// appModel is the top-level bubbletea Model. Every submodel lives
// as a field here (rather than as a tea.Model stack) because the
// TUI is small enough that swapping in place stays readable.
type appModel struct {
	kc     KubeClientProvider
	width  int
	height int

	mode mode
	prev mode // for Esc-out-of-help

	tenants    []v1alpha1.Tenant
	principals []v1alpha1.Principal
	err        error

	// cursor is a flat index into visibleRows() (tenants + their
	// principals, in order). Simpler than maintaining a tree with
	// expand/collapse for MVP; everything is expanded by default.
	cursor int

	// toast is a transient one-line message shown at the bottom
	// after an action completes. Cleared on next data refresh.
	toast    string
	toastErr bool

	// nodeLabels caches the cluster's Node labels (nodeName ->
	// labels) so principal detail can show which specific nodes
	// currently match a Principal.spec.nodeSelector. Refreshed
	// alongside the 3s data poll so it stays in sync with
	// user-visible node labeling changes. nil until first fetch
	// completes.
	nodeLabels map[string]map[string]string

	// pending tracks in-flight operator actions the TUI is waiting
	// for reality to converge on. Keyed by "<kind>/<ns>/<name>".
	// The corresponding row renders with a spinner glyph and the
	// bottom bar surfaces an aggregate "in progress" line, until
	// either the target row's status signature changes (convergence)
	// or 120s elapse (timeout).
	//
	// v0.8.4: bridges the ~30-90s user-perceived lag between "we
	// wrote the spec" and "the daemon reconciled" so first-time
	// users don't think the TUI is broken.
	pending map[string]*pendingAction

	// spinnerFrame advances on every spinnerTickMsg (250ms).
	// Only ticked while len(pending) > 0.
	spinnerFrame int

	// Forms; only the one matching `mode` is rendered.
	tenantForm    tenantForm
	principalForm principalForm
	rotate        rotateForm
	selector      selectorForm
	deleteC       deleteForm
}

// newModel constructs the initial appModel. Starts on the splash
// screen so first-time visitors get a short welcome
// before landing on the tree view. Any key dismisses the splash.
func newModel(kc KubeClientProvider) appModel {
	return appModel{
		kc:   kc,
		mode: modeSplash,
	}
}

// Init is bubbletea's kickoff. We fetch data immediately and start a
// 3-second refresh ticker.
func (m appModel) Init() tea.Cmd {
	return tea.Batch(m.refresh(), tick(3*time.Second))
}

// Update is bubbletea's message pump. Delegates to per-mode
// handlers for form input; keeps global keys (?, q, Esc) at the top.
func (m appModel) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch msg := msg.(type) {
	case tea.WindowSizeMsg:
		m.width, m.height = msg.Width, msg.Height
		return m, nil

	case dataMsg:
		if msg.err != nil {
			m.err = msg.err
			return m, tea.ClearScreen
		}
		m.tenants = msg.tenants
		m.principals = msg.principals
		if msg.nodeLabels != nil {
			m.nodeLabels = msg.nodeLabels
		}
		m.err = nil
		if m.cursor >= len(m.visibleRows()) {
			m.cursor = len(m.visibleRows()) - 1
		}
		// After each fresh snapshot, check every pending action:
		// converged (state changed) or timed out.
		//
		// v0.8.12: no more tea.ClearScreen on every data tick —
		// it caused a visible flicker every 3s. The tea.ClearScreen
		// remains on actionResultMsg (form close), which is a
		// natural transition point where a full redraw is
		// imperceptible. If the duplicate-row bug returns, we'll
		// need a targeted trigger (mode-change, cursor-move on
		// a specific transition) rather than the everything-tick
		// carpet bomb.
		converged := m.resolvePending(time.Now())
		if m.cursor < 0 {
			m.cursor = 0
		}
		if converged {
			// A pending marker cleared this tick — force a full
			// redraw ONCE so the spinner-to-final-badge transition
			// looks crisp. This fires only on convergence, not
			// on every refresh, so it doesn't flicker steady-state.
			return m, tea.ClearScreen
		}
		return m, nil

	case tickMsg:
		return m, tea.Batch(m.refresh(), tick(3*time.Second))

	case spinnerTickMsg:
		// Advance the spinner frame; self-schedule the next tick
		// only while there's something pending. Zero cost when idle.
		m.spinnerFrame++
		if len(m.pending) > 0 {
			return m, spinnerTickCmd()
		}
		return m, nil

	case nodeLabelsLoadedMsg:
		// Result of the async fetch kicked off when a selector
		// form opened. Update the selector's picker state
		// regardless of whether that form still has focus — a
		// dropped message is harmless (user just doesn't see the
		// picker in that session).
		m.selector.labels = msg.labels
		m.selector.labelsLoading = false
		m.selector.labelsErr = msg.err
		return m, nil

	case actionResultMsg:
		m.toast = msg.summary
		m.toastErr = msg.err != nil
		m.mode = modeList
		if msg.err != nil && msg.pendingKey != "" {
			// The action bounced (e.g. K8s API rejected). Clear
			// the pending marker we optimistically installed so
			// the spinner doesn't grind against nothing.
			delete(m.pending, msg.pendingKey)
		}
		// tea.ClearScreen here forces a full re-render after any
		// form closes (add-tenant, add-principal, rotate, selector,
		// delete). Without it, bubbletea's diff renderer sometimes
		// leaves a shadow of the form's title/body in the first
		// couple lines of the list view. See v0.8.10.
		return m, tea.Batch(tea.ClearScreen, m.refresh())

	case tea.KeyMsg:
		// Splash mode: any key dismisses. Handle first so principals
		// can't accidentally quit before seeing the tree view.
		if m.mode == modeSplash {
			if msg.String() == "ctrl+c" {
				return m, tea.Quit
			}
			m.mode = modeList
			return m, nil
		}
		// Global keys first.
		switch msg.String() {
		case "ctrl+c":
			return m, tea.Quit
		case "q":
			if m.mode == modeList {
				return m, tea.Quit
			}
		case "?":
			if m.mode != modeHelp {
				m.prev = m.mode
				m.mode = modeHelp
				return m, nil
			}
		case "esc":
			if m.mode == modeHelp {
				m.mode = m.prev
				return m, nil
			}
			if m.mode != modeList {
				m.mode = modeList
				return m, nil
			}
		}
		// Mode-specific handling.
		switch m.mode {
		case modeList:
			return m.updateList(msg)
		case modeFormTenant:
			return m.updateTenantForm(msg)
		case modeFormPrincipal:
			return m.updatePrincipalForm(msg)
		case modeFormRotate:
			return m.updateRotateForm(msg)
		case modeFormSelector:
			return m.updateSelectorForm(msg)
		case modeFormDelete:
			return m.updateDeleteForm(msg)
		}
	}
	return m, nil
}

// View is the top-level renderer. Composes the always-present title
// bar, mode-specific body, and status/help footer.
//
// Splash mode is a special case: it renders full-screen without the
// title bar or footer, so first-time viewers see just the centered
// logo.
//
// v0.8.8: the composed output is passed through lipgloss.Place() to
// pad it to EXACTLY (m.width, m.height). Bubbletea's diff-based
// renderer misbehaves when total line count changes between frames
// (footer growing/shrinking with pending banner + toast), leaving
// stale content from earlier renders. Fixed-size output means every
// frame has the same shape and the renderer's diff has no ambiguity.
func (m appModel) View() string {
	if m.mode == modeSplash {
		return m.viewSplash()
	}
	var body string
	switch m.mode {
	case modeList:
		body = m.viewList()
	case modeFormTenant:
		body = m.viewTenantForm()
	case modeFormPrincipal:
		body = m.viewPrincipalForm()
	case modeFormRotate:
		body = m.viewRotateForm()
	case modeFormSelector:
		body = m.viewSelectorForm()
	case modeFormDelete:
		body = m.viewDeleteForm()
	case modeHelp:
		body = m.viewHelp()
	}
	composed := lipgloss.JoinVertical(lipgloss.Left,
		m.titleBar(),
		body,
		m.footer(),
	)
	// Pad the composed output to exactly the terminal dimensions.
	// If the terminal hasn't reported a size yet (very early
	// frame), fall through to the unpadded version — Place() with
	// zero dimensions is undefined behavior.
	if m.width <= 0 || m.height <= 0 {
		return composed
	}
	return lipgloss.Place(m.width, m.height, lipgloss.Left, lipgloss.Top, composed)
}

// titleBar prints the always-visible top strip: product name +
// resolved cluster context hint if we have one.
func (m appModel) titleBar() string {
	title := styleTitle.Render("kerberator")
	sub := styleMuted.Render(" — interactive TUI")
	return title + sub
}

// footer renders help hints appropriate to the current mode plus
// the last toast if any.
func (m appModel) footer() string {
	var hints []string
	switch m.mode {
	case modeList:
		hints = []string{
			key("↑/↓", "navigate"),
			key("n", "new tenant"),
			key("t", "new principal"),
			key("r", "rotate keytab"),
			key("s", "node selector"),
			key("x", "toggle disable"),
			key("e", "edit"),
			key("d", "delete"),
			key("?", "help"),
			key("q", "quit"),
		}
	case modeFormTenant, modeFormPrincipal, modeFormRotate, modeFormSelector, modeFormDelete:
		hints = []string{
			key("tab", "next field"),
			key("shift+tab", "prev"),
			key("enter", "submit"),
			key("esc", "cancel"),
		}
	case modeHelp:
		hints = []string{key("esc", "back")}
	}
	line := strings.Join(hints, styleMuted.Render(" • "))

	// Pending-actions banner: one line summarizing what we're
	// waiting on. Shown ABOVE any toast so the user sees both
	// (a) confirmation that the API accepted the change, and
	// (b) that the visible convergence is still pending.
	if len(m.pending) > 0 {
		line += "\n" + m.pendingBanner()
	}
	if m.toast != "" {
		style := styleOK
		if m.toastErr {
			style = styleErr
		}
		return line + "\n" + style.Render(m.toast)
	}
	if m.err != nil {
		return line + "\n" + styleErr.Render("error: "+m.err.Error())
	}
	return line
}

// pendingBanner renders a single line summarizing the in-flight
// pending actions. Concise for one action ("⣾ disabling uid-2001
// (12s)…"); truncated with a count for multiple ("⣾ 3 actions
// pending (⣾ disabling uid-2001, …)").
func (m appModel) pendingBanner() string {
	if len(m.pending) == 0 {
		return ""
	}
	spin := spinnerGlyph(m.spinnerFrame)
	now := time.Now()
	// Sort by submission time so the display order is stable.
	// Not strictly needed for correctness but nicer for the user.
	oldest := time.Now()
	var lead *pendingAction
	for _, p := range m.pending {
		if lead == nil || p.submittedAt.Before(oldest) {
			lead = p
			oldest = p.submittedAt
		}
	}
	elapsed := int(now.Sub(lead.submittedAt).Seconds())
	if len(m.pending) == 1 {
		return styleMuted.Render(fmt.Sprintf("%s %s %s/%s (%ds — reconcile typically ~30-90s)",
			spin, lead.verb, lead.ns, lead.name, elapsed))
	}
	return styleMuted.Render(fmt.Sprintf("%s %d actions pending — oldest: %s %s/%s (%ds)",
		spin, len(m.pending), lead.verb, lead.ns, lead.name, elapsed))
}

func key(k, desc string) string {
	return styleHelpKey.Render(k) + styleHelpTxt.Render(" "+desc)
}

// row is a flattened view of the tree. Kind is "tenant" or "principal";
// tenants come first, each followed by its principals (indented).
type row struct {
	kind      string
	tenant    *v1alpha1.Tenant
	principal *v1alpha1.Principal
}

func (m appModel) visibleRows() []row {
	rows := make([]row, 0, len(m.tenants)+len(m.principals))
	for i := range m.tenants {
		r := &m.tenants[i]
		rows = append(rows, row{kind: "tenant", tenant: r})
		for j := range m.principals {
			t := &m.principals[j]
			if t.Namespace == r.Namespace && t.Spec.TenantRef.Name == r.Name {
				rows = append(rows, row{kind: "principal", tenant: r, principal: t})
			}
		}
	}
	// Orphan principals (tenantRef not found) at the bottom so the
	// principal still sees them.
	for j := range m.principals {
		t := &m.principals[j]
		orphan := true
		for i := range m.tenants {
			r := &m.tenants[i]
			if t.Namespace == r.Namespace && t.Spec.TenantRef.Name == r.Name {
				orphan = false
				break
			}
		}
		if orphan {
			rows = append(rows, row{kind: "principal", principal: t})
		}
	}
	return rows
}

// selectedRow returns the row the cursor is currently on, or the
// zero row if the list is empty.
func (m appModel) selectedRow() row {
	rs := m.visibleRows()
	if len(rs) == 0 || m.cursor < 0 || m.cursor >= len(rs) {
		return row{}
	}
	return rs[m.cursor]
}

// Run + runProgram live in cmd.go so this file stays focused on the
// bubbletea Model/Update/View trio.
