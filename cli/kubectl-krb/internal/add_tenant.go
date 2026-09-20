// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

package internal

import (
	"context"
	"fmt"
	"io"
	"os"
	"strings"

	"github.com/spf13/cobra"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"

	v1alpha1 "github.com/dell/kerberator/operator/api/v1alpha1"
)

// NewAddTenantCmd builds `kubectl krb add-tenant <name>` — the
// imperative counterpart to hand-authoring a Tenant YAML. Prompts (via
// flags, since a CLI can't really prompt from within a pipe) for
// tenant string, daemon image, and krb5.conf content, then applies.
//
// The heavy lifting is factored into `AddTenant` so the TUI wizard
// calls the same code path as the CLI subcommand — guarantees CLI/TUI
// parity and keeps validation in one place.
func NewAddTenantCmd(cf *ConfigFlags) *cobra.Command {
	var (
		tenantStr      string
		image          string
		renewMinutes   int32
		pruneStale     bool
		krb5ConfPath   string
		krb5ConfInline string
		dryRun         bool
	)
	cmd := &cobra.Command{
		Use:   "add-tenant <name>",
		Short: "Scaffold + apply a Tenant CR",
		Args:  cobra.ExactArgs(1),
		Example: `  # Simple: krb5.conf from a file
  kubectl krb add-tenant demo \
      --realm EXAMPLE.COM \
      --image registry.example.com/kerberator-daemon:v0.5.2 \
      --krb5-conf-file ./krb5.conf

  # Inline krb5.conf (multiline; escape newlines with $'\n' in bash)
  kubectl krb add-tenant demo --realm EXAMPLE.COM --image ... \
      --krb5-conf $'[libdefaults]\n  default_realm = EXAMPLE.COM\n[realms]\n  EXAMPLE.COM = { kdc = kdc.example.com }\n'`,
	}
	cmd.Flags().StringVar(&tenantStr, "realm", "", "Kerberos realm string (e.g. EXAMPLE.COM). Uppercase by convention.")
	cmd.Flags().StringVar(&image, "image", "", "Daemon container image (registry/kerberator-daemon:tag).")
	cmd.Flags().Int32Var(&renewMinutes, "renew-minutes", 30, "How often the daemon renews each ccache, in minutes.")
	cmd.Flags().BoolVar(&pruneStale, "prune-stale", true, "If true, daemon prunes ccaches for UIDs no longer in the roster.")
	cmd.Flags().StringVar(&krb5ConfPath, "krb5-conf-file", "", "Path to the krb5.conf to embed. Mutually exclusive with --krb5-conf.")
	cmd.Flags().StringVar(&krb5ConfInline, "krb5-conf", "", "Inline krb5.conf content. Mutually exclusive with --krb5-conf-file.")
	cmd.Flags().BoolVar(&dryRun, "dry-run", false, "Print the Tenant we would apply without touching the API.")
	_ = cmd.MarkFlagRequired("realm")
	_ = cmd.MarkFlagRequired("image")

	cmd.RunE = func(cmd *cobra.Command, args []string) error {
		spec, err := loadTenantSpecFromFlags(tenantStr, image, renewMinutes, pruneStale, krb5ConfPath, krb5ConfInline)
		if err != nil {
			return err
		}
		return runAddTenant(cmd.Context(), cmd.OutOrStdout(), cf, args[0], spec, dryRun)
	}
	return cmd
}

// loadTenantSpecFromFlags builds a validated TenantSpec from the flag
// surface. Split out so the TUI can pass a spec assembled from form
// fields into the same validator.
func loadTenantSpecFromFlags(tenant, image string, renewMinutes int32, pruneStale bool, confPath, confInline string) (v1alpha1.TenantSpec, error) {
	if confPath != "" && confInline != "" {
		return v1alpha1.TenantSpec{}, fmt.Errorf("--krb5-conf-file and --krb5-conf are mutually exclusive")
	}
	conf := confInline
	if confPath != "" {
		b, err := os.ReadFile(confPath)
		if err != nil {
			return v1alpha1.TenantSpec{}, fmt.Errorf("read krb5.conf: %w", err)
		}
		conf = string(b)
	}
	return v1alpha1.TenantSpec{
			Realm: tenant,
			Daemon: v1alpha1.DaemonTemplate{
				Image:        image,
				RenewMinutes: renewMinutes,
				PruneStale:   pruneStale,
				Krb5Conf:     conf,
			},
		}, ValidateTenantSpec(v1alpha1.TenantSpec{
			Realm: tenant,
			Daemon: v1alpha1.DaemonTemplate{
				Image:        image,
				RenewMinutes: renewMinutes,
				PruneStale:   pruneStale,
				Krb5Conf:     conf,
			},
		})
}

