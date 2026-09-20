# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: Copyright (c) 2026 Dell Technologies

# Kerberator top-level Makefile.
#
# Every target is a thin wrapper around go / helm / kind / a container tool,
# so `make -n <target>` shows exactly what runs.
#
#   Build:      make build | operator | daemon | kubectl-krb
#   Test:       make check   (vet + unit + integration)
#   Images:     make images | push        IMAGE_PREFIX=ghcr.io/dell CONTAINER_TOOL=docker
#   Install:    make install | uninstall  (Helm, current kube-context)
#   Quickstart: make quickstart | quickstart-clean   (kind + in-cluster KDC)
#   Release:    make manifests | release-prep VERSION=x.y.z

SHELL := bash
.DEFAULT_GOAL := help

VERSION        ?= $(shell git describe --tags --always --dirty 2>/dev/null || echo dev)
IMAGE_PREFIX   ?= ghcr.io/dell
CONTAINER_TOOL ?= docker
OPERATOR_IMAGE ?= $(IMAGE_PREFIX)/kerberator-operator:$(VERSION)
DAEMON_IMAGE   ?= $(IMAGE_PREFIX)/kerberator-daemon:$(VERSION)
CHART          := operator/charts/kerberator
NAMESPACE      ?= kerberator-system

LDFLAGS := -s -w -X github.com/dell/kerberator/shared/version.Version=$(VERSION)

LOCALBIN              := $(shell pwd)/bin
SETUP_ENVTEST         := $(LOCALBIN)/setup-envtest
SETUP_ENVTEST_VERSION ?= release-0.22
ENVTEST_K8S_VERSION   ?= 1.34.x
GOLANGCI_LINT         := $(LOCALBIN)/golangci-lint
GOLANGCI_LINT_VERSION ?= v2.6.0

.PHONY: help
help: ## Show this help.
	@awk 'BEGIN {FS = ":.*##"; printf "\nKerberator targets:\n"} /^[a-zA-Z0-9_-]+:.*?##/ { printf "  \033[1m%-20s\033[0m %s\n", $$1, $$2 } /^##@/ { printf "\n\033[1m%s\033[0m\n", substr($$0, 5) }' $(MAKEFILE_LIST)

##@ Build

.PHONY: build
build: operator daemon kubectl-krb ## Build all three binaries into ./bin.

.PHONY: operator
operator: ## Build the operator binary.
	CGO_ENABLED=0 go build -trimpath -ldflags '$(LDFLAGS)' -o $(LOCALBIN)/kerberator-operator ./operator/cmd/kerberator-operator

.PHONY: daemon
daemon: ## Build the node daemon binary.
	CGO_ENABLED=0 go build -trimpath -ldflags '$(LDFLAGS)' -o $(LOCALBIN)/kerberator-daemon ./daemon/cmd/kerberator-daemon

.PHONY: kubectl-krb
kubectl-krb: ## Build the kubectl-krb plugin binary.
	CGO_ENABLED=0 go build -trimpath -ldflags '$(LDFLAGS)' -o $(LOCALBIN)/kubectl-krb ./cli/kubectl-krb/cmd/kubectl-krb

.PHONY: kubectl-krb-install
kubectl-krb-install: kubectl-krb ## Build and install kubectl-krb into ~/.local/bin.
	install -D $(LOCALBIN)/kubectl-krb $(HOME)/.local/bin/kubectl-krb
	@echo 'Installed. Make sure $(HOME)/.local/bin is on your PATH so `kubectl krb` works.'

##@ Test

.PHONY: fmt
fmt: ## gofmt all Go files in place.
	gofmt -w $$(git ls-files '*.go')

.PHONY: vet
vet: ## go vet.
	go vet ./...

.PHONY: test
test: ## Unit tests (integration tests skip themselves without envtest assets).
	go test ./...

$(SETUP_ENVTEST):
	@mkdir -p $(LOCALBIN)
	GOBIN=$(LOCALBIN) go install sigs.k8s.io/controller-runtime/tools/setup-envtest@$(SETUP_ENVTEST_VERSION)

.PHONY: envtest
envtest: $(SETUP_ENVTEST) ## Download envtest kube-apiserver + etcd binaries.
	@$(SETUP_ENVTEST) use -p path $(ENVTEST_K8S_VERSION) > /dev/null

