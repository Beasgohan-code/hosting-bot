# syntax=docker/dockerfile:1.7

# Default image: a single container for the dashboard/API and Telegram worker.
# Separate runtime Dockerfiles remain available for deployments that prefer isolation.
FROM node:22-bookworm-slim AS frontend-build
WORKDIR /app

COPY package.json package-lock.json ./
RUN --mount=type=cache,id=hosting-bot-common-build-npm,target=/root/.npm npm ci

COPY . .
RUN npm run build

FROM node:22-bookworm-slim AS runtime
ENV NODE_ENV=production \
    PORT=8787 \
    DATA_DIR=/var/lib/hosting-bot/control-plane \
    HOSTING_DATA_DIR=/var/lib/hosting-bot/telegram-worker \
    HEALTH_HOST=0.0.0.0 \
    HEALTH_PORT=8788 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH="/opt/telegram-venv/bin:${PATH}"
WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates git procps python3 python3-venv tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 hosting \
    && useradd --uid 10001 --gid hosting --create-home --home-dir /home/hosting --shell /usr/sbin/nologin hosting \
    && mkdir -p /var/lib/hosting-bot/control-plane /var/lib/hosting-bot/telegram-worker \
    && chown -R hosting:hosting /var/lib/hosting-bot /home/hosting

COPY package.json package-lock.json ./
RUN --mount=type=cache,id=hosting-bot-common-runtime-npm,target=/root/.npm npm ci --omit=dev \
    && chown -R hosting:hosting /app

COPY requirements.txt /tmp/requirements.txt
RUN python3 -m venv /opt/telegram-venv \
    && /opt/telegram-venv/bin/pip install --disable-pip-version-check --no-cache-dir -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY --from=frontend-build --chown=hosting:hosting /app/dist ./dist
COPY --from=frontend-build --chown=hosting:hosting /app/dist-server ./dist-server
COPY --chown=hosting:hosting ultimate_hosting_bot_fixed.py ./ultimate_hosting_bot_fixed.py
COPY --chown=hosting:hosting bot_plugins ./bot_plugins
COPY docker/entrypoint.sh /usr/local/bin/hosting-bot-entrypoint
RUN chmod 0755 /usr/local/bin/hosting-bot-entrypoint

USER hosting:hosting
EXPOSE 8787 8788
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD node -e "const p=process.env.PORT||'8787',h=process.env.HEALTH_PORT||'8788';Promise.all([fetch('http://127.0.0.1:'+p+'/api/health'),fetch('http://127.0.0.1:'+h+'/readyz')]).then(rs=>process.exit(rs.every(r=>r.ok)?0:1)).catch(()=>process.exit(1))"

ENTRYPOINT ["tini", "--", "/usr/local/bin/hosting-bot-entrypoint"]
