// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

package kerberos

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestTicketRequestValidate(t *testing.T) {
	cases := []struct {
		name    string
		req     TicketRequest
		wantErr string
	}{
		{"valid", TicketRequest{UID: 1000, Principal: "a@R", KeytabPath: "/k", CachePath: "/c"}, ""},
		{"negative uid", TicketRequest{UID: -1, Principal: "a@R", KeytabPath: "/k", CachePath: "/c"}, "invalid uid"},
		{"missing principal", TicketRequest{UID: 1, KeytabPath: "/k", CachePath: "/c"}, "principal is required"},
		{"missing keytab", TicketRequest{UID: 1, Principal: "a@R", CachePath: "/c"}, "keytab path is required"},
		{"missing cache", TicketRequest{UID: 1, Principal: "a@R", KeytabPath: "/k"}, "cache path is required"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			err := tc.req.Validate()
			if tc.wantErr == "" {
				if err != nil {
					t.Fatalf("expected no error, got %v", err)
				}
				return
			}
			if err == nil || !strings.Contains(err.Error(), tc.wantErr) {
				t.Fatalf("expected error containing %q, got %v", tc.wantErr, err)
			}
		})
	}
}

// fakeMinter is a stand-in for capturing runner invocations.
type runnerCall struct {
	name string
	args []string
}

func newMinter(t *testing.T, runner CommandRunner) (*HostMinter, *[]string, *[]int) {
	t.Helper()
	chowns := &[]int{}
	chmods := &[]string{}
	return &HostMinter{
		Runner: runner,
		Chown: func(_ string, uid, _ int) error {
			*chowns = append(*chowns, uid)
			return nil
		},
		Chmod: func(name string, _ os.FileMode) error {
			*chmods = append(*chmods, name)
			return nil
		},
	}, chmods, chowns
}

func TestHostMinter_MintSuccess(t *testing.T) {
	var calls []runnerCall
	runner := func(ctx context.Context, name string, args ...string) ([]byte, error) {
		calls = append(calls, runnerCall{name: name, args: args})
		return []byte("ok"), nil
	}
	m, chmods, chowns := newMinter(t, runner)
	req := TicketRequest{UID: 1234, Principal: "alice/agent@TENANT", KeytabPath: "/keytabs/a.keytab", CachePath: "/host-tmp/krb5cc_1234"}
	if err := m.Mint(context.Background(), req); err != nil {
		t.Fatalf("Mint returned error: %v", err)
	}
	if len(calls) != 1 || calls[0].name != "kinit" {
		t.Fatalf("expected 1 kinit call, got %+v", calls)
	}
	wantArgs := []string{"-k", "-t", req.KeytabPath, "-c", "FILE:" + req.CachePath, req.Principal}
	if strings.Join(calls[0].args, " ") != strings.Join(wantArgs, " ") {
		t.Fatalf("unexpected kinit args:\n got=%v\nwant=%v", calls[0].args, wantArgs)
	}
	if len(*chowns) != 1 || (*chowns)[0] != 1234 {
		t.Fatalf("expected chown to UID 1234, got %v", *chowns)
	}
	if len(*chmods) != 1 || (*chmods)[0] != req.CachePath {
		t.Fatalf("expected chmod on %s, got %v", req.CachePath, *chmods)
	}
}

func TestHostMinter_MintInvalidRequest(t *testing.T) {
	m := &HostMinter{Runner: DefaultCommandRunner, Chown: os.Chown, Chmod: os.Chmod}
	err := m.Mint(context.Background(), TicketRequest{})
	if err == nil || !strings.Contains(err.Error(), "invalid ticket request") {
		t.Fatalf("expected validation error, got %v", err)
	}
}

func TestHostMinter_MintKinitFailure(t *testing.T) {
	runner := func(ctx context.Context, name string, args ...string) ([]byte, error) {
		return []byte("kinit: KDC unreachable"), errors.New("exit status 1")
	}
	m, _, _ := newMinter(t, runner)
	err := m.Mint(context.Background(), TicketRequest{UID: 1, Principal: "p", KeytabPath: "/k", CachePath: "/c"})
	if err == nil || !strings.Contains(err.Error(), "kinit failed") || !strings.Contains(err.Error(), "KDC unreachable") {
		t.Fatalf("expected wrapped kinit failure, got %v", err)
	}
}

func TestHostMinter_MintChownFailure(t *testing.T) {
	runner := func(ctx context.Context, name string, args ...string) ([]byte, error) { return nil, nil }
	m := &HostMinter{
		Runner: runner,
		Chown:  func(string, int, int) error { return errors.New("EPERM") },
		Chmod:  func(string, os.FileMode) error { return nil },
	}
	err := m.Mint(context.Background(), TicketRequest{UID: 1, Principal: "p", KeytabPath: "/k", CachePath: "/c"})
	if err == nil || !strings.Contains(err.Error(), "chown") {
		t.Fatalf("expected chown error, got %v", err)
	}
}

func TestHostMinter_MintChmodFailure(t *testing.T) {
	runner := func(ctx context.Context, name string, args ...string) ([]byte, error) { return nil, nil }
	m := &HostMinter{
		Runner: runner,
		Chown:  func(string, int, int) error { return nil },
		Chmod:  func(string, os.FileMode) error { return errors.New("EPERM") },
	}
	err := m.Mint(context.Background(), TicketRequest{UID: 1, Principal: "p", KeytabPath: "/k", CachePath: "/c"})
	if err == nil || !strings.Contains(err.Error(), "chmod") {
		t.Fatalf("expected chmod error, got %v", err)
	}
}

func TestHostMinter_MintPassesContext(t *testing.T) {
	// Ensure the context is threaded to the runner (deadline propagation).
	deadlineSeen := false
	runner := func(ctx context.Context, name string, args ...string) ([]byte, error) {
		if _, ok := ctx.Deadline(); ok {
			deadlineSeen = true
		}
		return nil, nil
	}
	m := &HostMinter{
		Runner:  runner,
		Chown:   func(string, int, int) error { return nil },
		Chmod:   func(string, os.FileMode) error { return nil },
		Timeout: 5, // 5 nanoseconds is enough to set a deadline
	}
	_ = m.Mint(context.Background(), TicketRequest{UID: 1, Principal: "p", KeytabPath: "/k", CachePath: "/c"})
	if !deadlineSeen {
		t.Fatalf("expected timeout to produce a context deadline")
	}
}

// Ensure real DefaultCommandRunner path works with a benign command (`true`),
// providing an integration touch-point without requiring kinit.
func TestDefaultCommandRunner(t *testing.T) {
	if _, err := os.Stat("/bin/true"); err != nil {
		t.Skip("/bin/true not available")
	}
	if _, err := DefaultCommandRunner(context.Background(), "/bin/true"); err != nil {
		t.Fatalf("expected /bin/true to succeed: %v", err)
	}
}

// Sanity check that filepath.Join style paths won't confuse Validate().
func TestValidateWithJoinedPaths(t *testing.T) {
	req := TicketRequest{
		UID:        1000,
		Principal:  "svc@TENANT",
		KeytabPath: filepath.Join("/keytabs", "svc.keytab"),
		CachePath:  filepath.Join("/host-tmp", "krb5cc_1000"),
	}
	if err := req.Validate(); err != nil {
		t.Fatalf("expected valid request, got %v", err)
	}
}
