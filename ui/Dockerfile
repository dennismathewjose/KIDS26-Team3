# syntax=docker/dockerfile:1

# ---- Dependencies stage ----
FROM node:24.21.0-alpine3.24 AS deps
WORKDIR /app
# Install production dependencies using the lockfile for reproducible builds
COPY package.json package-lock.json ./
RUN npm ci --omit=dev

# ---- Runtime stage ----
FROM node:24.21.0-alpine3.24 AS runtime
ENV NODE_ENV=production
# Keep the app on an unprivileged port so it can run as the non-root node user.
ENV PORT=8080
WORKDIR /app

# Bring in the pre-installed production dependencies
COPY --from=deps /app/node_modules ./node_modules

# Copy application source
COPY package.json server.js ./
COPY public ./public

# Writable directory for multer temp uploads, owned by the non-root user
RUN mkdir -p uploads && chown -R node:node /app

USER node
EXPOSE 8080

CMD ["node", "server.js"]
