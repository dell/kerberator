// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

package tui

import (
	"context"
	"fmt"
	"os"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/charmbracelet/bubbles/textarea"
	"github.com/charmbracelet/bubbles/textinput"
	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"

	"github.com/dell/kerberator/cli/kubectl-krb/internal"
	v1alpha1 "github.com/dell/kerberator/operator/api/v1alpha1"
)

// -------- Add Tenant form --------

type tenantForm struct {
	inputs []textinput.Model
	conf   textarea.Model
	focus  int // index into inputs+conf; conf is last (== len(inputs))
}

func newTenantForm() tenantForm {
	name := textinput.New()
	name.Placeholder = "prod"
	name.Focus()
	name.CharLimit = 63
	name.Width = 40

	tenant := textinput.New()
	tenant.Placeholder = "EXAMPLE.COM"
	tenant.CharLimit = 128
	tenant.Width = 40

	image := textinput.New()
	image.Placeholder = "your-registry.example.com/kerberator-daemon:v0.5.2"
	image.CharLimit = 256
	image.Width = 60

	renew := textinput.New()
	renew.Placeholder = "30"
	renew.CharLimit = 6
	renew.Width = 10
	renew.SetValue("30")

	conf := textarea.New()
	conf.Placeholder = "[libdefaults]\n  default_realm = EXAMPLE.COM\n[realms]\n  EXAMPLE.COM = { kdc = kdc.example.com }\n"
	conf.SetHeight(8)
	conf.SetWidth(72)

	return tenantForm{
		inputs: []textinput.Model{name, tenant, image, renew},
		conf:   conf,
		focus:  0,
	}
}

func (m appModel) updateTenantForm(msg tea.KeyMsg) (tea.Model, tea.Cmd) {
	f := m.tenantForm
	switch msg.String() {
	case "tab", "shift+tab", "up", "down":
		if msg.String() == "shift+tab" || msg.String() == "up" {
			f.focus--
		} else {
			f.focus++
		}
		total := len(f.inputs) + 1
		if f.focus < 0 {
			f.focus = total - 1
		}
		if f.focus >= total {
			f.focus = 0
		}
		for i := range f.inputs {
			if i == f.focus {
				f.inputs[i].Focus()
			} else {
				f.inputs[i].Blur()
			}
		}
		if f.focus == len(f.inputs) {
			f.conf.Focus()
		} else {
			f.conf.Blur()
		}
		m.tenantForm = f
		return m, nil
	case "enter":
		// Enter in the textarea inserts a newline; treat enter only
		// as submit when NOT focused on the textarea.
		if f.focus != len(f.inputs) {
			return m.submitTenantForm()
		}
	}

	// Route the raw key to the focused control.
	var cmd tea.Cmd
	if f.focus < len(f.inputs) {
		f.inputs[f.focus], cmd = f.inputs[f.focus].Update(msg)
	} else {
		f.conf, cmd = f.conf.Update(msg)
	}
	m.tenantForm = f
	return m, cmd
}

func (m appModel) viewTenantForm() string {
	f := m.tenantForm
	labels := []string{"Name (in-namespace)", "Kerberos realm", "Daemon image", "Renew minutes"}
	var b strings.Builder
	b.WriteString(styleTitle.Render("Create Tenant"))
	b.WriteString("\n\n")
	for i, in := range f.inputs {
		b.WriteString(fmt.Sprintf("  %s:\n  %s\n\n", labels[i], in.View()))
	}
	b.WriteString("  krb5.conf:\n")
	b.WriteString(f.conf.View())
	b.WriteString("\n\n")
	if err := f.validate(); err != nil && anyFieldDirty(f) {
		b.WriteString(styleErr.Render("  " + err.Error()))
	}
	return styleActive.Width(m.width - 2).Height(m.height - 4).Render(b.String())
}

func anyFieldDirty(f tenantForm) bool {
	for _, i := range f.inputs {
		if i.Value() != "" {
			return true
		}
	}
	return f.conf.Value() != ""
}

