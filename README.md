# Hosting Bot

Hosting Bot includes a TypeScript/Express control plane, a Vite + React dashboard, and a Telegram worker. The dashboard reports real control-plane process telemetry and real public HTTP monitor results; it does not invent sample services, deployment states, resource usage, or activity.

> Repository deployments are not enabled in the dashboard. A restricted isolated runner is required before arbitrary builds, start/stop controls, deployment history, or per-service resource metrics can be reported safely. Until one is configured, those operations are not presented as real.

## What is included

| Layer | Implementation | Responsibility |
| --- | --- | --- |
| Dashboard | React, Vite, Three.js, anime.js | Live endpoint health, real process telemetry, monitor history, and recorded activity |
| API | TypeScript, Express, Zod | Control-plane health, process metrics, SSRF-safe monitor management, and Server-Sent Events |
| Persistence | `data/monitoring.json` | Single-instance keep-alive monitors and real monitor activity; migrated from the old demo registry file |
| Telegram worker | Python bot + plugin runtime | Telegram-native deployment management, logs, circuit-breaker auto-healing, and public endpoint monitors with Telegram alerts |
| Keep-alive API | Express scheduler + JSON registry | Scheduled HTTP probes, latency/status history, pause/resume, manual checks, and SSRF-safe public target validation |
| Deployment | Docker Compose + `render.yaml` | Self-hosted control plane and Telegram worker; a repository deployment runner is not included |

## Local development

```bash
npm install
npm run dev
```

The Vite dashboard is available at `http://localhost:5173` and proxies `/api` to the TypeScript service on `http://localhost:8787`.

For a production-style local run:

```bash
npm run build
NODE_ENV=production PORT=8787 npm start
```

The API exposes these working control-plane and monitoring routes:

| Method | Route | Purpose |
| --- | --- | --- |
| `GET` | `/api/health` | Real process health, uptime, and RSS memory |
| `GET` | `/api/overview` | Monitor readings, recorded events, and live process telemetry |
| `GET` | `/api/events` | Server-Sent Events for real overview updates |
| `GET` | `/api/keepalives` | List persisted public endpoint monitors |
| `POST` | `/api/keepalives` | Validate and create a real HTTP(S) health monitor |
| `PATCH` | `/api/keepalives/:id` | Pause or resume scheduled checks |
| `POST` | `/api/keepalives/:id/check` | Run a real immediate HTTP check and record its result |
| `DELETE` | `/api/keepalives/:id` | Remove a monitor |

Requests to `/api/services` return `501` until an isolated deployment runner is configured. The dashboard no longer shows fake service records or offers simulated start, stop, or redeploy controls.

Dashboard keep-alive checks run every minute or longer, store the latest 20 results per target, report latency and HTTP status, and broadcast state changes over the event stream. Targets are restricted to public HTTP(S) endpoints; private/local IPs, embedded credentials, query strings, and redirects are blocked to reduce SSRF risk. The Telegram worker has its own separate `/keepalive` monitor store and sends alerts for monitors created through Telegram; the two stores are not synchronized.

## Telegram worker configuration

The Python worker now reads secrets from the environment. Set `BOT_TOKEN` and a comma-separated `ADMIN_IDS` value before starting it:

```bash
BOT_TOKEN=123456:replace-me ADMIN_IDS=123456789 python ultimate_hosting_bot_fixed.py
```

Do not commit tokens, cookies, or a populated `hosting_data` directory. If a bot token is ever exposed in a log, screenshot, chat, or URL, revoke it with BotFather immediately, replace `BOT_TOKEN` in the deployment environment, and redeploy; existing provider logs cannot be scrubbed from this worker. The worker now redacts Telegram bot tokens from its own logs and suppresses HTTPX request-URL info logs, but these controls are defense in depth, not a substitute for rotation. The worker's container health endpoint is available at `/livez`, `/readyz`, and `/healthz` on port `8788`; readiness becomes healthy only after the Telegram application and plugins have started. Configure `HEALTH_HOST` and `HEALTH_PORT` only if you need to change the defaults.