.PHONY: integration
integration: envtest ## Operator envtest suite.
	KUBEBUILDER_ASSETS="$$($(SETUP_ENVTEST) use -i -p path $(ENVTEST_K8S_VERSION))" \
		go test -count=1 ./operator/test/integration/...

$(GOLANGCI_LINT):
	@mkdir -p $(LOCALBIN)
	GOBIN=$(LOCALBIN) go install github.com/golangci/golangci-lint/v2/cmd/golangci-lint@$(GOLANGCI_LINT_VERSION)

.PHONY: lint
lint: $(GOLANGCI_LINT) ## golangci-lint.
	$(GOLANGCI_LINT) run ./...

.PHONY: helm-lint
helm-lint: ## helm lint the chart.
	helm lint $(CHART)

.PHONY: check
check: vet test integration helm-lint ## vet + unit + integration + helm lint.

##@ Images

.PHONY: image-operator
image-operator: ## Build the operator image.
	$(CONTAINER_TOOL) build -t $(OPERATOR_IMAGE) -f operator/Dockerfile --build-arg VERSION=$(VERSION) .

.PHONY: image-daemon
image-daemon: ## Build the daemon image.
	$(CONTAINER_TOOL) build -t $(DAEMON_IMAGE) -f daemon/Dockerfile --build-arg VERSION=$(VERSION) .

.PHONY: images
images: image-operator image-daemon ## Build both images.

.PHONY: push
push: ## Push both images to $(IMAGE_PREFIX).
	$(CONTAINER_TOOL) push $(OPERATOR_IMAGE)
	$(CONTAINER_TOOL) push $(DAEMON_IMAGE)

##@ Install

.PHONY: install
install: ## Helm install/upgrade into the current kube-context using $(OPERATOR_IMAGE) and $(DAEMON_IMAGE).
	helm upgrade --install kerberator $(CHART) \
		--namespace $(NAMESPACE) --create-namespace \
		--set image.repository=$(IMAGE_PREFIX)/kerberator-operator \
		--set image.tag=$(VERSION) \
		--set daemon.image=$(DAEMON_IMAGE) \
		--wait

.PHONY: uninstall
uninstall: ## Helm uninstall.
	helm uninstall kerberator --namespace $(NAMESPACE)

##@ Quickstart (local kind cluster with an in-cluster demo KDC)

KIND_CLUSTER         ?= kerberator
CERT_MANAGER_VERSION ?= v1.18.2
DEMO_NS              := kerberator-demo
# LOCAL_IMAGES=1 builds the images here and loads them into kind instead of
# pulling the published ones. Requires $(CONTAINER_TOOL).
LOCAL_IMAGES         ?= 0

.PHONY: quickstart
quickstart: quickstart-cluster quickstart-cert-manager quickstart-images quickstart-install quickstart-kdc quickstart-demo ## kind + cert-manager + Kerberator + demo KDC + one Principal.
	@echo
	@echo "Kerberator is running on kind cluster '$(KIND_CLUSTER)'."
	@echo
	@echo "  kubectl -n $(DEMO_NS) get tenants,principals"
	@echo "  kubectl -n $(DEMO_NS) describe principal svc-2001"
	@echo "  kubectl krb tui            # if kubectl-krb is installed"
	@echo
	@echo "  make quickstart-clean      # tear it all down"

.PHONY: quickstart-cluster
quickstart-cluster: ## Create the kind cluster if it does not exist.
	@kind get clusters 2>/dev/null | grep -qx '$(KIND_CLUSTER)' || kind create cluster --name $(KIND_CLUSTER)

.PHONY: quickstart-cert-manager
quickstart-cert-manager: ## Install cert-manager.
	kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/$(CERT_MANAGER_VERSION)/cert-manager.yaml
	kubectl -n cert-manager wait --for=condition=Available deployment --all --timeout=180s

.PHONY: quickstart-images
quickstart-images: ## With LOCAL_IMAGES=1, build images and load them into kind.
ifeq ($(LOCAL_IMAGES),1)
	$(MAKE) images
	@if [ "$(CONTAINER_TOOL)" = "docker" ]; then \
		kind load docker-image $(OPERATOR_IMAGE) $(DAEMON_IMAGE) --name $(KIND_CLUSTER); \
	else \
		$(CONTAINER_TOOL) save $(OPERATOR_IMAGE) -o $(LOCALBIN)/operator.tar && kind load image-archive $(LOCALBIN)/operator.tar --name $(KIND_CLUSTER); \
		$(CONTAINER_TOOL) save $(DAEMON_IMAGE)   -o $(LOCALBIN)/daemon.tar   && kind load image-archive $(LOCALBIN)/daemon.tar   --name $(KIND_CLUSTER); \
	fi