func (f tenantForm) validate() error {
	renewMin, err := strconv.ParseInt(f.inputs[3].Value(), 10, 32)
	if err != nil {
		return fmt.Errorf("renew minutes must be an integer")
	}
	return internal.ValidateTenantSpec(v1alpha1.TenantSpec{
		Realm: f.inputs[1].Value(),
		Daemon: v1alpha1.DaemonTemplate{
			Image:        f.inputs[2].Value(),
			RenewMinutes: int32(renewMin),
			Krb5Conf:     f.conf.Value(),
		},
	})
}

func (m appModel) submitTenantForm() (tea.Model, tea.Cmd) {
	f := m.tenantForm
	if err := f.validate(); err != nil {
		return m, func() tea.Msg { return actionResultMsg{summary: err.Error(), err: err} }
	}
	name := f.inputs[0].Value()
	if strings.TrimSpace(name) == "" {
		return m, func() tea.Msg {
			return actionResultMsg{summary: "name is required", err: fmt.Errorf("name required")}
		}
	}
	renewMin, _ := strconv.ParseInt(f.inputs[3].Value(), 10, 32)
	spec := v1alpha1.TenantSpec{
		Realm: f.inputs[1].Value(),
		Daemon: v1alpha1.DaemonTemplate{
			Image:        f.inputs[2].Value(),
			RenewMinutes: int32(renewMin),
			PruneStale:   true,
			Krb5Conf:     f.conf.Value(),
		},
	}
	kc := m.kc
	cl := kc.Client
	ns := kc.Namespace
	return m, func() tea.Msg {
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		tenant := &v1alpha1.Tenant{
			ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: ns},
			Spec:       spec,
		}
		if err := internal.AddTenant(ctx, cl, tenant); err != nil {
			return actionResultMsg{summary: "create tenant: " + err.Error(), err: err}
		}
		return actionResultMsg{summary: fmt.Sprintf("tenant %s/%s created", ns, name)}
	}
}

// -------- Add Principal form --------

type principalForm struct {
	inputs       []textinput.Model
	tenants      []v1alpha1.Tenant
	tenantCursor int // index into tenants
	focus        int // 0..len(inputs)+1 (last slot = tenant picker)
}

func newPrincipalForm(tenants []v1alpha1.Tenant) principalForm {
	uid := textinput.New()
	uid.Placeholder = "2001"
	uid.CharLimit = 10
	uid.Width = 12
	uid.Focus()

	principal := textinput.New()
	principal.Placeholder = "svc-2001@EXAMPLE.COM"
	principal.CharLimit = 128
	principal.Width = 40

	keytab := textinput.New()
	keytab.Placeholder = "/absolute/path/to/user.keytab"
	keytab.CharLimit = 256
	keytab.Width = 60

	return principalForm{
		inputs:       []textinput.Model{uid, principal, keytab},
		tenants:      tenants,
		tenantCursor: 0,
	}
}

func (m appModel) updatePrincipalForm(msg tea.KeyMsg) (tea.Model, tea.Cmd) {
	f := m.principalForm
	switch msg.String() {
	case "tab", "down":
		f.focus++
		total := len(f.inputs) + 1
		if f.focus >= total {
			f.focus = 0
		}
		syncPrincipalFocus(&f)
	case "shift+tab", "up":
		f.focus--
		total := len(f.inputs) + 1
		if f.focus < 0 {
			f.focus = total - 1
		}
		syncPrincipalFocus(&f)
	case "left":
		if f.focus == len(f.inputs) && f.tenantCursor > 0 {
			f.tenantCursor--
		}
	case "right":
		if f.focus == len(f.inputs) && f.tenantCursor < len(f.tenants)-1 {
			f.tenantCursor++
		}
	case "enter":
		return m.submitPrincipalForm()
	default:
		if f.focus < len(f.inputs) {
			var cmd tea.Cmd
			f.inputs[f.focus], cmd = f.inputs[f.focus].Update(msg)
			m.principalForm = f
			return m, cmd
		}
	}
	m.principalForm = f
	return m, nil
}

func syncPrincipalFocus(f *principalForm) {
	for i := range f.inputs {
		if i == f.focus {
			f.inputs[i].Focus()
		} else {
			f.inputs[i].Blur()
		}
	}
}

