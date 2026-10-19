# Caddy with the Route 53 DNS plugin, so it can get a Let's Encrypt certificate for a name that
# points at a home-network address (the certificate is proven through DNS, not by visiting the server).
FROM caddy:2.10.2-builder-alpine AS build
RUN xcaddy build --with github.com/caddy-dns/route53@v1.6.2

FROM caddy:2.10.2-alpine
COPY --from=build /usr/bin/caddy /usr/bin/caddy
