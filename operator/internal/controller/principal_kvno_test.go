// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

package controller

import (
	"context"
	"encoding/base64"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"

	v1alpha1 "github.com/dell/kerberator/operator/api/v1alpha1"
)

// Same synthetic fixture the shared/keytab tests use: a ktutil-
// generated keytab with a throwaway key. KVNO=3, 4 enctypes,
// principal svc-2001@EXAMPLE.COM.
const fixtureKeytabBase64 = `BQIAAAA6AAEAC0VYQU1QTEUuQ09NAAhzdmMtMjAwMQAAAAFqsEpuAwARABDcXipRjo1+fOtYicCrvWZ5AAAAAwAAAEoAAQALRVhBTVBMRS5DT00ACHN2Yy0yMDAxAAAAAWqwSm4DABIAIOECd5L2xMaiucO+Zj6WmuPClzlqMTsgxvsF/U+R9J/9AAAAAwAAADoAAQALRVhBTVBMRS5DT00ACHN2Yy0yMDAxAAAAAWqwSm4DABMAEG7LDK4T7m46nIQ4o65tOrAAAAADAAAASgABAAtFWEFNUExFLkNPTQAIc3ZjLTIwMDEAAAABarBKbgMAFAAgL60kF+V673e4mdeA2MErpkABOvJ/sfuHkVumYcmljbUAAAAD`

func mkFixtureKeytabSecret(t *testing.T, name, key string, b64 string) *corev1.Secret {
	t.Helper()
	data, err := base64.StdEncoding.DecodeString(b64)
	if err != nil {
		t.Fatalf("decode fixture: %v", err)
	}
	return &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: finNS},
		Data:       map[string][]byte{key: data},
	}
}

func TestPublishKvnoStatus_ParsesFixtureKeytab(t *testing.T) {
	now := time.Date(2026, 8, 19, 12, 0, 0, 0, time.UTC)

	principal := mkManagePrincipal(nil, PrincipalFinalizer)
	// Principal already points at the Secret keytab-2001 with key
	// svc-a.keytab via mkManagePrincipal's default.
	sec := mkFixtureKeytabSecret(t, "keytab-2001", "svc-a.keytab", fixtureKeytabBase64)

	r := newPrincipalReconciler(now, principal, sec)

	if err := r.publishKvnoStatus(context.Background(), principal); err != nil {
		t.Fatalf("publishKvnoStatus: %v", err)
	}

	var got v1alpha1.Principal
	if err := r.Get(context.Background(), types.NamespacedName{Namespace: finNS, Name: finName}, &got); err != nil {
		t.Fatalf("re-get principal: %v", err)
	}

	if got.Status.KvnoFromSecret != 3 {
		t.Errorf("KvnoFromSecret: got %d, want 3", got.Status.KvnoFromSecret)
	}
	wantEnctypes := "aes128-cts-hmac-sha1-96, aes128-cts-hmac-sha256-128, aes256-cts-hmac-sha1-96, aes256-cts-hmac-sha384-192"
	if got.Status.EnctypesFromSecret != wantEnctypes {
		t.Errorf("EnctypesFromSecret:\n  got:  %q\n  want: %q", got.Status.EnctypesFromSecret, wantEnctypes)
	}
	if got.Status.KeytabObservedAt == nil {
		t.Fatalf("KeytabObservedAt: nil, want %v", now)
	}
	if !got.Status.KeytabObservedAt.Time.Equal(now) {
		t.Errorf("KeytabObservedAt: got %v, want %v", got.Status.KeytabObservedAt.Time, now)
	}
}

