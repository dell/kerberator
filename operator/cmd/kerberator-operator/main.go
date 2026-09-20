// SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: Copyright © 2026 Dell Technologies

// Command kerberator-operator runs the Kerberator controller manager.
//
// Configuration flags:
//
//	--metrics-addr         Address to bind the metrics endpoint on.
//	--health-probe-addr    Address to bind /healthz and /readyz on.
//	--requeue-interval     How often to re-reconcile without an event.
//	--webhook-enabled      Serve the validating admission webhook.
//	--webhook-port         TCP port for the webhook server (default: 9443).
//	--webhook-cert-dir     Directory containing tls.crt + tls.key for the webhook.
//	--events-enabled       Ingest per-UID daemon Events for enriched principal status.
//	--event-cache-ttl      TTL for entries in the per-UID event observation cache.
//	--event-gc-interval    How often the event cache garbage-collects stale observations.
//	--daemon-image         Daemon image for Tenants that do not set spec.daemon.image.
//	--version              Print the build version and exit.
package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"time"

	"k8s.io/apimachinery/pkg/runtime"
	utilruntime "k8s.io/apimachinery/pkg/util/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/healthz"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"
	"sigs.k8s.io/controller-runtime/pkg/manager"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"
	webhookserver "sigs.k8s.io/controller-runtime/pkg/webhook"

	v1alpha1 "github.com/dell/kerberator/operator/api/v1alpha1"
	"github.com/dell/kerberator/operator/internal/controller"
	krbevents "github.com/dell/kerberator/operator/internal/events"
	"github.com/dell/kerberator/operator/internal/tenant"
	"github.com/dell/kerberator/operator/internal/webhook"
	"github.com/dell/kerberator/shared/version"
)

var (
	scheme   = runtime.NewScheme()
	setupLog = ctrl.Log.WithName("setup")
)

func init() {
	utilruntime.Must(clientgoscheme.AddToScheme(scheme))
	utilruntime.Must(v1alpha1.AddToScheme(scheme))
}

func main() {
	var (
		metricsAddr     string
		probeAddr       string
		requeueInterval time.Duration
		webhookEnabled  bool
		webhookPort     int
		webhookCertDir  string
		eventsEnabled   bool
		eventCacheTTL   time.Duration
		eventGCInterval time.Duration
		daemonImage     string
		showVersion     bool
	)
	flag.StringVar(&metricsAddr, "metrics-addr", ":8080", "Address to bind Prometheus metrics on.")
	flag.StringVar(&probeAddr, "health-probe-addr", ":8081", "Address to bind /healthz and /readyz on.")
	flag.DurationVar(&requeueInterval, "requeue-interval", 15*time.Second, "How often to re-reconcile without an event.")
	flag.BoolVar(&webhookEnabled, "webhook-enabled", false, "Serve the validating admission webhook for Principals.")
	flag.IntVar(&webhookPort, "webhook-port", 9443, "TCP port the webhook server binds to.")
	flag.StringVar(&webhookCertDir, "webhook-cert-dir", "/etc/webhook/certs", "Directory containing tls.crt + tls.key for the webhook server.")
	flag.BoolVar(&eventsEnabled, "events-enabled", true, "Ingest per-UID daemon Events into the in-memory cache for enriched principal status.")
	flag.DurationVar(&eventCacheTTL, "event-cache-ttl", krbevents.DefaultTTL, "TTL for entries in the per-UID event observation cache.")
	flag.DurationVar(&eventGCInterval, "event-gc-interval", 10*time.Minute, "How often the event cache garbage-collects stale observations.")
	flag.StringVar(&daemonImage, "daemon-image", tenant.DefaultDaemonImage(), "Daemon image for Tenants that do not set spec.daemon.image.")
	flag.BoolVar(&showVersion, "version", false, "Print the build version and exit.")

	opts := zap.Options{Development: false}
	opts.BindFlags(flag.CommandLine)
	flag.Parse()
	if showVersion {
		fmt.Printf("kerberator-operator %s\n", version.String())
		return
	}
	ctrl.SetLogger(zap.New(zap.UseFlagOptions(&opts)))
	setupLog.Info("kerberator-operator", "version", version.String(), "daemonImage", daemonImage)

	mgrOpts := ctrl.Options{
		Scheme:                 scheme,
		Metrics:                metricsserver.Options{BindAddress: metricsAddr},
		HealthProbeBindAddress: probeAddr,
		LeaderElection:         false,
	}
	if webhookEnabled {
		mgrOpts.WebhookServer = webhookserver.NewServer(webhookserver.Options{
			Port:    webhookPort,
			CertDir: webhookCertDir,
		})
	}
	mgr, err := ctrl.NewManager(ctrl.GetConfigOrDie(), mgrOpts)
	if err != nil {
		setupLog.Error(err, "unable to start manager")
		os.Exit(1)
	}

	var eventCache *krbevents.Cache
	if eventsEnabled {
		eventCache = krbevents.NewCache(eventCacheTTL)
	}

	fr := &controller.TenantReconciler{
		Client:          mgr.GetClient(),
		Scheme:          mgr.GetScheme(),
		Recorder:        mgr.GetEventRecorderFor("kerberator-tenant-controller"),
		RequeueInterval: requeueInterval,
		DaemonImage:     daemonImage,
	}
	if err := fr.SetupTenantWithManager(mgr); err != nil {
		setupLog.Error(err, "unable to set up tenant reconciler")
		os.Exit(1)
	}

	tr := &controller.PrincipalReconciler{
		Client:     mgr.GetClient(),
		Recorder:   mgr.GetEventRecorderFor("kerberator-principal-controller"),
		EventCache: eventCache,
	}
	if err := tr.SetupPrincipalWithManager(mgr); err != nil {
		setupLog.Error(err, "unable to set up principal reconciler")
		os.Exit(1)
	}

	if eventCache != nil {
		er := &krbevents.EventReconciler{
			Client: mgr.GetClient(),
			Cache:  eventCache,
		}
		if err := er.SetupWithManager(mgr); err != nil {
			setupLog.Error(err, "unable to set up event reconciler")
			os.Exit(1)
		}
		if err := mgr.Add(manager.RunnableFunc(func(ctx context.Context) error {
			ticker := time.NewTicker(eventGCInterval)
			defer ticker.Stop()
			for {
				select {
				case <-ctx.Done():
					return nil
				case now := <-ticker.C:
					n := eventCache.GC(now)
					if n > 0 {
						setupLog.Info("event cache gc", "evicted", n, "remaining", eventCache.Len())
					}
				}
			}
		})); err != nil {
			setupLog.Error(err, "unable to add event cache gc runnable")
			os.Exit(1)
		}
	}

	if webhookEnabled {
		if err := webhook.SetupPrincipalWebhookWithManager(mgr); err != nil {
			setupLog.Error(err, "unable to set up principal webhook")
			os.Exit(1)
		}
		setupLog.Info("principal webhook registered", "path", webhook.WebhookPath, "port", webhookPort, "certDir", webhookCertDir)
	}

	if err := mgr.AddHealthzCheck("healthz", healthz.Ping); err != nil {
		setupLog.Error(err, "unable to add healthz check")
		os.Exit(1)
	}
	if err := mgr.AddReadyzCheck("readyz", healthz.Ping); err != nil {
		setupLog.Error(err, "unable to add readyz check")
		os.Exit(1)
	}

	setupLog.Info("starting kerberator-operator")
	if err := mgr.Start(ctrl.SetupSignalHandler()); err != nil {
		setupLog.Error(err, "manager exited with error")
		os.Exit(1)
	}
}
