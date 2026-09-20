// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

// Package integration exercises the operator against a real
// kube-apiserver + etcd via controller-runtime's envtest package.
//
// What this suite adds on top of the colocated unit tests
// (internal/{controller,webhook,events,tenant,roster}/*_test.go):
//
//   - Structural CRD validity: the chart's CRD YAMLs are installed
//     into a live apiserver and accept our sample objects.
//   - Real admission-webhook path: the ValidatingWebhookConfiguration
//     is installed and a bad-UID Principal is rejected by the
//     apiserver before reaching a reconciler.
//   - Real controller wiring: TenantReconciler + PrincipalReconciler
//     run in a shared manager, so a Tenant in manage
//     mode with multiple principals aggregates them into a single
//     roster ConfigMap + keytab Secret, and the owned children
//     carry OwnerReferences that Kubernetes' garbage collector
//     will honor.
//   - Finalizer lifecycle: manage-mode principals gain the
//     `kerberator.dell.com/ccache-purged` finalizer on create,
//     and its removal follows either the KerberosCcachePruned
//     event path or the deletionTimeout fallback.
//
// Envtest binaries are located via the KUBEBUILDER_ASSETS env var
// or, if unset, `setup-envtest`'s default install location. Run
// `make envtest` at the repo root once to download them, then
// `go test ./test/integration/...`.
package integration

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"testing"
	"time"

	admissionregistrationv1 "k8s.io/api/admissionregistration/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	utilruntime "k8s.io/apimachinery/pkg/util/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"k8s.io/client-go/rest"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/envtest"
	logf "sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"
	webhookserver "sigs.k8s.io/controller-runtime/pkg/webhook"

	v1alpha1 "github.com/dell/kerberator/operator/api/v1alpha1"
	"github.com/dell/kerberator/operator/internal/controller"
	krbevents "github.com/dell/kerberator/operator/internal/events"
	"github.com/dell/kerberator/operator/internal/webhook"
)

// Shared state populated by TestMain and consumed by all tests in
// this package. Keeping them package-level rather than passing them
// around avoids repeating the manager setup for every test — the
// envtest apiserver spinup dominates runtime, and reusing it across
// subtests keeps the suite well under a minute.
var (
	testEnv    *envtest.Environment
	restConfig *rest.Config
	// k8sClient is a direct (uncached) client. Tests should prefer
	// this over the manager's cached client so status updates written
	// by reconcilers are observed without waiting on informers.
	k8sClient client.Client
	testCtx   context.Context
	cancelMgr context.CancelFunc
	testSch   = runtime.NewScheme()
)

// crdDir is the path this suite loads CRDs from. Kept as a relative
// path from the test binary's location so the suite works both from
// `go test ./test/integration/...` and from an IDE runner.
func crdDir() string {
	// suite_test.go lives at test/integration/, chart CRDs at
	// charts/kerberator/crd/.
	return filepath.Join("..", "..", "charts", "kerberator", "crd")
}

// principalVWC builds the ValidatingWebhookConfiguration matching what
// the chart's webhook.yaml renders in production, minus cert-manager
// specifics (envtest injects the CA bundle it generates). It's built
// programmatically rather than parsed from the chart template so
// tests aren't coupled to Helm-templating quirks.
func principalVWC() *admissionregistrationv1.ValidatingWebhookConfiguration {
	fail := admissionregistrationv1.Fail
	ignore := admissionregistrationv1.Ignore
	side := admissionregistrationv1.SideEffectClassNone
	scope := admissionregistrationv1.NamespacedScope
	path := webhook.WebhookPath
	rules := []admissionregistrationv1.RuleWithOperations{{
		Operations: []admissionregistrationv1.OperationType{
			admissionregistrationv1.Create,
			admissionregistrationv1.Update,
		},
		Rule: admissionregistrationv1.Rule{
			APIGroups:   []string{"kerberator.dell.com"},
			APIVersions: []string{"v1alpha1"},
			Resources:   []string{"principals"},
			Scope:       &scope,
		},
	}}
	return &admissionregistrationv1.ValidatingWebhookConfiguration{
		ObjectMeta: metav1.ObjectMeta{Name: "kerberator-principal-validator"},
		Webhooks: []admissionregistrationv1.ValidatingWebhook{
			{
				Name:                    "create.principals.kerberator.dell.com",
				AdmissionReviewVersions: []string{"v1"},
				SideEffects:             &side,
				FailurePolicy:           &fail,
				Rules: []admissionregistrationv1.RuleWithOperations{{
					Operations: []admissionregistrationv1.OperationType{admissionregistrationv1.Create},
					Rule:       rules[0].Rule,
				}},
				ClientConfig: admissionregistrationv1.WebhookClientConfig{
					Service: &admissionregistrationv1.ServiceReference{
						Name:      "kerberator-webhook",
						Namespace: "default",
						Path:      &path,
					},
				},
			},
			{
				Name:                    "update.principals.kerberator.dell.com",
				AdmissionReviewVersions: []string{"v1"},
				SideEffects:             &side,
				FailurePolicy:           &ignore,
				Rules: []admissionregistrationv1.RuleWithOperations{{
					Operations: []admissionregistrationv1.OperationType{admissionregistrationv1.Update},
					Rule:       rules[0].Rule,
				}},
				ClientConfig: admissionregistrationv1.WebhookClientConfig{
					Service: &admissionregistrationv1.ServiceReference{
						Name:      "kerberator-webhook",
						Namespace: "default",
						Path:      &path,
					},
				},
			},
		},
	}
}

