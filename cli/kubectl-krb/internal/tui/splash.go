// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

package tui

import (
	"strings"

	"github.com/charmbracelet/lipgloss"

	"github.com/dell/kerberator/shared/version"
)

// splashArt is the ASCII logo shown at TUI startup. Kept small
// enough to fit in an 80-column terminal — 6 rows × ~72 cols. The
// block-drawing glyphs render cleanly in every terminal we've
// tested (WezTerm, iTerm2, Windows Terminal, GNOME Terminal).
const splashArt = `
██╗  ██╗███████╗██████╗ ██████╗ ███████╗██████╗  █████╗ ████████╗ ██████╗ ██████╗
██║ ██╔╝██╔════╝██╔══██╗██╔══██╗██╔════╝██╔══██╗██╔══██╗╚══██╔══╝██╔═══██╗██╔══██╗
█████╔╝ █████╗  ██████╔╝██████╔╝█████╗  ██████╔╝███████║   ██║   ██║   ██║██████╔╝
██╔═██╗ ██╔══╝  ██╔══██╗██╔══██╗██╔══╝  ██╔══██╗██╔══██║   ██║   ██║   ██║██╔══██╗
██║  ██╗███████╗██║  ██║██████╔╝███████╗██║  ██║██║  ██║   ██║   ╚██████╔╝██║  ██║
╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝╚═════╝ ╚══════╝╚═╝  ╚═╝╚═╝  ╚═╝   ╚═╝    ╚════╝ ╚═╝  ╚═╝`

const splashTagline = "Per-identity Kerberos ticket-cache management for Kubernetes nodes"

// styleSplashLogo is the base style for the ASCII logo — magenta
// like our title bar, with slight bold.
var (
	styleSplashLogo = lipgloss.NewStyle().Foreground(lipgloss.Color("13")).Bold(true)
	styleSplashHint = lipgloss.NewStyle().Foreground(lipgloss.Color("8")).Italic(true)
)

// viewSplash renders the startup splash screen centered in the
// available terminal. Any key transitions to the main list.
func (m appModel) viewSplash() string {
	if m.width < 20 || m.height < 10 {
		// Terminal too small — degrade gracefully.
		return "kerberator " + version.Version + "\n\nPress any key to continue."
	}

	logo := styleSplashLogo.Render(strings.TrimLeft(splashArt, "\n"))
	tagline := styleAccent.Render(splashTagline)
	ver := styleMuted.Render("version " + version.Version)
	hint := styleSplashHint.Render("[ press any key to continue ]")

	block := lipgloss.JoinVertical(lipgloss.Center,
		logo,
		"",
		tagline,
		ver,
		"",
		"",
		hint,
	)

	// Center the whole block in the terminal.
	return lipgloss.Place(m.width, m.height,
		lipgloss.Center, lipgloss.Center,
		block)
}
