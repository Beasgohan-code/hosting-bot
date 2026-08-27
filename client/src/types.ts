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

export type Metrics = {
  live: number;
  building: number;
  total: number;
  memory: number;
  uptime: string;
  requests: string;
};