func TestMain(m *testing.M) {
	// envtest needs kube-apiserver + etcd binaries. `make integration`
	// downloads them and sets KUBEBUILDER_ASSETS; a plain `go test ./...`
	// should not fail for contributors who have not done that.
	if os.Getenv("KUBEBUILDER_ASSETS") == "" {
		fmt.Fprintln(os.Stderr, "KUBEBUILDER_ASSETS not set; skipping envtest integration suite (run `make integration`)")
		os.Exit(0)
	}

	logf.SetLogger(zap.New(zap.WriteTo(os.Stderr), zap.UseDevMode(true)))

	utilruntime.Must(clientgoscheme.AddToScheme(testSch))
	utilruntime.Must(v1alpha1.AddToScheme(testSch))

	testEnv = &envtest.Environment{
		CRDDirectoryPaths:     []string{crdDir()},
		ErrorIfCRDPathMissing: true,
		WebhookInstallOptions: envtest.WebhookInstallOptions{
			ValidatingWebhooks: []*admissionregistrationv1.ValidatingWebhookConfiguration{principalVWC()},
		},
	}

	cfg, err := testEnv.Start()
	if err != nil {
		fmt.Fprintf(os.Stderr, "failed to start envtest: %v\n", err)
		os.Exit(1)
	}
	restConfig = cfg

	k8sClient, err = client.New(cfg, client.Options{Scheme: testSch})
	if err != nil {
		fmt.Fprintf(os.Stderr, "failed to build k8s client: %v\n", err)
		_ = testEnv.Stop()
		os.Exit(1)
	}

	// Manager wired with the same reconcilers cmd/manager wires in
	// production.
	whOpts := testEnv.WebhookInstallOptions
	mgr, err := ctrl.NewManager(cfg, ctrl.Options{
		Scheme:                 testSch,
		Metrics:                metricsserver.Options{BindAddress: "0"},
		HealthProbeBindAddress: "0",
		LeaderElection:         false,
		WebhookServer: webhookserver.NewServer(webhookserver.Options{
			Host:    whOpts.LocalServingHost,
			Port:    whOpts.LocalServingPort,
			CertDir: whOpts.LocalServingCertDir,
		}),
	})
	if err != nil {
		fmt.Fprintf(os.Stderr, "failed to build manager: %v\n", err)
		_ = testEnv.Stop()
		os.Exit(1)
	}

	eventCache := krbevents.NewCache(24 * time.Hour)

	if err := (&controller.TenantReconciler{
		Client:          mgr.GetClient(),
		Scheme:          mgr.GetScheme(),
		Recorder:        mgr.GetEventRecorderFor("kerberator-daemon-controller"),
		RequeueInterval: 1 * time.Second,
	}).SetupTenantWithManager(mgr); err != nil {
		fmt.Fprintf(os.Stderr, "tenant reconciler: %v\n", err)
		_ = testEnv.Stop()
		os.Exit(1)
	}
	if err := (&controller.PrincipalReconciler{
		Client:     mgr.GetClient(),
		Recorder:   mgr.GetEventRecorderFor("kerberator-daemon-principal"),
		EventCache: eventCache,
	}).SetupPrincipalWithManager(mgr); err != nil {
		fmt.Fprintf(os.Stderr, "principal reconciler: %v\n", err)
		_ = testEnv.Stop()
		os.Exit(1)
	}
	if err := (&krbevents.EventReconciler{
		Client: mgr.GetClient(),
		Cache:  eventCache,
	}).SetupWithManager(mgr); err != nil {
		fmt.Fprintf(os.Stderr, "event reconciler: %v\n", err)
		_ = testEnv.Stop()
		os.Exit(1)
	}
	if err := webhook.SetupPrincipalWebhookWithManager(mgr); err != nil {
		fmt.Fprintf(os.Stderr, "principal webhook: %v\n", err)
		_ = testEnv.Stop()
		os.Exit(1)
	}

	testCtx, cancelMgr = context.WithCancel(context.Background())
	go func() {
		if err := mgr.Start(testCtx); err != nil {
			fmt.Fprintf(os.Stderr, "manager exited: %v\n", err)
		}
	}()

	// Wait for the webhook server to be reachable before running
	// tests. Without this, the first webhook-driven test can flake
	// on a race where the manager's TLS listener isn't up yet.
	deadline := time.Now().Add(15 * time.Second)
	for time.Now().Before(deadline) {
		if mgr.GetWebhookServer().StartedChecker()(nil) == nil {
			break
		}
		time.Sleep(100 * time.Millisecond)
	}

	code := m.Run()

	cancelMgr()
	if err := testEnv.Stop(); err != nil {
		fmt.Fprintf(os.Stderr, "envtest stop: %v\n", err)
	}
	os.Exit(code)
}

// mustCreateNS creates a Namespace with the given SCC UID-range
// annotation. Used by tests that exercise webhook rule 2. Errors
// fatally — a namespace we can't create is a bug in the test, not
// the operator.
func mustCreateNS(t *testing.T, name, sccRange string) {
	t.Helper()
	ns := &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: name}}
	if sccRange != "" {
		ns.Annotations = map[string]string{"openshift.io/sa.scc.uid-range": sccRange}
	}
	if err := k8sClient.Create(testCtx, ns); err != nil {
		t.Fatalf("create ns %s: %v", name, err)
	}
}

// eventually polls fn until it returns nil or timeout elapses.
// Envtest's watch is real, but the manager's cached client can lag
// briefly; tests use this for assertions that depend on a
// reconciler having run.
func eventually(t *testing.T, timeout time.Duration, fn func() error) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	var lastErr error
	for time.Now().Before(deadline) {
		lastErr = fn()
		if lastErr == nil {
			return
		}
		time.Sleep(100 * time.Millisecond)
	}
	t.Fatalf("condition never met within %s: %v", timeout, lastErr)
}
