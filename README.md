# Hosting Bot

Hosting Bot is a small deployment control plane for the existing Telegram hosting worker. It now includes a TypeScript/Express service, a Vite + React dashboard, and a persistent JSON service registry for local or single-instance deployments.

> The dashboard is intentionally honest about its boundary: it manages the service registry and deployment lifecycle today, while repository execution belongs behind an isolated runner. This keeps the control plane safe to deploy before adding a privileged build sandbox.

## What is included

| Layer | Implementation | Responsibility |
| --- | --- | --- |
| Dashboard | React, Vite, Three.js, anime.js | Service overview, deploy flow, activity, status controls, detail drawer |
| API | TypeScript, Express, Zod | Health checks, service CRUD, start/stop, redeploy lifecycle |
| Persistence | `data/services.json` | Local registry and activity timeline; replace with Postgres when multi-user auth is added |
| Telegram worker | Existing Python bot | Telegram-native deployment management, logs, auto-healing, and process controls |
| Deployment | Docker + `render.yaml` | Separate web control plane and Telegram worker services |

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

The service API exposes:

| Method | Route | Purpose |
| --- | --- | --- |
| `GET` | `/api/health` | Render health check |
| `GET` | `/api/overview` | Services, activity, and workspace metrics |
| `POST` | `/api/services` | Queue a service deployment |
| `PATCH` | `/api/services/:id/status` | Start or stop a service |
| `POST` | `/api/services/:id/redeploy` | Start a new release cycle |
| `DELETE` | `/api/services/:id` | Remove a service from the registry |

## Telegram worker configuration

The Python worker now reads secrets from the environment. Set `BOT_TOKEN` and a comma-separated `ADMIN_IDS` value before starting it:

```bash
BOT_TOKEN=123456:replace-me ADMIN_IDS=123456789 python ultimate_hosting_bot_fixed.py
```

Do not commit tokens, cookies, or a populated `hosting_data` directory.

## Render deployment

The included `render.yaml` creates two services: a Node web service named `hosting-bot-control-plane` and a Docker worker named `hosting-bot-telegram-worker`. Set the `BOT_TOKEN` and `ADMIN_IDS` environment variables in the Render dashboard. The control plane is deployable as-is; for real arbitrary repository builds, attach a separate isolated runner and replace the lifecycle simulation in `server/index.ts` with a queue-backed runner adapter.

## Next production steps

The current implementation is a durable single-instance control plane rather than a multi-tenant PaaS. The next hardening steps are authenticated users and teams, Postgres-backed service state, Redis-backed deployment jobs, signed GitHub webhooks, per-service isolated containers, resource quotas, and log streaming over Server-Sent Events or WebSockets.

## Telegram plugin system

The Telegram worker now loads extensions through `bot_plugins.core.PluginManager`. Built-in plugins are discovered automatically at startup, and additional modules can be loaded with the comma-separated `PLUGIN_MODULES` environment variable. Each plugin is a normal Python module with a `create_plugin()` factory returning a `BotPlugin` instance.

| Plugin | Commands | Purpose |
| --- | --- | --- |
| `diagnostics` | `/health`, `/plugins`, `/stats` | Inspect health, loaded extensions, and aggregate service counts |
| `services` | `/services`, `/service <bot_id>` | List hosted services and inspect one service |
| `admin` | `/admin`, `/restartall` | Guarded admin summary and production restarts |
| `example` | `/ping` | Reference local extension in `bot_plugins/local/example_plugin.py` |

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

## Live dashboard visualization

The web dashboard now includes a live infrastructure topology on the Overview page. The Three.js scene renders the control plane as a central hub with service nodes, connection lines, runtime-colored halos, hover states, and clickable labels that open the existing service drawer. Anime.js handles node entrance, selection, and rotation transitions while respecting the dashboard’s reduced-motion styling.

The browser subscribes to `GET /api/events` using Server-Sent Events. Every service mutation broadcasts a fresh overview payload containing services, activity, and metrics, so start, stop, deploy, redeploy, and delete actions appear without a manual refresh. The existing periodic fetch remains as a lightweight fallback if the stream is unavailable.
