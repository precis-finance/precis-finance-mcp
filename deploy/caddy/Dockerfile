# Bundled ingress image: stock Caddy + the rate-limit module (the one Caddy's
# own module docs list as http.handlers.rate_limit; not in core), so the
# bundled proxy matches the reference nginx vhost's per-IP DCR rate limit.
# Official two-stage xcaddy pattern. Everything is pinned: both image digests,
# the module commit, and security-patched transitive modules. Bump and scan them
# together, deliberately.
FROM caddy:2.11.7-builder@sha256:bf6a7d38add2508b7d2e3e3f40cf2af399214f143c0fa3daa51f254e01f8d293 AS builder

# Pin the patched standard library as well as the builder image: its bundled
# Go may lag security releases. Go verifies the downloaded toolchain.
ENV GOTOOLCHAIN=go1.26.10

RUN xcaddy build v2.11.9 \
    --with github.com/mholt/caddy-ratelimit@16aecbbcb8ca \
    --replace golang.org/x/crypto=golang.org/x/crypto@v0.57.0 \
    --replace golang.org/x/net=golang.org/x/net@v0.60.0 \
    --replace golang.org/x/text=golang.org/x/text@v0.41.0 \
    --replace github.com/go-jose/go-jose/v4=github.com/go-jose/go-jose/v4@v4.1.7 \
    --replace github.com/klauspost/compress=github.com/klauspost/compress@v1.20.1 \
    --replace google.golang.org/grpc=google.golang.org/grpc@v1.83.2

FROM caddy:2.11.7@sha256:5e5286cb37d44086d651a5363f05f43a692ce4d649d88fb1bd844579f45fa9e8

RUN apk upgrade --no-cache

COPY --from=builder /usr/bin/caddy /usr/bin/caddy

# Docker tags for 2.11.9 lag its source release. The patched binary above is
# independent of this pinned runtime filesystem, refreshed with apk upgrade.
LABEL org.opencontainers.image.version="v2.11.9"
