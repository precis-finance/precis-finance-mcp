# Bundled ingress image: stock Caddy + the rate-limit module (the one Caddy's
# own module docs list as http.handlers.rate_limit; not in core), so the
# bundled proxy matches the reference nginx vhost's per-IP DCR rate limit.
# Official two-stage xcaddy pattern. Everything is pinned: both image digests
# and the module commit — bump them together, deliberately.
FROM caddy:2.10-builder@sha256:01668408cc26e2e00c9d067c30cb43b2ba14ad1f2808beda55503cb2a31f59dc AS builder

RUN xcaddy build \
    --with github.com/mholt/caddy-ratelimit@16aecbbcb8ca

FROM caddy:2.10@sha256:c3d7ee5d2b11f9dc54f947f68a734c84e9c9666c92c88a7f30b9cba5da182adb

COPY --from=builder /usr/bin/caddy /usr/bin/caddy
