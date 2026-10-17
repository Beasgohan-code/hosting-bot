import cors from "cors";
import express, { type Request, type Response } from "express";
import { promises as fs } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { randomUUID } from "node:crypto";
import { z } from "zod";
import {
  DEFAULT_MONITOR_INTERVAL_MINUTES,
  MAX_MONITOR_INTERVAL_MINUTES,
  MIN_MONITOR_INTERVAL_MINUTES,
  probeMonitorTarget,
  validateMonitorUrl,
} from "./monitor.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const rootDir = path.resolve(__dirname, "..");
const dataDir = path.resolve(process.env.DATA_DIR ?? path.join(rootDir, "data"));
const dataFile = path.join(dataDir, "monitoring.json");
const legacyDataFile = path.join(dataDir, "services.json");
const port = Number(process.env.PORT ?? 8787);
const monitorTickMs = Math.max(5_000, Number(process.env.KEEPALIVE_TICK_MS ?? 15_000));

type KeepAliveStatus = "pending" | "up" | "down" | "paused";

type Activity = {
  id: string;
  targetId: string;
  targetName: string;
  action: string;
  detail: string;
  createdAt: string;
  tone: "success" | "info" | "warning";
};

type MonitorCheck = {
  id: string;
  checkedAt: string;
  ok: boolean;
  statusCode: number | null;
  latencyMs: number;
  error: string | null;
};

type KeepAliveMonitor = {
  id: string;
  name: string;
  url: string;
  intervalMinutes: number;
  enabled: boolean;
  status: KeepAliveStatus;
  createdAt: string;
  lastCheckedAt: string | null;
  lastStatusCode: number | null;
  lastLatencyMs: number | null;
  consecutiveFailures: number;
  successCount: number;
  failureCount: number;
  lastError: string | null;
  history: MonitorCheck[];
};

const keepAliveInput = z.object({
  name: z.string().trim().min(2).max(48).optional(),
  url: z.string().trim().min(1).max(2048),
  intervalMinutes: z.number().int().min(MIN_MONITOR_INTERVAL_MINUTES).max(MAX_MONITOR_INTERVAL_MINUTES).default(DEFAULT_MONITOR_INTERVAL_MINUTES),
});
const keepAlivePatchInput = z.object({ enabled: z.boolean() });

let activities: Activity[] = [];
let keepAlives: KeepAliveMonitor[] = [];
let requestCount = 0;
let persistQueue: Promise<void> = Promise.resolve();
let monitorSweepRunning = false;
let shuttingDown = false;
let shutdownPromise: Promise<void> | null = null;
const monitorChecksInFlight = new Set<string>();
const eventClients = new Set<Response>();

const now = () => new Date().toISOString();

function publicMonitor(monitor: KeepAliveMonitor) {
  const lastChecked = monitor.lastCheckedAt ? new Date(monitor.lastCheckedAt).getTime() : null;
  return {
    ...monitor,
    createdAt: new Date(monitor.createdAt).getTime(),
    lastCheckedAt: lastChecked,
    nextCheckAt: monitor.enabled
      ? (lastChecked === null ? Date.now() : lastChecked + monitor.intervalMinutes * 60_000)
      : null,
    history: monitor.history.map((check) => ({ ...check, checkedAt: new Date(check.checkedAt).getTime() })),
  };
}

function overviewPayload() {
  const activeMonitors = keepAlives.filter((monitor) => monitor.enabled).length;
  const healthyMonitors = keepAlives.filter((monitor) => monitor.enabled && monitor.status === "up").length;
  const failingMonitors = keepAlives.filter((monitor) => monitor.enabled && monitor.status === "down").length;
  const totalChecks = keepAlives.reduce((sum, monitor) => sum + monitor.successCount + monitor.failureCount, 0);
  const successfulChecks = keepAlives.reduce((sum, monitor) => sum + monitor.successCount, 0);
  const probeSuccessRate = totalChecks
    ? Number(((successfulChecks / totalChecks) * 100).toFixed(2))
    : null;
  return {
    activities: [...activities],
    keepAlives: keepAlives.map(publicMonitor),
    metrics: {
      totalMonitors: keepAlives.length,
      activeMonitors,
      healthyMonitors,
      failingMonitors,
      totalChecks,
      probeSuccessRate,
      processMemoryMb: Math.round(process.memoryUsage().rss / 1_048_576),
      uptimeSeconds: Math.floor(process.uptime()),
      apiRequests: requestCount,
    },
  };
}

