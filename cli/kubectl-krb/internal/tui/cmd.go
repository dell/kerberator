// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

// Package tui implements the interactive bubbletea-based UI for the
// kubectl-krb plugin. Placed in its own subpackage so:
//
//   - the top-level internal/ package doesn't drag in the
//     charmbracelet dep tree for callers that only want CLI subcommands;
//   - the import graph is one-way: internal/tui -> internal (for the
//     shared driver funcs AddTenant, RotateKeytab, ...), never the
//     reverse.
//
// The tui subpackage exposes exactly one cobra factory
// (NewTUICmd) plus a small provider struct (KubeClientProvider) that
// the enclosing CLI passes in so we don't take a hard dep on
// cli-runtime here.
package tui

import (
	"context"
	"fmt"

	"github.com/go-logr/logr"
	"github.com/spf13/cobra"
	"sigs.k8s.io/controller-runtime/pkg/client"
	ctrllog "sigs.k8s.io/controller-runtime/pkg/log"

	tea "github.com/charmbracelet/bubbletea"
)

// KubeClientProvider carries the Kubernetes client + resolved
// namespace the TUI uses. Both are built ONCE, before bubbletea
// starts, and are safe to share across the many goroutines a
// bubbletea Program spawns for tea.Cmd execution.
//
// Rationale: cli-runtime's ConfigFlags is not goroutine-safe. Our
// previous shape (factory func()s called from tea.Cmds) raced on
// every overlapping refresh — an in-flight Init() refresh + a tick
// refresh concurrently mutated ConfigFlags internals and one
// eventually nil-deref'd inside controller-runtime's client.New.
// Pre-building both values sidesteps the race entirely and has the
// bonus of surfacing "kubeconfig missing" as a startup error rather
// than a mid-session panic.
type KubeClientProvider struct {
	Client    client.Client
	Namespace string
}

// NewTUICmd builds the `kubectl krb tui` cobra command. Takes
// factory functions rather than the *ConfigFlags type so this
// package stays a leaf (no import cycle with internal/).
func NewTUICmd(clientFactory func() (client.Client, error), nsFactory func() (string, error)) *cobra.Command {
	cmd := &cobra.Command{
		Use:   "tui",
		Short: "Launch the interactive Kerberator terminal UI",
		Long: "kubectl krb tui — a bubbletea-based interactive interface " +
			"for listing, adding, editing, and deleting Tenants and Principals. " +
			"Every action calls the same shared functions as the parallel CLI " +
			"subcommands (add-tenant, add-principal, rotate-keytab, ...) so what " +
			"you can do here is exactly what you can do at the CLI.",
		Args: cobra.NoArgs,
	}
	cmd.RunE = func(cmd *cobra.Command, args []string) error {
		// Silence controller-runtime's "log.SetLogger was never
		// called" warning — it prints to stderr on the FIRST client
		// operation and would land in the middle of our alt-screen,
		// producing a stray banner in the middle of the screen.
		ctrllog.SetLogger(logr.Discard())

		// Resolve client + namespace up front. Any kubeconfig
		// errors surface here, before we take over the terminal.
		cl, err := clientFactory()
		if err != nil {
			return fmt.Errorf("build k8s client: %w", err)
		}
		ns, err := nsFactory()
		if err != nil {
			return fmt.Errorf("resolve namespace: %w", err)
		}
		return Run(cmd.Context(), KubeClientProvider{Client: cl, Namespace: ns})
	}
	return cmd
}

// Run launches the bubbletea program and blocks until the user
// quits. Exposed as a function (rather than only via NewTUICmd) so
// callers embedding kubectl-krb in another binary can drop straight
// into the TUI without cobra plumbing.
func Run(ctx context.Context, kc KubeClientProvider) error {
	m := newModel(kc)
	p := tea.NewProgram(m, tea.WithAltScreen(), tea.WithContext(ctx))
	if _, err := p.Run(); err != nil {
		return fmt.Errorf("tui: %w", err)
	}
	return nil
}