else
	@echo "Using published images from $(IMAGE_PREFIX) (set LOCAL_IMAGES=1 to build locally)."
endif

.PHONY: quickstart-install
quickstart-install: ## Helm install into kind (published images at the chart's appVersion unless LOCAL_IMAGES=1).
	helm upgrade --install kerberator $(CHART) \
		--namespace $(NAMESPACE) --create-namespace \
		--set image.repository=$(IMAGE_PREFIX)/kerberator-operator \
		$(if $(filter 1,$(LOCAL_IMAGES)),--set image.tag=$(VERSION) --set daemon.image=$(DAEMON_IMAGE) --set image.pullPolicy=IfNotPresent,) \
		--wait

.PHONY: quickstart-kdc
quickstart-kdc: ## Deploy a throwaway MIT KDC with one demo principal and store its keytab in a Secret.
	@mkdir -p $(LOCALBIN)
	kubectl create namespace $(DEMO_NS) --dry-run=client -o yaml | kubectl apply -f -
	kubectl -n $(DEMO_NS) apply -f examples/quickstart/kdc/
	kubectl -n $(DEMO_NS) wait --for=condition=Available deployment/kdc --timeout=180s
	@echo "Waiting for the KDC to export the demo keytab..."
	@for i in $$(seq 1 60); do \
		if kubectl -n $(DEMO_NS) exec deploy/kdc -- test -s /var/lib/kerberator-kdc/export/svc-2001.keytab 2>/dev/null; then break; fi; sleep 2; \
	done
	kubectl -n $(DEMO_NS) exec deploy/kdc -- cat /var/lib/kerberator-kdc/export/svc-2001.keytab > $(LOCALBIN)/svc-2001.keytab
	kubectl -n $(DEMO_NS) create secret generic svc-2001-keytab \
		--from-file=svc-2001.keytab=$(LOCALBIN)/svc-2001.keytab \
		--dry-run=client -o yaml | kubectl apply -f -
	@rm -f $(LOCALBIN)/svc-2001.keytab

.PHONY: quickstart-demo
quickstart-demo: ## Apply the demo Tenant + Principal and wait for Ready.
	kubectl -n $(DEMO_NS) apply -f examples/quickstart/tenant.yaml -f examples/quickstart/principal.yaml
	kubectl -n $(DEMO_NS) wait --for=jsonpath='{.status.conditions[?(@.type=="Ready")].status}'=True tenant/demo --timeout=120s
	kubectl -n $(DEMO_NS) wait --for=jsonpath='{.status.conditions[?(@.type=="Ready")].status}'=True principal/svc-2001 --timeout=180s
	kubectl -n $(DEMO_NS) get tenants,principals

.PHONY: quickstart-clean
quickstart-clean: ## Delete the kind cluster.
	kind delete cluster --name $(KIND_CLUSTER)

##@ Release

.PHONY: manifests
manifests: ## Render dist/install.yaml (chart with default values, CRDs included).
	@mkdir -p dist
	helm template kerberator $(CHART) --namespace $(NAMESPACE) \
		--set image.tag=$(VERSION) --set daemon.image=$(DAEMON_IMAGE) > dist/install.yaml
	@echo "wrote dist/install.yaml"

.PHONY: release-prep
release-prep: ## Bump chart version/appVersion to VERSION=x.y.z (no leading v) and regenerate manifests.
	@test -n "$(VERSION)" || { echo "VERSION=x.y.z required"; exit 1; }
	@v=$(patsubst v%,%,$(VERSION)); \
	sed -i -E "s/^version: .*/version: $$v/; s/^appVersion: .*/appVersion: \"$$v\"/" $(CHART)/Chart.yaml; \
	echo "Chart.yaml -> $$v"
	$(MAKE) manifests VERSION=v$(patsubst v%,%,$(VERSION))

##@ Clean

.PHONY: clean
clean: ## Remove build artifacts.
	rm -rf $(LOCALBIN) dist
