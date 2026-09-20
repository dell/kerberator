// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

package watcher

import (
	"bytes"
	"context"
	"errors"
	"io"
	"log"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/dell/kerberator/daemon/pkg/kerberos"
)

func TestParseRosterLine(t *testing.T) {
	cases := []struct {
		name    string
		line    string
		wantOK  bool
		wantErr string
		wantReq kerberos.TicketRequest
	}{
		{"empty", "", false, "", kerberos.TicketRequest{}},
		{"whitespace only", "   \t  ", false, "", kerberos.TicketRequest{}},
		{"comment", "# hello world", false, "", kerberos.TicketRequest{}},
		{"comment indented", "   # ignore me", false, "", kerberos.TicketRequest{}},
		{
			name:   "valid",
			line:   "1001:svc/agent@TENANT:svc.keytab",
			wantOK: true,
			wantReq: kerberos.TicketRequest{
				UID:        1001,
				Principal:  "svc/agent@TENANT",
				KeytabPath: filepath.Join("/keytabs", "svc.keytab"),
				CachePath:  filepath.Join("/host-tmp", "krb5cc_1001"),
			},
		},
		{
			name:   "valid with surrounding whitespace",
			line:   "  1002 : analytics/spark@TENANT : spark.keytab  ",
			wantOK: true,
			wantReq: kerberos.TicketRequest{
				UID:        1002,
				Principal:  "analytics/spark@TENANT",
				KeytabPath: filepath.Join("/keytabs", "spark.keytab"),
				CachePath:  filepath.Join("/host-tmp", "krb5cc_1002"),
			},
		},
		{"too few fields", "1001:svc@TENANT", false, "malformed", kerberos.TicketRequest{}},
		{"too many fields", "1001:svc:foo:bar", false, "malformed", kerberos.TicketRequest{}},
		{"non-numeric uid", "abc:svc@TENANT:svc.keytab", false, "invalid uid", kerberos.TicketRequest{}},
		{"empty principal", "1001::svc.keytab", false, "empty principal", kerberos.TicketRequest{}},
		{"empty keytab", "1001:svc@TENANT:", false, "empty principal or keytab", kerberos.TicketRequest{}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			req, ok, err := ParseRosterLine(tc.line, "/keytabs", "/host-tmp")
			if tc.wantErr != "" {
				if err == nil || !strings.Contains(err.Error(), tc.wantErr) {
					t.Fatalf("want err containing %q, got %v", tc.wantErr, err)
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if ok != tc.wantOK {
				t.Fatalf("want ok=%v, got %v", tc.wantOK, ok)
			}
			if ok && req != tc.wantReq {
				t.Fatalf("req mismatch:\n got=%+v\nwant=%+v", req, tc.wantReq)
			}
		})
	}
}

func TestParseRosterMixedContent(t *testing.T) {
	roster := `# comment header
1001:svc/a@R:a.keytab

1002:svc/b@R:b.keytab
bogus line
:missing:uid
1003:svc/c@R:c.keytab
`
	reqs, errs := ParseRoster(strings.NewReader(roster), "/kt", "/hc")
	if len(reqs) != 3 {
		t.Fatalf("want 3 valid requests, got %d: %+v", len(reqs), reqs)
	}
	if len(errs) != 2 {
		t.Fatalf("want 2 parse errors, got %d: %v", len(errs), errs)
	}
}

// erroringReader lets us verify scanner.Err() is surfaced.
type erroringReader struct{ n int }

func (e *erroringReader) Read(p []byte) (int, error) {
	if e.n == 0 {
		e.n++
		copy(p, []byte("1001:svc@R:svc.keytab\n"))
		return len("1001:svc@R:svc.keytab\n"), nil
	}
	return 0, errors.New("boom")
}

func TestParseRosterSurfacesScannerError(t *testing.T) {
	_, errs := ParseRoster(&erroringReader{}, "/kt", "/hc")
	found := false
	for _, e := range errs {
		if strings.Contains(e.Error(), "scanner") {
			found = true
		}
	}
	if !found {
		t.Fatalf("expected scanner error, got %v", errs)
	}
}

func TestHashFileDetectsChanges(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "roster")
	if err := os.WriteFile(path, []byte("1001:svc@R:svc.keytab\n"), 0644); err != nil {
		t.Fatal(err)
	}
	h1, err := HashFile(path)
	if err != nil {
		t.Fatal(err)
	}
	h2, err := HashFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if h1 != h2 {
		t.Fatalf("hash should be stable across identical reads: %s vs %s", h1, h2)
	}
	if err := os.WriteFile(path, []byte("1002:other@R:o.keytab\n"), 0644); err != nil {
		t.Fatal(err)
	}
	h3, err := HashFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if h1 == h3 {
		t.Fatalf("hash should change after file mutation")
	}
}

