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
  parseMonitorUrl,
  probeMonitorTarget,
  validateMonitorUrl,
} from "./monitor.js";
import { sendTelegramMonitorAlert } from "./notifications.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const rootDir = path.resolve(__dirname, "..");
const dataDir = path.resolve(process.env.DATA_DIR ?? path.join(rootDir, "data"));
const dataFile = path.join(dataDir, "monitoring.json");
const legacyDataFile = path.join(dataDir, "services.json");

function integerEnv(name: string, fallback: number, min: number, max: number): number {
  const raw = process.env[name]?.trim();
  if (!raw) return fallback;
  const value = Number(raw);
  if (!Number.isSafeInteger(value) || value < min || value > max) {
    throw new Error(`${name} must be an integer between ${min} and ${max}.`);
  }
  return value;
}

const port = integerEnv("PORT", 8787, 1, 65_535);
const monitorTickMs = integerEnv("KEEPALIVE_TICK_MS", 15_000, 5_000, 3_600_000);
const MAX_KEEPALIVES = 100;
const MAX_CONCURRENT_MONITOR_CHECKS = 10;
const MAX_SSE_CLIENTS = 100;
const API_RATE_LIMIT = 240;
const API_RATE_WINDOW_MS = 60_000;
const MAX_RATE_LIMIT_KEYS = 10_000;
const KEEPALIVE_TELEGRAM_BOT_TOKEN = process.env.KEEPALIVE_TELEGRAM_BOT_TOKEN?.trim() ?? "";
const KEEPALIVE_TELEGRAM_CHAT_ID = process.env.KEEPALIVE_TELEGRAM_CHAT_ID?.trim() ?? "";
const telegramAlertsConfigured = Boolean(KEEPALIVE_TELEGRAM_BOT_TOKEN && KEEPALIVE_TELEGRAM_CHAT_ID);

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

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function storedDate(value: unknown, fallback: string | null): string | null {
  if (typeof value !== "string") return fallback;
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp) ? new Date(timestamp).toISOString() : fallback;
}

function storedInteger(value: unknown, fallback = 0, min = 0, max = Number.MAX_SAFE_INTEGER): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value)) return fallback;
  return Math.max(min, Math.min(max, value));
}

function storedNullableInteger(value: unknown, min: number, max: number): number | null {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= min && value <= max ? value : null;
}

function sanitizeCheck(value: unknown): MonitorCheck | null {
  if (!isRecord(value)) return null;
  const checkedAt = storedDate(value.checkedAt, null);
  if (!checkedAt || typeof value.ok !== "boolean") return null;
  return {
    id: typeof value.id === "string" ? value.id.slice(0, 80) : randomUUID(),
    checkedAt,
    ok: value.ok,
    statusCode: storedNullableInteger(value.statusCode, 100, 599),
    latencyMs: typeof value.latencyMs === "number" && Number.isFinite(value.latencyMs) && value.latencyMs >= 0 ? Math.min(value.latencyMs, 3_600_000) : 0,
    error: typeof value.error === "string" ? value.error.slice(0, 180) : null,
  };
}

