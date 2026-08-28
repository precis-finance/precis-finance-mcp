# Bundled ingress image: stock Caddy + the rate-limit module (the one Caddy's
# own module docs list as http.handlers.rate_limit; not in core), so the
# bundled proxy matches the reference nginx vhost's per-IP DCR rate limit.
# Official two-stage xcaddy pattern. Everything is pinned: both image digests,
# the module commit, and security-patched transitive modules. Bump and scan them
# together, deliberately.
FROM caddy:2.11-builder@sha256:4bdeabce8e79d36b23d1cba7d20598cec2c1117ace960d8ca06071f945e8fc9b AS builder

RUN xcaddy build \
    --with github.com/mholt/caddy-ratelimit@16aecbbcb8ca \
    --replace golang.org/x/net=golang.org/x/net@v0.56.0 \
    --replace golang.org/x/text=golang.org/x/text@v0.39.0 \
    --replace google.golang.org/grpc=google.golang.org/grpc@v1.82.1

FROM caddy:2.11@sha256:df7f1c2fb114453b951de51a98efc010db1655a92c2e86be6706714e2417a78d

RUN apk upgrade --no-cache

COPY --from=builder /usr/bin/caddy /usr/bin/caddy
