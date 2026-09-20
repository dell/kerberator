// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

// Package events implements the daemon-side Kubernetes Event emitter
// for per-UID mint lifecycle. Reason strings, the UID annotation
// key, and the component name all live in the shared/events package
// so the kerberator-operator can consume them without a string-
// coupled contract.
//
// Two implementations ship:
//
//   - NopRecorder is the zero-value; it swallows every call and
//     returns nil. It's the default when POD_NAME / POD_NAMESPACE
//     are unset (dev laptop, tests) or when a Kubernetes client
//     could not be constructed at startup. The daemon keeps
//     running in this mode; only observability is degraded.
//
//   - KubeRecorder wraps client-go's record.EventRecorder and
//     posts Events whose involvedObject is the daemon Pod itself
//     (via the downward-API POD_NAME / POD_NAMESPACE env vars).
//     Every call is best-effort: any error surfaced by the
//     recorder is logged once and swallowed, so a transient API
//     server outage never fails a mint.
package events

import (
	"context"
	"log"
	"strconv"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	runtime "k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	utilruntime "k8s.io/apimachinery/pkg/util/runtime"
	"k8s.io/client-go/kubernetes"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"k8s.io/client-go/tools/record"

	sharedevents "github.com/dell/kerberator/shared/events"
)

// Recorder captures the four per-UID lifecycle events the daemon
// can observe. Every method is best-effort and never returns an
// error: a failure to emit an Event must never fail the underlying
// mint attempt.
type Recorder interface {
	// MintSucceeded is called after kinit succeeds for the given
	// principal, keyed by UID. Type=Normal.
	MintSucceeded(uid int, principal string)

	// MintFailed is called when kinit returns a non-nil error.
	// The error is included verbatim in the Event message so
	// operators can grep across `kubectl describe pod` output.
	// Type=Warning.
	MintFailed(uid int, principal string, err error)

	// Pruned is called after pruneStale removes an on-node ccache
	// whose UID is no longer in the roster. Type=Normal. This is
	// the signal the operator's finalizer waits for before
	// removing the Principal finalizer on delete.
	Pruned(uid int)

	// KeytabMissing is called when a roster entry references a
	// keytab file that doesn't exist on disk. Type=Warning.
	// Distinguished from MintFailed so operators can tell
	// "wrong config" (fix the keytab) from "kinit broke"
	// (call the KDC team).
	KeytabMissing(uid int, principal, keytabPath string)
}

// NopRecorder swallows every event. Its zero value is usable and
// safe for concurrent use.
type NopRecorder struct{}

func (NopRecorder) MintSucceeded(int, string)         {}
func (NopRecorder) MintFailed(int, string, error)     {}
func (NopRecorder) Pruned(int)                        {}
func (NopRecorder) KeytabMissing(int, string, string) {}

// KubeRecorder posts Events on a target Pod (typically the daemon
// Pod itself, identified by the downward-API env vars).
type KubeRecorder struct {
	recorder record.EventRecorder
	pod      *corev1.ObjectReference
	logger   *log.Logger
}

// NewKubeRecorder wires a record.EventRecorder against the given
// clientset and targets the given Pod. The broadcaster runs in
// the background; callers must invoke the returned stop function
// when shutting down to flush pending events cleanly.
//
// podName / podNamespace should come from the downward API. If
// either is empty, NewKubeRecorder returns a NopRecorder (with a
// log line) rather than a broken KubeRecorder: emitting Events
// against an involvedObject with an empty name silently produces
// no-op events at the API server and is worse than not emitting
// at all.
func NewKubeRecorder(client kubernetes.Interface, podNamespace, podName string, logger *log.Logger) (Recorder, func()) {
	if logger == nil {
		logger = log.Default()
	}
	if podName == "" || podNamespace == "" {
		logger.Printf("INFO: POD_NAME/POD_NAMESPACE unset; per-UID events disabled")
		return NopRecorder{}, func() {}
	}
	b := record.NewBroadcaster()
	b.StartRecordingToSink(&kubeEventSink{client: client, ns: podNamespace, logger: logger})
	rec := b.NewRecorder(scheme, corev1.EventSource{
		Component: sharedevents.EventComponentName,
		Host:      podName,
	})
	target := &corev1.ObjectReference{
		Kind:      "Pod",
		Namespace: podNamespace,
		Name:      podName,
	}
	stop := func() {
		// Shutdown returns when in-flight events have flushed.
		b.Shutdown()
	}
	return &KubeRecorder{recorder: rec, pod: target, logger: logger}, stop
}

