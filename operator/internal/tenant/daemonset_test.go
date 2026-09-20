// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

package tenant

import (
	"testing"

	corev1 "k8s.io/api/core/v1"

	v1alpha1 "github.com/dell/kerberator/operator/api/v1alpha1"
)

func TestBuildDaemonSet_Defaults(t *testing.T) {
	kcf := &v1alpha1.Tenant{}
	ds := BuildDaemonSet(kcf, DaemonSetInputs{
		Name:            "kcf-kerberator-daemon",
		Namespace:       "kt-manage",
		Labels:          map[string]string{"app": "kcf"},
		RosterConfigMap: "kcf-roster",
		KeytabSecret:    "kcf-keytabs",
	})
	if ds.Name != "kcf-kerberator-daemon" || ds.Namespace != "kt-manage" {
		t.Fatalf("meta wrong: %+v", ds.ObjectMeta)
	}
	if ds.Spec.Selector.MatchLabels["app"] != "kcf" {
		t.Errorf("selector wrong: %+v", ds.Spec.Selector)
	}
	if len(ds.Spec.Template.Spec.Containers) != 1 {
		t.Fatalf("expected 1 container, got %d", len(ds.Spec.Template.Spec.Containers))
	}
	c := ds.Spec.Template.Spec.Containers[0]
	if c.Image == "" {
		t.Errorf("image default should be non-empty")
	}
	if c.ImagePullPolicy != corev1.PullIfNotPresent {
		t.Errorf("pull policy default wrong: %s", c.ImagePullPolicy)
	}
	var haveRoster, haveKeytabs bool
	for _, v := range ds.Spec.Template.Spec.Volumes {
		if v.Name == "roster" && v.ConfigMap != nil && v.ConfigMap.Name == "kcf-roster" {
			haveRoster = true
		}
		if v.Name == "keytabs" && v.Secret != nil && v.Secret.SecretName == "kcf-keytabs" {
			haveKeytabs = true
		}
	}
	if !haveRoster || !haveKeytabs {
		t.Errorf("expected roster+keytabs volumes, got %+v", ds.Spec.Template.Spec.Volumes)
	}
}

func TestBuildDaemonSet_HonorsSpec(t *testing.T) {
	kcf := &v1alpha1.Tenant{
		Spec: v1alpha1.TenantSpec{
			Daemon: v1alpha1.DaemonTemplate{
				Image:           "example.com/krbcf:v9",
				ImagePullPolicy: corev1.PullAlways,
				HostCachePath:   "/opt/ccaches",
				RenewMinutes:    17,
				PruneStale:      true,
				HostAliases: []corev1.HostAlias{
					{IP: "192.0.2.1", Hostnames: []string{"kdc.example.com"}},
				},
				NodeSelector: map[string]string{"role": "krb"},
			},
		},
	}
	ds := BuildDaemonSet(kcf, DaemonSetInputs{
		Name: "kcf", Namespace: "ns", Labels: map[string]string{"app": "kcf"},
		RosterConfigMap: "r", KeytabSecret: "k",
	})
	c := ds.Spec.Template.Spec.Containers[0]
	if c.Image != "example.com/krbcf:v9" || c.ImagePullPolicy != corev1.PullAlways {
		t.Errorf("image/pull wrong: %+v", c)
	}
	haveRenew, havePrune, haveCacheDir := false, false, false
	for _, a := range c.Args {
		if a == "--renew-minutes=17" {
			haveRenew = true
		}
		if a == "--prune-stale" {
			havePrune = true
		}
		if a == "--caches=/opt/ccaches" {
			haveCacheDir = true
		}
	}
	if !haveRenew || !havePrune || !haveCacheDir {
		t.Errorf("args mismatch: %v", c.Args)
	}
	if len(ds.Spec.Template.Spec.HostAliases) != 1 {
		t.Errorf("hostAliases dropped")
	}
	if ds.Spec.Template.Spec.NodeSelector["role"] != "krb" {
		t.Errorf("nodeSelector dropped")
	}
}
