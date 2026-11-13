FROM node:22.12.0-alpine3.20 AS deps
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund

FROM node:22.12.0-alpine3.20 AS build
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1 NODE_ENV=production
COPY --from=deps /app/node_modules ./node_modules
COPY frontend/ .
RUN npm run build

FROM node:22.12.0-alpine3.20
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3000 HOSTNAME=0.0.0.0
RUN addgroup -S rr && adduser -S rr -G rr
COPY --from=build --chown=rr:rr /app/.next/standalone ./
COPY --from=build --chown=rr:rr /app/.next/static ./.next/static
COPY --from=build --chown=rr:rr /app/public ./public
USER rr
EXPOSE 3000
CMD ["node", "server.js"]
