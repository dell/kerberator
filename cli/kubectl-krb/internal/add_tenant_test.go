// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

package internal

import (
	"strings"
	"testing"

	v1alpha1 "github.com/dell/kerberator/operator/api/v1alpha1"
)

func TestValidateTenantSpec_Valid(t *testing.T) {
	spec := v1alpha1.TenantSpec{
		Realm: "EXAMPLE.COM",
		Daemon: v1alpha1.DaemonTemplate{
			Image:        "registry.example.com/kerberator-daemon:v0.5.2",
			RenewMinutes: 720,
			Krb5Conf:     "[libdefaults]\ndefault_realm = EXAMPLE.COM\n[realms]\nEXAMPLE.COM = { kdc = kdc.example.com }\n",
		},
	}
	if err := ValidateTenantSpec(spec); err != nil {
		t.Fatalf("expected valid spec, got: %v", err)
	}
}

func TestValidateTenantSpec_RejectsCases(t *testing.T) {
	base := v1alpha1.TenantSpec{
		Realm: "EXAMPLE.COM",
		Daemon: v1alpha1.DaemonTemplate{
			Image:        "reg.example.com/x:v1",
			RenewMinutes: 720,
			Krb5Conf:     "[realms] EXAMPLE.COM = { kdc = kdc }",
		},
	}
	cases := []struct {
		name    string
		mutate  func(*v1alpha1.TenantSpec)
		wantSub string
	}{
		{
			name:    "empty tenant",
			mutate:  func(s *v1alpha1.TenantSpec) { s.Realm = "" },
			wantSub: "tenant string is required",
		},
		{
			name:    "lowercase tenant",
			mutate:  func(s *v1alpha1.TenantSpec) { s.Realm = "example.com" },
			wantSub: "looks wrong",
		},
		{
			name:    "empty image",
			mutate:  func(s *v1alpha1.TenantSpec) { s.Daemon.Image = "" },
			wantSub: "daemon image is required",
		},
		{
			name:    "image with no colon or slash",
			mutate:  func(s *v1alpha1.TenantSpec) { s.Daemon.Image = "nginx" },
			wantSub: "container reference",
		},
		{
			name:    "zero renew",
			mutate:  func(s *v1alpha1.TenantSpec) { s.Daemon.RenewMinutes = 0 },
			wantSub: "renewMinutes must be > 0",
		},
		{
			name: "krb5.conf without tenant string",
			mutate: func(s *v1alpha1.TenantSpec) {
				// Change the tenant but leave krb5.conf pointing at the old one.
				s.Realm = "OTHER.REALM"
				// krb5.conf still says EXAMPLE.COM — classic copy-paste bug.
			},
			wantSub: "does not mention tenant",
		},
		{
			name: "krb5.conf missing [realms]",
			mutate: func(s *v1alpha1.TenantSpec) {
				s.Daemon.Krb5Conf = "[libdefaults]\ndefault_realm = EXAMPLE.COM\n"
			},
			wantSub: "[realms]",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			spec := base
			tc.mutate(&spec)
			err := ValidateTenantSpec(spec)
			if err == nil {
				t.Fatalf("expected error containing %q, got nil", tc.wantSub)
			}
			if !strings.Contains(err.Error(), tc.wantSub) {
				t.Errorf("expected error containing %q, got %q", tc.wantSub, err.Error())
			}
		})
	}
}

func TestLooksLikeKerberosTenant(t *testing.T) {
	cases := map[string]bool{
		"EXAMPLE.COM":   true,
		"CORP.INTERNAL": true,
		"EXAMPLE.COM.5": true,
		"AB.CD":         true,
		"example.com":   false, // lowercase
		"MiXeD.CaSe":    false,
		"HAS SPACE":     false,
		"":              false, // empty
	}
	for input, want := range cases {
		if got := looksLikeKerberosRealm(input); got != want {
			t.Errorf("looksLikeKerberosRealm(%q) = %v, want %v", input, got, want)
		}
	}
}

func TestLooksLikeImageRef(t *testing.T) {
	if !looksLikeImageRef("registry.example.com/kerberator-daemon:v0.5.2") {
		t.Error("full ref should be accepted")
	}
	if !looksLikeImageRef("image:tag") {
		t.Error("short ref with tag should be accepted")
	}
	if !looksLikeImageRef("registry/image") {
		t.Error("registry/image should be accepted")
	}
	if looksLikeImageRef("bareword") {
		t.Error("bareword shouldn't be accepted as an image ref")
	}
}
