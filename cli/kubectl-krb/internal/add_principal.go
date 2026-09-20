// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

package internal

import (
	"context"
	"encoding/base64"
	"fmt"
	"io"
	"os"

	"github.com/spf13/cobra"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"

	v1alpha1 "github.com/dell/kerberator/operator/api/v1alpha1"
)

// NewAddPrincipalCmd builds `kubectl krb add-principal <tenant> <uid>` — a
// one-shot "here's a keytab, register this identity" command that:
//
//  1. Fetches the target Tenant to derive its Kerberos realm string.
//  2. Reads the keytab bytes from --keytab-file (or - for stdin).
//  3. Creates a per-principal Secret in the Tenant's namespace.
//  4. Creates the Principal CR referencing that Secret + the Tenant.
//
// This is the imperative counterpart to applying a Principal manifest.
// Useful for one-off principal additions where writing YAML feels heavy.
func NewAddPrincipalCmd(cf *ConfigFlags) *cobra.Command {
	var (
		keytabPath   string
		principal    string
		dryRun       bool
		nodeSelector []string
	)
	cmd := &cobra.Command{
		Use:   "add-principal <tenant> <uid>",
		Short: "Register a new Principal against an existing Tenant",
		Args:  cobra.ExactArgs(2),
		Example: `  kubectl krb add-principal demo 2003 --keytab-file ./svc-2003.keytab
  kubectl krb add-principal demo 2003 --keytab-file - --principal svc-2003@EXAMPLE.COM < keytab

  # v0.8: restrict a principal's ccache to worker nodes labeled
  # security-zone=prod-a AND tier=gpu (AND semantics — same as
  # DaemonSet nodeSelector).
  kubectl krb add-principal demo 2003 --keytab-file ./svc-2003.keytab \
      --node-selector security-zone=prod-a --node-selector tier=gpu`,
	}
	cmd.Flags().StringVar(&keytabPath, "keytab-file", "", "Path to the keytab bytes for this principal (or '-' for stdin).")
	cmd.Flags().StringVar(&principal, "principal", "", "Full principal (name@REALM). Defaults to svc-<uid>@<realm>.")
	cmd.Flags().StringArrayVar(&nodeSelector, "node-selector", nil,
		"Restrict this Principal's ccache to nodes matching key=value. Repeatable; AND semantics.")
	cmd.Flags().BoolVar(&dryRun, "dry-run", false, "Print the manifests we would apply without touching the API.")
	_ = cmd.MarkFlagRequired("keytab-file")

	cmd.RunE = func(cmd *cobra.Command, args []string) error {
		selectors, err := parseNodeSelectorFlags(nodeSelector)
		if err != nil {
			return err
		}
		return runAddPrincipal(cmd.Context(), cmd.OutOrStdout(), cf, args[0], args[1], keytabPath, principal, selectors, dryRun)
	}
	return cmd
}

// parseNodeSelectorFlags turns repeated `--node-selector k=v` flags
// into a map[string]string. Errors on malformed entries so mistakes
// like `--node-selector zone` don't silently do the wrong thing.
func parseNodeSelectorFlags(entries []string) (map[string]string, error) {
	if len(entries) == 0 {
		return nil, nil
	}
	out := make(map[string]string, len(entries))
	for _, e := range entries {
		idx := -1
		for i, r := range e {
			if r == '=' {
				idx = i
				break
			}
		}
		if idx <= 0 || idx == len(e)-1 {
			return nil, fmt.Errorf("--node-selector %q: expected key=value", e)
		}
		out[e[:idx]] = e[idx+1:]
	}
	return out, nil
}

func runAddPrincipal(ctx context.Context, out io.Writer, cf *ConfigFlags, tenantName, uidStr, keytabPath, principal string, nodeSelector map[string]string, dryRun bool) error {
	uid, err := parseUID(uidStr)
	if err != nil {
		return err
	}
	cl, err := cf.Client()
	if err != nil {
		return err
	}
	ns, err := cf.Namespace()
	if err != nil {
		return err
	}

	// Fetch the Tenant so we can (a) derive the default principal
	// and (b) fail early if the user typo'd the Tenant name.
	var tenant v1alpha1.Tenant
	if err := cl.Get(ctx, types.NamespacedName{Namespace: ns, Name: tenantName}, &tenant); err != nil {
		if apierrors.IsNotFound(err) {
			return fmt.Errorf("tenant %s/%s not found; create it first (kubectl apply -f examples/quickstart/tenant.yaml)", ns, tenantName)
		}
		return fmt.Errorf("get tenant: %w", err)
	}
	if principal == "" {
		principal = fmt.Sprintf("svc-%d@%s", uid, tenant.Spec.Realm)
	}

	// Read keytab bytes.
	var keytabBytes []byte
	if keytabPath == "-" {
		keytabBytes, err = io.ReadAll(os.Stdin)
	} else {
		keytabBytes, err = os.ReadFile(keytabPath)
	}
	if err != nil {
		return fmt.Errorf("read keytab: %w", err)
	}
	if len(keytabBytes) == 0 {
		return fmt.Errorf("keytab is empty")
	}

	secretName := fmt.Sprintf("uid-%d-keytab", uid)
	keytabKey := fmt.Sprintf("uid-%d.keytab", uid)
	principalName := fmt.Sprintf("uid-%d", uid)

	sec := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{
			Name:      secretName,
			Namespace: ns,
			Labels:    map[string]string{"kerberator.dell.com/uid": uidStr},
		},
		Type: corev1.SecretTypeOpaque,
		Data: map[string][]byte{keytabKey: keytabBytes},
	}
	obj := &v1alpha1.Principal{
		ObjectMeta: metav1.ObjectMeta{
			Name:      principalName,
			Namespace: ns,
		},
		Spec: v1alpha1.PrincipalSpec{
			UID:             uid,
			Principal:       principal,
			TenantRef:       v1alpha1.TenantRef{Name: tenantName},
			KeytabSecretRef: &v1alpha1.SecretKeyRef{Name: secretName, Key: keytabKey},
			NodeSelector:    nodeSelector,
		},
	}

	if dryRun {
		fmt.Fprintln(out, "# --- would apply the following ---")
		printYAML(out, sec, "Secret")
		fmt.Fprintln(out, "---")
		printYAML(out, obj, "Principal")
		// Sanity-hint: the Secret's Data will render as base64 in the
		// YAML output above, which is the actual wire format.
		fmt.Fprintf(out, "# (keytab %d bytes, will land at %s/%s.data[%q])\n",
			len(keytabBytes), ns, secretName, keytabKey)
		_ = base64.StdEncoding // keep import used
		return nil
	}

	if err := createSecretAndPrincipal(ctx, cl, sec, obj); err != nil {
		return err
	}
	fmt.Fprintf(out, "secret/%s created\n", secretName)
	fmt.Fprintf(out, "principal.kerberator.dell.com/%s created\n", principalName)
	return nil
}

