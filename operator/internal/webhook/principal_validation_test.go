// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

package webhook

import (
	"strings"
	"testing"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	v1alpha1 "github.com/dell/kerberator/operator/api/v1alpha1"
)

func mkPrincipalForVal(uid int64, principal, tenantRefName string, keytabRef *v1alpha1.SecretKeyRef) *v1alpha1.Principal {
	kt := &v1alpha1.Principal{
		ObjectMeta: metav1.ObjectMeta{Name: "kt", Namespace: "ns"},
		Spec: v1alpha1.PrincipalSpec{
			UID:       uid,
			Principal: principal,
			TenantRef: v1alpha1.TenantRef{
				Name: tenantRefName,
			},
			KeytabSecretRef: keytabRef,
		},
	}
	return kt
}

func mkFactoryForVal(name, tenant string) *v1alpha1.Tenant {
	return &v1alpha1.Tenant{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: "ns"},
		Spec: v1alpha1.TenantSpec{
			Realm: tenant,
		},
	}
}

func TestValidatePrincipal_HappyPath(t *testing.T) {
	kt := mkPrincipalForVal(1000660010, "svc-a@EXAMPLE.COM", "kcf", &v1alpha1.SecretKeyRef{Name: "s", Key: "k"})
	kcf := mkFactoryForVal("kcf", "EXAMPLE.COM")
	ann := map[string]string{SCCUIDRangeAnnotation: "1000660000/10000"}
	errs := ValidatePrincipal(kt, ann, kcf, true, OpCreate)
	if len(errs) != 0 {
		t.Errorf("expected no errors, got: %v", errs)
	}
}

func TestValidatePrincipal_UIDZeroRejected(t *testing.T) {
	kt := mkPrincipalForVal(0, "svc-a@EXAMPLE.COM", "", nil)
	errs := ValidatePrincipal(kt, nil, nil, false, OpCreate)
	if len(errs) == 0 || !strings.Contains(errs.ToAggregate().Error(), "positive integer") {
		t.Errorf("expected uid>0 rejection, got: %v", errs)
	}
}

func TestValidatePrincipal_UIDNegativeRejected(t *testing.T) {
	kt := mkPrincipalForVal(-1, "svc-a@EXAMPLE.COM", "", nil)
	errs := ValidatePrincipal(kt, nil, nil, false, OpCreate)
	if len(errs) == 0 {
		t.Errorf("expected negative uid rejection")
	}
}

func TestValidatePrincipal_UIDOutsideSCCRange(t *testing.T) {
	// SCC range = [1000660000, 1000670000). Try 500 too low, then too high.
	ann := map[string]string{SCCUIDRangeAnnotation: "1000660000/10000"}

	tooLow := mkPrincipalForVal(1000659999, "svc-a@TENANT", "", nil)
	errs := ValidatePrincipal(tooLow, ann, nil, false, OpCreate)
	if len(errs) == 0 || !strings.Contains(errs.ToAggregate().Error(), "SCC uid-range") {
		t.Errorf("expected SCC uid-range rejection for too-low uid, got: %v", errs)
	}

	tooHigh := mkPrincipalForVal(1000670000, "svc-a@TENANT", "", nil)
	errs = ValidatePrincipal(tooHigh, ann, nil, false, OpCreate)
	if len(errs) == 0 || !strings.Contains(errs.ToAggregate().Error(), "SCC uid-range") {
		t.Errorf("expected SCC uid-range rejection for too-high uid (exclusive upper bound), got: %v", errs)
	}
}

func TestValidatePrincipal_SCCAnnotationAbsentSkipsCheck(t *testing.T) {
	// Non-OpenShift cluster: no annotation, uid 1234 should pass the
	// SCC rule (Update op so unresolved tenantRef doesn't fire
	// rule 4).
	kt := mkPrincipalForVal(1234, "svc-a@TENANT", "kcf", nil)
	errs := ValidatePrincipal(kt, nil, nil, false, OpUpdate)
	if len(errs) != 0 {
		t.Errorf("expected no errors when SCC annotation absent, got: %v", errs)
	}
}

func TestValidatePrincipal_MalformedSCCAnnotationIsFailOpen(t *testing.T) {
	// Malformed annotation should not block admission.
	ann := map[string]string{SCCUIDRangeAnnotation: "not-a-range"}
	kt := mkPrincipalForVal(1234, "svc-a@TENANT", "kcf", nil)
	errs := ValidatePrincipal(kt, ann, nil, false, OpUpdate)
	if len(errs) != 0 {
		t.Errorf("expected fail-open on malformed SCC annotation, got: %v", errs)
	}
}

func TestValidatePrincipal_PrincipalShapeBad(t *testing.T) {
	for _, bad := range []string{"no-at-sign", "@no-name", "no-tenant@", "two@ats@bad"} {
		t.Run(bad, func(t *testing.T) {
			kt := mkPrincipalForVal(1000, bad, "", nil)
			errs := ValidatePrincipal(kt, nil, nil, false, OpCreate)
			if len(errs) == 0 || !strings.Contains(errs.ToAggregate().Error(), "principal") {
				t.Errorf("expected principal-shape rejection for %q, got: %v", bad, errs)
			}
		})
	}
}

