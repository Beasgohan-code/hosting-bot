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
const dataFile = path.join(dataDir, "services.json");
const port = Number(process.env.PORT ?? 8787);
const monitorTickMs = Math.max(5_000, Number(process.env.KEEPALIVE_TICK_MS ?? 15_000));

type ServiceStatus = "live" | "building" | "stopped" | "failed";
type Runtime = "Node.js" | "Python" | "Docker";
type KeepAliveStatus = "pending" | "up" | "down" | "paused";

type Service = {
  id: string;
  name: string;
  repoUrl: string;
  branch: string;
  runtime: Runtime;
  region: string;
  status: ServiceStatus;
  url: string;
  lastDeploy: string;
  createdAt: string;
  updatedAt: string;
  buildTime: string;
  cpu: number;
  memory: number;
  description: string;
};

type Activity = {
  id: string;
  serviceId: string;
  serviceName: string;
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

const serviceInput = z.object({
  name: z.string().trim().min(2).max(40),
  repoUrl: z.string().trim().url(),
  branch: z.string().trim().min(1).max(80).default("main"),
  runtime: z.enum(["Node.js", "Python", "Docker"]).default("Node.js"),
  region: z.string().trim().min(2).max(60).default("Singapore (southeast-1)"),
  description: z.string().trim().max(120).default("Managed by Hosting Bot"),
});

const statusInput = z.object({ status: z.enum(["live", "stopped"]) });
const keepAliveInput = z.object({
  name: z.string().trim().min(2).max(48).optional(),
  url: z.string().trim().min(1).max(2048),
  intervalMinutes: z.number().int().min(MIN_MONITOR_INTERVAL_MINUTES).max(MAX_MONITOR_INTERVAL_MINUTES).default(DEFAULT_MONITOR_INTERVAL_MINUTES),
});
const keepAlivePatchInput = z.object({ enabled: z.boolean() });

let services: Service[] = [];
let activities: Activity[] = [];
let keepAlives: KeepAliveMonitor[] = [];
let requestCount = 0;
let persistQueue: Promise<void> = Promise.resolve();
let monitorSweepRunning = false;
const monitorChecksInFlight = new Set<string>();
const deploymentTimers = new Map<string, NodeJS.Timeout>();
const eventClients = new Set<Response>();

const now = () => new Date().toISOString();

function publicService(service: Service) {
  return {
    ...service,
    lastDeploy: new Date(service.lastDeploy).getTime(),
    createdAt: new Date(service.createdAt).getTime(),
    updatedAt: new Date(service.updatedAt).getTime(),
  };
}

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
  const live = services.filter((service) => service.status === "live").length;
  const building = services.filter((service) => service.status === "building").length;
  const memory = services.reduce((sum, service) => sum + service.memory, 0);
  const checkCount = keepAlives.reduce((sum, monitor) => sum + monitor.successCount + monitor.failureCount, 0);
  const successCount = keepAlives.reduce((sum, monitor) => sum + monitor.successCount, 0);
  const uptime = checkCount ? `${((successCount / checkCount) * 100).toFixed(2)}%` : "—";
  return {
    services: services.map(publicService),
    activities: [...activities],
    keepAlives: keepAlives.map(publicMonitor),
    metrics: {
      live,
      building,
      total: services.length,
      memory,
      uptime,
      requests: requestCount.toLocaleString("en-US"),
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
  const snapshot = JSON.stringify({ services, activities, keepAlives }, null, 2);
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
    const created = now();
    services = [
      {
        id: "svc_gateway",
        name: "telegram-gateway",
        repoUrl: "https://github.com/Beasgohan-code/hosting-bot",
        branch: "main",
        runtime: "Python",
        region: "Singapore (southeast-1)",
        status: "live",
        url: "telegram-gateway.hosting.bot",
        lastDeploy: new Date(Date.now() - 1000 * 60 * 18).toISOString(),
        createdAt: created,
        updatedAt: created,
        buildTime: "42s",
        cpu: 12,
        memory: 148,
        description: "Telegram worker with auto-healing and log streaming",
      },
      {
        id: "svc_dashboard",
        name: "dashboard-api",
        repoUrl: "https://github.com/Beasgohan-code/hosting-bot",
        branch: "main",
        runtime: "Node.js",
        region: "Singapore (southeast-1)",
        status: "live",
        url: "dashboard-api.hosting.bot",
        lastDeploy: new Date(Date.now() - 1000 * 60 * 47).toISOString(),
        createdAt: created,
        updatedAt: created,
        buildTime: "31s",
        cpu: 7,
        memory: 96,
        description: "TypeScript control plane API",
      },
      {
        id: "svc_landing",
        name: "landing-preview",
        repoUrl: "https://github.com/Beasgohan-code/hosting-bot",
        branch: "redesign",
        runtime: "Node.js",
        region: "Frankfurt (eu-central-1)",
        status: "stopped",
        url: "landing-preview.hosting.bot",
        lastDeploy: new Date(Date.now() - 1000 * 60 * 60 * 8).toISOString(),
        createdAt: created,
        updatedAt: created,
        buildTime: "28s",
        cpu: 0,
        memory: 0,
        description: "Preview environment for the dashboard refresh",
      },
    ];
    activities = [
      { id: randomUUID(), serviceId: "svc_gateway", serviceName: "telegram-gateway", action: "Deployment succeeded", detail: "Build completed in 42s", createdAt: new Date(Date.now() - 1000 * 60 * 18).toISOString(), tone: "success" },
      { id: randomUUID(), serviceId: "svc_dashboard", serviceName: "dashboard-api", action: "Deployment succeeded", detail: "v1.4.0 is live", createdAt: new Date(Date.now() - 1000 * 60 * 47).toISOString(), tone: "success" },
      { id: randomUUID(), serviceId: "svc_landing", serviceName: "landing-preview", action: "Service stopped", detail: "Manual action from dashboard", createdAt: new Date(Date.now() - 1000 * 60 * 79).toISOString(), tone: "warning" },
      { id: randomUUID(), serviceId: "svc_gateway", serviceName: "telegram-gateway", action: "Health check passed", detail: "Latency 84ms", createdAt: new Date(Date.now() - 1000 * 60 * 96).toISOString(), tone: "info" },
    ];
    keepAlives = [];
    await persist();
    return;
  }

  const parsed = JSON.parse(content) as {
    services?: Service[];
    activities?: Activity[];
    keepAlives?: KeepAliveMonitor[];
  };
  services = parsed.services ?? [];
  activities = parsed.activities ?? [];
  keepAlives = parsed.keepAlives ?? [];

  // A deploy simulation cannot survive a process restart; don't leave stale jobs
  // appearing to build forever after a server restart.
  let interruptedDeployment = false;
  for (const service of services) {
    if (service.status === "building") {
      service.status = "failed";
      service.updatedAt = now();
      interruptedDeployment = true;
      activity(service, "Deployment interrupted", "Control plane restarted while this release was building", "warning");
    }
  }
  if (interruptedDeployment) await persist();
}

function activity(service: Pick<Service, "id" | "name">, action: string, detail: string, tone: Activity["tone"] = "info") {
  activities.unshift({ id: randomUUID(), serviceId: service.id, serviceName: service.name, action, detail, createdAt: now(), tone });
  activities = activities.slice(0, 50);
}

function activityForMonitor(monitor: KeepAliveMonitor, action: string, detail: string, tone: Activity["tone"] = "info") {
  activities.unshift({ id: randomUUID(), serviceId: monitor.id, serviceName: monitor.name, action, detail, createdAt: now(), tone });
  activities = activities.slice(0, 50);
}

function clearDeploymentTimer(id: string) {
  const timer = deploymentTimers.get(id);
  if (timer) clearTimeout(timer);
  deploymentTimers.delete(id);
}

async function finishDeployment(id: string) {
  const service = services.find((entry) => entry.id === id);
  if (!service || service.status !== "building") return;
  service.status = "live";
  service.buildTime = `${Math.floor(24 + Math.random() * 28)}s`;
  service.cpu = Math.floor(4 + Math.random() * 10);
  service.memory = Math.floor(72 + Math.random() * 80);
  service.updatedAt = now();
  activity(service, "Deployment succeeded", `${service.name} is live at ${service.url}`, "success");
  clearDeploymentTimer(id);
  await persist();
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

app.get("/api/health", (_req, res) => {
  const monitored = keepAlives.length;
  const failing = keepAlives.filter((monitor) => monitor.enabled && monitor.status === "down").length;
  res.json({
    ok: true,
    service: "hosting-bot",
    timestamp: now(),
    uptimeSeconds: Math.floor(process.uptime()),
    keepAlive: { monitored, failing },
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

app.post("/api/services", async (req: Request, res: Response) => {
  const parsed = serviceInput.safeParse(req.body);
  if (!parsed.success) return res.status(400).json({ error: "Invalid service details", issues: parsed.error.issues });
  const input = parsed.data;
  const created = now();
  const id = `svc_${randomUUID().slice(0, 8)}`;
  const service: Service = {
    id,
    name: input.name,
    repoUrl: input.repoUrl,
    branch: input.branch,
    runtime: input.runtime,
    region: input.region,
    status: "building",
    url: `${input.name.toLowerCase().replace(/[^a-z0-9]+/g, "-")}.hosting.bot`,
    lastDeploy: created,
    createdAt: created,
    updatedAt: created,
    buildTime: "—",
    cpu: 0,
    memory: 0,
    description: input.description,
  };
  services.unshift(service);
  activity(service, "Deployment queued", `Building ${input.branch} from ${input.repoUrl}`, "info");
  await persist();
  const timer = setTimeout(() => void finishDeployment(id).catch((error) => console.error("[hosting-bot] deployment completion failed", error)), 1_600);
  timer.unref();
  deploymentTimers.set(id, timer);
  res.status(201).json(publicService(service));
});

app.patch("/api/services/:id/status", async (req: Request, res: Response) => {
  const parsed = statusInput.safeParse(req.body);
  const service = services.find((entry) => entry.id === req.params.id);
  if (!service) return res.status(404).json({ error: "Service not found" });
  if (!parsed.success) return res.status(400).json({ error: "Invalid status" });
  if (parsed.data.status === "stopped") clearDeploymentTimer(service.id);
  service.status = parsed.data.status;
  service.cpu = service.status === "live" ? Math.max(4, service.cpu) : 0;
  service.memory = service.status === "live" ? Math.max(72, service.memory) : 0;
  service.updatedAt = now();
  activity(service, service.status === "live" ? "Service started" : "Service stopped", "Manual action from dashboard", service.status === "live" ? "success" : "warning");
  await persist();
  res.json(publicService(service));
});

app.post("/api/services/:id/redeploy", async (req: Request, res: Response) => {
  const service = services.find((entry) => entry.id === req.params.id);
  if (!service) return res.status(404).json({ error: "Service not found" });
  clearDeploymentTimer(service.id);
  service.status = "building";
  service.updatedAt = now();
  activity(service, "Redeploy started", `Pulling ${service.branch} and preparing a new release`, "info");
  await persist();
  const timer = setTimeout(() => void finishDeployment(service.id).catch((error) => console.error("[hosting-bot] deployment completion failed", error)), 1_800);
  timer.unref();
  deploymentTimers.set(service.id, timer);
  res.json(publicService(service));
});

app.delete("/api/services/:id", async (req, res) => {
  const index = services.findIndex((entry) => entry.id === req.params.id);
  if (index === -1) return res.status(404).json({ error: "Service not found" });
  const [removed] = services.splice(index, 1);
  clearDeploymentTimer(req.params.id);
  if (removed) activity(removed, "Service removed", "Removed from the project registry", "warning");
  await persist();
  res.status(204).send();
});

app.use((error: unknown, _req: Request, res: Response, _next: express.NextFunction) => {
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
app.listen(port, "0.0.0.0", () => console.log(`[hosting-bot] control plane listening on :${port}`));