func TestHashFileMissing(t *testing.T) {
	_, err := HashFile("/does/not/exist/xyz")
	if err == nil {
		t.Fatalf("expected error for missing file")
	}
}

// fakeMinter is an in-memory kerberos.Minter.
type fakeMinter struct {
	mu     sync.Mutex
	calls  []kerberos.TicketRequest
	failOn map[string]error
}

func (f *fakeMinter) Mint(_ context.Context, req kerberos.TicketRequest) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls = append(f.calls, req)
	if err, ok := f.failOn[req.Principal]; ok {
		return err
	}
	return nil
}

// setupProcessor creates a Processor with a fake minter and a real roster/keytab dir.
func setupProcessor(t *testing.T, roster string, keytabs map[string][]byte) (*Processor, *fakeMinter) {
	t.Helper()
	dir := t.TempDir()
	keytabDir := filepath.Join(dir, "keytabs")
	cacheDir := filepath.Join(dir, "cache")
	if err := os.MkdirAll(keytabDir, 0755); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(cacheDir, 0755); err != nil {
		t.Fatal(err)
	}
	for name, body := range keytabs {
		if err := os.WriteFile(filepath.Join(keytabDir, name), body, 0600); err != nil {
			t.Fatal(err)
		}
	}
	rosterPath := filepath.Join(dir, "roster")
	if err := os.WriteFile(rosterPath, []byte(roster), 0644); err != nil {
		t.Fatal(err)
	}
	fm := &fakeMinter{failOn: map[string]error{}}
	p := &Processor{
		RosterPath: rosterPath,
		KeytabDir:  keytabDir,
		CacheDir:   cacheDir,
		Minter:     fm,
		Logger:     log.New(io.Discard, "", 0),
	}
	return p, fm
}

func TestProcessRosterHappyPath(t *testing.T) {
	roster := "1001:svc/a@R:a.keytab\n1002:svc/b@R:b.keytab\n"
	p, fm := setupProcessor(t, roster, map[string][]byte{"a.keytab": {1}, "b.keytab": {2}})
	ok, fail := p.ProcessRoster(context.Background())
	if ok != 2 || fail != 0 {
		t.Fatalf("want ok=2 fail=0, got ok=%d fail=%d", ok, fail)
	}
	if len(fm.calls) != 2 {
		t.Fatalf("expected 2 mint calls, got %d", len(fm.calls))
	}
}

func TestProcessRosterMissingKeytab(t *testing.T) {
	roster := "1001:svc/a@R:a.keytab\n1002:svc/b@R:missing.keytab\n"
	p, fm := setupProcessor(t, roster, map[string][]byte{"a.keytab": {1}})
	ok, fail := p.ProcessRoster(context.Background())
	if ok != 1 || fail != 1 {
		t.Fatalf("want ok=1 fail=1, got ok=%d fail=%d", ok, fail)
	}
	if len(fm.calls) != 1 {
		t.Fatalf("mint should only be called for present keytab, got %d calls", len(fm.calls))
	}
}

