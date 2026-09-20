// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

package events

import (
	"sync"
	"time"
)

// Observation is a single per-UID event as recorded by the
// tenant. Keyed in the cache by (podName, uid); overwritten in
// place when a newer event for the same key arrives.
//
// Fields are exported so BuildNodeStatuses can copy them into a
// NodeStatus verbatim.
type Observation struct {
	// Reason is the raw tenant reason string (e.g.
	// "KerberosMintFailed"). Consumers apply ShortReason to
	// get the display form.
	Reason string

	// Message is the event's message text, unmodified.
	Message string

	// Timestamp is the event's own timestamp (LastTimestamp or
	// EventTime, whichever is set), captured at ingest.
	Timestamp time.Time

	// PodName / UID reproduce the cache key on the value side
	// so consumers that get an Observation via Snapshot don't
	// have to plumb the key separately.
	PodName string
	UID     int64
}

// key uniquely identifies one (pod, uid) observation. Kept as a
// value type so the map avoids allocation on lookup.
type key struct {
	pod string
	uid int64
}

// Cache is a thread-safe in-memory map of (podName, uid) to the
// most recent Observation for that pair. It's the sole state
// EventReconciler owns.
//
// Eviction:
//
//   - GC(now) removes entries whose Timestamp is older than TTL
//     (default 24h). Called on a timer from cmd/manager.
//
//   - EvictUID(uid) removes every entry for the given UID
//     across all pods. Called from the principal finalizer when a
//     Principal is being deleted, so a stale event from a
//     previous incarnation of the same UID doesn't leak into a
//     replacement principal's status.
//
//   - EvictPod(podName) removes every entry for a given pod
//     across all UIDs. Called when a tenant Pod is deleted so
//     dead pods don't linger. (The event reconciler can safely
//     skip this and let TTL GC clean up; EvictPod is provided
//     for cases where deterministic cleanup is desirable, e.g.
//     tests.)
//
// The zero value is not usable; use NewCache.
type Cache struct {
	mu   sync.RWMutex
	ttl  time.Duration
	data map[key]Observation
}

// DefaultTTL is how long an observation stays in the cache
// before GC removes it. Chosen well beyond the K8s Event object
// TTL (1h by default) so a stale-but-still-authoritative
// observation doesn't get lost between GC cycles.
const DefaultTTL = 24 * time.Hour

// NewCache returns an initialized Cache with the given TTL.
// A ttl of zero or negative resolves to DefaultTTL; this makes
// NewCache(0) the "just give me sensible defaults" constructor.
func NewCache(ttl time.Duration) *Cache {
	if ttl <= 0 {
		ttl = DefaultTTL
	}
	return &Cache{
		ttl:  ttl,
		data: make(map[key]Observation),
	}
}

// TTL returns the configured cache TTL. Exported so tests can
// assert on it without exporting the field.
func (c *Cache) TTL() time.Duration { return c.ttl }

// Put records or overwrites the observation for (podName, uid).
// Observations older than the currently-cached one are
// discarded so an out-of-order event delivery (K8s does not
// guarantee event ordering) does not clobber a fresher entry.
func (c *Cache) Put(o Observation) {
	if o.PodName == "" || o.UID <= 0 {
		// Defensive: the reconciler filters these out before
		// calling Put. If a corrupt event reaches here we
		// silently drop it -- letting an empty key into the
		// cache would collide across every pod.
		return
	}
	k := key{pod: o.PodName, uid: o.UID}
	c.mu.Lock()
	defer c.mu.Unlock()
	if existing, ok := c.data[k]; ok && existing.Timestamp.After(o.Timestamp) {
		return
	}
	c.data[k] = o
}

// Lookup returns the observation for (podName, uid) and whether
// one was present. Copies the value out so callers can't mutate
// the cache through the returned struct.
func (c *Cache) Lookup(podName string, uid int64) (Observation, bool) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	o, ok := c.data[key{pod: podName, uid: uid}]
	return o, ok
}

// EvictUID removes every entry for the given uid across all
// pods. Idempotent; safe to call on a UID that was never
// observed.
func (c *Cache) EvictUID(uid int64) {
	c.mu.Lock()
	defer c.mu.Unlock()
	for k := range c.data {
		if k.uid == uid {
			delete(c.data, k)
		}
	}
}

// EvictPod removes every entry for the given pod across all
// uids. Idempotent.
func (c *Cache) EvictPod(podName string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	for k := range c.data {
		if k.pod == podName {
			delete(c.data, k)
		}
	}
}

// GC removes entries whose Timestamp is older than TTL. Returns
// the number of entries evicted; the count is used by the
// caller (usually cmd/manager on a wall-clock ticker) to log
// GC activity.
func (c *Cache) GC(now time.Time) int {
	cutoff := now.Add(-c.ttl)
	c.mu.Lock()
	defer c.mu.Unlock()
	n := 0
	for k, o := range c.data {
		if o.Timestamp.Before(cutoff) {
			delete(c.data, k)
			n++
		}
	}
	return n
}

// Len returns the current entry count. Exposed for tests and
// metrics; production code should not branch on it.
func (c *Cache) Len() int {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return len(c.data)
}
