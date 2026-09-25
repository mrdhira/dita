# Stage 1: Build
FROM golang:1.27-alpine AS builder

# Install git and ca-certificates (needed to download modules and make HTTPS requests)
RUN apk --no-cache add git ca-certificates

WORKDIR /app

# Leverage Docker cache layer: only re-run if go.mod/go.sum change
COPY go.mod go.sum ./
RUN go mod download

# Copy the rest of the application source code
COPY . .

# Build a statically linked binary (-s -w strips debug symbols for a smaller size)
RUN CGO_ENABLED=0 GOOS=linux go build -ldflags="-s -w" -o main .

# The store's directory, created here so the runtime stage can copy it with an owner. A scratch
# image has no shell and no RUN, so `mkdir && chown` is not available after this point.
RUN mkdir -p /store/data/decisions

# Stage 2: Runtime
FROM scratch

# Copy SSL certificates from the builder stage for external HTTPS calls
COPY --from=builder /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/

# Copy the compiled binary from the builder stage
COPY --from=builder /app/main /main

# DECISIONS_DIR, owned by the uid the process runs as. Copied rather than created because scratch
# cannot run a command; the ownership travels with the layer, and a named volume Docker initialises
# from this image inherits it.
COPY --from=builder --chown=65532:0 /store/data /data

# Expose the application port
EXPOSE 2104

# Not root. Without this the process ran as uid 0 inside a scratch image. 65532 is distroless
# "nonroot"; group 0 so a bind mount that is group-root readable still works. An EXISTING volume
# keeps its old ownership and must be chown'd once -- see the comment on the volume in compose.yaml.
USER 65532:0

# Run the binary
ENTRYPOINT ["/main"]