func TestProcessRosterMintError(t *testing.T) {
	roster := "1001:svc/a@R:a.keytab\n1002:svc/b@R:b.keytab\n"
	p, fm := setupProcessor(t, roster, map[string][]byte{"a.keytab": {1}, "b.keytab": {2}})
	fm.failOn["svc/b@R"] = errors.New("kdc down")
	ok, fail := p.ProcessRoster(context.Background())
	if ok != 1 || fail != 1 {
		t.Fatalf("want ok=1 fail=1, got ok=%d fail=%d", ok, fail)
	}
}

func TestProcessRosterMalformedLinesCountAsFailures(t *testing.T) {
	roster := "1001:svc/a@R:a.keytab\nbogus\n"
	p, _ := setupProcessor(t, roster, map[string][]byte{"a.keytab": {1}})
	ok, fail := p.ProcessRoster(context.Background())
	if ok != 1 || fail != 1 {
		t.Fatalf("want ok=1 fail=1, got ok=%d fail=%d", ok, fail)
	}
}

func TestProcessRosterMissingRoster(t *testing.T) {
	p := &Processor{
		RosterPath: "/nope/nowhere",
		KeytabDir:  "/kt",
		CacheDir:   "/hc",
		Minter:     &fakeMinter{failOn: map[string]error{}},
		Logger:     log.New(io.Discard, "", 0),
	}
	ok, fail := p.ProcessRoster(context.Background())
	if ok != 0 || fail != 1 {
		t.Fatalf("want ok=0 fail=1, got ok=%d fail=%d", ok, fail)
	}
}

func TestStartLoopReturnsOnContextCancel(t *testing.T) {
	roster := "1001:svc/a@R:a.keytab\n"
	p, _ := setupProcessor(t, roster, map[string][]byte{"a.keytab": {1}})
	p.Poll = 10 * time.Millisecond
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- p.StartLoop(ctx, 720) }()
	time.Sleep(30 * time.Millisecond)
	cancel()
	select {
	case err := <-done:
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("want context.Canceled, got %v", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("StartLoop did not return after cancel")
	}
}

func TestStartLoopDetectsRosterChange(t *testing.T) {
	roster := "1001:svc/a@R:a.keytab\n"
	p, fm := setupProcessor(t, roster, map[string][]byte{"a.keytab": {1}, "b.keytab": {2}})
	p.Poll = 5 * time.Millisecond
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() { _ = p.StartLoop(ctx, 720); close(done) }()

	// Wait for initial sweep to record 1 call.
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		fm.mu.Lock()
		n := len(fm.calls)
		fm.mu.Unlock()
		if n >= 1 {
			break
		}
		time.Sleep(5 * time.Millisecond)
	}

	// Mutate the roster.
	if err := os.WriteFile(p.RosterPath, []byte("1001:svc/a@R:a.keytab\n1002:svc/b@R:b.keytab\n"), 0644); err != nil {
		t.Fatal(err)
	}

	// Wait for the loop to observe the change.
	deadline = time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		fm.mu.Lock()
		n := len(fm.calls)
		fm.mu.Unlock()
		if n >= 3 { // initial=1 + reprocess=2
			cancel()
			<-done
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatalf("expected roster change to trigger reprocess; calls=%d", len(fm.calls))
}

func TestStartLoopBadInitialHash(t *testing.T) {
	p := &Processor{
		RosterPath: "/nope/nowhere",
		Logger:     log.New(io.Discard, "", 0),
		Minter:     &fakeMinter{failOn: map[string]error{}},
		Poll:       10 * time.Millisecond,
	}
	err := p.StartLoop(context.Background(), 720)
	if err == nil {
		t.Fatalf("expected initial hash error")
	}
}

// --------------------------------------------------------------------
// Stale-ccache prune
// --------------------------------------------------------------------

