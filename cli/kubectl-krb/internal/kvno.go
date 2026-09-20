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

// NewKvnoCmd builds `kubectl krb kvno [<principal>]` — shows the KVNO
// and enctypes the operator observed in each Principal's referenced
// Keytab Secret on the last successful reconcile.
//
// With no argument, prints a single table of every Principal in the
// current namespace. Useful for spotting kvno drift at a glance
// after re-exporting keytabs on the KDC. With a Principal
// name, prints a focused single-principal view including recent
// mint-failure events (drift signal).
//
// The data comes from status.kvnoFromSecret / enctypesFromSecret,
// which the operator's PrincipalReconciler populates by reading the
// Secret and parsing its keytab bytes. No CLI-side Secret access
// or Kerberos library required.
func NewKvnoCmd(cf *ConfigFlags) *cobra.Command {
	cmd := &cobra.Command{
		Use:   "kvno [principal]",
		Short: "Show KVNO + enctypes for Principal keytab Secrets",
		Long: `Displays the KVNO (Kerberos key version number) and encryption types
present in each Principal's referenced Keytab Secret, as observed by the
operator on the last successful reconcile.

Use this to detect drift between what your KDC (e.g. FreeIPA) has
minted and what the operator sees in the K8s Secret — a common
failure mode after a manual ipa-getkeytab that wasn't followed by
` + "`kubectl krb rotate-keytab`" + `. The daemon's next kinit will fail with
"Preauthentication failed" once the KDC bumps kvno past what the
Secret holds.

Examples:
  kubectl krb kvno                    # all Principals in current namespace
  kubectl krb kvno uid-2001           # focused view of one Principal
  kubectl krb -n kerberator-demo kvno`,
		Args: cobra.MaximumNArgs(1),
	}
	cmd.RunE = func(cmd *cobra.Command, args []string) error {
		ctx := cmd.Context()
		cl, err := cf.Client()
		if err != nil {
			return err
		}
		ns, err := cf.Namespace()
		if err != nil {
			return err
		}
		if len(args) == 1 {
			return kvnoOne(ctx, cmd.OutOrStdout(), cl, ns, args[0])
		}
		return kvnoAll(ctx, cmd.OutOrStdout(), cl, ns)
	}
	return cmd
}

func kvnoAll(ctx context.Context, out io.Writer, cl client.Client, ns string) error {
	var list v1alpha1.PrincipalList
	if err := cl.List(ctx, &list, client.InNamespace(ns)); err != nil {
		return fmt.Errorf("list principals in %s: %w", ns, err)
	}
	principals := append([]v1alpha1.Principal{}, list.Items...)
	sort.Slice(principals, func(i, j int) bool { return principals[i].Spec.UID < principals[j].Spec.UID })

	if len(principals) == 0 {
		fmt.Fprintf(out, "no Principals in namespace %q\n", ns)
		return nil
	}

	tw := tabwriter.NewWriter(out, 2, 0, 2, ' ', 0)
	fmt.Fprintln(tw, "NAME\tUID\tPRINCIPAL\tKVNO\tOBSERVED\tENCTYPES")
	for _, u := range principals {
		kvno := "-"
		if u.Status.KvnoFromSecret != 0 {
			kvno = fmt.Sprintf("%d", u.Status.KvnoFromSecret)
		}
		observed := "-"
		if u.Status.KeytabObservedAt != nil {
			observed = ageStr(u.Status.KeytabObservedAt.Time)
		}
		enctypes := "-"
		if u.Status.EnctypesFromSecret != "" {
			enctypes = u.Status.EnctypesFromSecret
		}
		fmt.Fprintf(tw, "%s\t%d\t%s\t%s\t%s\t%s\n",
			u.Name, u.Spec.UID, u.Spec.Principal, kvno, observed, enctypes)
	}
	return tw.Flush()
}

