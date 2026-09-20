// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

package events

import (
	"errors"
	"io"
	"log"
	"strings"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes/fake"
	k8stesting "k8s.io/client-go/testing"

	sharedevents "github.com/dell/kerberator/shared/events"
)

// waitForEvents polls the fake clientset's action log until at
// least `want` create actions on `events` have appeared, or the
// deadline elapses. Necessary because client-go's broadcaster is
// asynchronous: EventRecorder.Eventf returns immediately and the
// actual API call happens on a background goroutine.
func waitForEvents(t *testing.T, client *fake.Clientset, want int, timeout time.Duration) []corev1.Event {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		var got []corev1.Event
		for _, a := range client.Actions() {
			ca, ok := a.(k8stesting.CreateAction)
			if !ok || a.GetVerb() != "create" || a.GetResource().Resource != "events" {
				continue
			}
			ev, ok := ca.GetObject().(*corev1.Event)
			if !ok {
				continue
			}
			got = append(got, *ev)
		}
		if len(got) >= want {
			return got
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %d events; saw %d actions total", want, len(client.Actions()))
	return nil
}

func newTestRecorder(t *testing.T) (*fake.Clientset, Recorder, func()) {
	t.Helper()
	client := fake.NewSimpleClientset()
	rec, stop := NewKubeRecorder(client, "test-ns", "test-pod", log.New(io.Discard, "", 0))
	return client, rec, stop
}

func TestKubeRecorder_MintSucceeded(t *testing.T) {
	client, rec, stop := newTestRecorder(t)
	defer stop()
	rec.MintSucceeded(2001, "svc-a@TENANT")
	evs := waitForEvents(t, client, 1, 2*time.Second)
	e := evs[0]
	if e.Reason != sharedevents.ReasonMintSucceeded {
		t.Errorf("reason: got %q want %q", e.Reason, sharedevents.ReasonMintSucceeded)
	}
	if e.Type != corev1.EventTypeNormal {
		t.Errorf("type: got %q want Normal", e.Type)
	}
	if !strings.Contains(e.Message, "uid=2001") {
		t.Errorf("message missing uid=2001: %q", e.Message)
	}
	if !strings.Contains(e.Message, "svc-a@TENANT") {
		t.Errorf("message missing principal: %q", e.Message)
	}
	assertPodRef(t, e)
}

func TestKubeRecorder_MintFailed(t *testing.T) {
	client, rec, stop := newTestRecorder(t)
	defer stop()
	rec.MintFailed(2002, "svc-b@TENANT", errors.New("KDC reply did not match expectations"))
	evs := waitForEvents(t, client, 1, 2*time.Second)
	e := evs[0]
	if e.Reason != sharedevents.ReasonMintFailed {
		t.Errorf("reason: got %q want %q", e.Reason, sharedevents.ReasonMintFailed)
	}
	if e.Type != corev1.EventTypeWarning {
		t.Errorf("type: got %q want Warning", e.Type)
	}
	if !strings.Contains(e.Message, "uid=2002") ||
		!strings.Contains(e.Message, "KDC reply did not match expectations") {
		t.Errorf("message missing uid or wrapped error: %q", e.Message)
	}
}

func TestKubeRecorder_Pruned(t *testing.T) {
	client, rec, stop := newTestRecorder(t)
	defer stop()
	rec.Pruned(2003)
	evs := waitForEvents(t, client, 1, 2*time.Second)
	e := evs[0]
	if e.Reason != sharedevents.ReasonCcachePruned {
		t.Errorf("reason: got %q want %q", e.Reason, sharedevents.ReasonCcachePruned)
	}
	if e.Type != corev1.EventTypeNormal {
		t.Errorf("type: got %q want Normal", e.Type)
	}
	if !strings.Contains(e.Message, "uid=2003") {
		t.Errorf("message missing uid=2003: %q", e.Message)
	}
}

func TestKubeRecorder_KeytabMissing(t *testing.T) {
	client, rec, stop := newTestRecorder(t)
	defer stop()
	rec.KeytabMissing(2004, "svc-d@TENANT", "/keytabs/missing.keytab")
	evs := waitForEvents(t, client, 1, 2*time.Second)
	e := evs[0]
	if e.Reason != sharedevents.ReasonKeytabMissing {
		t.Errorf("reason: got %q want %q", e.Reason, sharedevents.ReasonKeytabMissing)
	}
	if e.Type != corev1.EventTypeWarning {
		t.Errorf("type: got %q want Warning", e.Type)
	}
	if !strings.Contains(e.Message, "uid=2004") ||
		!strings.Contains(e.Message, "/keytabs/missing.keytab") {
		t.Errorf("message missing uid or path: %q", e.Message)
	}
}

// TestKubeRecorder_UIDAnnotation asserts each emitted Event carries
// the `kerberator.dell.com/uid` annotation with the decimal UID
// value. Consumers (the operator) prefer this over grep-parsing the
// message text, so any regression here silently degrades their
// per-UID correlation to the (still-working) message fallback.
func TestKubeRecorder_UIDAnnotation(t *testing.T) {
	client, rec, stop := newTestRecorder(t)
	defer stop()

	rec.MintSucceeded(2001, "svc-a@TENANT")
	rec.MintFailed(2002, "svc-b@TENANT", errors.New("boom"))
	rec.Pruned(2003)
	rec.KeytabMissing(2004, "svc-d@TENANT", "/keytabs/missing.keytab")

	evs := waitForEvents(t, client, 4, 2*time.Second)

	wantByReason := map[string]string{
		sharedevents.ReasonMintSucceeded: "2001",
		sharedevents.ReasonMintFailed:    "2002",
		sharedevents.ReasonCcachePruned:  "2003",
		sharedevents.ReasonKeytabMissing: "2004",
	}
	for _, e := range evs {
		wantUID, ok := wantByReason[e.Reason]
		if !ok {
			t.Errorf("unexpected reason %q", e.Reason)
			continue
		}
		gotUID, present := e.Annotations[sharedevents.UIDAnnotationKey]
		if !present {
			t.Errorf("event reason=%s missing annotation %q; annotations=%v",
				e.Reason, sharedevents.UIDAnnotationKey, e.Annotations)
			continue
		}
		if gotUID != wantUID {
			t.Errorf("event reason=%s annotation %q = %q, want %q",
				e.Reason, sharedevents.UIDAnnotationKey, gotUID, wantUID)
		}
	}
}

func TestKubeRecorder_EmptyPodNameDegradesToNop(t *testing.T) {
	client := fake.NewSimpleClientset()
	rec, stop := NewKubeRecorder(client, "", "", log.New(io.Discard, "", 0))
	defer stop()
	if _, ok := rec.(NopRecorder); !ok {
		t.Fatalf("expected NopRecorder when POD_NAME/POD_NAMESPACE unset, got %T", rec)
	}
	rec.MintSucceeded(1, "p@R")
	rec.MintFailed(1, "p@R", errors.New("x"))
	rec.Pruned(1)
	rec.KeytabMissing(1, "p@R", "/nope")
	if len(client.Actions()) != 0 {
		t.Errorf("NopRecorder should not touch the API; got %d actions", len(client.Actions()))
	}
}

func TestNopRecorder_ZeroValueIsSafe(t *testing.T) {
	var r NopRecorder
	r.MintSucceeded(1, "p@R")
	r.MintFailed(1, "p@R", errors.New("x"))
	r.Pruned(1)
	r.KeytabMissing(1, "p@R", "/nope")
}

func assertPodRef(t *testing.T, e corev1.Event) {
	t.Helper()
	if e.InvolvedObject.Kind != "Pod" {
		t.Errorf("involvedObject.Kind: got %q want Pod", e.InvolvedObject.Kind)
	}
	if e.InvolvedObject.Name != "test-pod" {
		t.Errorf("involvedObject.Name: got %q want test-pod", e.InvolvedObject.Name)
	}
	if e.InvolvedObject.Namespace != "test-ns" {
		t.Errorf("involvedObject.Namespace: got %q want test-ns", e.InvolvedObject.Namespace)
	}
	if e.ObjectMeta.Name == "" {
		t.Errorf("event missing metadata.name")
	}
	_ = metav1.ObjectMeta{}
}
