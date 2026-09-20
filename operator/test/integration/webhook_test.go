// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

package integration

import (
	"strings"
	"testing"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	v1alpha1 "github.com/dell/kerberator/operator/api/v1alpha1"
)

// TestWebhookRejectsBadPrincipals exercises the ValidatingAdmissionWebhook
// path end-to-end: a Principal with an invalid spec must be
// rejected by the apiserver at admission time, before any reconciler
// sees it. The pure ValidatePrincipal function is covered exhaustively
// by internal/webhook/principal_validation_test.go; this test proves the
// webhook is actually plumbed into the apiserver (VWC + service +
// cert) and that a real Create call carries the denial back to the
// client.
func TestWebhookRejectsBadPrincipals(t *testing.T) {
	// Namespace with an SCC UID-range annotation so we can also
	// exercise rule 2 (out-of-range UIDs).
	nsName := "webhook-negatives"
	mustCreateNS(t, nsName, "2001/1")

	// Pre-create a tenant the "good" cases can reference. Rule 3
	// tightens the tenant check to manage mode, so the tenant must
	// be manage mode with the same realm as the principal's Kerberos principal.
	factoryName := "wh-tenant"
	tenant := &v1alpha1.Tenant{
		ObjectMeta: metav1.ObjectMeta{Name: factoryName, Namespace: nsName},
		Spec: v1alpha1.TenantSpec{
			Realm: "EXAMPLE.COM",
			Daemon: v1alpha1.DaemonTemplate{
				Image: "ghcr.io/dell/kerberator-daemon:v0.3.0",
			},
		},
	}
	if err := k8sClient.Create(testCtx, tenant); err != nil {
		t.Fatalf("create tenant: %v", err)
	}

	cases := []struct {
		name        string
		principal   *v1alpha1.Principal
		wantSubstr  string
		wantAllowed bool
	}{
		{
			name: "rule1-uid-zero",
			principal: &v1alpha1.Principal{
				ObjectMeta: metav1.ObjectMeta{Name: "t-uid-zero", Namespace: nsName},
				Spec: v1alpha1.PrincipalSpec{
					UID:             0,
					Principal:       "svc@EXAMPLE.COM",
					TenantRef:       v1alpha1.TenantRef{Name: factoryName},
					KeytabSecretRef: &v1alpha1.SecretKeyRef{Name: "s", Key: "k"},
				},
			},
			wantSubstr: "uid must be a positive integer",
		},
		{
			name: "rule2-uid-out-of-scc-range",
			principal: &v1alpha1.Principal{
				ObjectMeta: metav1.ObjectMeta{Name: "t-uid-oor", Namespace: nsName},
				Spec: v1alpha1.PrincipalSpec{
					UID:             9999, // ns range is 2001/1 => [2001,2002)
					Principal:       "svc@EXAMPLE.COM",
					TenantRef:       v1alpha1.TenantRef{Name: factoryName},
					KeytabSecretRef: &v1alpha1.SecretKeyRef{Name: "s", Key: "k"},
				},
			},
			wantSubstr: "outside the namespace's SCC uid-range",
		},
		{
			name: "rule3-tenant-mismatch",
			principal: &v1alpha1.Principal{
				ObjectMeta: metav1.ObjectMeta{Name: "t-tenant", Namespace: nsName},
				Spec: v1alpha1.PrincipalSpec{
					UID:             2001,
					Principal:       "svc@WRONG.TENANT",
					TenantRef:       v1alpha1.TenantRef{Name: factoryName},
					KeytabSecretRef: &v1alpha1.SecretKeyRef{Name: "s", Key: "k"},
				},
			},
			wantSubstr: "tenant",
		},
		{
			name: "rule4-unknown-tenant-ref",
			principal: &v1alpha1.Principal{
				ObjectMeta: metav1.ObjectMeta{Name: "t-unknown-tenant", Namespace: nsName},
				Spec: v1alpha1.PrincipalSpec{
					UID:             2001,
					Principal:       "svc@EXAMPLE.COM",
					TenantRef:       v1alpha1.TenantRef{Name: "does-not-exist"},
					KeytabSecretRef: &v1alpha1.SecretKeyRef{Name: "s", Key: "k"},
				},
			},
			wantSubstr: "not found",
		},
		{
			name: "rule5-keytab-secret-ref-missing-fields",
			principal: &v1alpha1.Principal{
				ObjectMeta: metav1.ObjectMeta{Name: "t-secret-fields", Namespace: nsName},
				Spec: v1alpha1.PrincipalSpec{
					UID:             2001,
					Principal:       "svc@EXAMPLE.COM",
					TenantRef:       v1alpha1.TenantRef{Name: factoryName},
					KeytabSecretRef: &v1alpha1.SecretKeyRef{Name: "", Key: ""}, // both empty
				},
			},
			wantSubstr: "keytabSecretRef",
		},
		{
			name: "allowed-happy-path",
			principal: &v1alpha1.Principal{
				ObjectMeta: metav1.ObjectMeta{Name: "t-ok", Namespace: nsName},
				Spec: v1alpha1.PrincipalSpec{
					UID:             2001,
					Principal:       "svc@EXAMPLE.COM",
					TenantRef:       v1alpha1.TenantRef{Name: factoryName},
					KeytabSecretRef: &v1alpha1.SecretKeyRef{Name: "keytabs", Key: "svc.keytab"},
				},
			},
			wantAllowed: true,
		},
	}

	for _, tc := range cases {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			err := k8sClient.Create(testCtx, tc.principal)
			if tc.wantAllowed {
				if err != nil {
					t.Fatalf("expected create to succeed, got %v", err)
				}
				return
			}
			if err == nil {
				t.Fatalf("expected create to be rejected by webhook, got nil error (principal persisted)")
			}
			// Webhook denials round-trip as apierrors.StatusError; the
			// aggregated field-error text is in .Status().Message.
			statusErr, ok := err.(*apierrors.StatusError)
			if !ok {
				t.Fatalf("expected StatusError, got %T: %v", err, err)
			}
			if !strings.Contains(statusErr.Status().Message, tc.wantSubstr) {
				t.Fatalf("expected error to mention %q, got %q", tc.wantSubstr, statusErr.Status().Message)
			}
		})
	}
}
