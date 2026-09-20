// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

package internal

import (
	"context"
	"fmt"
	"io"

	"github.com/spf13/cobra"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"

	v1alpha1 "github.com/dell/kerberator/operator/api/v1alpha1"
)

// NewDisableCmd builds `kubectl krb disable <tenant|principal> <name>` —
// flips spec.disabled=true on the target. Effect (per v0.7.0):
//
//	principal: excluded from Tenant's aggregated roster on next
//	        reconcile; daemon prunes on-node ccache on next poll
//	        (respecting Tenant.spec.daemon.pruneStale).
//	tenant : operator aggregates an EMPTY roster for the whole tenant;
//	        every child principal is effectively disabled at once.
//
// Non-destructive by design: CR, Secret, audit history all stay
// intact. Use `kubectl krb enable` to restore.
func NewDisableCmd(cf *ConfigFlags) *cobra.Command {
	var reason string
	cmd := &cobra.Command{
		Use:   "disable <tenant|principal> <name>",
		Short: "Soft-revoke a Tenant or Principal (excludes from roster; on-node ccache is pruned)",
		Args:  cobra.ExactArgs(2),
		Example: `  # Suspend a principal while investigating a suspected leak
  kubectl krb disable principal uid-2001 --reason compromised

  # Bring a tenant down for maintenance
  kubectl krb disable tenant prod

  # Restore later
  kubectl krb enable principal uid-2001`,
	}
	cmd.Flags().StringVar(&reason, "reason", "",
		"Free-form label surfaced on Ready condition (compromised|maintenance|audit-hold recommended). Ignored for Tenants — tenants take no reason today.")

	cmd.RunE = func(cmd *cobra.Command, args []string) error {
		return runDisableToggle(cmd.Context(), cmd.OutOrStdout(), cf, args[0], args[1], true, reason)
	}
	return cmd
}

// NewEnableCmd is disable's counterpart: flips spec.disabled=false.
// Emits nothing on a no-op (principal already enabled) — matches
// `kubectl` idiom that these mutations are declarative.
func NewEnableCmd(cf *ConfigFlags) *cobra.Command {
	cmd := &cobra.Command{
		Use:   "enable <tenant|principal> <name>",
		Short: "Restore a soft-revoked Tenant or Principal",
		Args:  cobra.ExactArgs(2),
		Example: "  kubectl krb enable principal uid-2001\n" +
			"  kubectl krb enable tenant prod",
	}
	cmd.RunE = func(cmd *cobra.Command, args []string) error {
		return runDisableToggle(cmd.Context(), cmd.OutOrStdout(), cf, args[0], args[1], false, "")
	}
	return cmd
}

// runDisableToggle is the shared driver both `disable` and `enable`
// use. Flipping the bool is idempotent — running `disable` on an
// already-disabled Principal is a no-op that still prints a friendly
// summary line. Same for `enable`.
func runDisableToggle(ctx context.Context, out io.Writer, cf *ConfigFlags, kind, name string, disable bool, reason string) error {
	cl, err := cf.Client()
	if err != nil {
		return err
	}
	ns, err := cf.Namespace()
	if err != nil {
		return err
	}
	return ToggleDisabled(ctx, cl, out, ns, kind, name, disable, reason)
}

// ToggleDisabled is the shared driver used by BOTH the CLI
// subcommands and the TUI's `x` keybinding. Kept public in
// `internal` so the tui subpackage can call it (avoids duplicating
// the mutation logic and its subtle "no-op if already in target
// state" behavior).
func ToggleDisabled(ctx context.Context, cl client.Client, out io.Writer, ns, kind, name string, disable bool, reason string) error {
	switch normalizeKind(kind) {
	case "principal":
		return togglePrincipalDisabled(ctx, cl, out, ns, name, disable, reason)
	case "tenant":
		return toggleTenantDisabled(ctx, cl, out, ns, name, disable)
	default:
		return fmt.Errorf("unknown kind %q (want tenant|principal)", kind)
	}
}

