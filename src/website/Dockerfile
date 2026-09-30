# Production website image: static React build served by nginx, which also
# proxies /api/ to the backend (production-readiness B-07). Build context is the
# repository root (docker-compose.prod.yml) so nginx.prod.conf can be baked in.
#
# The backend origin is compiled into the bundle by Create React App, so it is a
# build argument: pass the public origin the participants use (same origin as
# the website, since nginx proxies /api/ there).

FROM node:22-alpine AS build
WORKDIR /website
# .npmrc carries legacy-peer-deps=true: the lock file was generated with it, so
# `npm ci` must see it too or it refuses the lock as out of sync.
COPY src/website/package.json src/website/package-lock.json src/website/.npmrc ./
RUN npm ci --no-audit --no-fund
COPY src/website/ ./
ARG REACT_APP_BACKEND_HOST
ARG REACT_APP_BACKEND_PORT=
ENV REACT_APP_BACKEND_HOST=$REACT_APP_BACKEND_HOST \
    REACT_APP_BACKEND_PORT=$REACT_APP_BACKEND_PORT \
    GENERATE_SOURCEMAP=false \
    CI=true
RUN test -n "$REACT_APP_BACKEND_HOST" || (echo "REACT_APP_BACKEND_HOST build argument is required" && exit 1)
RUN npm run build

FROM nginx:1.27-alpine
COPY nginx.prod.conf /etc/nginx/nginx.conf
COPY --from=build /website/build /usr/share/nginx/html
EXPOSE 80
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD wget -q --spider http://127.0.0.1/nginx-health || exit 1
