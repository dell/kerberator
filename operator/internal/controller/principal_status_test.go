// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

package controller

import (
	"testing"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	v1alpha1 "github.com/dell/kerberator/operator/api/v1alpha1"
	"github.com/dell/kerberator/operator/internal/events"
)

func TestResolveStaleness(t *testing.T) {
	// nil tenant -> floor.
	if got := ResolveStaleness(nil); got != MinEventStaleness {
		t.Errorf("nil tenant: got %s want %s", got, MinEventStaleness)
	}

	// Explicit override wins.
	kcf := &v1alpha1.Tenant{
		Spec: v1alpha1.TenantSpec{
			EventStalenessThreshold: &metav1.Duration{Duration: 42 * time.Minute},
			Daemon:                  v1alpha1.DaemonTemplate{RenewMinutes: 60},
		},
	}
	if got := ResolveStaleness(kcf); got != 42*time.Minute {
		t.Errorf("explicit override: got %s want 42m", got)
	}

	// Derived from renew minutes, above floor.
	kcf.Spec.EventStalenessThreshold = nil
	kcf.Spec.Daemon.RenewMinutes = 60
	if got := ResolveStaleness(kcf); got != 2*time.Hour {
		t.Errorf("derived: got %s want 2h", got)
	}

	// Derived below floor -> floor.
	kcf.Spec.Daemon.RenewMinutes = 1
	if got := ResolveStaleness(kcf); got != MinEventStaleness {
		t.Errorf("derived below floor: got %s want %s", got, MinEventStaleness)
	}

	// No renew, no override -> floor.
	kcf.Spec.Daemon.RenewMinutes = 0
	if got := ResolveStaleness(kcf); got != MinEventStaleness {
		t.Errorf("no signal: got %s want %s", got, MinEventStaleness)
	}
}

func TestBuildNodeStatuses_HealthNotFound(t *testing.T) {
	kt := &v1alpha1.Principal{Spec: v1alpha1.PrincipalSpec{UID: 1000}}
	out := BuildNodeStatuses(kt, TenantHealth{Found: false}, events.NewCache(0), time.Now(), MinEventStaleness)
	if out != nil {
		t.Errorf("expected nil when health not found, got %+v", out)
	}
}

func TestBuildNodeStatuses_FallbackNoCache(t *testing.T) {
	kt := &v1alpha1.Principal{Spec: v1alpha1.PrincipalSpec{UID: 1000}}
	h := TenantHealth{
		Found: true,
		Nodes: []v1alpha1.NodeStatus{
			{Name: "nodeB", DaemonPodReady: true},
			{Name: "nodeA", DaemonPodReady: false, Reason: "NotReady"},
		},
		PodByNode: map[string]string{"nodeA": "podA", "nodeB": "podB"},
	}
	out := BuildNodeStatuses(kt, h, nil, time.Now(), MinEventStaleness)
	if len(out) != 2 {
		t.Fatalf("expected 2 entries, got %d", len(out))
	}
	if out[0].Name != "nodeA" || out[1].Name != "nodeB" {
		t.Errorf("not sorted: %+v", out)
	}
	if out[0].Reason != "NotReady" || out[1].Reason != "" {
		t.Errorf("fallback fields lost: %+v", out)
	}
	// Message must remain empty when there's no cache.
	if out[0].Message != "" {
		t.Errorf("expected empty Message on fallback, got %q", out[0].Message)
	}
}

func TestBuildNodeStatuses_EventEnrichment(t *testing.T) {
	kt := &v1alpha1.Principal{Spec: v1alpha1.PrincipalSpec{UID: 1000}}
	now := time.Now()
	c := events.NewCache(0)
	c.Put(events.Observation{
		PodName:   "podA",
		UID:       1000,
		Reason:    events.ReasonMintFailed,
		Message:   "uid=1000 mint failed: krb5 kdc",
		Timestamp: now.Add(-30 * time.Second),
	})
	h := TenantHealth{
		Found: true,
		Nodes: []v1alpha1.NodeStatus{
			{Name: "nodeA", DaemonPodReady: true, Reason: ""},
			{Name: "nodeB", DaemonPodReady: true, Reason: ""},
		},
		PodByNode: map[string]string{"nodeA": "podA", "nodeB": "podB"},
	}
	out := BuildNodeStatuses(kt, h, c, now, MinEventStaleness)
	if len(out) != 2 {
		t.Fatalf("expected 2 entries, got %d", len(out))
	}
	nodeA := out[0]
	if nodeA.Name != "nodeA" {
		t.Fatalf("sort broken: %+v", out)
	}
	if nodeA.Reason != "MintFailed" {
		t.Errorf("expected short reason MintFailed, got %q", nodeA.Reason)
	}
	if nodeA.Message != "uid=1000 mint failed: krb5 kdc" {
		t.Errorf("message not carried: %q", nodeA.Message)
	}
	// Underlying pod-readiness bit is left alone.
	if !nodeA.DaemonPodReady {
		t.Error("DaemonPodReady overwritten")
	}
	// nodeB has no cache entry -> fallback (no enrichment).
	if out[1].Reason != "" || out[1].Message != "" {
		t.Errorf("nodeB unexpectedly enriched: %+v", out[1])
	}
}

func TestBuildNodeStatuses_StaleObservationIgnored(t *testing.T) {
	kt := &v1alpha1.Principal{Spec: v1alpha1.PrincipalSpec{UID: 1000}}
	now := time.Now()
	c := events.NewCache(0)
	c.Put(events.Observation{
		PodName:   "podA",
		UID:       1000,
		Reason:    events.ReasonMintFailed,
		Message:   "uid=1000 stale mint failed",
		Timestamp: now.Add(-2 * MinEventStaleness),
	})
	h := TenantHealth{
		Found:     true,
		Nodes:     []v1alpha1.NodeStatus{{Name: "nodeA", DaemonPodReady: true}},
		PodByNode: map[string]string{"nodeA": "podA"},
	}
	out := BuildNodeStatuses(kt, h, c, now, MinEventStaleness)
	if len(out) != 1 {
		t.Fatalf("got %+v", out)
	}
	if out[0].Reason != "" || out[0].Message != "" {
		t.Errorf("stale observation leaked into status: %+v", out[0])
	}
}

func TestBuildNodeStatuses_NilPodByNodeFallsBack(t *testing.T) {
	kt := &v1alpha1.Principal{Spec: v1alpha1.PrincipalSpec{UID: 1000}}
	c := events.NewCache(0)
	c.Put(events.Observation{PodName: "p", UID: 1000, Reason: events.ReasonMintSucceeded, Timestamp: time.Now()})
	h := TenantHealth{
		Found: true,
		Nodes: []v1alpha1.NodeStatus{{Name: "n", DaemonPodReady: true}},
		// PodByNode intentionally nil (test-hand-built).
	}
	out := BuildNodeStatuses(kt, h, c, time.Now(), MinEventStaleness)
	if out[0].Reason != "" {
		t.Errorf("nil PodByNode must fall back; got Reason=%q", out[0].Reason)
	}
}
