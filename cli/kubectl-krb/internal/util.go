// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

package internal

import (
	"fmt"
	"strconv"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// ageStr renders a time as a kubectl-style short duration ("2m", "3h",
// "5d"). Falls back to "-" for the zero time.
func ageStr(t time.Time) string {
	if t.IsZero() {
		return "-"
	}
	d := time.Since(t)
	switch {
	case d < time.Minute:
		return fmt.Sprintf("%ds", int(d.Seconds()))
	case d < time.Hour:
		return fmt.Sprintf("%dm", int(d.Minutes()))
	case d < 24*time.Hour:
		return fmt.Sprintf("%dh", int(d.Hours()))
	default:
		return fmt.Sprintf("%dd", int(d.Hours()/24))
	}
}

// readyOf digests a []metav1.Condition into the value of the "Ready"
// condition status, or "-" if not present. Used by list output.
func readyOf(conds []metav1.Condition) string {
	for _, c := range conds {
		if c.Type == "Ready" {
			return string(c.Status)
		}
	}
	return "-"
}

// truncate keeps table columns aligned by clipping long strings.
func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	if n < 4 {
		return s[:n]
	}
	return s[:n-1] + "…"
}

// parseUID parses a decimal UID from a string, returning a clearer
// error than strconv.ParseInt on its own.
func parseUID(s string) (int64, error) {
	n, err := strconv.ParseInt(s, 10, 64)
	if err != nil {
		return 0, fmt.Errorf("uid %q is not a decimal integer", s)
	}
	if n <= 0 {
		return 0, fmt.Errorf("uid must be > 0 (0 is reserved for root)")
	}
	return n, nil
}
