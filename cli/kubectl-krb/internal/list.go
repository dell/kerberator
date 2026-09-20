// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

package internal

import (
	"context"
	"fmt"
	"io"
	"sort"
	"text/tabwriter"

	"github.com/spf13/cobra"

	v1alpha1 "github.com/dell/kerberator/operator/api/v1alpha1"
)

// NewListCmd builds `kubectl krb list` — a single-table view of every
// Tenant + its Principals in the current namespace (or across all
// namespaces with -A). Optimized for the "what does this cluster
// have going on Kerberos-wise" question that `kubectl get tenants`
// only partially answers.
func NewListCmd(cf *ConfigFlags) *cobra.Command {
	allNs := &AllNamespacesFlag{}
	var tenantFilter string

	cmd := &cobra.Command{
		Use:   "list",
		Short: "List Tenants and Principals",
		Long: "" +
			"List every Kerberator Tenant and, indented under each, every Principal " +
			"that references it. Pass --tenant <name> to focus on one tenant.",
		Example: "  kubectl krb list -A\n" +
			"  kubectl krb list -n kerberator --tenant demo",
		RunE: func(cmd *cobra.Command, _ []string) error {
			ctx := cmd.Context()
			return runList(ctx, cmd.OutOrStdout(), cf, allNs.Value, tenantFilter)
		},
	}
	allNs.AddTo(cmd.Flags())
	cmd.Flags().StringVar(&tenantFilter, "tenant", "", "Only show one Tenant and its Principals.")
	return cmd
}

func runList(ctx context.Context, out io.Writer, cf *ConfigFlags, allNs bool, tenantFilter string) error {
	cl, err := cf.Client()
	if err != nil {
		return err
	}
	opts, err := listOpts(cf, allNs)
	if err != nil {
		return err
	}

	var tenants v1alpha1.TenantList
	if err := cl.List(ctx, &tenants, opts...); err != nil {
		return fmt.Errorf("list tenants: %w", err)
	}
	var principals v1alpha1.PrincipalList
	if err := cl.List(ctx, &principals, opts...); err != nil {
		return fmt.Errorf("list principals: %w", err)
	}

	// Group principals by (namespace, tenantRef.name) so the printer can
	// walk tenants and pluck out its own principals in one lookup.
	byTenant := map[string][]v1alpha1.Principal{}
	for _, t := range principals.Items {
		if t.Spec.TenantRef.Name == "" {
			continue
		}
		key := t.Namespace + "/" + t.Spec.TenantRef.Name
		byTenant[key] = append(byTenant[key], t)
	}

	// Stable sort so the output is diff-friendly for scripts.
	sort.Slice(tenants.Items, func(i, j int) bool {
		a, b := tenants.Items[i], tenants.Items[j]
		if a.Namespace != b.Namespace {
			return a.Namespace < b.Namespace
		}
		return a.Name < b.Name
	})

	tw := tabwriter.NewWriter(out, 0, 0, 2, ' ', 0)
	defer tw.Flush()
	fmt.Fprintln(tw, "NAMESPACE\tKIND\tNAME\tTENANT/UID\tREADY\tNODES\tAGE")
	printed := 0
	for _, r := range tenants.Items {
		if tenantFilter != "" && r.Name != tenantFilter {
			continue
		}
		fmt.Fprintf(tw, "%s\tTenant\t%s\t%s\t%s\t%s\t%s\n",
			r.Namespace, r.Name, r.Spec.Realm, readyOf(r.Status.Conditions), r.Status.ReadySummary, ageStr(r.CreationTimestamp.Time))
		printed++

		key := r.Namespace + "/" + r.Name
		ts := byTenant[key]
		sort.Slice(ts, func(i, j int) bool { return ts[i].Spec.UID < ts[j].Spec.UID })
		for _, t := range ts {
			fmt.Fprintf(tw, "%s\t  Principal\t%s\t%d\t%s\t%s\t%s\n",
				t.Namespace, t.Name, t.Spec.UID, readyOf(t.Status.Conditions),
				fmt.Sprintf("%d/%d", t.Status.Tenant.ReadyNodes, t.Status.Tenant.DesiredNodes),
				ageStr(t.CreationTimestamp.Time))
			printed++
		}
	}
	if printed == 0 {
		fmt.Fprintln(out, "No Kerberator resources found.")
	}
	return nil
}