func kvnoOne(ctx context.Context, out io.Writer, cl client.Client, ns, name string) error {
	var u v1alpha1.Principal
	if err := cl.Get(ctx, types.NamespacedName{Namespace: ns, Name: name}, &u); err != nil {
		return fmt.Errorf("get principal %s/%s: %w", ns, name, err)
	}

	fmt.Fprintf(out, "Principal:      %s/%s\n", u.Namespace, u.Name)
	fmt.Fprintf(out, "UID:       %d\n", u.Spec.UID)
	fmt.Fprintf(out, "Principal: %s\n", u.Spec.Principal)
	if u.Spec.KeytabSecretRef != nil {
		fmt.Fprintf(out, "Secret:    %s/%s  (key: %s)\n",
			u.Namespace, u.Spec.KeytabSecretRef.Name, u.Spec.KeytabSecretRef.Key)
	} else {
		fmt.Fprintln(out, "Secret:    <none>  (spec.keytabSecretRef is unset)")
	}
	fmt.Fprintln(out, "")

	if u.Status.KvnoFromSecret == 0 {
		fmt.Fprintln(out, "KVNO:      <not observed>")
		fmt.Fprintln(out, "  The operator has not yet observed valid keytab bytes in the")
		fmt.Fprintln(out, "  referenced Secret. Common causes: Secret not yet created, key")
		fmt.Fprintln(out, "  is empty, bytes are corrupt, or the reconcile hasn't run yet.")
	} else {
		fmt.Fprintf(out, "KVNO:      %d\n", u.Status.KvnoFromSecret)
		if u.Status.EnctypesFromSecret != "" {
			fmt.Fprintf(out, "Enctypes:  %s\n", u.Status.EnctypesFromSecret)
		}
		if u.Status.KeytabObservedAt != nil {
			fmt.Fprintf(out, "Observed:  %s ago  (%s)\n",
				ageStr(u.Status.KeytabObservedAt.Time),
				u.Status.KeytabObservedAt.Time.UTC().Format("2006-01-02T15:04:05Z"))
		}
	}

	// Drift signal: recent MintFailed events on this UID with
	// "Preauthentication" in the message are a strong signal
	// that KDC-side kvno diverges from Secret-side kvno.
	//
	// We don't reach out to the KDC ourselves — that would
	// require Kerberos client credentials in the CLI. Instead
	// we surface the daemon's kinit failures verbatim so the
	// operator can decide whether to rotate.
	drift := detectMintDrift(ctx, cl, ns, u.Spec.UID)
	if len(drift) > 0 {
		fmt.Fprintln(out, "")
		fmt.Fprintln(out, "⚠  Recent mint failures for this UID (possible kvno drift):")
		for _, e := range drift {
			fmt.Fprintf(out, "  [%s] %s on %s: %s\n",
				ageStr(e.LastTimestamp.Time), e.Reason, e.InvolvedObject.Name,
				truncate(e.Message, 100))
		}
		fmt.Fprintln(out, "")
		fmt.Fprintln(out, "  If the KDC has a newer kvno than the Secret, run:")
		fmt.Fprintf(out, "    kubectl krb rotate-keytab %s\n", u.Name)
	}
	return nil
}

// detectMintDrift returns up to 5 recent MintFailed events on
// the given UID whose messages hint at kvno/preauth drift. The
// tenant image annotates every mint outcome Event with the UID
// so we can filter cheaply.
func detectMintDrift(ctx context.Context, cl client.Client, ns string, uid int64) []corev1.Event {
	var evs corev1.EventList
	if err := cl.List(ctx, &evs, client.InNamespace(ns)); err != nil {
		return nil
	}
	uidStr := fmt.Sprintf("%d", uid)
	var out []corev1.Event
	for _, e := range evs.Items {
		if e.Annotations[sharedevents.UIDAnnotationKey] != uidStr {
			continue
		}
		// MintFailed is the canonical reason; a broader net
		// catches variants without matching healthy events.
		if e.Reason != "KerberosMintFailed" && e.Reason != "MintFailed" {
			continue
		}
		out = append(out, e)
	}
	// Sort newest first so the truncation to 5 shows the most
	// recent drift signal.
	sort.Slice(out, func(i, j int) bool {
		return out[i].LastTimestamp.Time.After(out[j].LastTimestamp.Time)
	})
	if len(out) > 5 {
		out = out[:5]
	}
	return out
}
