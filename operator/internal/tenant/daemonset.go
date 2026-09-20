// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

package tenant

import (
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/intstr"

	v1alpha1 "github.com/dell/kerberator/operator/api/v1alpha1"
	"github.com/dell/kerberator/shared/version"
)

// DaemonSetInputs is the small, controller-owned bag of knobs that
// BuildDaemonSet needs beyond what's on the Tenant
// itself. Keeping them explicit (rather than reading them off the
// Tenant struct) makes the builder easy to unit-test
// without a live client.
type DaemonSetInputs struct {
	// Name is the DaemonSet metadata.name. Conventionally
	// "<tenant>-kerberator-daemon".
	Name string
	// Namespace is the DaemonSet metadata.namespace, equal to the
	// Tenant's own namespace.
	Namespace string
	// Labels are applied to metadata.labels AND used as the
	// pod-template selector, matching the kerberator-daemon Helm
	// chart's convention. Must contain at least one entry.
	Labels map[string]string
	// RosterConfigMap is the name of the aggregated roster CM the
	// DS should mount at /etc/kerberator-daemon/roster.
	RosterConfigMap string
	// KeytabSecret is the name of the aggregated keytab Secret the
	// DS should mount at /etc/kerberator-daemon/keytabs.
	KeytabSecret string
	// MountKrb5Conf toggles projecting a "krb5.conf" key from the
	// aggregated roster ConfigMap at /etc/krb5.conf inside the
	// container. The stock tenant image ships without a
	// tenant-specific krb5.conf, so kinit fails without this in
	// practice. False is retained for tests and images that bake
	// krb5.conf in themselves.
	MountKrb5Conf bool
	// ServiceAccountName is the SA the pods run as. Empty leaves
	// the pods on "default" — acceptable in dev but the caller
	// should typically pass a dedicated SA.
	ServiceAccountName string
	// DefaultImage is the daemon image used when the Tenant does not
	// set spec.daemon.image. The operator derives it from its
	// --daemon-image flag (the chart pins it to the operator's own
	// version). Empty falls back to DefaultDaemonImage.
	DefaultImage string
}

// DefaultDaemonImage is the last-resort daemon image when neither the
// Tenant nor the operator specifies one. It tracks the operator's own
// build version so a release always pairs matching images.
func DefaultDaemonImage() string {
	return "ghcr.io/dell/kerberator-daemon:" + version.Version
}

// DefaultHostCachePath is where the daemon writes ticket caches on the
// node when spec.daemon.hostCachePath is empty. /tmp is what rpc.gssd
// (and most other FILE-ccache consumers) search by default.
const DefaultHostCachePath = "/tmp"

