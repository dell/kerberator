// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

// Command kubectl-krb is the Kerberator kubectl plugin. Installed as
// `kubectl-krb` on $PATH, invocable as `kubectl krb <subcommand>`.
//
// Subcommands (see the per-command Go files under internal/):
//
//	kubectl krb list [--tenant <name>]         — list Tenants + Principals in one table
//	kubectl krb describe tenant <name>         — deep-dive on one Tenant
//	kubectl krb describe principal <name>        — deep-dive on one Principal (per-node status)
//	kubectl krb add-tenant <name>              — scaffold + apply a Tenant CR
//	kubectl krb add-principal <tenant> <uid>      — scaffold + apply a Principal CR + keytab Secret
//	kubectl krb rotate-keytab <principal>        — replace a Principal's keytab + force re-mint
//	kubectl krb kvno [principal]                  — show KVNO + enctypes in each Principal's Secret
//	kubectl krb edit <tenant|principal> <name>    — edit in-place with $EDITOR
//	kubectl krb disable <tenant|principal> <name> — soft-revoke (prune ccaches, keep the CR)
//	kubectl krb enable <tenant|principal> <name>  — restore a soft-revoked Tenant/Principal
//	kubectl krb drain-node <node>             — cordon a node + delete its kerberator-daemon pod
//	kubectl krb node-labels                   — list Node labels usable in Principal nodeSelectors
//	kubectl krb version                       — print the plugin version
//	kubectl krb events [--uid <n>]            — tail the Kerberos* Events across all daemons
//	kubectl krb tui                           — interactive terminal UI (Tenants/Principals CRUD)
//
// Design goals:
//   - Zero surprises: every subcommand is a thin wrapper over kubectl
//     verbs (get/apply/patch/delete). We do not shell out to kubectl;
//     we use client-go directly so the plugin composes with $KUBECONFIG,
//     --context, --namespace like any first-class kubectl plugin.
//   - No new state: every operation is a stateless CR mutation. If
//     the plugin is uninstalled tomorrow, everything it created stays.
package main

import (
	"os"

	"github.com/spf13/cobra"

	"github.com/dell/kerberator/cli/kubectl-krb/internal"
	"github.com/dell/kerberator/cli/kubectl-krb/internal/tui"
)

func main() {
	root := &cobra.Command{
		Use:           "kubectl-krb",
		Short:         "Kerberator kubectl plugin",
		Long:          "kubectl-krb — inspect and mutate Kerberator Tenants and Principals.",
		SilenceUsage:  true,
		SilenceErrors: true,
	}

	// Standard kubectl-style flags: --namespace / -n, --kubeconfig, --context.
	// cli-runtime's ConfigFlags does this for us; every subcommand shares the
	// same configuration surface.
	cf := internal.NewConfigFlags()
	cf.AddFlags(root.PersistentFlags())

	root.AddCommand(
		internal.NewListCmd(cf),
		internal.NewDescribeCmd(cf),
		internal.NewAddTenantCmd(cf),
		internal.NewAddPrincipalCmd(cf),
		internal.NewRotateKeytabCmd(cf),
		internal.NewKvnoCmd(cf),
		internal.NewEditCmd(cf),
		internal.NewDisableCmd(cf),
		internal.NewEnableCmd(cf),
		internal.NewNodeLabelsCmd(cf),
		internal.NewDrainNodeCmd(cf),
		internal.NewEventsCmd(cf),
		tui.NewTUICmd(cf.Client, cf.Namespace),
		internal.NewVersionCmd(),
	)

	if err := root.Execute(); err != nil {
		if _, printed := root.ErrOrStderr().(*os.File); printed {
			root.PrintErrln("error:", err.Error())
		}
		os.Exit(1)
	}
}