// uidAnnotations returns a fresh single-entry map for the given
// UID. Constructed per-call rather than cached so the recorder
// stays free of shared state that could get mutated by the
// broadcaster's async pipeline.
func uidAnnotations(uid int) map[string]string {
	return map[string]string{sharedevents.UIDAnnotationKey: strconv.Itoa(uid)}
}

func (k *KubeRecorder) MintSucceeded(uid int, principal string) {
	k.recorder.AnnotatedEventf(k.pod, uidAnnotations(uid),
		corev1.EventTypeNormal, sharedevents.ReasonMintSucceeded,
		"Minted ccache for uid=%d principal=%s", uid, principal)
}

func (k *KubeRecorder) MintFailed(uid int, principal string, err error) {
	k.recorder.AnnotatedEventf(k.pod, uidAnnotations(uid),
		corev1.EventTypeWarning, sharedevents.ReasonMintFailed,
		"kinit failed for uid=%d principal=%s: %v", uid, principal, err)
}

func (k *KubeRecorder) Pruned(uid int) {
	k.recorder.AnnotatedEventf(k.pod, uidAnnotations(uid),
		corev1.EventTypeNormal, sharedevents.ReasonCcachePruned,
		"Pruned stale ccache for uid=%d (no longer in roster)", uid)
}

func (k *KubeRecorder) KeytabMissing(uid int, principal, keytabPath string) {
	k.recorder.AnnotatedEventf(k.pod, uidAnnotations(uid),
		corev1.EventTypeWarning, sharedevents.ReasonKeytabMissing,
		"Keytab missing for uid=%d principal=%s at path=%s", uid, principal, keytabPath)
}

// kubeEventSink adapts our best-effort semantics onto client-go's
// EventSink interface. Errors are logged once at Info level and
// swallowed — a daemon pod that cannot reach the API server must
// still mint tickets and log to stdout.
type kubeEventSink struct {
	client kubernetes.Interface
	ns     string
	logger *log.Logger
}

func (s *kubeEventSink) Create(ev *corev1.Event) (*corev1.Event, error) {
	out, err := s.client.CoreV1().Events(s.ns).Create(ctxTODO(), ev, createOpts)
	if err != nil {
		s.logger.Printf("INFO: could not post event %s/%s: %v (swallowed)", ev.Reason, ev.InvolvedObject.Name, err)
	}
	return out, err
}

func (s *kubeEventSink) Update(ev *corev1.Event) (*corev1.Event, error) {
	out, err := s.client.CoreV1().Events(s.ns).Update(ctxTODO(), ev, updateOpts)
	if err != nil {
		s.logger.Printf("INFO: could not patch event %s/%s: %v (swallowed)", ev.Reason, ev.InvolvedObject.Name, err)
	}
	return out, err
}

func (s *kubeEventSink) Patch(ev *corev1.Event, data []byte) (*corev1.Event, error) {
	out, err := s.client.CoreV1().Events(s.ns).Patch(
		ctxTODO(), ev.Name, patchType, data, patchOpts,
	)
	if err != nil {
		s.logger.Printf("INFO: could not patch event %s/%s: %v (swallowed)", ev.Reason, ev.InvolvedObject.Name, err)
	}
	return out, err
}

// --- support values (scheme + option types + context helper) ---

var scheme = runtime.NewScheme()

func init() {
	utilruntime.Must(clientgoscheme.AddToScheme(scheme))
}

var (
	createOpts = metav1.CreateOptions{}
	updateOpts = metav1.UpdateOptions{}
	patchOpts  = metav1.PatchOptions{}
	patchType  = types.StrategicMergePatchType
)

func ctxTODO() context.Context { return context.Background() }

// Ensure the Event type is registered on the scheme; without this
// the recorder cannot serialize into a Patch.
var _ = corev1.EventTypeNormal
