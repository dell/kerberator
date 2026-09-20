// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

package events

import (
	"testing"
	"time"
)

func TestCache_PutLookup(t *testing.T) {
	c := NewCache(time.Hour)
	now := time.Now()
	c.Put(Observation{PodName: "p1", UID: 1000, Reason: ReasonMintSucceeded, Message: "uid=1000 minted", Timestamp: now})
	got, ok := c.Lookup("p1", 1000)
	if !ok {
		t.Fatal("expected Lookup hit")
	}
	if got.Reason != ReasonMintSucceeded || got.Message != "uid=1000 minted" {
		t.Errorf("wrong observation returned: %+v", got)
	}
	if _, ok := c.Lookup("p1", 9999); ok {
		t.Error("expected miss for wrong uid")
	}
	if _, ok := c.Lookup("other", 1000); ok {
		t.Error("expected miss for wrong pod")
	}
}

func TestCache_Put_DropsInvalidKey(t *testing.T) {
	c := NewCache(0)
	c.Put(Observation{PodName: "", UID: 1000, Timestamp: time.Now()})
	c.Put(Observation{PodName: "p", UID: 0, Timestamp: time.Now()})
	if c.Len() != 0 {
		t.Errorf("empty-key observations must be dropped; got Len=%d", c.Len())
	}
}

func TestCache_Put_KeepsNewerTimestamp(t *testing.T) {
	c := NewCache(time.Hour)
	old := time.Now().Add(-time.Minute)
	newer := time.Now()
	c.Put(Observation{PodName: "p", UID: 1, Reason: "A", Timestamp: newer})
	c.Put(Observation{PodName: "p", UID: 1, Reason: "B", Timestamp: old})
	got, _ := c.Lookup("p", 1)
	if got.Reason != "A" {
		t.Errorf("stale write clobbered newer entry: got Reason=%s", got.Reason)
	}
}

func TestCache_EvictUID(t *testing.T) {
	c := NewCache(0)
	now := time.Now()
	c.Put(Observation{PodName: "p1", UID: 1, Timestamp: now})
	c.Put(Observation{PodName: "p2", UID: 1, Timestamp: now})
	c.Put(Observation{PodName: "p1", UID: 2, Timestamp: now})
	c.EvictUID(1)
	if _, ok := c.Lookup("p1", 1); ok {
		t.Error("EvictUID left p1/1 in cache")
	}
	if _, ok := c.Lookup("p2", 1); ok {
		t.Error("EvictUID left p2/1 in cache")
	}
	if _, ok := c.Lookup("p1", 2); !ok {
		t.Error("EvictUID also removed unrelated uid")
	}
}

func TestCache_EvictPod(t *testing.T) {
	c := NewCache(0)
	now := time.Now()
	c.Put(Observation{PodName: "p1", UID: 1, Timestamp: now})
	c.Put(Observation{PodName: "p1", UID: 2, Timestamp: now})
	c.Put(Observation{PodName: "p2", UID: 1, Timestamp: now})
	c.EvictPod("p1")
	if _, ok := c.Lookup("p1", 1); ok {
		t.Error("EvictPod left p1/1 in cache")
	}
	if _, ok := c.Lookup("p2", 1); !ok {
		t.Error("EvictPod also removed unrelated pod")
	}
}

func TestCache_GC(t *testing.T) {
	c := NewCache(time.Minute)
	now := time.Now()
	c.Put(Observation{PodName: "p", UID: 1, Timestamp: now.Add(-2 * time.Minute)})
	c.Put(Observation{PodName: "p", UID: 2, Timestamp: now})
	n := c.GC(now)
	if n != 1 {
		t.Errorf("expected 1 eviction, got %d", n)
	}
	if _, ok := c.Lookup("p", 1); ok {
		t.Error("GC left expired entry")
	}
	if _, ok := c.Lookup("p", 2); !ok {
		t.Error("GC removed fresh entry")
	}
}

func TestCache_NewCache_DefaultTTL(t *testing.T) {
	if got := NewCache(0).TTL(); got != DefaultTTL {
		t.Errorf("NewCache(0) TTL=%s want %s", got, DefaultTTL)
	}
	if got := NewCache(-time.Second).TTL(); got != DefaultTTL {
		t.Errorf("NewCache(negative) TTL=%s want %s", got, DefaultTTL)
	}
	if got := NewCache(5 * time.Minute).TTL(); got != 5*time.Minute {
		t.Errorf("NewCache(5m) TTL=%s", got)
	}
}
