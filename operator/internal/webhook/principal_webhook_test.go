// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

package webhook

import (
	"context"
	"strings"
	"testing"

	admissionv1 "k8s.io/api/admission/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/webhook/admission"

	v1alpha1 "github.com/dell/kerberator/operator/api/v1alpha1"
)

func newTestScheme() *runtime.Scheme {
	s := runtime.NewScheme()
	_ = clientgoscheme.AddToScheme(s)
	_ = v1alpha1.AddToScheme(s)
	return s
}

func newHandler(objs ...runtime.Object) *PrincipalValidator {
	s := newTestScheme()
	cl := fake.NewClientBuilder().WithScheme(s).WithRuntimeObjects(objs...).Build()
	return &PrincipalValidator{
		Client:  cl,
		decoder: admission.NewDecoder(s),
	}
}

func mkNamespace(name string, annotations map[string]string) *corev1.Namespace {
	return &corev1.Namespace{
		ObjectMeta: metav1.ObjectMeta{Name: name, Annotations: annotations},
	}
}

func TestHandle_HappyPath(t *testing.T) {
	kt := mkPrincipalForVal(1000660010, "svc-a@EXAMPLE.COM", "kcf", &v1alpha1.SecretKeyRef{Name: "s", Key: "k"})
	kcf := mkFactoryForVal("kcf", "EXAMPLE.COM")
	ns := mkNamespace("ns", map[string]string{SCCUIDRangeAnnotation: "1000660000/10000"})

	h := newHandler(kcf, ns)
	req, err := AdmissionReviewFromRaw(admissionv1.Create, "ns", kt)
	if err != nil {
		t.Fatalf("build req: %v", err)
	}
	resp := h.Handle(context.Background(), req)
	if !resp.Allowed {
		t.Fatalf("expected Allowed, got denied: %s", resp.Result.Message)
	}
}

func TestHandle_UIDOutsideSCCRange_Denied(t *testing.T) {
	kt := mkPrincipalForVal(2000000000, "svc-a@TENANT", "", nil)
	ns := mkNamespace("ns", map[string]string{SCCUIDRangeAnnotation: "1000660000/10000"})
	h := newHandler(ns)
	req, _ := AdmissionReviewFromRaw(admissionv1.Create, "ns", kt)
	resp := h.Handle(context.Background(), req)
	if resp.Allowed {
		t.Fatalf("expected denial, got allowed")
	}
	if !strings.Contains(resp.Result.Message, "SCC uid-range") {
		t.Errorf("expected SCC uid-range in message, got: %s", resp.Result.Message)
	}
}

func TestHandle_UnknownFactoryOnCreate_Denied(t *testing.T) {
	kt := mkPrincipalForVal(1000, "svc-a@TENANT", "missing-kcf", nil)
	ns := mkNamespace("ns", nil)
	h := newHandler(ns)
	req, _ := AdmissionReviewFromRaw(admissionv1.Create, "ns", kt)
	resp := h.Handle(context.Background(), req)
	if resp.Allowed {
		t.Fatalf("expected denial for unknown tenant on Create")
	}
	if !strings.Contains(resp.Result.Message, "not found") {
		t.Errorf("expected not-found in message, got: %s", resp.Result.Message)
	}
}

func TestHandle_UnknownFactoryOnUpdate_Allowed(t *testing.T) {
	kt := mkPrincipalForVal(1000, "svc-a@TENANT", "missing-kcf", nil)
	ns := mkNamespace("ns", nil)
	h := newHandler(ns)
	req, _ := AdmissionReviewFromRaw(admissionv1.Update, "ns", kt)
	resp := h.Handle(context.Background(), req)
	if !resp.Allowed {
		t.Fatalf("expected allowed for unknown tenant on Update, got: %s", resp.Result.Message)
	}
}

func TestHandle_DeleteAlwaysAllowed(t *testing.T) {
	kt := mkPrincipalForVal(-1, "totally-invalid", "", nil)
	h := newHandler()
	req, _ := AdmissionReviewFromRaw(admissionv1.Delete, "ns", kt)
	resp := h.Handle(context.Background(), req)
	if !resp.Allowed {
		t.Fatalf("expected Delete to be allowed regardless of spec, got: %s", resp.Result.Message)
	}
}

func TestHandle_TenantMismatch_Denied(t *testing.T) {
	kt := mkPrincipalForVal(1000, "svc-a@WRONG.TENANT", "kcf", nil)
	kcf := mkFactoryForVal("kcf", "EXAMPLE.COM")
	ns := mkNamespace("ns", nil)
	h := newHandler(kcf, ns)
	req, _ := AdmissionReviewFromRaw(admissionv1.Create, "ns", kt)
	resp := h.Handle(context.Background(), req)
	if resp.Allowed {
		t.Fatalf("expected denial for tenant mismatch")
	}
	if !strings.Contains(resp.Result.Message, "does not match") {
		t.Errorf("expected tenant mismatch in message, got: %s", resp.Result.Message)
	}
}

func TestHandle_MissingNamespaceSkipsSCCButKeepsOtherRules(t *testing.T) {
	// No Namespace object in the fake client => Get returns NotFound.
	// UID rule should still fire.
	kt := mkPrincipalForVal(0, "svc-a@TENANT", "", nil)
	h := newHandler()
	req, _ := AdmissionReviewFromRaw(admissionv1.Create, "ns", kt)
	resp := h.Handle(context.Background(), req)
	if resp.Allowed {
		t.Fatalf("expected UID=0 to still be denied when ns fetch fails")
	}
	if !strings.Contains(resp.Result.Message, "positive integer") {
		t.Errorf("expected UID>0 message, got: %s", resp.Result.Message)
	}
}
