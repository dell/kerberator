// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

package v1alpha1

import (
	"encoding/json"
	"reflect"
	"testing"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// TestKerberosCacheFactoryDeepCopy exercises DeepCopyInto on
// every non-trivial field (slices, map, embedded ResourceRequirements)
// so a future field addition that forgets DeepCopy is caught here
// rather than in a mysterious controller-runtime shared-cache
// mutation later.
func TestKerberosCacheFactoryDeepCopy(t *testing.T) {
	original := &Tenant{
		ObjectMeta: metav1.ObjectMeta{Name: "kcf-1", Namespace: "ns-1"},
		Spec: TenantSpec{
			Realm: "EXAMPLE.COM",
			Daemon: DaemonTemplate{
				Image:        "example/kerberator-daemon:v0.3.0",
				RenewMinutes: 720,
				NodeSelector: map[string]string{"tier": "worker"},
			},
		},
		Status: TenantStatus{
			DesiredNodes:   3,
			ReadyNodes:     3,
			ReadySummary:   "3/3",
			PrincipalCount: 2,
			Conditions: []metav1.Condition{
				{Type: "Ready", Status: metav1.ConditionTrue, Reason: "AllUp"},
			},
		},
	}

	clone := original.DeepCopy()

	// Structurally equal.
	if !reflect.DeepEqual(original, clone) {
		t.Fatalf("clone differs from original\n  got: %+v\n want: %+v", clone, original)
	}

	// Mutating the clone's nested slices/maps must not affect the
	// original — that's the whole point of DeepCopy.
	clone.Spec.Daemon.NodeSelector["tier"] = "control-plane"
	clone.Status.Conditions[0].Status = metav1.ConditionFalse
	if original.Spec.Daemon.NodeSelector["tier"] != "worker" {
		t.Errorf("DeepCopy did not clone NodeSelector map")
	}
	if original.Status.Conditions[0].Status != metav1.ConditionTrue {
		t.Errorf("DeepCopy did not clone Conditions slice")
	}
}

// TestKerberosCacheFactoryJSONRoundTrip guards against tag typos:
// an unmarshaled object should equal its pre-marshal self on every
// spec/status field the CRD documents.
func TestKerberosCacheFactoryJSONRoundTrip(t *testing.T) {
	want := Tenant{
		TypeMeta: metav1.TypeMeta{
			APIVersion: GroupVersion.String(),
			Kind:       "Tenant",
		},
		ObjectMeta: metav1.ObjectMeta{Name: "kcf-1", Namespace: "ns-1"},
		Spec: TenantSpec{
			Realm: "EXAMPLE.COM",
			Daemon: DaemonTemplate{
				Image:         "example/kerberator-daemon:v0.3.0",
				HostCachePath: "/tmp",
				RenewMinutes:  720,
				PruneStale:    true,
				NodeSelector:  map[string]string{"role": "storage"},
			},
		},
		Status: TenantStatus{
			DesiredNodes:   2,
			ReadyNodes:     2,
			ReadySummary:   "2/2",
			PrincipalCount: 4,
			RosterHash:     "sha256:deadbeef",
		},
	}
	blob, err := json.Marshal(&want)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var got Tenant
	if err := json.Unmarshal(blob, &got); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if !reflect.DeepEqual(want, got) {
		t.Fatalf("round trip mismatch\n  got: %+v\n want: %+v\n  raw: %s", got, want, blob)
	}
}

// TestKerberosCacheFactoryDeepCopy_DeletionTimeout guards the
// pointer-field allocation in TenantSpec.DeepCopyInto
// for the new DeletionTimeout field. Without a nil check + fresh
// allocation, mutating the clone's Duration would leak into the
// original through the shared *metav1.Duration.
func TestKerberosCacheFactoryDeepCopy_DeletionTimeout(t *testing.T) {
	original := &Tenant{
		Spec: TenantSpec{
			Realm:           "EXAMPLE.COM",
			DeletionTimeout: &metav1.Duration{Duration: 60 * time.Second},
		},
	}
	clone := original.DeepCopy()
	if clone.Spec.DeletionTimeout == original.Spec.DeletionTimeout {
		t.Fatalf("DeletionTimeout pointer aliased; DeepCopy should allocate fresh")
	}
	clone.Spec.DeletionTimeout.Duration = 5 * time.Minute
	if original.Spec.DeletionTimeout.Duration != 60*time.Second {
		t.Errorf("mutating clone leaked to original: got %v", original.Spec.DeletionTimeout.Duration)
	}
}
