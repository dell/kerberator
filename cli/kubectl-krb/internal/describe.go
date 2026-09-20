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
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"

	v1alpha1 "github.com/dell/kerberator/operator/api/v1alpha1"
	sharedevents "github.com/dell/kerberator/shared/events"
)

// NewDescribeCmd builds `kubectl krb describe tenant|principal <name>`.
// This is a higher-signal cousin of `kubectl describe`: for Tenants it
// enumerates every owned child (DaemonSet + roster CM + keytab Secret +
// events RBAC) and each Principal's mint state; for Principals it lays out
// the per-node status.nodes[] table joined with recent daemon Events.
func NewDescribeCmd(cf *ConfigFlags) *cobra.Command {
	cmd := &cobra.Command{
		Use:       "describe <tenant|principal> <name>",
		Short:     "Deep-dive on a Tenant or Principal",
		Args:      cobra.ExactArgs(2),
		ValidArgs: []string{"tenant", "principal"},
	}
	cmd.RunE = func(cmd *cobra.Command, args []string) error {
		kind, name := args[0], args[1]
		ctx := cmd.Context()
		cl, err := cf.Client()
		if err != nil {
			return err
		}
		ns, err := cf.Namespace()
		if err != nil {
			return err
		}
		switch kind {
		case "tenant", "tenants", "krbtenant", "kt":
			return describeTenant(ctx, cmd.OutOrStdout(), cl, ns, name)
		case "principal", "principals", "krbprincipal", "kp":
			return describePrincipal(ctx, cmd.OutOrStdout(), cl, ns, name)
		default:
			return fmt.Errorf("unknown kind %q (want tenant|principal)", kind)
		}
	}
	return cmd
}

func describeTenant(ctx context.Context, out io.Writer, cl client.Client, ns, name string) error {
	var r v1alpha1.Tenant
	if err := cl.Get(ctx, types.NamespacedName{Namespace: ns, Name: name}, &r); err != nil {
		return fmt.Errorf("get tenant %s/%s: %w", ns, name, err)
	}

	// Owned children by naming convention (matches the operator's tenant_helpers.go).
	dsName := name + "-kerberator-daemon"
	cmName := name + "-roster"
	secretName := name + "-keytabs"
	eventsRole := name + "-kerberator-daemon-events"

	// Tenant CR (the K8s object) vs the Kerberos realm string (the
	// spec.tenant value, e.g. EXAMPLE.COM). Keep these labels
	// distinct so operators reading `describe` output don't
	// mistake the ns/name for the Kerberos realm.
	fmt.Fprintf(out, "Tenant CR:         %s/%s\n", r.Namespace, r.Name)
	fmt.Fprintf(out, "  Kerberos realm: %s\n", r.Spec.Realm)
	fmt.Fprintf(out, "  Age:            %s\n", ageStr(r.CreationTimestamp.Time))
	fmt.Fprintf(out, "  Nodes ready:    %s\n", r.Status.ReadySummary)
	fmt.Fprintf(out, "  Principals:        %d\n", r.Status.PrincipalCount)
	fmt.Fprintf(out, "  Roster hash:    %s\n", r.Status.RosterHash)
	fmt.Fprintf(out, "  Conditions:\n")
	for _, c := range r.Status.Conditions {
		fmt.Fprintf(out, "    - %s=%s  %s: %s\n", c.Type, c.Status, c.Reason, c.Message)
	}

	fmt.Fprintln(out, "")
	fmt.Fprintln(out, "Owned children (all ownerRef'd to this Tenant):")
	fmt.Fprintf(out, "  DaemonSet:   %s\n", dsName)
	fmt.Fprintf(out, "  ConfigMap:   %s   (keys: users.roster, krb5.conf)\n", cmName)
	fmt.Fprintf(out, "  Secret:      %s   (one entry per principal keytab)\n", secretName)
	fmt.Fprintf(out, "  Role/RB:     %s   (create,patch on events)\n", eventsRole)

	// Principals for this tenant.
	var principals v1alpha1.PrincipalList
	_ = cl.List(ctx, &principals, client.InNamespace(ns))
	var mine []v1alpha1.Principal
	for _, t := range principals.Items {
		if t.Spec.TenantRef.Name == name {
			mine = append(mine, t)
		}
	}
	sort.Slice(mine, func(i, j int) bool { return mine[i].Spec.UID < mine[j].Spec.UID })

	fmt.Fprintln(out, "")
	fmt.Fprintln(out, "Principals:")
	tw := tabwriter.NewWriter(out, 2, 0, 2, ' ', 0)
	fmt.Fprintln(tw, "  UID\tPRINCIPAL\tKEYTAB SECRET/KEY\tKVNO\tREADY\tNODES")
	for _, t := range mine {
		secret := "<none>"
		if t.Spec.KeytabSecretRef != nil {
			secret = fmt.Sprintf("%s/%s", t.Spec.KeytabSecretRef.Name, t.Spec.KeytabSecretRef.Key)
		}
		kvno := "-"
		if t.Status.KvnoFromSecret != 0 {
			kvno = fmt.Sprintf("%d", t.Status.KvnoFromSecret)
		}
		fmt.Fprintf(tw, "  %d\t%s\t%s\t%s\t%s\t%d/%d\n",
			t.Spec.UID, t.Spec.Principal, secret, kvno, readyOf(t.Status.Conditions),
			t.Status.Tenant.ReadyNodes, t.Status.Tenant.DesiredNodes)
	}
	_ = tw.Flush()
	return nil
}