// ValidateTenantSpec runs the client-side subset of the admission
// webhook's tenant checks so we surface user errors *before* a
// round-trip. Kept public so the TUI can call it on form-field blur.
//
// Rules mirror what the operator's webhook enforces:
//   - tenant string is non-empty and uppercase-ish (letters+dots+digits)
//   - image is non-empty and looks like a reference (has a slash OR colon)
//   - renewMinutes is > 0
//   - krb5.conf, if provided, mentions the tenant and has a [realms] section
//     (weak but catches obvious cargo-copy mistakes)
func ValidateTenantSpec(spec v1alpha1.TenantSpec) error {
	if strings.TrimSpace(spec.Realm) == "" {
		return fmt.Errorf("tenant string is required")
	}
	if !looksLikeKerberosRealm(spec.Realm) {
		return fmt.Errorf("tenant %q looks wrong: expected uppercase with dots (e.g. EXAMPLE.COM)", spec.Realm)
	}
	if strings.TrimSpace(spec.Daemon.Image) == "" {
		return fmt.Errorf("daemon image is required")
	}
	if !looksLikeImageRef(spec.Daemon.Image) {
		return fmt.Errorf("image %q doesn't look like a container reference (registry/name:tag)", spec.Daemon.Image)
	}
	if spec.Daemon.RenewMinutes <= 0 {
		return fmt.Errorf("renewMinutes must be > 0 (got %d)", spec.Daemon.RenewMinutes)
	}
	if c := spec.Daemon.Krb5Conf; c != "" {
		if !strings.Contains(c, spec.Realm) {
			return fmt.Errorf("krb5.conf does not mention tenant %q (likely a stale template)", spec.Realm)
		}
		if !strings.Contains(c, "[realms]") {
			return fmt.Errorf("krb5.conf is missing the [realms] section (probably not a valid krb5.conf)")
		}
	}
	return nil
}

func looksLikeKerberosRealm(s string) bool {
	// Kerberos realm strings are conventionally uppercase and DNS-domain-shaped.
	// This is intentionally lenient — the KDC has the final say.
	if s != strings.ToUpper(s) {
		return false
	}
	if !strings.ContainsAny(s, "._-") && len(s) < 3 {
		return false
	}
	for _, r := range s {
		if !(r >= 'A' && r <= 'Z') && !(r >= '0' && r <= '9') && r != '.' && r != '-' && r != '_' {
			return false
		}
	}
	return true
}

func looksLikeImageRef(s string) bool {
	// Accept anything that has a colon (tag) or a slash (registry/name).
	// The image resolver will complain in more detail at pull time.
	return strings.Contains(s, ":") || strings.Contains(s, "/")
}

// runAddTenant is the CLI-side driver. TUI callers use AddTenant directly
// with a pre-built TenantSpec.
func runAddTenant(ctx context.Context, out io.Writer, cf *ConfigFlags, name string, spec v1alpha1.TenantSpec, dryRun bool) error {
	cl, err := cf.Client()
	if err != nil {
		return err
	}
	ns, err := cf.Namespace()
	if err != nil {
		return err
	}
	tenant := &v1alpha1.Tenant{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: ns},
		Spec:       spec,
	}
	if dryRun {
		fmt.Fprintln(out, "# --- would apply the following ---")
		printTenantYAML(out, tenant)
		return nil
	}
	if err := AddTenant(ctx, cl, tenant); err != nil {
		return err
	}
	fmt.Fprintf(out, "tenant.kerberator.dell.com/%s created\n", tenant.Name)
	return nil
}

// AddTenant is the shared, client-driven creator that both the CLI
// subcommand and the TUI wizard use. It's a straight Create; the
// caller is responsible for having already validated the spec.
func AddTenant(ctx context.Context, cl client.Client, tenant *v1alpha1.Tenant) error {
	if err := cl.Create(ctx, tenant); err != nil {
		if apierrors.IsAlreadyExists(err) {
			return fmt.Errorf("tenant %s/%s already exists (use `kubectl krb edit tenant %s` to modify)",
				tenant.Namespace, tenant.Name, tenant.Name)
		}
		return fmt.Errorf("create tenant: %w", err)
	}
	return nil
}

// printTenantYAML is add-principal's printYAML for Tenant objects. Kept
// small: multi-line krb5Conf is emitted with the block literal (|)
// scalar so the operator side round-trips cleanly.
func printTenantYAML(out io.Writer, r *v1alpha1.Tenant) {
	fmt.Fprintf(out, "apiVersion: kerberator.dell.com/v1alpha1\nkind: Tenant\n")
	fmt.Fprintf(out, "metadata:\n  name: %s\n  namespace: %s\n", r.Name, r.Namespace)
	fmt.Fprintf(out, "spec:\n  realm: %s\n  daemon:\n", r.Spec.Realm)
	fmt.Fprintf(out, "    image: %s\n", r.Spec.Daemon.Image)
	fmt.Fprintf(out, "    renewMinutes: %d\n", r.Spec.Daemon.RenewMinutes)
	fmt.Fprintf(out, "    pruneStale: %t\n", r.Spec.Daemon.PruneStale)
	if r.Spec.Daemon.Krb5Conf != "" {
		fmt.Fprintln(out, "    krb5Conf: |")
		for _, line := range strings.Split(strings.TrimRight(r.Spec.Daemon.Krb5Conf, "\n"), "\n") {
			fmt.Fprintf(out, "      %s\n", line)
		}
	}
}