func (m appModel) viewPrincipalForm() string {
	f := m.principalForm
	labels := []string{"UID", "Principal", "Keytab file path"}
	var b strings.Builder
	b.WriteString(styleTitle.Render("Create Principal"))
	b.WriteString("\n\n")
	for i, in := range f.inputs {
		b.WriteString(fmt.Sprintf("  %s:\n  %s\n\n", labels[i], in.View()))
	}
	// Tenant picker.
	label := "  Tenant: "
	if f.focus == len(f.inputs) {
		label = styleActive.Render("  Tenant: ")
	}
	b.WriteString(label)
	if len(f.tenants) == 0 {
		b.WriteString(styleErr.Render("no tenants exist — create one first (press 'n' on the main view)"))
	} else {
		var chunks []string
		for i, r := range f.tenants {
			label := fmt.Sprintf("%s/%s (%s)", r.Namespace, r.Name, r.Spec.Realm)
			if i == f.tenantCursor {
				label = styleSel.Render(label)
			}
			chunks = append(chunks, label)
		}
		b.WriteString(strings.Join(chunks, " · "))
		b.WriteString("\n  " + styleMuted.Render("(←/→ to switch tenant)"))
	}
	return styleActive.Width(m.width - 2).Height(m.height - 4).Render(b.String())
}

func (m appModel) submitPrincipalForm() (tea.Model, tea.Cmd) {
	f := m.principalForm
	if len(f.tenants) == 0 {
		return m, func() tea.Msg {
			return actionResultMsg{summary: "no tenants — create one first", err: fmt.Errorf("no tenants")}
		}
	}
	uid, err := strconv.ParseInt(f.inputs[0].Value(), 10, 64)
	if err != nil || uid <= 0 {
		return m, func() tea.Msg {
			return actionResultMsg{summary: "UID must be a positive integer", err: fmt.Errorf("uid")}
		}
	}
	tenant := &f.tenants[f.tenantCursor]
	principal := f.inputs[1].Value()
	if strings.TrimSpace(principal) == "" {
		principal = fmt.Sprintf("svc-%d@%s", uid, tenant.Spec.Realm)
	}
	keytabPath := f.inputs[2].Value()
	if strings.TrimSpace(keytabPath) == "" {
		return m, func() tea.Msg {
			return actionResultMsg{summary: "keytab file path is required", err: fmt.Errorf("keytab")}
		}
	}
	keytabBytes, err := os.ReadFile(keytabPath)
	if err != nil {
		return m, func() tea.Msg {
			return actionResultMsg{summary: "read keytab: " + err.Error(), err: err}
		}
	}
	cl := m.kc.Client
	return m, func() tea.Msg {
		ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
		defer cancel()
		// TUI's add-principal form doesn't collect node selectors
		// today — principals can set them via `kubectl krb edit principal`
		// after creation, or via the CLI's --node-selector flag.
		// A dedicated form field is a future polish item.
		raw := internal.CreatePrincipalWithKeytab(ctx, cl, tenant.Namespace, tenant.Name, uid, principal, keytabBytes, nil)
		ar, _ := raw.(internal.ActionResult)
		return actionResultMsg{summary: ar.Summary, err: ar.Err}
	}
}

// -------- Rotate Keytab form --------

type rotateForm struct {
	principalNS   string
	principalName string
	path          textinput.Model
}

func newRotateForm(t *v1alpha1.Principal) rotateForm {
	p := textinput.New()
	p.Placeholder = "/absolute/path/to/new.keytab"
	p.CharLimit = 256
	p.Width = 60
	p.Focus()
	return rotateForm{principalNS: t.Namespace, principalName: t.Name, path: p}
}

func (m appModel) updateRotateForm(msg tea.KeyMsg) (tea.Model, tea.Cmd) {
	f := m.rotate
	switch msg.String() {
	case "enter":
		return m.submitRotateForm()
	default:
		var cmd tea.Cmd
		f.path, cmd = f.path.Update(msg)
		m.rotate = f
		return m, cmd
	}
}

