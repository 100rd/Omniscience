# Makefile for omniscience-operator.
#
# Targets are intentionally narrow: build / test / vet / lint / docker-build
# and a kind-based local dev loop. The kind targets assume `kind` and `kubectl`
# are on PATH; the README documents how to install them.

# ─── Variables ────────────────────────────────────────────────────────────────
SHELL := /usr/bin/env bash
.SHELLFLAGS := -eu -o pipefail -c

GO            ?= go
GOLANGCI_LINT ?= golangci-lint
HELM          ?= helm
KIND          ?= kind
KUBECTL       ?= kubectl

IMAGE_REPO    ?= ghcr.io/100rd/omniscience-operator
IMAGE_TAG     ?= dev
IMAGE         := $(IMAGE_REPO):$(IMAGE_TAG)

KIND_CLUSTER  ?= omniscience-operator-dev

CHART_DIR     := ../helm/omniscience-operator

# ─── Phony targets ────────────────────────────────────────────────────────────
.PHONY: help build test vet lint fmt tidy docker-build helm-lint helm-template \
        kind-up kind-down kind-load e2e clean

help:
	@echo "Targets:"
	@echo "  build         Compile the manager binary"
	@echo "  test          Run unit tests with race detector"
	@echo "  vet           go vet ./..."
	@echo "  lint          golangci-lint run"
	@echo "  fmt           gofmt -w on all sources"
	@echo "  tidy          go mod tidy"
	@echo "  docker-build  Build the operator image as $(IMAGE)"
	@echo "  helm-lint     Lint the operator helm chart"
	@echo "  helm-template Render the helm chart with default values"
	@echo "  kind-up       Create a kind cluster ($(KIND_CLUSTER))"
	@echo "  kind-down     Delete the kind cluster"
	@echo "  kind-load     Load the operator image into kind"
	@echo "  e2e           kind-up + helm install + smoke (manual smoke; see README)"
	@echo "  clean         Remove build artifacts"

# ─── Go ───────────────────────────────────────────────────────────────────────
build:
	$(GO) build -trimpath -ldflags="-s -w" -o bin/manager ./cmd/manager

test:
	$(GO) test -race -count=1 ./...

vet:
	$(GO) vet ./...

lint:
	$(GOLANGCI_LINT) run ./...

fmt:
	$(GO) fmt ./...

tidy:
	$(GO) mod tidy

# ─── Docker ───────────────────────────────────────────────────────────────────
docker-build:
	docker build -t $(IMAGE) .

# ─── Helm ─────────────────────────────────────────────────────────────────────
helm-lint:
	$(HELM) lint $(CHART_DIR)

helm-template:
	$(HELM) template omniscience-operator $(CHART_DIR) \
	  --set workspaceId=11111111-2222-3333-4444-555555555555 \
	  --set clusterName=dev \
	  --set nats.url=nats://nats:4222

# ─── Kind ─────────────────────────────────────────────────────────────────────
kind-up:
	$(KIND) create cluster --name $(KIND_CLUSTER) --wait 2m

kind-down:
	$(KIND) delete cluster --name $(KIND_CLUSTER)

kind-load: docker-build
	$(KIND) load docker-image $(IMAGE) --name $(KIND_CLUSTER)

# e2e is the documented manual smoke procedure — see README.
# Automating it lives in a follow-up issue (epic #98).
e2e: kind-up kind-load
	@echo "Cluster up; image loaded. Install the chart, then create a Pod and"
	@echo "verify an entity appears in Omniscience. See README §Local dev loop."

# ─── Cleanup ──────────────────────────────────────────────────────────────────
clean:
	rm -rf bin/ vendor/
