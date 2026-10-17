export type ServiceStatus = "live" | "building" | "stopped" | "failed";
export type Runtime = "Node.js" | "Python" | "Docker";

export type Service = {
  id: string;
  name: string;
  repoUrl: string;
  branch: string;
  runtime: Runtime;
  region: string;
  status: ServiceStatus;
  url: string;
  lastDeploy: number;
  createdAt: number;
  updatedAt: number;
  buildTime: string;
  cpu: number;
  memory: number;
  description: string;
};

export type ActivityItem = {
  id: string;
  serviceName: string;
  action: string;
  detail: string;
  createdAt: string;
  tone: "success" | "info" | "warning";
};

export type KeepAliveStatus = "pending" | "up" | "down" | "paused";

export type KeepAliveCheck = {
  id: string;
  checkedAt: number;
  ok: boolean;
  statusCode: number | null;
  latencyMs: number;
  error: string | null;
};

export type KeepAliveMonitor = {
  id: string;
  name: string;
  url: string;
  intervalMinutes: number;
  enabled: boolean;
  status: KeepAliveStatus;
  createdAt: number;
  lastCheckedAt: number | null;
  nextCheckAt: number | null;
  lastStatusCode: number | null;
  lastLatencyMs: number | null;
  consecutiveFailures: number;
  successCount: number;
  failureCount: number;
  lastError: string | null;
  history: KeepAliveCheck[];
};

export type Metrics = {
  live: number;
  building: number;
  total: number;
  memory: number;
  uptime: string;
  requests: string;
};