func (m appModel) viewRotateForm() string {
	f := m.rotate
	var b strings.Builder
	b.WriteString(styleTitle.Render(fmt.Sprintf("Rotate keytab: %s/%s", f.principalNS, f.principalName)))
	b.WriteString("\n\n")
	b.WriteString("  New keytab file path:\n  " + f.path.View() + "\n\n")
	// Newlines outside Render() — trailing ANSI reset otherwise
	// shifts the next line's cursor in some terminals. See v0.6.1
	// list.go note.
	b.WriteString(styleMuted.Render("  After submit: updates the Principal's keytab Secret,"))
	b.WriteString("\n")
	b.WriteString(styleMuted.Render("  rolls the DaemonSet, and waits for every node to re-mint."))
	b.WriteString("\n")
	return styleActive.Width(m.width - 2).Height(m.height - 4).Render(b.String())
}

func (m appModel) submitRotateForm() (tea.Model, tea.Cmd) {
	f := m.rotate
	// Re-check pending conflicts at submit time (see selectorForm
	// for rationale). Parent tenant or this principal may have entered
	// a pending state while the user was picking a keytab file.
	if t := m.findPrincipal(f.principalNS, f.principalName); t != nil {
		if reason := m.checkPendingConflict(row{kind: "principal", principal: t}); reason != "" {
			return m, func() tea.Msg {
				return actionResultMsg{summary: reason, err: fmt.Errorf("blocked")}
			}
		}
	}
	path := f.path.Value()
	if strings.TrimSpace(path) == "" {
		return m, func() tea.Msg {
			return actionResultMsg{summary: "keytab path required", err: fmt.Errorf("path")}
		}
	}
	bytes, err := os.ReadFile(path)
	if err != nil {
		return m, func() tea.Msg {
			return actionResultMsg{summary: "read keytab: " + err.Error(), err: err}
		}
	}
	cl := m.kc.Client
	ns, name := f.principalNS, f.principalName
	return m, func() tea.Msg {
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Minute)
		defer cancel()
		// Discard progress writes; the summary line reflects the outcome.
		var sink strings.Builder
		if err := internal.RotateKeytab(ctx, cl, &sink, ns, name, bytes, true, 2*time.Minute); err != nil {
			return actionResultMsg{summary: "rotate: " + err.Error(), err: err}
		}
		return actionResultMsg{summary: fmt.Sprintf("keytab rotated on %s/%s", ns, name)}
	}
}

// -------- Node selector editor --------
//
// v0.8 form for setting/clearing a Principal's spec.nodeSelector.
// Input is a space- or comma-separated list of key=value pairs.
// Empty input clears the selector (removes restriction).
//
// Examples the user might type:
//   security-zone=prod-a
//   security-zone=prod-a tier=gpu
//   security-zone=prod-a,tier=gpu

// selectorForm has two focus targets:
//
//	focusInput  — typing free-form k=v pairs into the text input
//	focusPicker — navigating the discovered-labels list with j/k
//	              and pressing enter to insert the selected pair
//	              into the input.
//
// Tab (and shift+tab) cycles between them. `s` on a Principal opens
// with focusInput so the common "I already know my label" case is
// friction-free.
type selectorFocus int

const (
	focusInput selectorFocus = iota
	focusPicker
)

type selectorForm struct {
	principalNS   string
	principalName string
	current       map[string]string
	input         textinput.Model

	// Discovered cluster labels for the picker. Populated by an
	// async command after the form opens (mkLoadLabelsCmd →
	// nodeLabelsLoadedMsg). Nil while loading; empty slice after
	// a successful-but-no-labels fetch.
	labels        []internal.NodeLabelSummary
	labelsLoading bool
	labelsErr     error

	// UI state
	focus      selectorFocus
	pickerIdx  int  // 0-based index into visibleLabels()
	showSystem bool // toggled with 'S' inside the picker
}

