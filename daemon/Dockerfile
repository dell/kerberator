# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: Copyright (c) 2026 Dell Technologies

# kerberator-daemon: per-node Kerberos ticket-cache minter.
#
# Build from the repo root (the whole module is the build context):
#   docker build -t kerberator-daemon:dev -f daemon/Dockerfile .
# Multi-arch via buildx: TARGETOS/TARGETARCH are set automatically.
# Tests run in CI, not here.

FROM --platform=$BUILDPLATFORM docker.io/library/golang:1.26-alpine AS build
ARG TARGETOS=linux
ARG TARGETARCH=amd64
ARG VERSION=dev
WORKDIR /src

COPY go.mod go.sum ./
RUN go mod download

COPY shared/ shared/
COPY daemon/ daemon/
RUN CGO_ENABLED=0 GOOS=$TARGETOS GOARCH=$TARGETARCH \
    go build -trimpath \
      -ldflags="-s -w -X github.com/dell/kerberator/shared/version.Version=$VERSION" \
      -o /out/kerberator-daemon ./daemon/cmd/kerberator-daemon

# The runtime image needs MIT Kerberos (kinit, klist) from Alpine.
FROM docker.io/library/alpine:3.22
ARG VERSION=dev
LABEL org.opencontainers.image.title="kerberator-daemon" \
      org.opencontainers.image.description="Kerberator node daemon: mints and renews per-UID Kerberos ticket caches from keytabs" \
      org.opencontainers.image.source="https://github.com/dell/kerberator" \
      org.opencontainers.image.version="$VERSION" \
      org.opencontainers.image.licenses="MIT"
RUN apk add --no-cache krb5 krb5-pkinit ca-certificates
COPY LICENSE NOTICE /licenses/
COPY --from=build /out/kerberator-daemon /usr/local/bin/kerberator-daemon
# UID 0 is required so the daemon can chown ticket caches to arbitrary
# UIDs. The DaemonSet drops every capability except CHOWN and FOWNER.
USER 0
ENTRYPOINT ["/usr/local/bin/kerberator-daemon"]