function broadcastOverview() {
  const message = `event: overview\ndata: ${JSON.stringify(overviewPayload())}\n\n`;
  for (const client of eventClients) {
    try {
      client.write(message);
    } catch {
      eventClients.delete(client);
    }
  }
}

function persist() {
  const snapshot = JSON.stringify({ activities, keepAlives }, null, 2);
  persistQueue = persistQueue.catch(() => undefined).then(async () => {
    await fs.mkdir(dataDir, { recursive: true });
    const tempFile = `${dataFile}.${process.pid}.tmp`;
    await fs.writeFile(tempFile, snapshot, "utf8");
    await fs.rename(tempFile, dataFile);
    broadcastOverview();
  });
  return persistQueue;
}

async function load() {
  let content: string;
  try {
    content = await fs.readFile(dataFile, "utf8");
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
    try {
      content = await fs.readFile(legacyDataFile, "utf8");
    } catch (legacyError) {
      if ((legacyError as NodeJS.ErrnoException).code !== "ENOENT") throw legacyError;
      activities = [];
      keepAlives = [];
      await persist();
      return;
    }
  }

  const parsed = JSON.parse(content) as {
    activities?: Array<Partial<Activity> & { serviceId?: string; serviceName?: string }>;
    keepAlives?: KeepAliveMonitor[];
  };
  keepAlives = Array.isArray(parsed.keepAlives) ? parsed.keepAlives : [];

  // Keep real monitor history but discard the old fabricated service/deployment
  // records from previous dashboard versions.
  activities = (Array.isArray(parsed.activities) ? parsed.activities : []).flatMap((entry) => {
    const targetId = entry.targetId ?? entry.serviceId;
    if (!targetId?.startsWith("mon_") || typeof entry.action !== "string") return [];
    const tone = entry.tone === "success" || entry.tone === "warning" ? entry.tone : "info";
    return [{
      id: typeof entry.id === "string" ? entry.id : randomUUID(),
      targetId,
      targetName: typeof entry.targetName === "string"
        ? entry.targetName
        : typeof entry.serviceName === "string" ? entry.serviceName : "Monitor",
      action: entry.action,
      detail: typeof entry.detail === "string" ? entry.detail : "",
      createdAt: typeof entry.createdAt === "string" ? entry.createdAt : now(),
      tone,
    }];
  });

  // Write the cleaned schema, preserving real monitors but removing all former
  // demo services and simulated deployment history.
  await persist();
  try {
    await fs.unlink(legacyDataFile);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
  }
}

function activityForMonitor(monitor: KeepAliveMonitor, action: string, detail: string, tone: Activity["tone"] = "info") {
  activities.unshift({
    id: randomUUID(),
    targetId: monitor.id,
    targetName: monitor.name,
    action,
    detail,
    createdAt: now(),
    tone,
  });
  activities = activities.slice(0, 50);
}

async function checkMonitor(monitor: KeepAliveMonitor, source: "scheduled" | "manual" = "scheduled") {
  if (monitorChecksInFlight.has(monitor.id)) throw new Error("A check is already running for this monitor.");
  monitorChecksInFlight.add(monitor.id);
  try {
    const result = await probeMonitorTarget(monitor.url);
    const previousStatus = monitor.status;
    const checkedAt = now();
    const check: MonitorCheck = {
      id: randomUUID(),
      checkedAt,
      ok: result.ok,
      statusCode: result.statusCode,
      latencyMs: result.latencyMs,
      error: result.error,
    };
    monitor.lastCheckedAt = checkedAt;
    monitor.lastStatusCode = result.statusCode;
    monitor.lastLatencyMs = result.latencyMs;
    monitor.lastError = result.error;
    monitor.status = monitor.enabled ? (result.ok ? "up" : "down") : "paused";
    monitor.consecutiveFailures = result.ok ? 0 : monitor.consecutiveFailures + 1;
    if (result.ok) monitor.successCount += 1;
    else monitor.failureCount += 1;
    monitor.history.unshift(check);
    monitor.history = monitor.history.slice(0, 20);

    const detail = result.ok
      ? `HTTP ${result.statusCode} · ${result.latencyMs} ms`
      : `${result.statusCode ? `HTTP ${result.statusCode}` : "Request failed"} · ${result.error ?? "Unknown error"}`;
    if (result.ok && previousStatus !== "up") {
      activityForMonitor(monitor, "Keep-alive target recovered", detail, "success");
    } else if (!result.ok && previousStatus !== "down") {
      activityForMonitor(monitor, "Keep-alive target is down", detail, "warning");
    } else if (source === "manual") {
      activityForMonitor(monitor, "Manual keep-alive check", detail, result.ok ? "info" : "warning");
    }
    await persist();
    return publicMonitor(monitor);
  } finally {
    monitorChecksInFlight.delete(monitor.id);
  }
}