When a deployment, manual start/restart, update, environment-change restart, or auto-heal attempt fails, the worker sends configured admins a Telegram DM with the service/owner IDs, failure reason, and recent log lines. Unhandled Telegram update errors are also logged safely and sent to admins with a short cooldown to prevent alert floods. Set `ADMIN_IDS` to comma-separated Telegram user IDs, and have each admin open the bot and send `/start` once so Telegram permits the worker to message them. Common token/password formats are redacted before logs are sent, but this is best-effort—not a guarantee that every secret is detected—so hosted applications should not print credentials to stdout/stderr. If an admin DM cannot be delivered, the worker records the send failure in its own logs.

Core commands are `/help`, `/status`, `/id`, `/logs`, and `/cancel`. `/help` reflects the currently loaded plugins; `/status` scopes service and keep-alive counts to the requesting user, while admins see global counts and worker metrics. Telegram's default command menu omits admin-only plugin commands, and configured admins receive a private command menu. Admin broadcasts escape HTML, rate-limit delivery, and report sent/failed counts.

The one-click backup action creates private ZIP archives with a 100 MiB uncompressed-data and 20,000-file limit, keeps the five newest per service, and excludes `.git`, dependency/cache directories, and symbolic links. Archives up to 20 MiB are sent to the requesting user's Telegram DM; larger ones remain in the persistent worker volume.

## Docker Compose deployment

The default Compose stack runs the API/dashboard control plane and Telegram worker as separate, non-root containers. It includes image-level and Compose health checks, graceful-stop windows, restart policies, read-only root filesystems, dropped Linux capabilities, bounded JSON logs, and named volumes for persistent application data. The root `Dockerfile` is an all-in-one image that runs both applications; `Dockerfile.control-plane` and `Dockerfile.bot` remain available for the isolated Compose services.

1. Install Docker Engine with the Compose v2 plugin, then create your local environment file:

   ```bash
   cp .env.example .env
   ```

2. Edit `.env` and set `BOT_TOKEN` and `ADMIN_IDS` for the Telegram worker. Keep `.env` private; it is excluded from both Git and the Docker build context.
3. Build and start the stack:

   ```bash
   docker compose up --build -d
   docker compose ps
   docker compose logs -f
   ```

4. Open `http://localhost:8787` (or the host port set with `DASHBOARD_PORT`). The control-plane API is exposed on that same port; the worker's health port is internal to the Compose network and is not published publicly.

The control plane's health endpoint is `GET /api/health`. The Telegram worker's readiness endpoint is `GET /readyz` on container port `8788`. Health status is visible with `docker compose ps`; inspect a service with `docker compose logs -f control-plane` or `docker compose logs -f telegram-worker`. To stop the stack without removing state, run `docker compose down`. The named volumes `control-plane-data` and `telegram-worker-data` preserve monitor state, the worker SQLite database, logs, backups, and hosted repositories across rebuilds; the all-in-one profile uses `all-in-one-data`. **Do not use `docker compose down -v` unless you intend to permanently delete that data.** Back up these volumes before maintenance or migration.

The control-plane image installs only API runtime dependencies in its final layer. The Python worker installs the project's application requirements at image build time. Update the project files and rebuild with `docker compose up --build -d`; changes to Python requirements also require rebuilding the worker image. If port `8787` is already in use, set `DASHBOARD_PORT` in `.env` to another host port. Never publish the Telegram health port or put a real bot token in a Dockerfile or image.

### Single-image / all-in-one option

For a single Docker container that starts both the dashboard/API and Telegram polling worker, use the root `Dockerfile`:

```bash
docker compose --profile all-in-one up --build -d all-in-one
docker compose logs -f all-in-one
```

Or build and run it directly:

```bash
docker build -t hosting-bot:all-in-one .
docker run -d --name hosting-bot --restart unless-stopped \
  --read-only --cap-drop ALL --security-opt no-new-privileges:true \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --env-file .env -e NODE_ENV=production -e PORT=8787 \
  -e DATA_DIR=/var/lib/hosting-bot/control-plane \
  -e HOSTING_DATA_DIR=/var/lib/hosting-bot/telegram-worker \
  -p 8787:8787 -v hosting-bot-data:/var/lib/hosting-bot \
  hosting-bot:all-in-one
```

The image requires `BOT_TOKEN` and keeps both services' data in `/var/lib/hosting-bot`; only the dashboard/API port is published. Its health check requires both the API and Telegram worker to be ready. Choose this mode **instead of** the default Compose services, not alongside them—the combined container shares one security/restart boundary and would otherwise duplicate Telegram polling. For production and hosted-repository isolation, the separate services remain preferred.

