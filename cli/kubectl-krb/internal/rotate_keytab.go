// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

package internal

import (
	"context"
	"fmt"
	"io"
	"os"
	"time"

	"github.com/spf13/cobra"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"

	v1alpha1 "github.com/dell/kerberator/operator/api/v1alpha1"
)

// NewRotateKeytabCmd builds `kubectl krb rotate-keytab <principal>` — the
// multi-step wrapper for the "here's a fresh keytab, replace the old
// one everywhere" workflow.
//
// The gotcha this exists to prevent:
//
//	Updating the keytab Secret alone does NOT retrigger a mint. The
//	daemon's poll loop only re-sweeps when the roster's MD5 changes
//	or when renewMinutes elapses (12h by default). So a naive
//	"kubectl apply -f new-secret.yaml" leaves the node ccaches on
//	the OLD principal until the next 12h boundary.
//
// This subcommand does the correct sequence:
//
//  1. Read new keytab bytes.
//  2. PATCH the Principal's keytab Secret (in-place; keeps the Secret's
//     ownerRef + labels intact).
//  3. Rollout-restart the Tenant's DaemonSet so every daemon re-mints
//     with the new bytes.
//  4. Watch until every node's Principal status transitions back to
//     Minted, or the --timeout expires.
func NewRotateKeytabCmd(cf *ConfigFlags) *cobra.Command {
	var (
		fromFile string
		wait     bool
		timeout  time.Duration
	)
	cmd := &cobra.Command{
		Use:   "rotate-keytab <principal>",
		Short: "Replace a Principal's keytab and force a re-mint on every node",
		Args:  cobra.ExactArgs(1),
		Example: `  kubectl krb rotate-keytab uid-2001 --from-file ./svc-2001.keytab
  kubectl krb rotate-keytab uid-2001 --from-file - < svc-2001.keytab
  kubectl krb rotate-keytab uid-2001 --from-file ./kt --wait=false`,
	}
	cmd.Flags().StringVar(&fromFile, "from-file", "", "New keytab bytes ('-' for stdin).")
	cmd.Flags().BoolVar(&wait, "wait", true, "Wait for every node to re-mint before returning.")
	cmd.Flags().DurationVar(&timeout, "timeout", 2*time.Minute, "How long to wait for re-mint (0 = forever).")
	_ = cmd.MarkFlagRequired("from-file")

	cmd.RunE = func(cmd *cobra.Command, args []string) error {
		bytes, err := readKeytabBytes(fromFile)
		if err != nil {
			return err
		}
		return runRotateKeytab(cmd.Context(), cmd.OutOrStdout(), cf, args[0], bytes, wait, timeout)
	}
	return cmd
}

func readKeytabBytes(path string) ([]byte, error) {
	if path == "-" {
		b, err := io.ReadAll(os.Stdin)
		if err != nil {
			return nil, fmt.Errorf("read stdin: %w", err)
		}
		if len(b) == 0 {
			return nil, fmt.Errorf("empty keytab on stdin")
		}
		return b, nil
	}
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read keytab %s: %w", path, err)
	}
	if len(b) == 0 {
		return nil, fmt.Errorf("keytab %s is empty", path)
	}
	return b, nil
}

func runRotateKeytab(ctx context.Context, out io.Writer, cf *ConfigFlags, principalName string, newKeytab []byte, wait bool, timeout time.Duration) error {
	cl, err := cf.Client()
	if err != nil {
		return err
	}
	ns, err := cf.Namespace()
	if err != nil {
		return err
	}
	return RotateKeytab(ctx, cl, out, ns, principalName, newKeytab, wait, timeout)
}

