// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

// Package kerberos shells out to kinit to mint and refresh per-UID
// FILE ticket caches from keytabs, and fixes their ownership and mode.
package kerberos

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"time"
)

// TicketRequest describes a single Kerberos ticket cache to mint.
type TicketRequest struct {
	UID        int
	Principal  string
	KeytabPath string
	CachePath  string
}

// Validate returns an error if required fields are missing or malformed.
func (r TicketRequest) Validate() error {
	if r.UID < 0 {
		return fmt.Errorf("invalid uid: %d", r.UID)
	}
	if r.Principal == "" {
		return fmt.Errorf("principal is required")
	}
	if r.KeytabPath == "" {
		return fmt.Errorf("keytab path is required")
	}
	if r.CachePath == "" {
		return fmt.Errorf("cache path is required")
	}
	return nil
}

// Minter mints a Kerberos ticket cache for the given request.
type Minter interface {
	Mint(ctx context.Context, req TicketRequest) error
}

// CommandRunner runs an external command and returns its combined output.
// Injectable so tests do not need real kinit.
type CommandRunner func(ctx context.Context, name string, args ...string) ([]byte, error)

// DefaultCommandRunner runs commands via os/exec.
func DefaultCommandRunner(ctx context.Context, name string, args ...string) ([]byte, error) {
	return exec.CommandContext(ctx, name, args...).CombinedOutput()
}

// HostMinter is the production Minter that shells out to `kinit`.
type HostMinter struct {
	Runner  CommandRunner
	Chown   func(name string, uid, gid int) error
	Chmod   func(name string, mode os.FileMode) error
	Timeout time.Duration
}

// NewHostMinter returns a Minter using real filesystem and exec.
func NewHostMinter() *HostMinter {
	return &HostMinter{
		Runner:  DefaultCommandRunner,
		Chown:   os.Chown,
		Chmod:   os.Chmod,
		Timeout: 30 * time.Second,
	}
}

// Mint validates the request, runs kinit, then applies ownership and mode
// to the produced ticket cache file.
func (m *HostMinter) Mint(ctx context.Context, req TicketRequest) error {
	if err := req.Validate(); err != nil {
		return fmt.Errorf("invalid ticket request: %w", err)
	}
	if m.Timeout > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, m.Timeout)
		defer cancel()
	}

	output, err := m.Runner(ctx, "kinit", "-k", "-t", req.KeytabPath, "-c", "FILE:"+req.CachePath, req.Principal)
	if err != nil {
		return fmt.Errorf("kinit failed: %w (output: %s)", err, string(output))
	}

	if err := m.Chown(req.CachePath, req.UID, req.UID); err != nil {
		return fmt.Errorf("failed to chown cache file to UID %d: %w", req.UID, err)
	}

	if err := m.Chmod(req.CachePath, 0600); err != nil {
		return fmt.Errorf("failed to chmod cache file: %w", err)
	}

	return nil
}
