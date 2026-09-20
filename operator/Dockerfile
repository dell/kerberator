# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: Copyright (c) 2026 Dell Technologies

# kerberator-operator: the Kubernetes controller manager.
#
# Build from the repo root (the whole module is the build context):
#   docker build -t kerberator-operator:dev -f operator/Dockerfile .
# Multi-arch via buildx: TARGETOS/TARGETARCH are set automatically.
# Tests run in CI, not here, so image builds stay fast and reproducible.

FROM --platform=$BUILDPLATFORM docker.io/library/golang:1.26-alpine AS build
ARG TARGETOS=linux
ARG TARGETARCH=amd64
ARG VERSION=dev
WORKDIR /src

COPY go.mod go.sum ./
RUN go mod download

COPY shared/   shared/
COPY operator/ operator/
RUN CGO_ENABLED=0 GOOS=$TARGETOS GOARCH=$TARGETARCH \
    go build -trimpath \
      -ldflags="-s -w -X github.com/dell/kerberator/shared/version.Version=$VERSION" \
      -o /out/kerberator-operator ./operator/cmd/kerberator-operator

FROM gcr.io/distroless/static-debian12:nonroot
ARG VERSION=dev
LABEL org.opencontainers.image.title="kerberator-operator" \
      org.opencontainers.image.description="Kerberator controller manager: reconciles Tenant and Principal resources into per-node ticket-cache DaemonSets" \
      org.opencontainers.image.source="https://github.com/dell/kerberator" \
      org.opencontainers.image.version="$VERSION" \
      org.opencontainers.image.licenses="MIT"
COPY LICENSE NOTICE /licenses/
COPY --from=build /out/kerberator-operator /kerberator-operator
USER 65532:65532
ENTRYPOINT ["/kerberator-operator"]
