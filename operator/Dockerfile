# syntax=docker/dockerfile:1.6
#
# Multi-stage build for omniscience-operator. The final image is distroless
# static so the operator carries no shell, no libc surface, and runs as
# UID 65532 (nonroot) by default.

ARG GO_VERSION=1.22
FROM golang:${GO_VERSION}-alpine AS builder
WORKDIR /workspace

# Cache module downloads independent of source changes.
COPY go.mod go.sum* ./
RUN go mod download

COPY cmd/    cmd/
COPY internal/ internal/

# CGO disabled — we want a fully static binary that runs on distroless.
# -trimpath strips local paths from the binary; -ldflags "-s -w" strips the
# symbol table (smaller image; debugging is via logs and exec, not gdb).
RUN CGO_ENABLED=0 GOOS=linux go build \
    -trimpath \
    -ldflags="-s -w" \
    -o /out/manager ./cmd/manager

FROM gcr.io/distroless/static:nonroot
WORKDIR /
COPY --from=builder /out/manager /manager
USER 65532:65532
ENTRYPOINT ["/manager"]
