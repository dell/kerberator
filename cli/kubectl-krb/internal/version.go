// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright (c) 2026 Dell Technologies

package internal

import (
	"fmt"

	"github.com/spf13/cobra"

	"github.com/dell/kerberator/shared/version"
)

// NewVersionCmd builds `kubectl krb version`. Trivial but useful:
// when a bug report comes in from the field, the first ask is
// always "which version". The value is stamped at build time via
// -ldflags "-X github.com/dell/kerberator/shared/version.Version=...".
func NewVersionCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "version",
		Short: "Print the plugin version",
		Run: func(cmd *cobra.Command, _ []string) {
			fmt.Fprintf(cmd.OutOrStdout(), "kubectl-krb %s\n", version.String())
		},
	}
}
