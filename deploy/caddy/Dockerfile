# Bundled ingress image: stock Caddy + the rate-limit module (the one Caddy's
# own module docs list as http.handlers.rate_limit; not in core), so the
# bundled proxy matches the reference nginx vhost's per-IP DCR rate limit.
# Official two-stage xcaddy pattern. Everything is pinned: both image digests,
# the module commit, and security-patched transitive modules. Bump and scan them
# together, deliberately.
FROM caddy:2.11.4-builder@sha256:401121e61853cb9c7df83cba43e532e49a68e30f32921c7bdcb7a4912168c067 AS builder

RUN xcaddy build \
    --with github.com/mholt/caddy-ratelimit@16aecbbcb8ca \
    --replace golang.org/x/crypto=golang.org/x/crypto@v0.55.0 \
    --replace golang.org/x/net=golang.org/x/net@v0.56.0 \
    --replace golang.org/x/text=golang.org/x/text@v0.39.0 \
    --replace google.golang.org/grpc=google.golang.org/grpc@v1.83.2

FROM caddy:2.11.4@sha256:0c994536bddb66445885237f1a5dcc1916bccea922661c76b4e9fc24061f9b52

RUN apk upgrade --no-cache

COPY --from=builder /usr/bin/caddy /usr/bin/caddy