func TestPublishKvnoStatus_SecretMissing_ClearsStatus(t *testing.T) {
	now := time.Date(2026, 8, 19, 12, 0, 0, 0, time.UTC)

	// Pre-seed status with stale data so we can verify it gets
	// cleared when the Secret disappears.
	principal := mkManagePrincipal(nil, PrincipalFinalizer)
	prior := metav1.NewTime(now.Add(-1 * time.Hour))
	principal.Status = v1alpha1.PrincipalStatus{
		KvnoFromSecret:     5,
		EnctypesFromSecret: "aes256-cts-hmac-sha1-96",
		KeytabObservedAt:   &prior,
	}

	// No Secret in the fake client.
	r := newPrincipalReconciler(now, principal)

	if err := r.publishKvnoStatus(context.Background(), principal); err != nil {
		t.Fatalf("publishKvnoStatus: %v", err)
	}

	var got v1alpha1.Principal
	if err := r.Get(context.Background(), types.NamespacedName{Namespace: finNS, Name: finName}, &got); err != nil {
		t.Fatalf("re-get principal: %v", err)
	}

	if got.Status.KvnoFromSecret != 0 {
		t.Errorf("KvnoFromSecret: got %d, want 0 (cleared)", got.Status.KvnoFromSecret)
	}
	if got.Status.EnctypesFromSecret != "" {
		t.Errorf("EnctypesFromSecret: got %q, want empty", got.Status.EnctypesFromSecret)
	}
	if got.Status.KeytabObservedAt != nil {
		t.Errorf("KeytabObservedAt: got %v, want nil (cleared when Secret is gone)", got.Status.KeytabObservedAt)
	}
}

func TestPublishKvnoStatus_UnparseableBytes_ClearsKvno(t *testing.T) {
	now := time.Date(2026, 8, 19, 12, 0, 0, 0, time.UTC)

	principal := mkManagePrincipal(nil, PrincipalFinalizer)
	// Junk bytes — a 4-byte header with the wrong magic will
	// trip ErrUnsupportedVersion in the parser.
	sec := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{Name: "keytab-2001", Namespace: finNS},
		Data:       map[string][]byte{"svc-a.keytab": {0x00, 0x01, 0x02, 0x03}},
	}

	r := newPrincipalReconciler(now, principal, sec)

	if err := r.publishKvnoStatus(context.Background(), principal); err != nil {
		t.Fatalf("publishKvnoStatus: %v", err)
	}

	var got v1alpha1.Principal
	if err := r.Get(context.Background(), types.NamespacedName{Namespace: finNS, Name: finName}, &got); err != nil {
		t.Fatalf("re-get principal: %v", err)
	}

	// Unparseable ⇒ kvno=0. Operators seeing 0 know to look at
	// the Secret's actual bytes.
	if got.Status.KvnoFromSecret != 0 {
		t.Errorf("KvnoFromSecret: got %d, want 0 for junk bytes", got.Status.KvnoFromSecret)
	}
	if got.Status.KeytabObservedAt != nil {
		t.Errorf("KeytabObservedAt: got %v, want nil when parse failed", got.Status.KeytabObservedAt)
	}
}

func TestPublishKvnoStatus_NoOpOnUnchanged(t *testing.T) {
	// If a subsequent reconcile parses the same bytes it must
	// NOT bump KeytabObservedAt — that timestamp is for
	// "observed NEW bytes," not "reconciled again."
	firstObserved := time.Date(2026, 8, 19, 10, 0, 0, 0, time.UTC)
	secondReconcile := time.Date(2026, 8, 19, 12, 0, 0, 0, time.UTC)
	firstStamp := metav1.NewTime(firstObserved)

	principal := mkManagePrincipal(nil, PrincipalFinalizer)
	principal.Status = v1alpha1.PrincipalStatus{
		KvnoFromSecret:     3,
		EnctypesFromSecret: "aes128-cts-hmac-sha1-96, aes128-cts-hmac-sha256-128, aes256-cts-hmac-sha1-96, aes256-cts-hmac-sha384-192",
		KeytabObservedAt:   &firstStamp,
	}
	sec := mkFixtureKeytabSecret(t, "keytab-2001", "svc-a.keytab", fixtureKeytabBase64)

	r := newPrincipalReconciler(secondReconcile, principal, sec)

	if err := r.publishKvnoStatus(context.Background(), principal); err != nil {
		t.Fatalf("publishKvnoStatus: %v", err)
	}

	var got v1alpha1.Principal
	if err := r.Get(context.Background(), types.NamespacedName{Namespace: finNS, Name: finName}, &got); err != nil {
		t.Fatalf("re-get principal: %v", err)
	}

	if got.Status.KeytabObservedAt == nil {
		t.Fatalf("KeytabObservedAt: nil, want preserved %v", firstObserved)
	}
	if !got.Status.KeytabObservedAt.Time.Equal(firstObserved) {
		t.Errorf("KeytabObservedAt: got %v, want %v (should NOT bump on unchanged bytes)",
			got.Status.KeytabObservedAt.Time, firstObserved)
	}
}
