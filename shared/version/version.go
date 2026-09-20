// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright (c) 2026 Dell Technologies

// Package version holds the build version stamped into every Kerberator
// binary via -ldflags "-X github.com/dell/kerberator/shared/version.Version=...".
package version

import (
	"fmt"
	"runtime"
	"runtime/debug"
)

// Version is the semantic version (or git describe output) of this build.
// "dev" when built without ldflags.
var Version = "dev"

// Revision returns the VCS revision recorded by the Go toolchain, if any.
func Revision() string {
	info, ok := debug.ReadBuildInfo()
	if !ok {
		return ""
	}
	var rev, dirty string
	for _, s := range info.Settings {
		switch s.Key {
		case "vcs.revision":
			rev = s.Value
		case "vcs.modified":
			if s.Value == "true" {
				dirty = "-dirty"
			}
		}
	}
	if len(rev) > 12 {
		rev = rev[:12]
	}
	return rev + dirty
}

// String renders "version (revision, go version, os/arch)" for --version
// flags and startup logs.
func String() string {
	rev := Revision()
	if rev == "" {
		rev = "unknown"
	}
	return fmt.Sprintf("%s (%s, %s, %s/%s)", Version, rev, runtime.Version(), runtime.GOOS, runtime.GOARCH)
}