func newSelectorForm(t *v1alpha1.Principal) selectorForm {
	inp := textinput.New()
	inp.Placeholder = "key=value key=value  (blank = clear selector)"
	inp.CharLimit = 512
	inp.Width = 72
	// Pre-fill the current selector so the user can edit in place
	// rather than retype it. Sort keys for stable display.
	if len(t.Spec.NodeSelector) > 0 {
		keys := make([]string, 0, len(t.Spec.NodeSelector))
		for k := range t.Spec.NodeSelector {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		var pairs []string
		for _, k := range keys {
			pairs = append(pairs, fmt.Sprintf("%s=%s", k, t.Spec.NodeSelector[k]))
		}
		inp.SetValue(strings.Join(pairs, " "))
	}
	inp.Focus()
	return selectorForm{
		principalNS:   t.Namespace,
		principalName: t.Name,
		current:       t.Spec.NodeSelector,
		input:         inp,
		focus:         focusInput,
		labelsLoading: true,
	}
}

// nodeLabelsLoadedMsg is delivered by a background tea.Cmd after
// ListNodeLabels returns. Handled in the top-level Update so it
// can arrive whether or not the selector form still has focus (a
// dropped message is fine — user just doesn't see the picker).
type nodeLabelsLoadedMsg struct {
	labels []internal.NodeLabelSummary
	err    error
}

// mkLoadLabelsCmd returns the tea.Cmd that fetches Node labels
// once when the selector form opens.
func (m appModel) mkLoadLabelsCmd() tea.Cmd {
	cl := m.kc.Client
	return func() tea.Msg {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		labels, err := internal.ListNodeLabels(ctx, cl)
		return nodeLabelsLoadedMsg{labels: labels, err: err}
	}
}

// visibleLabels returns the label subset the picker should show
// given the current showSystem toggle. System labels are hidden by
// default so the initial view is short and operator-relevant.
func (f *selectorForm) visibleLabels() []internal.NodeLabelSummary {
	if f.showSystem {
		return f.labels
	}
	out := make([]internal.NodeLabelSummary, 0, len(f.labels))
	for _, s := range f.labels {
		if !s.System {
			out = append(out, s)
		}
	}
	return out
}

func (m appModel) updateSelectorForm(msg tea.KeyMsg) (tea.Model, tea.Cmd) {
	f := m.selector
	// Global (any-focus) shortcuts.
	switch msg.String() {
	case "tab":
		if len(f.visibleLabels()) > 0 {
			if f.focus == focusInput {
				f.focus = focusPicker
				f.input.Blur()
			} else {
				f.focus = focusInput
				f.input.Focus()
			}
		}
		m.selector = f
		return m, nil
	case "shift+tab":
		if f.focus == focusPicker {
			f.focus = focusInput
			f.input.Focus()
		}
		m.selector = f
		return m, nil
	}
	// Focus-specific handling.
	if f.focus == focusPicker {
		return m.updateSelectorPicker(msg)
	}
	// Input focus.
	switch msg.String() {
	case "enter":
		return m.submitSelectorForm()
	case "down":
		// Down-arrow while in input jumps into the picker for
		// keyboard-only workflows.
		if len(f.visibleLabels()) > 0 {
			f.focus = focusPicker
			f.input.Blur()
			m.selector = f
			return m, nil
		}
	}
	var cmd tea.Cmd
	f.input, cmd = f.input.Update(msg)
	m.selector = f
	return m, cmd
}

// updateSelectorPicker handles j/k/up/down/enter/S when focus is on
// the labels list. Enter inserts the highlighted `key=value` into
// the text input at the cursor position.
func (m appModel) updateSelectorPicker(msg tea.KeyMsg) (tea.Model, tea.Cmd) {
	f := m.selector
	visible := f.visibleLabels()
	if len(visible) == 0 {
		// Nothing to navigate; punt back to input.
		f.focus = focusInput
		f.input.Focus()
		m.selector = f
		return m, nil
	}
	switch msg.String() {
	case "j", "down":
		if f.pickerIdx < len(visible)-1 {
			f.pickerIdx++
		}
	case "k", "up":
		if f.pickerIdx > 0 {
			f.pickerIdx--
		}
	case "g":
		f.pickerIdx = 0
	case "G":
		f.pickerIdx = len(visible) - 1
	case "S":
		// Toggle system labels visibility. Clamp cursor since the
		// visible slice size may change.
		f.showSystem = !f.showSystem
		if newLen := len(f.visibleLabels()); f.pickerIdx >= newLen {
			f.pickerIdx = newLen - 1
		}
		if f.pickerIdx < 0 {
			f.pickerIdx = 0
		}
	case "enter":
		// Insert selected label's first value as key=value into
		// the input text. If the label has multiple values, the
		// user can either edit the value inline or cycle with
		// enter presses on the same label (each enter appends the
		// NEXT value). We take a simpler approach: always insert
		// the first value; user can edit it inline afterward.
		sel := visible[f.pickerIdx]
		var value string
		if len(sel.Values) > 0 {
			value = sel.Values[0]
		}
		pair := fmt.Sprintf("%s=%s", sel.Key, value)
		// Append with a space separator if the input has content
		// that doesn't already end in whitespace.
		cur := f.input.Value()
		if cur != "" && !strings.HasSuffix(cur, " ") {
			cur += " "
		}
		f.input.SetValue(cur + pair)
		f.input.SetCursor(len(cur + pair))
		// Snap focus back to the input so the user can edit the
		// value if the picker's first-value default is wrong.
		f.focus = focusInput
		f.input.Focus()
	}
	m.selector = f
	return m, nil
}

func (m appModel) viewSelectorForm() string {
	f := m.selector
	var b strings.Builder
	b.WriteString(styleTitle.Render(fmt.Sprintf("Node selector: %s/%s", f.principalNS, f.principalName)))
	b.WriteString("\n\n")

	// Input row (with focus indicator).
	inputLabel := "  key=value pairs (space or comma separated), or blank to clear:"
	if f.focus == focusInput {
		inputLabel = "▸ key=value pairs (space or comma separated), or blank to clear:"
	}
	b.WriteString(inputLabel)
	b.WriteString("\n  ")
	b.WriteString(f.input.View())
	b.WriteString("\n\n")

	// Picker panel.
	b.WriteString(renderSelectorPicker(&f, m.width))
	b.WriteString("\n")

	// Bottom hints line — key affordances in the current focus mode.
	var hint string
	switch f.focus {
	case focusInput:
		hint = "  tab/↓ → picker  •  enter submit  •  esc cancel"
	case focusPicker:
		hint = "  j/k or ↓/↑ navigate  •  enter insert into input  •  S toggle system labels  •  tab back to input  •  esc cancel"
	}
	b.WriteString(styleMuted.Render(hint))
	b.WriteString("\n\n")

	// Explanation footer.
	b.WriteString(styleMuted.Render("  On submit: writes to Principal.spec.nodeSelector."))
	b.WriteString("\n")
	b.WriteString(styleMuted.Render("  Operator will re-project selectors.json and daemons will"))
	b.WriteString("\n")
	b.WriteString(styleMuted.Render("  re-evaluate eligibility on the next roster hash sweep (~30-90s)."))
	b.WriteString("\n")
	return styleActive.Width(m.width - 2).Height(m.height - 4).Render(b.String())
}

// renderSelectorPicker builds the "Available Node labels" panel
// under the input. Three visual states: loading, error, and
// populated. The populated case shows a scrolling window of ~10
// labels centered on the current selection.
func renderSelectorPicker(f *selectorForm, width int) string {
	var b strings.Builder
	title := "Available Node labels (from cluster)"
	if f.showSystem {
		title += " — including system"
	}
	b.WriteString("  ")
	b.WriteString(styleMuted.Render(title))
	b.WriteString("\n")

	switch {
	case f.labelsLoading:
		b.WriteString("  ")
		b.WriteString(styleMuted.Render("(fetching...)"))
		b.WriteString("\n")
		return b.String()
	case f.labelsErr != nil:
		b.WriteString("  ")
		b.WriteString(styleErr.Render(fmt.Sprintf("(error: %v)", f.labelsErr)))
		b.WriteString("\n")
		return b.String()
	}

	visible := f.visibleLabels()
	if len(visible) == 0 {
		msg := "(no operator-set labels found; press S to include system labels)"
		if f.showSystem {
			msg = "(no labels at all — the cluster has no Nodes visible)"
		}
		b.WriteString("  ")
		b.WriteString(styleMuted.Render(msg))
		b.WriteString("\n")
		return b.String()
	}

	// Windowed rendering: at most 10 rows, centered on pickerIdx.
	const windowSize = 10
	start := f.pickerIdx - windowSize/2
	if start < 0 {
		start = 0
	}
	end := start + windowSize
	if end > len(visible) {
		end = len(visible)
		start = end - windowSize
		if start < 0 {
			start = 0
		}
	}
	// Compute a stable-width first column so values line up.
	keyWidth := 20
	for _, s := range visible[start:end] {
		if l := len(s.Key); l > keyWidth {
			keyWidth = l
		}
	}
	if keyWidth > 50 {
		keyWidth = 50
	}
	for i := start; i < end; i++ {
		s := visible[i]
		row := fmt.Sprintf("  %-*s  %s  (%d node%s)",
			keyWidth, truncate(s.Key, keyWidth),
			truncateList(s.Values, 40),
			s.Nodes, pluralS(s.Nodes))
		if f.focus == focusPicker && i == f.pickerIdx {
			// Highlight the current row; width-clip to the pane.
			w := width - 6
			if w < 20 {
				w = 20
			}
			row = styleSel.Width(w).Render(row)
		}
		b.WriteString(row)
		b.WriteString("\n")
	}
	if len(visible) > windowSize {
		b.WriteString("  ")
		b.WriteString(styleMuted.Render(fmt.Sprintf("(%d/%d)", f.pickerIdx+1, len(visible))))
		b.WriteString("\n")
	}
	return b.String()
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	if n <= 1 {
		return s[:n]
	}
	return s[:n-1] + "…"
}

func truncateList(values []string, maxLen int) string {
	if len(values) == 0 {
		return "(no values)"
	}
	joined := strings.Join(values, ", ")
	if len(joined) <= maxLen {
		return joined
	}
	return joined[:maxLen-1] + "…"
}

func pluralS(n int) string {
	if n == 1 {
		return ""
	}
	return "s"
}

func (m appModel) submitSelectorForm() (tea.Model, tea.Cmd) {
	f := m.selector
	raw := f.input.Value()
	selector, err := parseSelectorInput(raw)
	if err != nil {
		return m, func() tea.Msg {
			return actionResultMsg{summary: err.Error(), err: err}
		}
	}
	cl := m.kc.Client
	ns, name := f.principalNS, f.principalName
	// Re-check for pending conflicts at submit time — the parent
	// tenant (or this principal) may have entered a pending state
	// while the user was filling out the form.
	if t := m.findPrincipal(ns, name); t != nil {
		if reason := m.checkPendingConflict(row{kind: "principal", principal: t}); reason != "" {
			return m, func() tea.Msg {
				return actionResultMsg{summary: reason, err: fmt.Errorf("blocked")}
			}
		}
		verb := "restricting"
		if len(selector) == 0 {
			verb = "clearing selector"
		}
		m.markPendingPrincipal(t, verb)
	}
	pk := pendingKey("principal", ns, name)
	return m, tea.Batch(spinnerTickCmd(), func() tea.Msg {
		ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
		defer cancel()
		if err := internal.SetPrincipalNodeSelector(ctx, cl, ns, name, selector); err != nil {
			return actionResultMsg{summary: err.Error(), err: err, pendingKey: pk}
		}
		if len(selector) == 0 {
			return actionResultMsg{summary: fmt.Sprintf("cleared nodeSelector on %s/%s — waiting for reconcile", ns, name)}
		}
		return actionResultMsg{summary: fmt.Sprintf("set nodeSelector on %s/%s (%d pairs) — waiting for reconcile", ns, name, len(selector))}
	})
}

// parseSelectorInput turns the free-form user input into a
// map[string]string. Accepts space- and comma-separated k=v pairs.
// Empty/whitespace-only input is valid (clears the selector).
func parseSelectorInput(raw string) (map[string]string, error) {
	trimmed := strings.TrimSpace(raw)
	if trimmed == "" {
		return map[string]string{}, nil
	}
	// Normalize commas to spaces so both separators work.
	trimmed = strings.ReplaceAll(trimmed, ",", " ")
	out := map[string]string{}
	for _, tok := range strings.Fields(trimmed) {
		i := strings.IndexByte(tok, '=')
		if i <= 0 || i == len(tok)-1 {
			return nil, fmt.Errorf("expected key=value, got %q", tok)
		}
		out[tok[:i]] = tok[i+1:]
	}
	return out, nil
}

// -------- Delete confirmation --------

type deleteForm struct {
	target row
}

func newDeleteForm(r row) deleteForm { return deleteForm{target: r} }

func (m appModel) updateDeleteForm(msg tea.KeyMsg) (tea.Model, tea.Cmd) {
	switch msg.String() {
	case "y", "Y", "enter":
		return m.submitDelete()
	case "n", "N", "esc":
		m.mode = modeList
	}
	return m, nil
}

func (m appModel) viewDeleteForm() string {
	f := m.deleteC
	var name, kind, note string
	switch f.target.kind {
	case "tenant":
		kind = "Tenant"
		name = f.target.tenant.Namespace + "/" + f.target.tenant.Name
		note = "This will also delete the DaemonSet, aggregated roster ConfigMap, and\naggregated keytab Secret owned by this Tenant."
	case "principal":
		kind = "Principal"
		name = f.target.principal.Namespace + "/" + f.target.principal.Name
		note = "The operator will hold the finalizer until every daemon confirms\nthe on-node ccache has been purged (see Tenant.spec.deletionTimeout)."
	}
	var b strings.Builder
	b.WriteString(styleErr.Render(fmt.Sprintf("Delete %s: %s", kind, name)))
	b.WriteString("\n\n")
	b.WriteString(note)
	b.WriteString("\n\n")
	b.WriteString(styleMuted.Render("  press [y] or [enter] to confirm, [n] or [esc] to cancel"))
	return styleActive.Width(m.width - 2).Height(m.height - 4).Render(b.String())
}

func (m appModel) submitDelete() (tea.Model, tea.Cmd) {
	f := m.deleteC
	cl := m.kc.Client
	target := f.target
	// Re-check pending conflicts at confirmation time — deleting a
	// row while its parent or itself has a pending action is even
	// more likely to produce confusing state (delete during a
	// disable cascade means the finalizer waits for a ccache purge
	// that's already happening for a different reason).
	if reason := m.checkPendingConflict(target); reason != "" {
		return m, func() tea.Msg {
			return actionResultMsg{summary: reason, err: fmt.Errorf("blocked")}
		}
	}
	return m, func() tea.Msg {
		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()
		switch target.kind {
		case "tenant":
			if err := cl.Delete(ctx, target.tenant); err != nil {
				return actionResultMsg{summary: "delete tenant: " + err.Error(), err: err}
			}
			return actionResultMsg{summary: fmt.Sprintf("tenant %s/%s deletion initiated",
				target.tenant.Namespace, target.tenant.Name)}
		case "principal":
			if err := cl.Delete(ctx, target.principal); err != nil {
				return actionResultMsg{summary: "delete principal: " + err.Error(), err: err}
			}
			return actionResultMsg{summary: fmt.Sprintf(
				"principal %s/%s deletion initiated (finalizer holds until ccache purged)",
				target.principal.Namespace, target.principal.Name)}
		}
		return actionResultMsg{summary: "nothing selected", err: fmt.Errorf("no target")}
	}
}

// -------- Help overlay --------

func (m appModel) viewHelp() string {
	var b strings.Builder
	b.WriteString(styleTitle.Render("kubectl krb tui — keybindings"))
	b.WriteString("\n\n")
	rows := [][2]string{
		{"↑/k, ↓/j", "navigate list"},
		{"n", "create Tenant"},
		{"t", "create Principal (against selected Tenant)"},
		{"r", "rotate keytab (on selected Principal)"},
		{"s", "edit node selector (Principal only, v0.8+)"},
		{"x", "toggle disable on selection (soft revoke)"},
		{"e", "edit (kubectl edit on selection)"},
		{"d", "delete (with confirm)"},
		{"R, ctrl+r", "manual refresh"},
		{"?", "this help"},
		{"esc", "back / cancel"},
		{"q, ctrl+c", "quit"},
	}
	for _, r := range rows {
		b.WriteString(fmt.Sprintf("  %-14s %s\n", styleHelpKey.Render(r[0]), r[1]))
	}
	b.WriteString("\n")
	b.WriteString(styleMuted.Render("Every action here calls the same shared functions as the"))
	b.WriteString("\n")
	b.WriteString(styleMuted.Render("parallel `kubectl krb <subcommand>` commands, so behavior is identical."))
	b.WriteString("\n")
	return styleActive.Width(m.width - 2).Height(m.height - 4).Render(b.String())
}

// Force imports for types + lipgloss (used by other files, referenced here for linter).
var (
	_ = types.NamespacedName{}
	_ = lipgloss.NewStyle
)