function sanitizeMonitor(value: unknown): KeepAliveMonitor | null {
  if (!isRecord(value)) return null;
  let url: URL;
  try {
    if (typeof value.url !== "string") return null;
    url = parseMonitorUrl(value.url);
  } catch {
    return null;
  }

  const id = typeof value.id === "string" && /^mon_[a-zA-Z0-9_-]{4,80}$/.test(value.id)
    ? value.id
    : `mon_${randomUUID().slice(0, 8)}`;
  const name = typeof value.name === "string" && value.name.trim()
    ? value.name.trim().slice(0, 48)
    : url.hostname.replace(/^\[|\]$/g, "").slice(0, 48);
  const enabled = typeof value.enabled === "boolean" ? value.enabled : true;
  const statuses: KeepAliveStatus[] = ["pending", "up", "down", "paused"];
  const status = !enabled ? "paused" : statuses.includes(value.status as KeepAliveStatus) && value.status !== "paused"
    ? value.status as KeepAliveStatus
    : "pending";
  const history = Array.isArray(value.history)
    ? value.history.map(sanitizeCheck).filter((check): check is MonitorCheck => check !== null).slice(0, 20)
    : [];

  return {
    id,
    name,
    url: url.toString(),
    intervalMinutes: storedInteger(value.intervalMinutes, DEFAULT_MONITOR_INTERVAL_MINUTES, MIN_MONITOR_INTERVAL_MINUTES, MAX_MONITOR_INTERVAL_MINUTES),
    enabled,
    status,
    createdAt: storedDate(value.createdAt, now()) ?? now(),
    lastCheckedAt: storedDate(value.lastCheckedAt, null),
    lastStatusCode: storedNullableInteger(value.lastStatusCode, 100, 599),
    lastLatencyMs: typeof value.lastLatencyMs === "number" && Number.isFinite(value.lastLatencyMs) && value.lastLatencyMs >= 0 ? Math.min(value.lastLatencyMs, 3_600_000) : null,
    consecutiveFailures: storedInteger(value.consecutiveFailures),
    successCount: storedInteger(value.successCount),
    failureCount: storedInteger(value.failureCount),
    lastError: typeof value.lastError === "string" ? value.lastError.slice(0, 180) : null,
    history,
  };
}

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
const apiRateBuckets = new Map<string, { count: number; resetAt: number }>();
const pendingNotifications = new Set<Promise<void>>();

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
    integrations: { telegramAlertsConfigured },
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
      if (!client.write(message)) {
        eventClients.delete(client);
        client.end();
      }
    } catch {
      eventClients.delete(client);
      client.end();
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

async function parseStateFile(filename: string, content: string): Promise<Record<string, unknown> | null> {
  try {
    const parsed: unknown = JSON.parse(content);
    if (isRecord(parsed)) return parsed;
  } catch {
    // A corrupt state file is preserved below before recovery starts.
  }

  const backup = `${filename}.corrupt-${Date.now()}`;
  try {
    await fs.rename(filename, backup);
    console.error(`[hosting-bot] invalid state file preserved as ${path.basename(backup)}`);
  } catch (error) {
    console.error(`[hosting-bot] could not quarantine invalid state file ${path.basename(filename)}`, error);
  }
  return null;
}

async function readStateFile(filename: string): Promise<{ content: string; exists: boolean }> {
  try {
    const stat = await fs.stat(filename);
    if (stat.size > 10 * 1024 * 1024) throw new Error("State file exceeds the 10 MB safety limit.");
    return { content: await fs.readFile(filename, "utf8"), exists: true };
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return { content: "", exists: false };
    throw error;
  }
}

async function load() {
  let sourceFile = dataFile;
  let { content, exists } = await readStateFile(dataFile);
  if (!exists) {
    sourceFile = legacyDataFile;
    ({ content, exists } = await readStateFile(legacyDataFile));
  }

  if (!exists) {
    activities = [];
    keepAlives = [];
    await persist();
    return;
  }

  let parsed = await parseStateFile(sourceFile, content);
  if (!parsed && sourceFile === dataFile) {
    sourceFile = legacyDataFile;
    const legacy = await readStateFile(legacyDataFile);
    parsed = legacy.exists ? await parseStateFile(legacyDataFile, legacy.content) : null;
  }
  if (!parsed) {
    activities = [];
    keepAlives = [];
    await persist();
    return;
  }

  const seenMonitorIds = new Set<string>();
  const candidates = Array.isArray(parsed.keepAlives) ? parsed.keepAlives : [];
  keepAlives = candidates.flatMap((entry) => {
    const monitor = sanitizeMonitor(entry);
    if (!monitor || seenMonitorIds.has(monitor.id)) return [];
    seenMonitorIds.add(monitor.id);
    return [monitor];
  }).slice(0, MAX_KEEPALIVES);
  if (candidates.length !== keepAlives.length) {
    console.warn(`[hosting-bot] discarded ${candidates.length - keepAlives.length} invalid or excess monitor record(s)`);
  }

  // Preserve real monitor activity while dropping simulated service/deployment records.
  const activityCandidates = Array.isArray(parsed.activities) ? parsed.activities : [];
  activities = activityCandidates.flatMap((entry) => {
    if (!isRecord(entry)) return [];
    const targetId = entry.targetId ?? entry.serviceId;
    if (typeof targetId !== "string" || !targetId.startsWith("mon_") || typeof entry.action !== "string") return [];
    const tone: Activity["tone"] = entry.tone === "success" || entry.tone === "warning" ? entry.tone : "info";
    return [{
      id: typeof entry.id === "string" ? entry.id.slice(0, 80) : randomUUID(),
      targetId,
      targetName: typeof entry.targetName === "string"
        ? entry.targetName.slice(0, 48)
        : typeof entry.serviceName === "string" ? entry.serviceName.slice(0, 48) : "Monitor",
      action: entry.action.slice(0, 80),
      detail: typeof entry.detail === "string" ? entry.detail.slice(0, 180) : "",
      createdAt: storedDate(entry.createdAt, now()) ?? now(),
      tone,
    }];
  }).slice(0, 50);

  await persist();
  if (sourceFile === legacyDataFile) {
    try {
      await fs.unlink(legacyDataFile);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
    }
  } else {
    // If the new file was valid, the old registry is obsolete after the clean
    // replacement is safely written. A corrupt legacy file is quarantined first.
    try {
      await fs.unlink(legacyDataFile);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
    }
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

function queueTelegramAlert(monitor: KeepAliveMonitor, state: "down" | "recovered", detail: string) {
  if (!telegramAlertsConfigured) return;
  const task = sendTelegramMonitorAlert(
    { botToken: KEEPALIVE_TELEGRAM_BOT_TOKEN, chatId: KEEPALIVE_TELEGRAM_CHAT_ID },
    { state, monitorName: monitor.name, monitorUrl: monitor.url, detail },
  ).then((result) => {
    if (!result.sent) console.warn(`[hosting-bot] Telegram alert failed for monitor ${monitor.id}: ${result.error ?? "unknown error"}`);
  });
  pendingNotifications.add(task);
  void task.finally(() => pendingNotifications.delete(task));
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
    let alertState: "down" | "recovered" | null = null;
    if (monitor.enabled && result.ok && previousStatus !== "up") {
      activityForMonitor(monitor, "Keep-alive target recovered", detail, "success");
      alertState = "recovered";
    } else if (monitor.enabled && !result.ok && previousStatus !== "down") {
      activityForMonitor(monitor, "Keep-alive target is down", detail, "warning");
      alertState = "down";
    } else if (source === "manual") {
      activityForMonitor(monitor, "Manual keep-alive check", detail, result.ok ? "info" : "warning");
    }
    await persist();
    if (alertState) queueTelegramAlert(monitor, alertState, detail);
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
    let cursor = 0;
    const workers = Array.from({ length: Math.min(MAX_CONCURRENT_MONITOR_CHECKS, due.length) }, async () => {
      while (cursor < due.length) {
        const monitor = due[cursor++];
        if (!monitor) continue;
        try {
          await checkMonitor(monitor);
        } catch (error) {
          console.error(`[hosting-bot] scheduled check failed for ${monitor.id}`, error);
        }
      }
    });
    await Promise.all(workers);
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
app.use((_req, res, next) => {
  res.setHeader("X-Content-Type-Options", "nosniff");
  res.setHeader("Referrer-Policy", "no-referrer");
  res.setHeader("Permissions-Policy", "camera=(), microphone=(), geolocation=()");
  next();
});
app.use(express.json({ limit: "100kb" }));
app.use((req, res, next) => {
  if (req.path.startsWith("/api/")) res.setHeader("Cache-Control", "no-store");
  if (req.path.startsWith("/api/") && req.path !== "/api/events") requestCount += 1;
  next();
});
app.use((req, res, next) => {
  if (shuttingDown && req.path.startsWith("/api/")) {
    return res.status(503).json({ error: "Control plane is shutting down" });
  }
  next();
});
app.use((req, res, next) => {
  if (!req.path.startsWith("/api/") || req.path === "/api/health" || req.method === "OPTIONS") return next();
  if (req.path === "/api/events") {
    if (eventClients.size >= MAX_SSE_CLIENTS) {
      return res.status(503).json({ error: "The event stream has reached its connection limit." });
    }
    return next();
  }

  const clientKey = req.ip || req.socket.remoteAddress || "unknown";
  const time = Date.now();
  let bucket = apiRateBuckets.get(clientKey);
  if (!bucket || bucket.resetAt <= time) {
    bucket = { count: 0, resetAt: time + API_RATE_WINDOW_MS };
    apiRateBuckets.set(clientKey, bucket);
  }
  if (bucket.count >= API_RATE_LIMIT) {
    const retryAfter = Math.max(1, Math.ceil((bucket.resetAt - time) / 1000));
    res.setHeader("Retry-After", String(retryAfter));
    res.setHeader("RateLimit-Limit", String(API_RATE_LIMIT));
    res.setHeader("RateLimit-Remaining", "0");
    res.setHeader("RateLimit-Reset", String(Math.ceil(bucket.resetAt / 1000)));
    return res.status(429).json({ error: "API rate limit exceeded. Try again shortly." });
  }
  bucket.count += 1;
  res.setHeader("RateLimit-Limit", String(API_RATE_LIMIT));
  res.setHeader("RateLimit-Remaining", String(API_RATE_LIMIT - bucket.count));
  res.setHeader("RateLimit-Reset", String(Math.ceil(bucket.resetAt / 1000)));

  if (apiRateBuckets.size > MAX_RATE_LIMIT_KEYS) {
    for (const [key, entry] of apiRateBuckets) {
      if (entry.resetAt <= time) apiRateBuckets.delete(key);
      if (apiRateBuckets.size <= MAX_RATE_LIMIT_KEYS) break;
    }
    while (apiRateBuckets.size > MAX_RATE_LIMIT_KEYS) {
      const oldestKey = apiRateBuckets.keys().next().value;
      if (oldestKey === undefined) break;
      apiRateBuckets.delete(oldestKey);
    }
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
    integrations: { telegramAlertsConfigured },
  });
});

app.get("/api/overview", (_req, res) => res.json(overviewPayload()));

app.get("/api/events", (req, res) => {
  if (eventClients.size >= MAX_SSE_CLIENTS) {
    return res.status(503).json({ error: "The event stream has reached its connection limit." });
  }
  res.setHeader("Content-Type", "text/event-stream; charset=utf-8");
  res.setHeader("Cache-Control", "no-cache, no-transform");
  res.setHeader("Connection", "keep-alive");
  res.setHeader("X-Accel-Buffering", "no");
  res.flushHeaders();
  eventClients.add(res);
  res.write(`event: overview\ndata: ${JSON.stringify(overviewPayload())}\n\n`);
  const removeClient = () => eventClients.delete(res);
  res.on("close", removeClient);
  res.on("error", removeClient);
  req.on("aborted", removeClient);
});

const heartbeat = setInterval(() => {
  for (const client of eventClients) {
    try {
      if (!client.write(": heartbeat\n\n")) {
        eventClients.delete(client);
        client.end();
      }
    } catch {
      eventClients.delete(client);
      client.end();
    }
  }
}, 15_000);
heartbeat.unref();

app.get("/api/keepalives", (_req, res) => {
  res.json({ keepAlives: keepAlives.map(publicMonitor) });
});

app.post("/api/keepalives", async (req: Request, res: Response) => {
  if (keepAlives.length >= MAX_KEEPALIVES) {
    return res.status(409).json({ error: `The monitor limit of ${MAX_KEEPALIVES} has been reached.` });
  }
  const parsed = keepAliveInput.safeParse(req.body);
  if (!parsed.success) return res.status(400).json({ error: "Invalid monitor details", issues: parsed.error.issues });

  let url: URL;
  try {
    url = await validateMonitorUrl(parsed.data.url);
  } catch (error) {
    return res.status(400).json({ error: error instanceof Error ? error.message : "Invalid monitor URL" });
  }

  const normalizedUrl = url.toString();
  if (keepAlives.some((monitor) => monitor.url === normalizedUrl)) {
    return res.status(409).json({ error: "A monitor for this URL already exists." });
  }
  const name = parsed.data.name?.trim() || url.hostname.replace(/^\[|\]$/g, "");
  const createdAt = now();
  const monitor: KeepAliveMonitor = {
    id: `mon_${randomUUID().slice(0, 8)}`,
    name,
    url: normalizedUrl,
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
  if (monitor.enabled === parsed.data.enabled) return res.json(publicMonitor(monitor));
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

app.delete("/api/keepalives/:id", async (req, res) => {
  const index = keepAlives.findIndex((entry) => entry.id === req.params.id);
  if (index < 0) return res.status(404).json({ error: "Monitor not found" });
  if (monitorChecksInFlight.has(req.params.id)) {
    return res.status(409).json({ error: "Wait for the active check to finish before removing this monitor." });
  }
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