// setupProcessorWithPrune extends setupProcessor to (a) enable pruning
// and (b) wire a Remove hook that touches the cache directory so tests
// can verify what was deleted vs. left alone.
func setupProcessorWithPrune(t *testing.T, roster string, keytabs map[string][]byte) (*Processor, *fakeMinter) {
	t.Helper()
	p, fm := setupProcessor(t, roster, keytabs)
	p.PruneStale = true
	p.Remove = os.Remove
	return p, fm
}

// writeCcache creates a fake ccache file so we can assert on prune.
func writeCcache(t *testing.T, dir, name string) string {
	t.Helper()
	path := filepath.Join(dir, name)
	if err := os.WriteFile(path, []byte("fake ccache"), 0600); err != nil {
		t.Fatal(err)
	}
	return path
}

func exists(t *testing.T, path string) bool {
	t.Helper()
	_, err := os.Stat(path)
	if err == nil {
		return true
	}
	if errors.Is(err, os.ErrNotExist) {
		return false
	}
	t.Fatalf("stat %s: %v", path, err)
	return false
}

func TestPruneRemovesCcacheWhenUIDDropsFromRoster(t *testing.T) {
	roster := "1001:svc/a@R:a.keytab\n1002:svc/b@R:b.keytab\n"
	p, _ := setupProcessorWithPrune(t, roster, map[string][]byte{"a.keytab": {1}, "b.keytab": {2}})

	// Materialize the ccache files that the fake minter would have written.
	cc1 := writeCcache(t, p.CacheDir, "krb5cc_1001")
	cc2 := writeCcache(t, p.CacheDir, "krb5cc_1002")

	// First sweep: both UIDs get into the managed set.
	if ok, fail := p.ProcessRoster(context.Background()); ok != 2 || fail != 0 {
		t.Fatalf("initial sweep: want ok=2 fail=0, got ok=%d fail=%d", ok, fail)
	}
	if !exists(t, cc1) || !exists(t, cc2) {
		t.Fatalf("initial ccaches should still exist")
	}

	// Drop UID 1002 from the roster and resweep.
	if err := os.WriteFile(p.RosterPath, []byte("1001:svc/a@R:a.keytab\n"), 0644); err != nil {
		t.Fatal(err)
	}
	if ok, fail := p.ProcessRoster(context.Background()); ok != 1 || fail != 0 {
		t.Fatalf("post-drop sweep: want ok=1 fail=0, got ok=%d fail=%d", ok, fail)
	}

	if !exists(t, cc1) {
		t.Fatalf("cc for still-present UID 1001 must not be pruned")
	}
	if exists(t, cc2) {
		t.Fatalf("cc for dropped UID 1002 should have been pruned")
	}
}

func TestPruneIgnoresForeignFiles(t *testing.T) {
	// Foreign files: matching-name-but-never-managed, and non-matching names.
	roster := "1001:svc/a@R:a.keytab\n"
	p, _ := setupProcessorWithPrune(t, roster, map[string][]byte{"a.keytab": {1}})

	managed := writeCcache(t, p.CacheDir, "krb5cc_1001")
	humanKinit := writeCcache(t, p.CacheDir, "krb5cc_5000") // never in a roster this processor observed
	unrelated := writeCcache(t, p.CacheDir, "not-a-ccache") // does not match pattern

	if ok, _ := p.ProcessRoster(context.Background()); ok != 1 {
		t.Fatalf("expected ok=1 on initial sweep")
	}
	// Second sweep with same roster: nothing should be pruned.
	if ok, _ := p.ProcessRoster(context.Background()); ok != 1 {
		t.Fatalf("expected ok=1 on second sweep")
	}
	for _, path := range []string{managed, humanKinit, unrelated} {
		if !exists(t, path) {
			t.Fatalf("prune must not delete %s", path)
		}
	}
}

