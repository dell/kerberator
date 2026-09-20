// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

package internal

import (
	"context"
	"fmt"
	"io"
	"os"
	"os/exec"
	"strings"

	"github.com/spf13/cobra"
	"k8s.io/apimachinery/pkg/types"

	v1alpha1 "github.com/dell/kerberator/operator/api/v1alpha1"
)

// NewEditCmd builds `kubectl krb edit tenant|principal <name>` — a thin
// wrapper around `kubectl edit` scoped to Kerberator's two CRDs.
//
// Why not reimplement the edit-in-place dance ourselves? Because
// kubectl's editor loop is battle-tested (locking, temp-file
// handling, retry-on-conflict, diff display). Reimplementing that in
// a plugin would be days of work for a UX that's already good. We
// just execve `kubectl edit <fully-qualified-resource>` and let the
// user's $EDITOR do the rest.
//
// After the edit returns, we do a client-side re-validate of the
// resulting object so a user who somehow saved an invalid Tenant
// (e.g. dropped the krb5.conf) gets a warning even if the admission
// webhook was permissive.
func NewEditCmd(cf *ConfigFlags) *cobra.Command {
	cmd := &cobra.Command{
		Use:   "edit <tenant|principal> <name>",
		Short: "Edit a Tenant or Principal in-place with $EDITOR",
		Long: "Wraps `kubectl edit` on the Kerberator CRDs. Uses $EDITOR " +
			"or $KUBE_EDITOR, whichever is set (falls back to vi). Post-edit, " +
			"we sanity-check the resulting object client-side and warn if it " +
			"looks off (e.g. mismatched tenant in krb5.conf).",
		Args: cobra.ExactArgs(2),
		Example: `  kubectl krb edit tenant demo
  kubectl krb edit principal uid-2001
  KUBE_EDITOR=code -w kubectl krb edit principal uid-2001`,
	}
	cmd.RunE = func(cmd *cobra.Command, args []string) error {
		return runEdit(cmd.Context(), cmd.OutOrStdout(), cf, args[0], args[1])
	}
	return cmd
}

func runEdit(ctx context.Context, out io.Writer, cf *ConfigFlags, kind, name string) error {
	resource, err := resolveEditResource(kind)
	if err != nil {
		return err
	}
	ns, err := cf.Namespace()
	if err != nil {
		return err
	}

	// Compose the kubectl edit invocation. We pass through the user's
	// --kubeconfig / --context / --namespace where set on our root
	// command; kubectl will pick up any KUBECONFIG env directly.
	args := []string{"edit", resource, name, "-n", ns}
	if cf.KubeConfig != nil && *cf.KubeConfig != "" {
		args = append(args, "--kubeconfig", *cf.KubeConfig)
	}
	if cf.Context != nil && *cf.Context != "" {
		args = append(args, "--context", *cf.Context)
	}

	kubectl := exec.CommandContext(ctx, "kubectl", args...)
	kubectl.Stdin = os.Stdin
	kubectl.Stdout = os.Stdout
	kubectl.Stderr = os.Stderr
	if err := kubectl.Run(); err != nil {
		return fmt.Errorf("kubectl edit: %w", err)
	}

	// Post-edit sanity check for tenant-level foot-guns.
	if kind == "tenant" || kind == "tenants" {
		return warnOnTenantDrift(ctx, out, cf, ns, name)
	}
	return nil
}

func resolveEditResource(kind string) (string, error) {
	switch strings.ToLower(kind) {
	case "tenant", "tenants", "krbtenant", "kt":
		return "tenant.kerberator.dell.com", nil
	case "principal", "principals", "krbprincipal", "kp":
		return "principal.kerberator.dell.com", nil
	default:
		return "", fmt.Errorf("unknown kind %q (want tenant|principal)", kind)
	}
}

// warnOnTenantDrift re-reads the Tenant after edit and runs a subset of
// the ValidateTenantSpec checks. We don't fail the command — the
// operator's admission webhook is authoritative — but we surface a
// warning line so the user notices before their DaemonSet
// reconciles into a bad state.
func warnOnTenantDrift(ctx context.Context, out io.Writer, cf *ConfigFlags, ns, name string) error {
	cl, err := cf.Client()
	if err != nil {
		return nil // best-effort; don't fail the edit for a validate warning
	}
	var r v1alpha1.Tenant
	if err := cl.Get(ctx, types.NamespacedName{Namespace: ns, Name: name}, &r); err != nil {
		return nil
	}
	if err := ValidateTenantSpec(r.Spec); err != nil {
		fmt.Fprintf(out, "warning: %s\n", err.Error())
	}
	return nil
}
