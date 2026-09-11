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

# Stage 2: Runtime
FROM scratch

# Copy SSL certificates from the builder stage for external HTTPS calls
COPY --from=builder /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/

# Copy the compiled binary from the builder stage
COPY --from=builder /app/main /main

# Expose the application port
EXPOSE 2104

# Run the binary
ENTRYPOINT ["/main"]