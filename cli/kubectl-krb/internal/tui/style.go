// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

package tui

import "github.com/charmbracelet/lipgloss"

// All colors here are ANSI ints for terminals that don't grok 24-bit
// color. Keeping the palette small (title, muted, accent, error, ok)
// makes the whole UI legible in low-color terminals like the default
// Windows Terminal profile or macOS Terminal.app.
var (
	styleTitle  = lipgloss.NewStyle().Bold(true).Foreground(lipgloss.Color("13")) // magenta
	styleMuted  = lipgloss.NewStyle().Foreground(lipgloss.Color("8"))             // grey
	styleAccent = lipgloss.NewStyle().Foreground(lipgloss.Color("12"))            // blue
	styleOK     = lipgloss.NewStyle().Foreground(lipgloss.Color("10"))            // green
	styleWarn   = lipgloss.NewStyle().Foreground(lipgloss.Color("11"))            // yellow
	styleErr    = lipgloss.NewStyle().Foreground(lipgloss.Color("9"))             // red
	// styleSel is applied to the selected row in list views. Uses
	// Background() rather than Reverse() so it composes cleanly with
	// inner styled spans (colored badges, muted node counts) — with
	// Reverse the terminal's per-token style reset breaks the
	// highlight after the first inner span. Callers set an explicit
	// .Width() so the background paints the entire row, not just the
	// visible characters.
	styleSel = lipgloss.NewStyle().
			Foreground(lipgloss.Color("15")). // bright white
			Background(lipgloss.Color("12")). // blue
			Bold(true)
	styleBox     = lipgloss.NewStyle().Border(lipgloss.RoundedBorder()).Padding(0, 1)
	styleActive  = lipgloss.NewStyle().Border(lipgloss.RoundedBorder()).BorderForeground(lipgloss.Color("13")).Padding(0, 1)
	styleHelpKey = lipgloss.NewStyle().Foreground(lipgloss.Color("12")).Bold(true)
	styleHelpTxt = lipgloss.NewStyle().Foreground(lipgloss.Color("8"))
)