func TestPruneDisabledLeavesStaleFiles(t *testing.T) {
	roster := "1001:svc/a@R:a.keytab\n1002:svc/b@R:b.keytab\n"
	p, _ := setupProcessor(t, roster, map[string][]byte{"a.keytab": {1}, "b.keytab": {2}})
	// PruneStale defaults to false; confirm nothing gets removed.

	cc1 := writeCcache(t, p.CacheDir, "krb5cc_1001")
	cc2 := writeCcache(t, p.CacheDir, "krb5cc_1002")
	p.ProcessRoster(context.Background())

	if err := os.WriteFile(p.RosterPath, []byte("1001:svc/a@R:a.keytab\n"), 0644); err != nil {
		t.Fatal(err)
	}
	p.ProcessRoster(context.Background())

	if !exists(t, cc1) || !exists(t, cc2) {
		t.Fatalf("with PruneStale=false, both ccaches must remain")
	}
}

func TestPruneSkipsMissingFileWithoutError(t *testing.T) {
	// If the ccache file was already deleted externally between sweeps,
	// prune must not warn spam and must still update the managed set.
	roster := "1001:svc/a@R:a.keytab\n1002:svc/b@R:b.keytab\n"
	p, _ := setupProcessorWithPrune(t, roster, map[string][]byte{"a.keytab": {1}, "b.keytab": {2}})

	writeCcache(t, p.CacheDir, "krb5cc_1001")
	writeCcache(t, p.CacheDir, "krb5cc_1002")
	p.ProcessRoster(context.Background())

	// External actor deletes 1002's ccache.
	_ = os.Remove(filepath.Join(p.CacheDir, "krb5cc_1002"))

	// Drop 1002 from roster; prune should silently succeed.
	if err := os.WriteFile(p.RosterPath, []byte("1001:svc/a@R:a.keytab\n"), 0644); err != nil {
		t.Fatal(err)
	}
	var buf bytes.Buffer
	p.Logger = log.New(&buf, "", 0)
	if ok, fail := p.ProcessRoster(context.Background()); ok != 1 || fail != 0 {
		t.Fatalf("want ok=1 fail=0, got ok=%d fail=%d", ok, fail)
	}
	if strings.Contains(buf.String(), "WARN") {
		t.Fatalf("no WARN expected for pre-deleted file; got: %s", buf.String())
	}
	// Managed set should no longer contain 1002.
	if _, ok := p.managed[1002]; ok {
		t.Fatalf("managed set should have dropped 1002")
	}
}

func TestPruneTransientMintFailureDoesNotDropFromManaged(t *testing.T) {
	// If a UID is still in the roster but mint fails this sweep, the UID
	// must not be pruned. Regression: an intermittent KDC error should
	// leave the previously-minted ccache in place.
	roster := "1001:svc/a@R:a.keytab\n"
	p, fm := setupProcessorWithPrune(t, roster, map[string][]byte{"a.keytab": {1}})
	cc := writeCcache(t, p.CacheDir, "krb5cc_1001")

	// Prime the managed set.
	if ok, _ := p.ProcessRoster(context.Background()); ok != 1 {
		t.Fatalf("prime sweep failed")
	}
	// Now cause the next mint to fail.
	fm.failOn["svc/a@R"] = errors.New("kdc temporary error")
	if ok, fail := p.ProcessRoster(context.Background()); ok != 0 || fail != 1 {
		t.Fatalf("want ok=0 fail=1 during transient failure, got ok=%d fail=%d", ok, fail)
	}
	if !exists(t, cc) {
		t.Fatalf("ccache must not be pruned on transient mint failure")
	}
	if _, still := p.managed[1001]; !still {
		t.Fatalf("UID 1001 should stay in managed set across transient failure")
	}
}

// Sanity check that log output is captured to injected logger, not stdout.
func TestProcessRosterLogsToInjectedLogger(t *testing.T) {
	var buf bytes.Buffer
	p := &Processor{
		RosterPath: "/nowhere",
		Logger:     log.New(&buf, "", 0),
		Minter:     &fakeMinter{failOn: map[string]error{}},
	}
	p.ProcessRoster(context.Background())
	if !strings.Contains(buf.String(), "Failed to open roster file") {
		t.Fatalf("expected log line captured, got %q", buf.String())
	}
}