func TestValidatePrincipal_PrincipalTenantMismatch(t *testing.T) {
	kt := mkPrincipalForVal(1000, "svc-a@WRONG.TENANT", "kcf", nil)
	kcf := mkFactoryForVal("kcf", "EXAMPLE.COM")
	errs := ValidatePrincipal(kt, nil, kcf, true, OpCreate)
	if len(errs) == 0 || !strings.Contains(errs.ToAggregate().Error(), "does not match") {
		t.Errorf("expected tenant-mismatch rejection, got: %v", errs)
	}
}

func TestValidatePrincipal_PrincipalTenantMatchAllowsSlashName(t *testing.T) {
	// MIT krb5 service principals have slashes; make sure we don't
	// reject them.
	kt := mkPrincipalForVal(1000, "svc/host.example.com@EXAMPLE.COM", "kcf", nil)
	kcf := mkFactoryForVal("kcf", "EXAMPLE.COM")
	errs := ValidatePrincipal(kt, nil, kcf, true, OpCreate)
	if len(errs) != 0 {
		t.Errorf("expected slash-name principal to be accepted, got: %v", errs)
	}
}

func TestValidatePrincipal_UnknownFactoryRejectedOnCreate(t *testing.T) {
	kt := mkPrincipalForVal(1000, "svc-a@TENANT", "missing-kcf", nil)
	errs := ValidatePrincipal(kt, nil, nil, false, OpCreate)
	if len(errs) == 0 || !strings.Contains(errs.ToAggregate().Error(), "not found") {
		t.Errorf("expected unknown-tenant rejection on Create, got: %v", errs)
	}
}

func TestValidatePrincipal_UnknownFactoryAllowedOnUpdate(t *testing.T) {
	// Update against a since-deleted tenant must be allowed so
	// operators can still fix or cleanup stranded principal CRs.
	kt := mkPrincipalForVal(1000, "svc-a@TENANT", "missing-kcf", nil)
	errs := ValidatePrincipal(kt, nil, nil, false, OpUpdate)
	if len(errs) != 0 {
		t.Errorf("expected unknown-tenant to be allowed on Update, got: %v", errs)
	}
}

func TestValidatePrincipal_EmptyTenantRefRejected(t *testing.T) {
	// tenantRef.name is now required (no more observer mode).
	kt := mkPrincipalForVal(1000, "svc-a@TENANT", "", nil)
	errs := ValidatePrincipal(kt, nil, nil, false, OpCreate)
	if len(errs) == 0 || !strings.Contains(errs.ToAggregate().Error(), "tenantRef.name") {
		t.Errorf("expected tenantRef.name required rejection, got: %v", errs)
	}
}

func TestValidatePrincipal_KeytabSecretRefShape(t *testing.T) {
	// Missing key.
	kt := mkPrincipalForVal(1000, "svc-a@TENANT", "", &v1alpha1.SecretKeyRef{Name: "s", Key: ""})
	errs := ValidatePrincipal(kt, nil, nil, false, OpCreate)
	if len(errs) == 0 || !strings.Contains(errs.ToAggregate().Error(), "keytabSecretRef.key") {
		t.Errorf("expected keytabSecretRef.key required, got: %v", errs)
	}
	// Missing name.
	kt = mkPrincipalForVal(1000, "svc-a@TENANT", "", &v1alpha1.SecretKeyRef{Name: "", Key: "k"})
	errs = ValidatePrincipal(kt, nil, nil, false, OpCreate)
	if len(errs) == 0 || !strings.Contains(errs.ToAggregate().Error(), "keytabSecretRef.name") {
		t.Errorf("expected keytabSecretRef.name required, got: %v", errs)
	}
}

func TestParseSCCRange(t *testing.T) {
	cases := []struct {
		name       string
		annotation map[string]string
		wantStart  int64
		wantLen    int64
		wantOK     bool
	}{
		{"typical", map[string]string{SCCUIDRangeAnnotation: "1000660000/10000"}, 1000660000, 10000, true},
		{"whitespace", map[string]string{SCCUIDRangeAnnotation: " 1000 / 10 "}, 1000, 10, true},
		{"absent", map[string]string{}, 0, 0, false},
		{"empty-value", map[string]string{SCCUIDRangeAnnotation: ""}, 0, 0, false},
		{"no-slash", map[string]string{SCCUIDRangeAnnotation: "1000660000"}, 0, 0, false},
		{"bad-int", map[string]string{SCCUIDRangeAnnotation: "abc/10000"}, 0, 0, false},
		{"zero-length", map[string]string{SCCUIDRangeAnnotation: "1000/0"}, 0, 0, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			s, l, ok := parseSCCRange(c.annotation)
			if ok != c.wantOK || s != c.wantStart || l != c.wantLen {
				t.Errorf("got (%d, %d, %v), want (%d, %d, %v)",
					s, l, ok, c.wantStart, c.wantLen, c.wantOK)
			}
		})
	}
}
