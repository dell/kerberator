// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

package v1alpha1

import (
	"encoding/json"
	"reflect"
	"testing"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// TestKerberosPrincipalJSONRoundTrip exercises the v1alpha1 spec
// shape: tenantRef.name + keytabSecretRef. Also asserts
// rosterLine round-trips through status.
func TestKerberosPrincipalJSONRoundTrip(t *testing.T) {
	want := Principal{
		TypeMeta: metav1.TypeMeta{
			APIVersion: GroupVersion.String(),
			Kind:       "Principal",
		},
		ObjectMeta: metav1.ObjectMeta{Name: "uid-2001", Namespace: "kerberator-demo"},
		Spec: PrincipalSpec{
			UID:       2001,
			Principal: "svc-2001@EXAMPLE.COM",
			TenantRef: TenantRef{
				Name: "demo",
			},
			KeytabSecretRef: &SecretKeyRef{
				Name: "demo-keytabs",
				Key:  "svc-2001.keytab",
			},
		},
		Status: PrincipalStatus{
			RosterLine: "2001:svc-2001@EXAMPLE.COM:svc-2001.keytab",
		},
	}
	blob, err := json.Marshal(&want)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var got Principal
	if err := json.Unmarshal(blob, &got); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if !reflect.DeepEqual(want, got) {
		t.Fatalf("round trip mismatch\n  got: %+v\n want: %+v\n  raw: %s", got, want, blob)
	}
}

// TestKerberosPrincipalDeepCopy_KeytabSecretRef exercises the new
// pointer field: mutating the clone's KeytabSecretRef must not
// affect the original. This is the field most likely to be
// broken by a future contributor since it's the only pointer in
// the spec.
func TestKerberosPrincipalDeepCopy_KeytabSecretRef(t *testing.T) {
	original := &Principal{
		Spec: PrincipalSpec{
			UID:       2001,
			Principal: "svc-2001@EXAMPLE.COM",
			TenantRef: TenantRef{Name: "kcf-1"},
			KeytabSecretRef: &SecretKeyRef{
				Name: "keytabs-a",
				Key:  "svc-2001.keytab",
			},
		},
	}
	clone := original.DeepCopy()

	if !reflect.DeepEqual(original, clone) {
		t.Fatalf("clone differs from original")
	}
	if clone.Spec.KeytabSecretRef == original.Spec.KeytabSecretRef {
		t.Fatalf("KeytabSecretRef pointer aliased; DeepCopy should allocate a new struct")
	}
	// Mutate the clone; the original must not observe it.
	clone.Spec.KeytabSecretRef.Name = "keytabs-b"
	clone.Spec.KeytabSecretRef.Key = "different.keytab"
	if original.Spec.KeytabSecretRef.Name != "keytabs-a" {
		t.Errorf("original mutated via clone: Name=%q", original.Spec.KeytabSecretRef.Name)
	}
	if original.Spec.KeytabSecretRef.Key != "svc-2001.keytab" {
		t.Errorf("original mutated via clone: Key=%q", original.Spec.KeytabSecretRef.Key)
	}
}

// TestKerberosPrincipalDeepCopy_NilKeytabSecretRef verifies the
// happy path where KeytabSecretRef is unset.
func TestKerberosPrincipalDeepCopy_NilKeytabSecretRef(t *testing.T) {
	original := &Principal{
		Spec: PrincipalSpec{
			UID:       2001,
			Principal: "svc-2001@EXAMPLE.COM",
			Keytab:    "svc-2001.keytab",
			TenantRef: TenantRef{Name: "kcf-1"},
		},
	}
	clone := original.DeepCopy()
	if clone.Spec.KeytabSecretRef != nil {
		t.Errorf("nil KeytabSecretRef should remain nil after DeepCopy, got %+v", clone.Spec.KeytabSecretRef)
	}
}