func togglePrincipalDisabled(ctx context.Context, cl client.Client, out io.Writer, ns, name string, disable bool, reason string) error {
	var t v1alpha1.Principal
	if err := cl.Get(ctx, types.NamespacedName{Namespace: ns, Name: name}, &t); err != nil {
		if apierrors.IsNotFound(err) {
			return fmt.Errorf("principal %s/%s not found", ns, name)
		}
		return fmt.Errorf("get principal: %w", err)
	}
	// No-op fast-path: already in the target state.
	if t.Spec.Disabled == disable {
		if disable {
			fmt.Fprintf(out, "principal %s/%s already disabled (no change)\n", ns, name)
		} else {
			fmt.Fprintf(out, "principal %s/%s already enabled (no change)\n", ns, name)
		}
		return nil
	}
	t.Spec.Disabled = disable
	if disable {
		t.Spec.DisabledReason = reason // may be empty; that's fine
	} else {
		t.Spec.DisabledReason = "" // clear on enable so stale reasons don't linger
	}
	if err := cl.Update(ctx, &t); err != nil {
		return fmt.Errorf("update principal: %w", err)
	}
	if disable {
		if reason != "" {
			fmt.Fprintf(out, "principal %s/%s disabled (reason: %s) — operator will prune on-node ccaches on next reconcile\n",
				ns, name, reason)
		} else {
			fmt.Fprintf(out, "principal %s/%s disabled — operator will prune on-node ccaches on next reconcile\n",
				ns, name)
		}
	} else {
		fmt.Fprintf(out, "principal %s/%s enabled — operator will re-add to Tenant roster on next reconcile\n",
			ns, name)
	}
	return nil
}

func toggleTenantDisabled(ctx context.Context, cl client.Client, out io.Writer, ns, name string, disable bool) error {
	var r v1alpha1.Tenant
	if err := cl.Get(ctx, types.NamespacedName{Namespace: ns, Name: name}, &r); err != nil {
		if apierrors.IsNotFound(err) {
			return fmt.Errorf("tenant %s/%s not found", ns, name)
		}
		return fmt.Errorf("get tenant: %w", err)
	}
	if r.Spec.Disabled == disable {
		if disable {
			fmt.Fprintf(out, "tenant %s/%s already disabled (no change)\n", ns, name)
		} else {
			fmt.Fprintf(out, "tenant %s/%s already enabled (no change)\n", ns, name)
		}
		return nil
	}
	r.Spec.Disabled = disable
	if err := cl.Update(ctx, &r); err != nil {
		return fmt.Errorf("update tenant: %w", err)
	}
	if disable {
		fmt.Fprintf(out, "tenant %s/%s disabled — operator will aggregate an empty roster and daemon will prune every ccache on next poll\n",
			ns, name)
	} else {
		fmt.Fprintf(out, "tenant %s/%s enabled — operator will re-aggregate the roster from active Principals on next reconcile\n",
			ns, name)
	}
	return nil
}

// SetPrincipalNodeSelector replaces spec.nodeSelector on a Principal.
// Pass an empty (or nil) map to clear the selector entirely. Same
// shared-driver shape as ToggleDisabled — usable by both a future
// CLI subcommand and the TUI's selector form.
//
// No admission-webhook validation is performed client-side today
// (unlike Tenant which has more shape to validate). Server-side
// admission still applies via the operator's webhook if
// configured.
func SetPrincipalNodeSelector(ctx context.Context, cl client.Client, ns, name string, selector map[string]string) error {
	var t v1alpha1.Principal
	if err := cl.Get(ctx, types.NamespacedName{Namespace: ns, Name: name}, &t); err != nil {
		if apierrors.IsNotFound(err) {
			return fmt.Errorf("principal %s/%s not found", ns, name)
		}
		return fmt.Errorf("get principal: %w", err)
	}
	// Normalize: empty input clears the field completely rather
	// than storing an empty map (schemas differ on how they
	// serialize {}). Both mean the same thing but "no field"
	// matches what a fresh Principal looks like.
	if len(selector) == 0 {
		t.Spec.NodeSelector = nil
	} else {
		t.Spec.NodeSelector = selector
	}
	if err := cl.Update(ctx, &t); err != nil {
		return fmt.Errorf("update principal: %w", err)
	}
	return nil
}

func normalizeKind(kind string) string {
	switch kind {
	case "tenant", "tenants", "krbtenant", "kt":
		return "tenant"
	case "principal", "principals", "krbprincipal", "kp":
		return "principal"
	default:
		return kind
	}
}
