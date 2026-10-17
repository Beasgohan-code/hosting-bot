export type ActivityItem = {
  id: string;
  targetId: string;
  targetName: string;
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
  totalMonitors: number;
  activeMonitors: number;
  healthyMonitors: number;
  failingMonitors: number;
  totalChecks: number;
  probeSuccessRate: number | null;
  processMemoryMb: number;
  uptimeSeconds: number;
  apiRequests: number;
};