The worker runs hosted repositories as an unprivileged user, but a container is not a full security boundary for executing arbitrary untrusted code. For multi-user/public hosting, isolate user deployments in separate restricted runner containers/VMs with resource limits, no shared secrets, and a queue-backed deployment service.

## Render deployment

The included `render.yaml` creates two services: a Node web service named `hosting-bot-control-plane` and a Docker worker named `hosting-bot-telegram-worker`. Set `BOT_TOKEN` and `ADMIN_IDS` in the Render dashboard. Keep both services on an always-on host for their schedulers. Repository deployments remain disabled until a separate isolated runner is integrated; no deployment success is fabricated by the control plane.

## Vercel deployment

`vercel.json` configures a static dashboard build. The dashboard never switches to demo data: without a reachable control-plane API it displays an offline state and empty/unavailable readings. To connect a persistent API, set `VITE_API_BASE_URL` to its origin and configure the API's `CORS_ORIGIN` with the exact Vercel dashboard origin(s).

Vercel can host the static dashboard, but it cannot keep the Telegram polling worker or the in-process keep-alive scheduler running continuously. Run the Python worker and scheduled monitor API on Render, a VPS, or another persistent host. The file-backed `data/monitoring.json` store is intentionally single-instance and is not a serverless database.

## Next production steps

The current implementation is a durable single-instance monitor control plane rather than a multi-tenant PaaS. The next hardening steps are authenticated users and teams, Postgres-backed monitor state, an isolated deployment runner with queue-backed jobs, signed GitHub webhooks, per-tenant resource quotas, and deployment-log streaming over Server-Sent Events or WebSockets.

## Telegram plugin system

The Telegram worker now loads extensions through `bot_plugins.core.PluginManager`. Built-in plugins are discovered automatically at startup, and additional modules can be loaded with the comma-separated `PLUGIN_MODULES` environment variable. Each plugin is a normal Python module with a `create_plugin()` factory returning a `BotPlugin` instance.

| Plugin | Commands | Purpose |
| --- | --- | --- |
| `diagnostics` | `/health`, `/plugins`, `/stats` (admin-only) | Inspect health and loaded extensions; aggregate service counts are restricted to admins |
| `services` | `/services`, `/service <bot_id>` | List hosted services and inspect one service |
| `keepalive` | `/keepalive` | Add, check, pause, resume, or remove public HTTP monitors; sends down/recovery alerts |
| `admin` | `/admin`, `/restartall` | Guarded admin summary and restart of running or failed production services; stopped services remain stopped |
| `example` | `/ping` | Reference local extension in `bot_plugins/local/example_plugin.py` |

Use `/keepalive help` for the command syntax. For example, `/keepalive add https://my-service.example/health 5` checks a public endpoint every five minutes. The worker blocks local/private destinations and does not follow redirects. Process auto-healing uses a configurable restart budget to prevent crash loops; after the budget is reached the service is marked as an error for manual inspection.

A minimal extension looks like this:

```python
from telegram import Update
from telegram.ext import CommandHandler, ContextTypes
from bot_plugins.core import BotPlugin, PluginSpec

class MetricsPlugin(BotPlugin):
    spec = PluginSpec("metrics", "1.0.0", "Runtime metrics", (("metrics", "Show metrics"),))

    def handlers(self):
        return (CommandHandler("metrics", self.metrics),)

    async def metrics(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("Metrics are ready")

def create_plugin():
    return MetricsPlugin()
```

The runtime isolates import and lifecycle failures, exposes a stable `PluginContext` containing the config, database, process manager, log streamer, and UI factory, and reports extension health through `/health`. Privileged plugins must perform their own user checks; the built-in admin plugin demonstrates the expected pattern.

## Live dashboard telemetry

The dashboard's Overview page displays actual monitored-target status, probe success rate from recorded checks, Node process RSS, process uptime, and API request counts. Values remain unavailable until the API responds; there are no seeded services, sample events, simulated build results, or fabricated per-service resource numbers.

The browser subscribes to `GET /api/events` using Server-Sent Events and polls the API every 15 seconds as a fallback. Events are written only when real monitor configuration or check results change. Repository deployment screens and controls are intentionally omitted until a restricted runner can provide real build and runtime data.