// RotateKeytab is the shared driver both the CLI subcommand and the
// TUI wizard use. Writes progress messages to `out` so callers can
// stream them into whatever UI they want (stdout, a bubbletea
// viewport, a log file, ...).
func RotateKeytab(ctx context.Context, cl client.Client, out io.Writer, ns, principalName string, newKeytab []byte, wait bool, timeout time.Duration) error {
	// 1. Fetch the Principal so we know which Secret + which Tenant.
	var principal v1alpha1.Principal
	if err := cl.Get(ctx, types.NamespacedName{Namespace: ns, Name: principalName}, &principal); err != nil {
		if apierrors.IsNotFound(err) {
			return fmt.Errorf("principal %s/%s not found", ns, principalName)
		}
		return fmt.Errorf("get principal: %w", err)
	}
	if principal.Spec.KeytabSecretRef == nil || principal.Spec.KeytabSecretRef.Name == "" {
		return fmt.Errorf("principal %s/%s has no keytabSecretRef — nothing to rotate", ns, principalName)
	}
	secretName := principal.Spec.KeytabSecretRef.Name
	keytabKey := principal.Spec.KeytabSecretRef.Key
	tenantName := principal.Spec.TenantRef.Name
	fmt.Fprintf(out, "==> rotating principal %s/%s (secret=%s key=%s tenant=%s)\n",
		ns, principalName, secretName, keytabKey, tenantName)

	// 2. Patch the Secret data. We use a plain Update after Get so
	// the operation is a no-op when the caller re-runs with the
	// same bytes (kubelet won't re-sync a Secret whose resourceVersion
	// didn't change).
	var sec corev1.Secret
	if err := cl.Get(ctx, types.NamespacedName{Namespace: ns, Name: secretName}, &sec); err != nil {
		return fmt.Errorf("get secret %s/%s: %w", ns, secretName, err)
	}
	if sec.Data == nil {
		sec.Data = map[string][]byte{}
	}
	sec.Data[keytabKey] = newKeytab
	if err := cl.Update(ctx, &sec); err != nil {
		return fmt.Errorf("update secret: %w", err)
	}
	fmt.Fprintf(out, "    secret/%s updated (%d bytes at key %s)\n", secretName, len(newKeytab), keytabKey)

	// 3. Rollout-restart the DaemonSet. DaemonSet name is
	// deterministic: <tenantName>-kerberator-daemon (see
	// operator/internal/controller/tenant_reconciler.go).
	dsName := tenantName + "-kerberator-daemon"
	if err := patchDaemonSetRolloutRestart(ctx, cl, ns, dsName); err != nil {
		return fmt.Errorf("rollout restart daemonset/%s: %w", dsName, err)
	}
	fmt.Fprintf(out, "    daemonset/%s rollout restarted\n", dsName)

	if !wait {
		fmt.Fprintln(out, "    (--wait=false: not waiting for re-mint)")
		return nil
	}

	// 4. Watch Principal status until every node reports Minted with a
	// LastObserved AFTER our rollout timestamp. We poll rather than
	// use a controller-runtime informer for a one-shot CLI/TUI
	// operation — it's much simpler and the target latency is O(seconds).
	fmt.Fprintf(out, "==> waiting up to %s for every node to re-mint\n", timeout)
	rolloutAt := time.Now()
	waitCtx := ctx
	if timeout > 0 {
		var cancel context.CancelFunc
		waitCtx, cancel = context.WithTimeout(ctx, timeout)
		defer cancel()
	}
	for {
		var t v1alpha1.Principal
		if err := cl.Get(waitCtx, types.NamespacedName{Namespace: ns, Name: principalName}, &t); err != nil {
			return fmt.Errorf("poll principal: %w", err)
		}
		if allNodesMintedSince(t, rolloutAt) {
			fmt.Fprintf(out, "    all %d nodes re-minted successfully\n", len(t.Status.Nodes))
			return nil
		}
		select {
		case <-waitCtx.Done():
			return fmt.Errorf("timed out waiting for re-mint (still: %s)", statusSummary(t))
		case <-time.After(2 * time.Second):
		}
	}
}

// patchDaemonSetRolloutRestart applies the same annotation kubectl
// uses for `rollout restart`: a spec.template.metadata annotation with
// the restart timestamp. Kubelet notices the pod-template change and
// spins new pods.
func patchDaemonSetRolloutRestart(ctx context.Context, cl client.Client, ns, name string) error {
	var ds appsv1.DaemonSet
	if err := cl.Get(ctx, types.NamespacedName{Namespace: ns, Name: name}, &ds); err != nil {
		return err
	}
	if ds.Spec.Template.ObjectMeta.Annotations == nil {
		ds.Spec.Template.ObjectMeta.Annotations = map[string]string{}
	}
	// Nanosecond precision means a rapid re-run of RotateKeytab still
	// produces a distinct value, forcing kubelet to see a template change.
	ds.Spec.Template.ObjectMeta.Annotations["kubectl.kubernetes.io/restartedAt"] =
		time.Now().Format(time.RFC3339Nano)
	return cl.Update(ctx, &ds)
}

func allNodesMintedSince(t v1alpha1.Principal, rolloutAt time.Time) bool {
	if len(t.Status.Nodes) == 0 {
		return false
	}
	for _, n := range t.Status.Nodes {
		if n.Reason != "Minted" {
			return false
		}
		if n.LastObserved.Time.Before(rolloutAt) {
			return false
		}
	}
	return true
}

func statusSummary(t v1alpha1.Principal) string {
	if len(t.Status.Nodes) == 0 {
		return "no nodes reporting"
	}
	minted := 0
	for _, n := range t.Status.Nodes {
		if n.Reason == "Minted" {
			minted++
		}
	}
	return fmt.Sprintf("%d/%d minted", minted, len(t.Status.Nodes))
}
