import cors from "cors";
import express, { type Request, type Response } from "express";
import { promises as fs } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { randomUUID } from "node:crypto";
import { z } from "zod";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const rootDir = path.resolve(__dirname, "..");
const dataDir = path.join(rootDir, "data");
const dataFile = path.join(dataDir, "services.json");
const port = Number(process.env.PORT ?? 8787);

type ServiceStatus = "live" | "building" | "stopped" | "failed";
type Runtime = "Node.js" | "Python" | "Docker";

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

const serviceInput = z.object({
  name: z.string().trim().min(2).max(40),
  repoUrl: z.string().trim().url(),
  branch: z.string().trim().min(1).max(80).default("main"),
  runtime: z.enum(["Node.js", "Python", "Docker"]).default("Node.js"),
  region: z.string().trim().min(2).max(60).default("Singapore (southeast-1)"),
  description: z.string().trim().max(120).default("Managed by Hosting Bot"),
});

const statusInput = z.object({ status: z.enum(["live", "stopped"]) });

let services: Service[] = [];
let activities: Activity[] = [];
const deploymentTimers = new Map<string, NodeJS.Timeout>();
const eventClients = new Set<Response>();

const now = () => new Date().toISOString();

function overviewPayload() {
  const live = services.filter((service) => service.status === "live").length;
  const building = services.filter((service) => service.status === "building").length;
  const memory = services.reduce((sum, service) => sum + service.memory, 0);
  return {
    services: services.map(publicService),
    activities,
    metrics: { live, building, total: services.length, memory, uptime: "99.98%", requests: "1.24M" },
  };
}

function broadcastOverview() {
  const message = `event: overview\\ndata: ${JSON.stringify(overviewPayload())}\\n\\n`;
  for (const client of eventClients) {
    try { client.write(message); } catch { eventClients.delete(client); }
  }
}

async function persist() {
  await fs.mkdir(dataDir, { recursive: true });
  await fs.writeFile(dataFile, JSON.stringify({ services, activities }, null, 2));
  broadcastOverview();
}

async function load() {
  try {
    const content = await fs.readFile(dataFile, "utf8");
    const parsed = JSON.parse(content) as { services?: Service[]; activities?: Activity[] };
    services = parsed.services ?? [];
    activities = parsed.activities ?? [];
  } catch {
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
    await persist();
  }
}

function activity(service: Service, action: string, detail: string, tone: Activity["tone"] = "info") {
  activities.unshift({ id: randomUUID(), serviceId: service.id, serviceName: service.name, action, detail, createdAt: now(), tone });
  activities = activities.slice(0, 24);
}

function publicService(service: Service) {
  return { ...service, lastDeploy: new Date(service.lastDeploy).getTime(), createdAt: new Date(service.createdAt).getTime(), updatedAt: new Date(service.updatedAt).getTime() };
}

const app = express();
app.use(cors());
app.use(express.json({ limit: "100kb" }));

app.get("/api/health", (_req, res) => res.json({ ok: true, service: "hosting-bot", timestamp: now() }));

app.get("/api/overview", (_req, res) => res.json(overviewPayload()));

app.get("/api/events", (req, res) => {
  res.setHeader("Content-Type", "text/event-stream");
  res.setHeader("Cache-Control", "no-cache");
  res.setHeader("Connection", "keep-alive");
  res.flushHeaders();
  eventClients.add(res);
  res.write(`event: overview\\ndata: ${JSON.stringify(overviewPayload())}\\n\\n`);
  req.on("close", () => eventClients.delete(res));
});

setInterval(() => {
  for (const client of eventClients) {
    try { client.write(": heartbeat\\n\\n"); } catch { eventClients.delete(client); }
  }
}, 15000);

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

  const timer = setTimeout(async () => {
    const current = services.find((entry) => entry.id === id);
    if (!current) return;
    current.status = "live";
    current.buildTime = `${Math.floor(24 + Math.random() * 28)}s`;
    current.cpu = Math.floor(4 + Math.random() * 10);
    current.memory = Math.floor(72 + Math.random() * 80);
    current.updatedAt = now();
    activity(current, "Deployment succeeded", `${current.name} is live at ${current.url}`, "success");
    await persist();
    deploymentTimers.delete(id);
  }, 1600);
  deploymentTimers.set(id, timer);
  res.status(201).json(publicService(service));
});

app.patch("/api/services/:id/status", async (req: Request, res: Response) => {
  const parsed = statusInput.safeParse(req.body);
  const service = services.find((entry) => entry.id === req.params.id);
  if (!service) return res.status(404).json({ error: "Service not found" });
  if (!parsed.success) return res.status(400).json({ error: "Invalid status" });
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
  service.status = "building";
  service.updatedAt = now();
  activity(service, "Redeploy started", `Pulling ${service.branch} and preparing a new release`, "info");
  await persist();
  setTimeout(async () => {
    service.status = "live";
    service.lastDeploy = now();
    service.updatedAt = now();
    service.cpu = Math.max(service.cpu, 5);
    service.memory = Math.max(service.memory, 88);
    activity(service, "Deployment succeeded", "New release promoted to production", "success");
    await persist();
  }, 1800);
  res.json(publicService(service));
});

app.delete("/api/services/:id", async (req, res) => {
  const index = services.findIndex((entry) => entry.id === req.params.id);
  if (index === -1) return res.status(404).json({ error: "Service not found" });
  const [removed] = services.splice(index, 1);
  if (removed) activity(removed, "Service removed", "Removed from the project registry", "warning");
  await persist();
  res.status(204).send();
});

if (process.env.NODE_ENV === "production") {
  const distDir = path.join(rootDir, "dist");
  app.use(express.static(distDir));
  app.use((_req, res) => res.sendFile(path.join(distDir, "index.html")));
}

await load();
app.listen(port, "0.0.0.0", () => console.log(`[hosting-bot] control plane listening on :${port}`));
