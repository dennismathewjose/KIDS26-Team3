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
# The server reads PORT (defaults to 8080 in server.js)
ENV PORT=80
WORKDIR /app

# Bring in the pre-installed production dependencies
COPY --from=deps /app/node_modules ./node_modules

# Copy application source
COPY package.json server.js ./
COPY public ./public

# Writable directory for multer temp uploads, owned by the non-root user
RUN mkdir -p uploads && chown -R node:node /app

USER node
EXPOSE 80

CMD ["node", "server.js"]