async function runScheduledMonitorChecks() {
  if (monitorSweepRunning) return;
  monitorSweepRunning = true;
  try {
    const time = Date.now();
    const due = keepAlives.filter((monitor) => {
      if (!monitor.enabled || monitorChecksInFlight.has(monitor.id)) return false;
      const last = monitor.lastCheckedAt ? Date.parse(monitor.lastCheckedAt) : 0;
      return !last || time - last >= monitor.intervalMinutes * 60_000;
    });
    await Promise.all(due.map((monitor) => checkMonitor(monitor)));
  } catch (error) {
    console.error("[hosting-bot] keep-alive scheduler error", error);
  } finally {
    monitorSweepRunning = false;
  }
}

const app = express();
app.disable("x-powered-by");
const corsOrigins = new Set((process.env.CORS_ORIGIN ?? "").split(",").map((origin) => origin.trim()).filter(Boolean));
if (corsOrigins.size) {
  app.use(cors({
    origin: (origin, callback) => callback(null, Boolean(origin && corsOrigins.has(origin)) ? origin : false),
  }));
}
app.use(express.json({ limit: "100kb" }));
app.use((req, _res, next) => {
  if (req.path.startsWith("/api/") && req.path !== "/api/events") requestCount += 1;
  next();
});
app.use((req, res, next) => {
  if (shuttingDown && req.path.startsWith("/api/")) {
    return res.status(503).json({ error: "Control plane is shutting down" });
  }
  next();
});

app.get("/api/health", (_req, res) => {
  const monitored = keepAlives.length;
  const failing = keepAlives.filter((monitor) => monitor.enabled && monitor.status === "down").length;
  res.json({
    ok: true,
    service: "hosting-bot",
    timestamp: now(),
    uptimeSeconds: Math.floor(process.uptime()),
    keepAlive: { monitored, failing },
    processMemoryMb: Math.round(process.memoryUsage().rss / 1_048_576),
  });
});

app.get("/api/overview", (_req, res) => res.json(overviewPayload()));

app.get("/api/events", (req, res) => {
  res.setHeader("Content-Type", "text/event-stream; charset=utf-8");
  res.setHeader("Cache-Control", "no-cache, no-transform");
  res.setHeader("Connection", "keep-alive");
  res.flushHeaders();
  eventClients.add(res);
  res.write(`event: overview\ndata: ${JSON.stringify(overviewPayload())}\n\n`);
  req.on("close", () => eventClients.delete(res));
});

const heartbeat = setInterval(() => {
  for (const client of eventClients) {
    try {
      client.write(": heartbeat\n\n");
    } catch {
      eventClients.delete(client);
    }
  }
}, 15_000);
heartbeat.unref();

app.get("/api/keepalives", (_req, res) => {
  res.json({ keepAlives: keepAlives.map(publicMonitor) });
});

app.post("/api/keepalives", async (req: Request, res: Response) => {
  const parsed = keepAliveInput.safeParse(req.body);
  if (!parsed.success) return res.status(400).json({ error: "Invalid monitor details", issues: parsed.error.issues });

  let url: URL;
  try {
    url = await validateMonitorUrl(parsed.data.url);
  } catch (error) {
    return res.status(400).json({ error: error instanceof Error ? error.message : "Invalid monitor URL" });
  }

  const name = parsed.data.name?.trim() || url.hostname.replace(/^\[|\]$/g, "");
  const createdAt = now();
  const monitor: KeepAliveMonitor = {
    id: `mon_${randomUUID().slice(0, 8)}`,
    name,
    url: url.toString(),
    intervalMinutes: parsed.data.intervalMinutes,
    enabled: true,
    status: "pending",
    createdAt,
    lastCheckedAt: null,
    lastStatusCode: null,
    lastLatencyMs: null,
    consecutiveFailures: 0,
    successCount: 0,
    failureCount: 0,
    lastError: null,
    history: [],
  };
  keepAlives.unshift(monitor);
  activityForMonitor(monitor, "Keep-alive monitor added", `Checks every ${monitor.intervalMinutes} minutes`, "info");
  await persist();
  void checkMonitor(monitor).catch((error) => console.error("[hosting-bot] initial monitor check failed", error));
  res.status(201).json(publicMonitor(monitor));
});