func describePrincipal(ctx context.Context, out io.Writer, cl client.Client, ns, name string) error {
	var t v1alpha1.Principal
	if err := cl.Get(ctx, types.NamespacedName{Namespace: ns, Name: name}, &t); err != nil {
		return fmt.Errorf("get principal %s/%s: %w", ns, name, err)
	}

	fmt.Fprintf(out, "Principal CR:      %s/%s\n", t.Namespace, t.Name)
	fmt.Fprintf(out, "  UID:          %d\n", t.Spec.UID)
	fmt.Fprintf(out, "  Principal:    %s\n", t.Spec.Principal)
	// "Tenant CR ref" mirrors the spec field name (spec.tenantRef.name)
	// and makes it clear this is a pointer to a Tenant CR by name,
	// not the Kerberos realm string (which lives on the parent
	// Tenant.spec.tenant — surface it below).
	fmt.Fprintf(out, "  Tenant CR ref: %s\n", t.Spec.TenantRef.Name)
	if t.Spec.KeytabSecretRef != nil {
		fmt.Fprintf(out, "  Keytab:       %s/%s\n", t.Spec.KeytabSecretRef.Name, t.Spec.KeytabSecretRef.Key)
	}
	// KVNO + enctypes as observed by the operator on the last
	// successful reconcile of the referenced Secret. Zero means
	// "not observed yet or Secret was empty/unparseable" — the
	// reconciler retries on the next Secret update or generic
	// requeue, so this typically self-heals within seconds.
	if t.Status.KvnoFromSecret != 0 {
		fmt.Fprintf(out, "  KVNO:         %d", t.Status.KvnoFromSecret)
		if t.Status.KeytabObservedAt != nil {
			fmt.Fprintf(out, "   (observed %s)", ageStr(t.Status.KeytabObservedAt.Time))
		}
		fmt.Fprintln(out, "")
		if t.Status.EnctypesFromSecret != "" {
			fmt.Fprintf(out, "  Enctypes:     %s\n", t.Status.EnctypesFromSecret)
		}
	} else if t.Spec.KeytabSecretRef != nil {
		fmt.Fprintf(out, "  KVNO:         <not observed>  (Secret missing, empty, or unparseable)\n")
	}
	fmt.Fprintf(out, "  Age:          %s\n", ageStr(t.CreationTimestamp.Time))
	fmt.Fprintf(out, "  Finalizers:   %v\n", t.Finalizers)
	if !t.DeletionTimestamp.IsZero() {
		fmt.Fprintf(out, "  Deleting:     since %s\n", ageStr(t.DeletionTimestamp.Time))
	}

	fmt.Fprintln(out, "")
	fmt.Fprintln(out, "Per-node status:")
	tw := tabwriter.NewWriter(out, 2, 0, 2, ' ', 0)
	fmt.Fprintln(tw, "  NODE\tDAEMON READY\tREASON\tOBSERVED\tMESSAGE")
	nodes := append([]v1alpha1.NodeStatus{}, t.Status.Nodes...)
	sort.Slice(nodes, func(i, j int) bool { return nodes[i].Name < nodes[j].Name })
	for _, n := range nodes {
		fmt.Fprintf(tw, "  %s\t%v\t%s\t%s\t%s\n",
			n.Name, n.DaemonPodReady, n.Reason, ageStr(n.LastObserved.Time), truncate(n.Message, 60))
	}
	_ = tw.Flush()

	// Recent kerberator daemon Events referencing this UID in this
	// namespace. We look them up cross-namespace-style using the
	// annotation (structured, doesn't need to grep messages).
	var evs corev1.EventList
	if err := cl.List(ctx, &evs, client.InNamespace(ns)); err == nil {
		fmt.Fprintln(out, "")
		fmt.Fprintln(out, "Recent daemon Events for this UID:")
		matched := 0
		for _, e := range evs.Items {
			if e.Annotations[sharedevents.UIDAnnotationKey] != fmt.Sprintf("%d", t.Spec.UID) {
				continue
			}
			fmt.Fprintf(out, "  [%s] %s on %s: %s\n",
				ageStr(e.LastTimestamp.Time), e.Reason,
				e.InvolvedObject.Name, truncate(e.Message, 120))
			matched++
			if matched >= 10 {
				break
			}
		}
		if matched == 0 {
			fmt.Fprintln(out, "  (none — either the daemon hasn't reported yet or events have been GC'd)")
		}
	}
	return nil
}
