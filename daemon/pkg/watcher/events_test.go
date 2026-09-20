// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

package watcher

import (
	"context"
	"errors"
	"os"
	"sort"
	"sync"
	"testing"

	"github.com/dell/kerberator/daemon/internal/events"
)

// spyRecorder captures every call in-order so the tests can
// assert the mint loop emits the expected sequence per UID.
// Implements events.Recorder.
type spyRecorder struct {
	mu      sync.Mutex
	success []int
	failed  []int
	pruned  []int
	missing []int
	lastMsg string // last MintFailed error text, for assertion
}

func (s *spyRecorder) MintSucceeded(uid int, _ string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.success = append(s.success, uid)
}

func (s *spyRecorder) MintFailed(uid int, _ string, err error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.failed = append(s.failed, uid)
	if err != nil {
		s.lastMsg = err.Error()
	}
}

func (s *spyRecorder) Pruned(uid int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.pruned = append(s.pruned, uid)
}

func (s *spyRecorder) KeytabMissing(uid int, _, _ string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.missing = append(s.missing, uid)
}

// TestProcessRoster_EmitsMintSucceededEvents verifies the happy
// path: every successful mint produces one MintSucceeded event
// keyed by UID.
func TestProcessRoster_EmitsMintSucceededEvents(t *testing.T) {
	roster := "1001:svc/a@R:a.keytab\n1002:svc/b@R:b.keytab\n"
	p, _ := setupProcessor(t, roster, map[string][]byte{"a.keytab": {1}, "b.keytab": {2}})
	spy := &spyRecorder{}
	p.Events = spy

	ok, fail := p.ProcessRoster(context.Background())
	if ok != 2 || fail != 0 {
		t.Fatalf("want ok=2 fail=0, got ok=%d fail=%d", ok, fail)
	}
	sort.Ints(spy.success)
	if len(spy.success) != 2 || spy.success[0] != 1001 || spy.success[1] != 1002 {
		t.Errorf("expected MintSucceeded for uids [1001 1002], got %v", spy.success)
	}
	if len(spy.failed)+len(spy.missing)+len(spy.pruned) != 0 {
		t.Errorf("expected only success events, got failed=%v missing=%v pruned=%v",
			spy.failed, spy.missing, spy.pruned)
	}
}

// TestProcessRoster_EmitsMintFailedWithError verifies the failure
// path: a Minter error surfaces as a MintFailed event carrying
// the wrapped error text (the operator's user reconciler will
// grep this).
func TestProcessRoster_EmitsMintFailedWithError(t *testing.T) {
	roster := "1001:svc/a@R:a.keytab\n1002:svc/b@R:b.keytab\n"
	p, fm := setupProcessor(t, roster, map[string][]byte{"a.keytab": {1}, "b.keytab": {2}})
	fm.failOn["svc/b@R"] = errors.New("KDC unreachable")
	spy := &spyRecorder{}
	p.Events = spy

	_, _ = p.ProcessRoster(context.Background())
	if len(spy.failed) != 1 || spy.failed[0] != 1002 {
		t.Fatalf("expected MintFailed for uid=1002 exactly once, got %v", spy.failed)
	}
	if spy.lastMsg != "KDC unreachable" {
		t.Errorf("expected error text propagated to recorder, got %q", spy.lastMsg)
	}
	if len(spy.success) != 1 || spy.success[0] != 1001 {
		t.Errorf("expected success for uid=1001 to still fire, got %v", spy.success)
	}
}

// TestProcessRoster_EmitsKeytabMissing verifies the file-check
// path: a missing keytab produces a KeytabMissing event (NOT a
// MintFailed event -- they are distinct reasons so operators can
// tell config errors from runtime errors).
func TestProcessRoster_EmitsKeytabMissing(t *testing.T) {
	roster := "1001:svc/a@R:present.keytab\n1002:svc/b@R:absent.keytab\n"
	p, _ := setupProcessor(t, roster, map[string][]byte{"present.keytab": {1}})
	spy := &spyRecorder{}
	p.Events = spy

	_, _ = p.ProcessRoster(context.Background())
	if len(spy.missing) != 1 || spy.missing[0] != 1002 {
		t.Errorf("expected KeytabMissing for uid=1002, got %v", spy.missing)
	}
	if len(spy.failed) != 0 {
		t.Errorf("KeytabMissing must not double-emit as MintFailed; got failed=%v", spy.failed)
	}
}

// TestProcessRoster_EmitsPrunedForRemovedUID verifies the prune
// path. Two sweeps: the first mints uid=1001. The second sees a
// roster with only uid=1002 in it, so uid=1001 gets pruned and
// must emit a KerberosCcachePruned event -- this is the event
// the operator's finalizer waits on before allowing user
// deletion.
func TestProcessRoster_EmitsPrunedForRemovedUID(t *testing.T) {
	roster1 := "1001:svc/a@R:a.keytab\n"
	p, _ := setupProcessor(t, roster1, map[string][]byte{"a.keytab": {1}, "b.keytab": {2}})
	p.PruneStale = true
	// The fakeMinter used by setupProcessor doesn't actually
	// create ccache files, so real os.Remove would return
	// ErrNotExist and pruneStale would silently skip the event.
	// Inject a Remove that always succeeds so the event path
	// runs.
	p.Remove = func(string) error { return nil }
	spy := &spyRecorder{}
	p.Events = spy

	_, _ = p.ProcessRoster(context.Background())
	// Rewrite the roster so uid=1001 is gone and uid=1002 appears.
	roster2 := "1002:svc/b@R:b.keytab\n"
	if err := os.WriteFile(p.RosterPath, []byte(roster2), 0644); err != nil {
		t.Fatal(err)
	}
	_, _ = p.ProcessRoster(context.Background())

	if len(spy.pruned) != 1 || spy.pruned[0] != 1001 {
		t.Errorf("expected Pruned for uid=1001 in second sweep, got %v", spy.pruned)
	}
}

// TestProcessRoster_NilEventsIsSafe verifies the nil-guard: a
// Processor built by hand (bypassing NewProcessor) with a nil
// Events field must not panic. This mirrors what a couple of
// older tests in watcher_test.go do.
func TestProcessRoster_NilEventsIsSafe(t *testing.T) {
	roster := "1001:svc/a@R:a.keytab\n"
	p, _ := setupProcessor(t, roster, map[string][]byte{"a.keytab": {1}})
	p.Events = nil // explicit for the test's intent

	// No panic allowed.
	ok, fail := p.ProcessRoster(context.Background())
	if ok != 1 || fail != 0 {
		t.Fatalf("want ok=1 fail=0, got ok=%d fail=%d", ok, fail)
	}
	// Emit* helpers deliberately no-op on nil; nothing else to
	// assert.
	_ = events.NopRecorder{} // keep the import used
}