app.patch("/api/keepalives/:id", async (req: Request, res: Response) => {
  const parsed = keepAlivePatchInput.safeParse(req.body);
  if (!parsed.success) return res.status(400).json({ error: "Invalid monitor settings" });
  const monitor = keepAlives.find((entry) => entry.id === req.params.id);
  if (!monitor) return res.status(404).json({ error: "Monitor not found" });
  monitor.enabled = parsed.data.enabled;
  monitor.status = monitor.enabled ? "pending" : "paused";
  if (monitor.enabled) monitor.lastCheckedAt = null;
  activityForMonitor(monitor, monitor.enabled ? "Keep-alive monitor resumed" : "Keep-alive monitor paused", monitor.enabled ? "Scheduled checks resumed" : "Scheduled checks paused", monitor.enabled ? "success" : "warning");
  await persist();
  res.json(publicMonitor(monitor));
});

app.post("/api/keepalives/:id/check", async (req: Request, res: Response) => {
  const monitor = keepAlives.find((entry) => entry.id === req.params.id);
  if (!monitor) return res.status(404).json({ error: "Monitor not found" });
  if (!monitor.enabled) return res.status(409).json({ error: "Resume this monitor before running a check." });
  if (monitorChecksInFlight.has(monitor.id)) return res.status(409).json({ error: "A check is already running for this monitor." });
  const result = await checkMonitor(monitor, "manual");
  res.json(result);
});

app.delete("/api/keepalives/:id", async (req: Request, res: Response) => {
  const index = keepAlives.findIndex((entry) => entry.id === req.params.id);
  if (index < 0) return res.status(404).json({ error: "Monitor not found" });
  const [removed] = keepAlives.splice(index, 1);
  if (removed) activityForMonitor(removed, "Keep-alive monitor removed", "The monitor was removed from the workspace", "warning");
  await persist();
  res.status(204).send();
});

app.use("/api/services", (_req, res) => res.status(501).json({
  error: "Deployments are unavailable because no isolated deployment runner is configured.",
}));

app.use("/api", (_req, res) => res.status(404).json({ error: "API route not found" }));

app.use((error: unknown, _req: Request, res: Response, _next: express.NextFunction) => {
  if (error instanceof SyntaxError && "status" in error && error.status === 400) {
    return res.status(400).json({ error: "Malformed JSON request body" });
  }
  console.error("[hosting-bot] request failed", error);
  res.status(500).json({ error: "Internal control-plane error" });
});

if (process.env.NODE_ENV === "production") {
  const distDir = path.join(rootDir, "dist");
  app.use(express.static(distDir));
  app.use((_req, res) => res.sendFile(path.join(distDir, "index.html")));
}

await load();
const monitorInterval = setInterval(() => void runScheduledMonitorChecks(), monitorTickMs);
monitorInterval.unref();
const httpServer = app.listen(port, "0.0.0.0", () => {
  httpServer.keepAliveTimeout = 5_000;
  httpServer.headersTimeout = 10_000;
  console.log(`[hosting-bot] control plane listening on :${port}`);
});

async function shutdown(signal: NodeJS.Signals) {
  if (shutdownPromise) return shutdownPromise;
  shuttingDown = true;
  shutdownPromise = (async () => {
    console.info(`[hosting-bot] received ${signal}; shutting down gracefully`);
    clearInterval(heartbeat);
    clearInterval(monitorInterval);
    for (const client of eventClients) {
      try {
        client.end();
      } catch {
        // A disconnected browser is already gone.
      }
    }
    eventClients.clear();

    const forceCloseTimer = setTimeout(() => {
      console.error("[hosting-bot] graceful shutdown timed out; closing open connections");
      httpServer.closeAllConnections();
    }, 12_000);
    forceCloseTimer.unref();
    const serverClosed = new Promise<void>((resolve) => httpServer.close(() => resolve()));

    try {
      await persist();
    } catch (error) {
      console.error("[hosting-bot] final persistence failed", error);
    }
    await serverClosed;
    clearTimeout(forceCloseTimer);
    console.info("[hosting-bot] shutdown complete");
  })();
  return shutdownPromise;
}

process.once("SIGTERM", () => void shutdown("SIGTERM"));
process.once("SIGINT", () => void shutdown("SIGINT"));