// BuildDaemonSet renders the tenant DaemonSet from a
// Tenant spec. The output is intentionally minimal:
// image + volumes + args + probes + resource limits + optional
// hostAliases / nodeSelector / tolerations. Anything not modeled
// on TenantSpec.Daemon uses controller-hardcoded
// defaults that match the existing kerberator-daemon Helm chart's
// v0.2 values.yaml.
//
// The caller is responsible for setting OwnerReferences on the
// returned object so `kubectl delete tenant`
// cascades to the DS.
func BuildDaemonSet(kcf *v1alpha1.Tenant, in DaemonSetInputs) *appsv1.DaemonSet {
	f := kcf.Spec.Daemon

	image := f.Image
	if image == "" {
		image = in.DefaultImage
	}
	if image == "" {
		image = DefaultDaemonImage()
	}
	pullPolicy := f.ImagePullPolicy
	if pullPolicy == "" {
		pullPolicy = corev1.PullIfNotPresent
	}
	hostCachePath := f.HostCachePath
	if hostCachePath == "" {
		hostCachePath = DefaultHostCachePath
	}
	renewMinutes := f.RenewMinutes
	if renewMinutes == 0 {
		renewMinutes = 30
	}

	labels := map[string]string{}
	for k, v := range in.Labels {
		labels[k] = v
	}

	args := []string{
		"--roster=/etc/kerberator-daemon/roster/" + RosterKey,
		"--keytabs=/etc/kerberator-daemon/keytabs",
		"--caches=" + hostCachePath,
		"--renew-minutes=" + intToStr(renewMinutes),
	}
	if f.PruneStale {
		args = append(args, "--prune-stale")
	}

	resources := f.Resources
	if resources.Requests == nil && resources.Limits == nil {
		resources = corev1.ResourceRequirements{
			Requests: corev1.ResourceList{
				corev1.ResourceCPU:    resource.MustParse("50m"),
				corev1.ResourceMemory: resource.MustParse("64Mi"),
			},
			Limits: corev1.ResourceList{
				corev1.ResourceCPU:    resource.MustParse("200m"),
				corev1.ResourceMemory: resource.MustParse("256Mi"),
			},
		}
	}

	ds := &appsv1.DaemonSet{
		TypeMeta: metav1.TypeMeta{
			APIVersion: "apps/v1",
			Kind:       "DaemonSet",
		},
		ObjectMeta: metav1.ObjectMeta{
			Name:      in.Name,
			Namespace: in.Namespace,
			Labels:    labels,
		},
		Spec: appsv1.DaemonSetSpec{
			Selector: &metav1.LabelSelector{MatchLabels: labels},
			UpdateStrategy: appsv1.DaemonSetUpdateStrategy{
				Type: appsv1.RollingUpdateDaemonSetStrategyType,
				RollingUpdate: &appsv1.RollingUpdateDaemonSet{
					MaxUnavailable: &intstr.IntOrString{Type: intstr.String, StrVal: "10%"},
				},
			},
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{Labels: labels},
				Spec: corev1.PodSpec{
					ServiceAccountName: in.ServiceAccountName,
					HostAliases:        f.HostAliases,
					NodeSelector:       f.NodeSelector,
					Tolerations:        f.Tolerations,
					Volumes: []corev1.Volume{
						{
							Name: "roster",
							VolumeSource: corev1.VolumeSource{
								ConfigMap: &corev1.ConfigMapVolumeSource{
									LocalObjectReference: corev1.LocalObjectReference{Name: in.RosterConfigMap},
								},
							},
						},
						{
							Name: "keytabs",
							VolumeSource: corev1.VolumeSource{
								Secret: &corev1.SecretVolumeSource{SecretName: in.KeytabSecret},
							},
						},
						{
							Name: "cache",
							VolumeSource: corev1.VolumeSource{
								HostPath: &corev1.HostPathVolumeSource{
									Path: hostCachePath,
									Type: hostPathTypePtr(corev1.HostPathDirectoryOrCreate),
								},
							},
						},
					},
					Containers: []corev1.Container{{
						Name:            "kerberator-daemon",
						Image:           image,
						ImagePullPolicy: pullPolicy,
						Args:            args,
						// POD_NAME / POD_NAMESPACE are consumed by the
						// tenant's per-UID Event emitter. Absent, the
						// tenant disables event emission with a
						// one-line log ("POD_NAME/POD_NAMESPACE unset;
						// per-UID event emission disabled") and the
						// operator falls back to pod-readiness rollup
						// for principal status.
						//
						// NODE_NAME is used by v0.8+ per-principal
						// nodeSelector filtering: the daemon looks up
						// its own labels from node-labels.json (also
						// projected by the operator into the roster
						// ConfigMap) using NODE_NAME as the key. Older
						// daemons that don't know about this env var
						// simply ignore it.
						Env: []corev1.EnvVar{
							{Name: "POD_NAME", ValueFrom: &corev1.EnvVarSource{
								FieldRef: &corev1.ObjectFieldSelector{FieldPath: "metadata.name"},
							}},
							{Name: "POD_NAMESPACE", ValueFrom: &corev1.EnvVarSource{
								FieldRef: &corev1.ObjectFieldSelector{FieldPath: "metadata.namespace"},
							}},
							{Name: "NODE_NAME", ValueFrom: &corev1.EnvVarSource{
								FieldRef: &corev1.ObjectFieldSelector{FieldPath: "spec.nodeName"},
							}},
						},
						VolumeMounts: []corev1.VolumeMount{
							{Name: "roster", MountPath: "/etc/kerberator-daemon/roster", ReadOnly: true},
							{Name: "keytabs", MountPath: "/etc/kerberator-daemon/keytabs", ReadOnly: true},
							{Name: "cache", MountPath: hostCachePath},
						},
						Resources: resources,
						SecurityContext: &corev1.SecurityContext{
							AllowPrivilegeEscalation: boolPtr(false),
							ReadOnlyRootFilesystem:   boolPtr(true),
							// CHOWN + FOWNER are required to chown ccache
							// files to the principal UID and enforce 0600
							// mode. Mirror the kerberator-daemon chart's
							// values.yaml which has always dropped ALL and
							// re-added exactly these two.
							Capabilities: &corev1.Capabilities{
								Drop: []corev1.Capability{"ALL"},
								Add:  []corev1.Capability{"CHOWN", "FOWNER"},
							},
						},
						ReadinessProbe: &corev1.Probe{
							// v0.8+: delegate readiness to the daemon
							// binary itself so filter semantics stay
							// consistent between "what will I mint on
							// this node?" and "am I ready?". The shell
							// probe we used pre-v0.8 didn't understand
							// per-principal nodeSelectors — it checked
							// every roster UID unconditionally, so
							// nodes that CORRECTLY skipped a
							// selector-restricted principal always
							// looked Unready. See cmd/readyz.go.
							ProbeHandler: corev1.ProbeHandler{
								Exec: &corev1.ExecAction{
									Command: []string{"/usr/local/bin/kerberator-daemon", "readyz"},
								},
							},
							InitialDelaySeconds: 5,
							PeriodSeconds:       10,
						},
					}},
				},
			},
		},
	}

	// Conditionally project /etc/krb5.conf. The key lives on the same
	// aggregated roster ConfigMap so we don't need a second mount.
	if in.MountKrb5Conf {
		podSpec := &ds.Spec.Template.Spec
		podSpec.Volumes = append(podSpec.Volumes, corev1.Volume{
			Name: "krb5-conf",
			VolumeSource: corev1.VolumeSource{
				ConfigMap: &corev1.ConfigMapVolumeSource{
					LocalObjectReference: corev1.LocalObjectReference{Name: in.RosterConfigMap},
					Items: []corev1.KeyToPath{
						{Key: "krb5.conf", Path: "krb5.conf"},
					},
				},
			},
		})
		podSpec.Containers[0].VolumeMounts = append(
			podSpec.Containers[0].VolumeMounts,
			corev1.VolumeMount{
				Name:      "krb5-conf",
				MountPath: "/etc/krb5.conf",
				SubPath:   "krb5.conf",
				ReadOnly:  true,
			},
		)
	}

	return ds
}

func hostPathTypePtr(t corev1.HostPathType) *corev1.HostPathType { return &t }
func boolPtr(b bool) *bool                                       { return &b }

// intToStr avoids a strconv import for a single tiny call.
func intToStr(n int32) string {
	if n == 0 {
		return "0"
	}
	neg := false
	if n < 0 {
		neg = true
		n = -n
	}
	var buf [12]byte
	i := len(buf)
	for n > 0 {
		i--
		buf[i] = byte('0' + n%10)
		n /= 10
	}
	if neg {
		i--
		buf[i] = '-'
	}
	return string(buf[i:])
}