// CreatePrincipalWithKeytab is the shared driver both the CLI subcommand
// and the TUI form call. Creates a Secret holding the keytab bytes and
// a Principal CR that references it. Optional nodeSelector restricts the
// on-node ccache placement (v0.8+). Returns an actionResultMsg-style
// (summary, err) pair for the TUI; the CLI ignores the summary and
// prints its own line.
func CreatePrincipalWithKeytab(ctx context.Context, cl client.Client, ns, tenantName string, uid int64, principal string, keytab []byte, nodeSelector map[string]string) any {
	secretName := fmt.Sprintf("uid-%d-keytab", uid)
	keytabKey := fmt.Sprintf("uid-%d.keytab", uid)
	principalName := fmt.Sprintf("uid-%d", uid)
	sec := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{
			Name:      secretName,
			Namespace: ns,
			Labels:    map[string]string{"kerberator.dell.com/uid": fmt.Sprintf("%d", uid)},
		},
		Type: corev1.SecretTypeOpaque,
		Data: map[string][]byte{keytabKey: keytab},
	}
	obj := &v1alpha1.Principal{
		ObjectMeta: metav1.ObjectMeta{Name: principalName, Namespace: ns},
		Spec: v1alpha1.PrincipalSpec{
			UID:             uid,
			Principal:       principal,
			TenantRef:       v1alpha1.TenantRef{Name: tenantName},
			KeytabSecretRef: &v1alpha1.SecretKeyRef{Name: secretName, Key: keytabKey},
			NodeSelector:    nodeSelector,
		},
	}
	if err := createSecretAndPrincipal(ctx, cl, sec, obj); err != nil {
		return ActionResult{Summary: err.Error(), Err: err}
	}
	return ActionResult{Summary: fmt.Sprintf("principal %s/%s created", ns, principalName)}
}

// ActionResult is a small (summary, err) struct the TUI uses to
// surface the outcome of a shared driver call. We can't reference
// tui.actionResultMsg directly from `internal` (import cycle), so the
// TUI-side glue interprets this shape.
type ActionResult struct {
	Summary string
	Err     error
}

func createSecretAndPrincipal(ctx context.Context, cl client.Client, sec *corev1.Secret, principal *v1alpha1.Principal) error {
	if err := cl.Create(ctx, sec); err != nil && !apierrors.IsAlreadyExists(err) {
		return fmt.Errorf("create secret: %w", err)
	}
	if err := cl.Create(ctx, principal); err != nil && !apierrors.IsAlreadyExists(err) {
		return fmt.Errorf("create principal: %w", err)
	}
	return nil
}

// printYAML dumps a client.Object as a small YAML block. We deliberately
// don't use sigs.k8s.io/yaml here for a printer this simple — a couple
// of fmt lines keep the CLI binary lean.
func printYAML(out io.Writer, obj client.Object, kind string) {
	fmt.Fprintf(out, "apiVersion: %s\n", inferAPIVersion(kind))
	fmt.Fprintf(out, "kind: %s\n", kind)
	fmt.Fprintf(out, "metadata:\n  name: %s\n  namespace: %s\n", obj.GetName(), obj.GetNamespace())
	if labels := obj.GetLabels(); len(labels) > 0 {
		fmt.Fprintln(out, "  labels:")
		for k, v := range labels {
			fmt.Fprintf(out, "    %s: %q\n", k, v)
		}
	}
	// Kind-specific spec dumping. Full YAML marshaling would live-load
	// sigs.k8s.io/yaml; for a dry-run preview the essentials suffice.
	switch v := obj.(type) {
	case *v1alpha1.Principal:
		fmt.Fprintf(out, "spec:\n  uid: %d\n  principal: %s\n  tenantRef:\n    name: %s\n  keytabSecretRef:\n    name: %s\n    key: %s\n",
			v.Spec.UID, v.Spec.Principal, v.Spec.TenantRef.Name,
			v.Spec.KeytabSecretRef.Name, v.Spec.KeytabSecretRef.Key)
	case *corev1.Secret:
		fmt.Fprintln(out, "type: Opaque")
		fmt.Fprintln(out, "data:")
		for k, b := range v.Data {
			fmt.Fprintf(out, "  %s: %s\n", k, base64.StdEncoding.EncodeToString(b))
		}
	}
}

func inferAPIVersion(kind string) string {
	switch kind {
	case "Principal", "Tenant":
		return "kerberator.dell.com/v1alpha1"
	default:
		return "v1"
	}
}
