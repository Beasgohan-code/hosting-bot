import { useEffect, useRef, useState } from "react";
import * as THREE from "three";
import { animate, stagger } from "animejs";
import {
  Activity,
  ArrowUpRight,
  Box,
  Check,
  ChevronDown,
  CircleHelp,
  Clock3,
  Cloud,
  Code2,
  Copy,
  Cpu,
  Database,
  ExternalLink,
  Github,
  GitBranch,
  Globe2,
  Layers3,
  LayoutDashboard,
  LifeBuoy,
  LoaderCircle,
  Menu,
  MoreHorizontal,
  Plus,
  Rocket,
  Search,
  Server,
  Settings2,
  ShieldCheck,
  Sparkles,
  SquareTerminal,
  Terminal,
  Trash2,
  TrendingUp,
  Users,
  X,
  Zap,
} from "lucide-react";
import "./styles.css";

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
  lastDeploy: number;
  createdAt: number;
  updatedAt: number;
  buildTime: string;
  cpu: number;
  memory: number;
  description: string;
};
type ActivityItem = { id: string; serviceName: string; action: string; detail: string; createdAt: string; tone: "success" | "info" | "warning" };
type Metrics = { live: number; building: number; total: number; memory: number; uptime: string; requests: string };

const fallbackServices: Service[] = [
  { id: "svc_gateway", name: "telegram-gateway", repoUrl: "https://github.com/Beasgohan-code/hosting-bot", branch: "main", runtime: "Python", region: "Singapore (southeast-1)", status: "live", url: "telegram-gateway.hosting.bot", lastDeploy: Date.now() - 1080000, createdAt: Date.now(), updatedAt: Date.now(), buildTime: "42s", cpu: 12, memory: 148, description: "Telegram worker with auto-healing and log streaming" },
  { id: "svc_dashboard", name: "dashboard-api", repoUrl: "https://github.com/Beasgohan-code/hosting-bot", branch: "main", runtime: "Node.js", region: "Singapore (southeast-1)", status: "live", url: "dashboard-api.hosting.bot", lastDeploy: Date.now() - 2820000, createdAt: Date.now(), updatedAt: Date.now(), buildTime: "31s", cpu: 7, memory: 96, description: "TypeScript control plane API" },
  { id: "svc_landing", name: "landing-preview", repoUrl: "https://github.com/Beasgohan-code/hosting-bot", branch: "redesign", runtime: "Node.js", region: "Frankfurt (eu-central-1)", status: "stopped", url: "landing-preview.hosting.bot", lastDeploy: Date.now() - 28800000, createdAt: Date.now(), updatedAt: Date.now(), buildTime: "28s", cpu: 0, memory: 0, description: "Preview environment for the dashboard refresh" },
];

function App() {
  const [services, setServices] = useState<Service[]>(fallbackServices);
  const [activities, setActivities] = useState<ActivityItem[]>([]);
  const [metrics, setMetrics] = useState<Metrics>({ live: 2, building: 0, total: 3, memory: 244, uptime: "99.98%", requests: "1.24M" });
  const [activeSection, setActiveSection] = useState("Overview");
  const [showDeploy, setShowDeploy] = useState(false);
  const [selected, setSelected] = useState<Service | null>(null);
  const [query, setQuery] = useState("");
  const [notice, setNotice] = useState("");

  async function refresh() {
    try {
      const response = await fetch("/api/overview");
      if (!response.ok) throw new Error("API unavailable");
      const data = await response.json();
      setServices(data.services);
      setActivities(data.activities);
      setMetrics(data.metrics);
    } catch {
      // The dashboard remains useful in preview mode when the API is not running.
    }
  }

  useEffect(() => { void refresh(); }, []);
  useEffect(() => {
    const timer = window.setInterval(() => void refresh(), 5000);
    return () => window.clearInterval(timer);
  }, []);
  useEffect(() => {
    animate(".animate-in", { opacity: [0, 1], translateY: [12, 0], delay: stagger(55), duration: 520, ease: "outExpo" });
  }, [activeSection]);

  const filteredServices = services.filter((service) => `${service.name} ${service.runtime} ${service.region}`.toLowerCase().includes(query.toLowerCase()));
  const showNotice = (message: string) => { setNotice(message); window.setTimeout(() => setNotice(""), 2600); };

  async function changeStatus(service: Service, status: "live" | "stopped") {
    setServices((current) => current.map((item) => item.id === service.id ? { ...item, status, cpu: status === "live" ? Math.max(item.cpu, 5) : 0, memory: status === "live" ? Math.max(item.memory, 80) : 0 } : item));
    try { await fetch(`/api/services/${service.id}/status`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ status }) }); await refresh(); } catch { /* preview mode */ }
    showNotice(`${service.name} ${status === "live" ? "started" : "stopped"}`);
  }

  async function redeploy(service: Service) {
    setServices((current) => current.map((item) => item.id === service.id ? { ...item, status: "building" } : item));
    try { await fetch(`/api/services/${service.id}/redeploy`, { method: "POST" }); } catch { window.setTimeout(() => setServices((current) => current.map((item) => item.id === service.id ? { ...item, status: "live", lastDeploy: Date.now() } : item)), 1600); }
    showNotice(`Redeploying ${service.name}`);
    window.setTimeout(() => void refresh(), 2200);
  }

  async function removeService(service: Service) {
    setServices((current) => current.filter((item) => item.id !== service.id));
    setSelected(null);
    try { await fetch(`/api/services/${service.id}`, { method: "DELETE" }); } catch { /* preview mode */ }
    showNotice(`${service.name} removed from project`);
  }

  async function createService(payload: Record<string, string>) {
    try {
      const response = await fetch("/api/services", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      if (response.ok) await refresh();
      else throw new Error("Unable to create");
    } catch {
      const created: Service = { id: `local_${Date.now()}`, name: payload.name, repoUrl: payload.repoUrl, branch: payload.branch, runtime: payload.runtime as Runtime, region: payload.region, status: "building", url: `${payload.name}.hosting.bot`, lastDeploy: Date.now(), createdAt: Date.now(), updatedAt: Date.now(), buildTime: "—", cpu: 0, memory: 0, description: payload.description || "Managed by Hosting Bot" };
      setServices((current) => [created, ...current]);
      window.setTimeout(() => setServices((current) => current.map((item) => item.id === created.id ? { ...item, status: "live", buildTime: "34s", cpu: 6, memory: 96, lastDeploy: Date.now() } : item)), 1600);
    }
    setShowDeploy(false);
    showNotice(`${payload.name} is being deployed`);
  }

  return (
    <div className="app-shell">
      <AmbientScene />
      <aside className="sidebar">
        <div className="brand"><div className="brand-mark"><span>H</span></div><div><strong>hosting<span className="accent">.</span>bot</strong><small>CONTROL PLANE</small></div></div>
        <div className="workspace"><div className="workspace-icon">B</div><div><span>Workspace</span><strong>Beasgohan labs</strong></div><ChevronDown size={14} /></div>
        <nav className="nav-list">
          <span className="nav-label">OPERATIONS</span>
          <NavItem icon={<LayoutDashboard size={16} />} label="Overview" active={activeSection === "Overview"} onClick={() => setActiveSection("Overview")} />
          <NavItem icon={<Server size={16} />} label="Services" count={services.length} active={activeSection === "Services"} onClick={() => setActiveSection("Services")} />
          <NavItem icon={<Rocket size={16} />} label="Deployments" active={activeSection === "Deployments"} onClick={() => setActiveSection("Deployments")} />
          <NavItem icon={<Activity size={16} />} label="Activity" active={activeSection === "Activity"} onClick={() => setActiveSection("Activity")} />
          <span className="nav-label nav-spacer">CONFIGURATION</span>
          <NavItem icon={<Database size={16} />} label="Environment" active={activeSection === "Environment"} onClick={() => setActiveSection("Environment")} />
          <NavItem icon={<ShieldCheck size={16} />} label="Access & teams" active={activeSection === "Access & teams"} onClick={() => setActiveSection("Access & teams")} />
          <NavItem icon={<Settings2 size={16} />} label="Settings" active={activeSection === "Settings"} onClick={() => setActiveSection("Settings")} />
        </nav>
        <div className="sidebar-bottom"><div className="plan-card"><div className="plan-top"><span className="plan-dot" /> Free workspace <span>·</span> <span className="mono">v1.4</span></div><div className="plan-meter"><span style={{ width: "32%" }} /></div><p>2.4 / 7.5 GB memory used</p><button onClick={() => showNotice("Billing and plan management coming soon")}>Manage plan <ArrowUpRight size={13} /></button></div><div className="user-row"><div className="avatar">BG</div><div><strong>Beasgohan</strong><span>Owner</span></div><MoreHorizontal size={17} /></div></div>
      </aside>
      <main className="main-content">
        <header className="topbar"><button className="mobile-menu"><Menu size={18} /></button><div className="breadcrumb"><span>Beasgohan labs</span><span>/</span><strong>{activeSection}</strong></div><div className="top-actions"><div className="system-pill"><span className="pulse" /> All systems operational</div><button className="icon-button" onClick={() => showNotice("Keyboard shortcuts: ⌘ K to search")}> <CircleHelp size={17} /></button><div className="top-avatar">BG</div></div></header>
        <div className="content-wrap">
          {activeSection === "Overview" && <Overview metrics={metrics} services={services} activities={activities} filteredServices={filteredServices} query={query} setQuery={setQuery} onDeploy={() => setShowDeploy(true)} onSelect={setSelected} onStatus={changeStatus} onRedeploy={redeploy} showNotice={showNotice} />}
          {activeSection === "Services" && <ServicesPage services={filteredServices} query={query} setQuery={setQuery} onDeploy={() => setShowDeploy(true)} onSelect={setSelected} onStatus={changeStatus} onRedeploy={redeploy} />}
          {activeSection === "Activity" && <ActivityPage activities={activities} />}
          {activeSection === "Deployments" && <DeploymentsPage services={services} onRedeploy={redeploy} onSelect={setSelected} />}
          {activeSection === "Environment" && <PlaceholderPage icon={<Database />} title="Environment variables" description="Encrypted project configuration will live here. Connect a service to manage secrets without committing them to Git." action="Add variable" onAction={() => showNotice("Environment variable editor coming soon")} />}
          {activeSection === "Access & teams" && <PlaceholderPage icon={<Users />} title="Access & teams" description="Invite your team and control who can deploy, inspect logs, or manage production services." action="Invite teammate" onAction={() => showNotice("Team invitations coming soon")} />}
          {activeSection === "Settings" && <PlaceholderPage icon={<Settings2 />} title="Workspace settings" description="Project identity, regions, deploy hooks, and runtime defaults will be configured here." action="Edit settings" onAction={() => showNotice("Settings editor coming soon")} />}
        </div>
      </main>
      {showDeploy && <DeployModal onClose={() => setShowDeploy(false)} onCreate={createService} />}
      {selected && <ServiceDrawer service={selected} onClose={() => setSelected(null)} onStatus={changeStatus} onRedeploy={redeploy} onRemove={removeService} />}
      {notice && <div className="toast"><Check size={15} />{notice}</div>}
    </div>
  );
}

function AmbientScene() {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!ref.current) return;
    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(50, 1, 0.1, 100);
    camera.position.z = 5;
    const renderer = new THREE.WebGLRenderer({ alpha: true, antialias: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    ref.current.appendChild(renderer.domElement);
    const geometry = new THREE.BufferGeometry();
    const positions = new Float32Array(1200 * 3);
    for (let i = 0; i < positions.length; i += 3) { positions[i] = (Math.random() - 0.5) * 11; positions[i + 1] = (Math.random() - 0.5) * 7; positions[i + 2] = (Math.random() - 0.5) * 5; }
    geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
    const points = new THREE.Points(geometry, new THREE.PointsMaterial({ color: 0x9ac9ff, size: 0.012, transparent: true, opacity: 0.5 }));
    scene.add(points);
    const resize = () => { if (!ref.current) return; const { width, height } = ref.current.getBoundingClientRect(); renderer.setSize(width, height, false); camera.aspect = width / height; camera.updateProjectionMatrix(); };
    resize(); window.addEventListener("resize", resize);
    let frame = 0;
    const animateScene = () => { frame = requestAnimationFrame(animateScene); points.rotation.y += 0.00035; points.rotation.x = Math.sin(Date.now() * 0.00012) * 0.06; renderer.render(scene, camera); };
    animateScene();
    return () => { cancelAnimationFrame(frame); window.removeEventListener("resize", resize); geometry.dispose(); points.material.dispose(); renderer.dispose(); renderer.domElement.remove(); };
  }, []);
  return <div ref={ref} className="ambient-scene" aria-hidden="true" />;
}

function NavItem({ icon, label, count, active, onClick }: { icon: React.ReactNode; label: string; count?: number; active: boolean; onClick: () => void }) { return <button className={`nav-item ${active ? "active" : ""}`} onClick={onClick}>{icon}<span>{label}</span>{count !== undefined && <em>{count}</em>}</button>; }
function StatusBadge({ status }: { status: ServiceStatus }) { return <span className={`status-badge ${status}`}><span className="status-dot" />{status === "building" ? "BUILDING" : status.toUpperCase()}</span>; }
function RuntimeIcon({ runtime }: { runtime: Runtime }) { return runtime === "Node.js" ? <Code2 size={15} /> : runtime === "Python" ? <SquareTerminal size={15} /> : <Box size={15} />; }
function timeAgo(value: number | string) { const delta = Math.max(0, Date.now() - new Date(value).getTime()); const mins = Math.floor(delta / 60000); if (mins < 1) return "just now"; if (mins < 60) return `${mins}m ago`; const hours = Math.floor(mins / 60); if (hours < 24) return `${hours}h ago`; return `${Math.floor(hours / 24)}d ago`; }

function Overview({ metrics, services, activities, filteredServices, query, setQuery, onDeploy, onSelect, onStatus, onRedeploy, showNotice }: { metrics: Metrics; services: Service[]; activities: ActivityItem[]; filteredServices: Service[]; query: string; setQuery: (value: string) => void; onDeploy: () => void; onSelect: (service: Service) => void; onStatus: (service: Service, status: "live" | "stopped") => void; onRedeploy: (service: Service) => void; showNotice: (message: string) => void }) {
  return <>
    <div className="hero animate-in"><div><div className="eyebrow"><Sparkles size={13} /> DEPLOYMENT CONTROL PLANE</div><h1>Ship <span>calmly.</span></h1><p>One quiet place to deploy, observe, and keep every service healthy.</p></div><div className="hero-actions"><button className="button secondary" onClick={() => showNotice("Project settings coming soon")}><Settings2 size={15} /> Project settings</button><button className="button primary" onClick={onDeploy}><Plus size={16} /> New service</button></div></div>
    <div className="metric-grid animate-in"><MetricCard icon={<Zap />} label="Live services" value={String(metrics.live)} detail={`${metrics.total} total services`} tone="lime" /><MetricCard icon={<TrendingUp />} label="30d uptime" value={metrics.uptime} detail="Across all production" tone="blue" /><MetricCard icon={<Cpu />} label="Memory usage" value={`${metrics.memory} MB`} detail="of 7.5 GB workspace" tone="violet" /><MetricCard icon={<Activity />} label="Requests today" value={metrics.requests} detail="+18.4% from yesterday" tone="orange" /></div>
    <div className="section-head animate-in"><div><div className="section-kicker"><span className="live-pulse" /> ACTIVE SERVICES</div><h2>Services <span>{services.length}</span></h2></div><div className="section-tools"><div className="search-box"><Search size={15} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search services" /></div><button className="button primary compact" onClick={onDeploy}><Plus size={15} /> New service</button></div></div>
    <div className="service-grid">{filteredServices.map((service) => <ServiceCard key={service.id} service={service} onSelect={onSelect} onStatus={onStatus} onRedeploy={onRedeploy} />)}{filteredServices.length === 0 && <EmptyState onDeploy={onDeploy} />}</div>
    <div className="lower-grid animate-in"><div className="panel activity-panel"><div className="panel-head"><div><span className="section-kicker">RECENT ACTIVITY</span><h3>Deployment stream</h3></div><button className="text-button" onClick={() => showNotice("Activity view is available in the sidebar")}>View all <ArrowUpRight size={13} /></button></div>{(activities.length ? activities.slice(0, 4) : fallbackActivity).map((item) => <ActivityRow key={item.id} item={item} />)}</div><div className="panel quick-panel"><div className="panel-head"><div><span className="section-kicker">QUICK ACCESS</span><h3>Build something</h3></div><Sparkles size={17} className="muted-icon" /></div><div className="quick-links"><button onClick={onDeploy}><div className="quick-icon cyan"><Rocket size={16} /></div><div><strong>Deploy a service</strong><span>Connect a Git repository</span></div><ArrowUpRight size={14} /></button><button onClick={() => showNotice("Logs are available from a service detail view")}><div className="quick-icon purple"><Terminal size={16} /></div><div><strong>Inspect logs</strong><span>Stream runtime output</span></div><ArrowUpRight size={14} /></button><button onClick={() => showNotice("Documentation portal coming soon")}><div className="quick-icon orange"><LifeBuoy size={16} /></div><div><strong>Read the docs</strong><span>Learn the deployment flow</span></div><ArrowUpRight size={14} /></button></div></div></div>
  </>;
}

const fallbackActivity: ActivityItem[] = [
  { id: "a1", serviceName: "telegram-gateway", action: "Deployment succeeded", detail: "Build completed in 42s", createdAt: new Date(Date.now() - 1080000).toISOString(), tone: "success" },
  { id: "a2", serviceName: "dashboard-api", action: "Deployment succeeded", detail: "v1.4.0 is live", createdAt: new Date(Date.now() - 2820000).toISOString(), tone: "success" },
  { id: "a3", serviceName: "landing-preview", action: "Service stopped", detail: "Manual action from dashboard", createdAt: new Date(Date.now() - 4740000).toISOString(), tone: "warning" },
  { id: "a4", serviceName: "telegram-gateway", action: "Health check passed", detail: "Latency 84ms", createdAt: new Date(Date.now() - 5760000).toISOString(), tone: "info" },
];

function MetricCard({ icon, label, value, detail, tone }: { icon: React.ReactNode; label: string; value: string; detail: string; tone: string }) { return <div className={`metric-card ${tone}`}><div className="metric-icon">{icon}</div><div className="metric-label">{label}</div><strong>{value}</strong><span>{detail}</span></div>; }
function ServiceCard({ service, onSelect, onStatus, onRedeploy }: { service: Service; onSelect: (service: Service) => void; onStatus: (service: Service, status: "live" | "stopped") => void; onRedeploy: (service: Service) => void }) { return <article className="service-card animate-in" onClick={() => onSelect(service)}><div className="service-top"><div className="service-title"><div className={`service-icon ${service.runtime === "Python" ? "gold" : "blue"}`}><RuntimeIcon runtime={service.runtime} /></div><div><h3>{service.name}</h3><span>{service.description}</span></div></div><button className="more-button" onClick={(event) => { event.stopPropagation(); onSelect(service); }}><MoreHorizontal size={18} /></button></div><div className="service-status"><StatusBadge status={service.status} /><span className="branch"><GitBranch size={13} />{service.branch}</span><span className="deployed-time"><Clock3 size={12} />{timeAgo(service.lastDeploy)}</span></div><div className="service-url"><Globe2 size={13} />{service.url}<ExternalLink size={12} /></div><div className="service-stats"><div><span>RUNTIME</span><strong>{service.runtime}</strong></div><div><span>REGION</span><strong>{service.region.split(" ")[0]}</strong></div><div><span>MEMORY</span><strong>{service.memory ? `${service.memory} MB` : "—"}</strong></div></div><div className="card-footer"><span className="health"><span className="health-line" />{service.status === "live" ? "Healthy" : service.status === "building" ? "Provisioning" : "Paused"}</span><div className="card-actions">{service.status === "live" ? <button onClick={(event) => { event.stopPropagation(); onStatus(service, "stopped"); }} title="Stop"><span className="stop-square" /></button> : <button onClick={(event) => { event.stopPropagation(); onStatus(service, "live"); }} title="Start"><Zap size={14} /></button>}<button onClick={(event) => { event.stopPropagation(); onRedeploy(service); }} title="Redeploy"><Rocket size={14} /></button></div></div></article>; }
function ActivityRow({ item }: { item: ActivityItem }) { return <div className="activity-row"><div className={`activity-icon ${item.tone}`}><span>{item.tone === "success" ? "✓" : item.tone === "warning" ? "!" : "i"}</span></div><div className="activity-copy"><strong>{item.action}</strong><span><b>{item.serviceName}</b> · {item.detail}</span></div><time>{timeAgo(item.createdAt)}</time></div>; }
function EmptyState({ onDeploy }: { onDeploy: () => void }) { return <div className="empty-state"><Cloud size={24} /><h3>No matching services</h3><p>Try another search or create a new service.</p><button className="button primary compact" onClick={onDeploy}><Plus size={14} /> New service</button></div>; }
function ServicesPage({ services, query, setQuery, onDeploy, onSelect, onStatus, onRedeploy }: { services: Service[]; query: string; setQuery: (value: string) => void; onDeploy: () => void; onSelect: (service: Service) => void; onStatus: (service: Service, status: "live" | "stopped") => void; onRedeploy: (service: Service) => void }) { return <><PageHeader eyebrow="SERVICE REGISTRY" title="Services" description="Every deployable workload in your workspace, in one view." action="New service" onAction={onDeploy} /><div className="wide-toolbar"><div className="search-box"><Search size={15} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Filter services" /></div><span className="toolbar-meta">{services.length} services</span></div><div className="service-grid wide-grid">{services.map((service) => <ServiceCard key={service.id} service={service} onSelect={onSelect} onStatus={onStatus} onRedeploy={onRedeploy} />)}</div></>; }
function ActivityPage({ activities }: { activities: ActivityItem[] }) { return <><PageHeader eyebrow="AUDIT TRAIL" title="Activity" description="A durable timeline for deploys, health checks, and manual actions." /><div className="panel activity-full">{(activities.length ? activities : fallbackActivity).map((item) => <ActivityRow key={item.id} item={item} />)}</div></>; }
function DeploymentsPage({ services, onRedeploy, onSelect }: { services: Service[]; onRedeploy: (service: Service) => void; onSelect: (service: Service) => void }) { return <><PageHeader eyebrow="RELEASE PIPELINE" title="Deployments" description="Promote changes with an explicit, observable release step." /><div className="panel deployment-table"><div className="table-head"><span>Service</span><span>Source</span><span>Status</span><span>Last deploy</span><span /></div>{services.map((service) => <div className="table-row" key={service.id} onClick={() => onSelect(service)}><div className="table-service"><div className="mini-icon"><RuntimeIcon runtime={service.runtime} /></div><strong>{service.name}</strong></div><span className="source"><Github size={13} /> {service.branch}</span><StatusBadge status={service.status} /><span className="mono muted-text">{timeAgo(service.lastDeploy)}</span><button className="text-button" onClick={(event) => { event.stopPropagation(); onRedeploy(service); }}>Redeploy</button></div>)}</div></>; }
function PageHeader({ eyebrow, title, description, action, onAction }: { eyebrow: string; title: string; description: string; action?: string; onAction?: () => void }) { return <div className="page-header animate-in"><div><span className="section-kicker">{eyebrow}</span><h1>{title}</h1><p>{description}</p></div>{action && onAction && <button className="button primary" onClick={onAction}><Plus size={16} /> {action}</button>}</div>; }
function PlaceholderPage({ icon, title, description, action, onAction }: { icon: React.ReactNode; title: string; description: string; action: string; onAction: () => void }) { return <div className="placeholder-page animate-in"><div className="placeholder-icon">{icon}</div><span className="section-kicker">COMING TO YOUR CONTROL PLANE</span><h1>{title}</h1><p>{description}</p><button className="button primary" onClick={onAction}><Plus size={15} /> {action}</button></div>; }

function DeployModal({ onClose, onCreate }: { onClose: () => void; onCreate: (payload: Record<string, string>) => Promise<void> }) { const [form, setForm] = useState({ name: "", repoUrl: "https://github.com/", branch: "main", runtime: "Node.js", region: "Singapore (southeast-1)", description: "" }); const [loading, setLoading] = useState(false); const update = (key: string, value: string) => setForm((current) => ({ ...current, [key]: value })); const submit = async (event: React.FormEvent) => { event.preventDefault(); if (!form.name || !form.repoUrl) return; setLoading(true); await onCreate(form); setLoading(false); }; return <div className="modal-backdrop" onMouseDown={(event) => event.target === event.currentTarget && onClose()}><form className="modal" onSubmit={submit}><div className="modal-head"><div><span className="section-kicker">NEW WORKLOAD</span><h2>Deploy a service</h2><p>Connect a repository and get a live endpoint.</p></div><button type="button" className="icon-button" onClick={onClose}><X size={17} /></button></div><label>Service name<input required value={form.name} onChange={(event) => update("name", event.target.value)} placeholder="e.g. payments-api" /></label><label>Repository URL<div className="input-with-icon"><Github size={15} /><input required type="url" value={form.repoUrl} onChange={(event) => update("repoUrl", event.target.value)} /></div></label><div className="form-row"><label>Branch<input value={form.branch} onChange={(event) => update("branch", event.target.value)} /></label><label>Runtime<select value={form.runtime} onChange={(event) => update("runtime", event.target.value)}><option>Node.js</option><option>Python</option><option>Docker</option></select></label></div><label>Region<select value={form.region} onChange={(event) => update("region", event.target.value)}><option>Singapore (southeast-1)</option><option>Frankfurt (eu-central-1)</option><option>Oregon (us-west-2)</option></select></label><label>Description <span className="optional">optional</span><input value={form.description} onChange={(event) => update("description", event.target.value)} placeholder="What does this service do?" /></label><div className="modal-note"><ShieldCheck size={15} /><span>Deploys start in an isolated control-plane preview. Connect a runner to execute repository builds.</span></div><div className="modal-actions"><button type="button" className="button secondary" onClick={onClose}>Cancel</button><button className="button primary" disabled={loading}>{loading ? <LoaderCircle size={15} className="spin" /> : <Rocket size={15} />} {loading ? "Queuing…" : "Deploy service"}</button></div></form></div>; }
function ServiceDrawer({ service, onClose, onStatus, onRedeploy, onRemove }: { service: Service; onClose: () => void; onStatus: (service: Service, status: "live" | "stopped") => void; onRedeploy: (service: Service) => void; onRemove: (service: Service) => void }) { return <div className="drawer-backdrop" onMouseDown={(event) => event.target === event.currentTarget && onClose()}><aside className="drawer"><div className="drawer-head"><div className="service-title"><div className={`service-icon ${service.runtime === "Python" ? "gold" : "blue"}`}><RuntimeIcon runtime={service.runtime} /></div><div><h2>{service.name}</h2><span>{service.description}</span></div></div><button className="icon-button" onClick={onClose}><X size={17} /></button></div><div className="drawer-status"><StatusBadge status={service.status} /><span className="branch"><GitBranch size={13} />{service.branch}</span></div><div className="drawer-url"><Globe2 size={14} /><span>{service.url}</span><button onClick={() => navigator.clipboard?.writeText(`https://${service.url}`)}><Copy size={13} /></button></div><div className="drawer-actions"><button className="button primary" onClick={() => onRedeploy(service)}><Rocket size={15} /> Redeploy</button><button className="button secondary" onClick={() => onStatus(service, service.status === "live" ? "stopped" : "live")}>{service.status === "live" ? "Stop service" : "Start service"}</button></div><div className="drawer-section"><span className="section-kicker">SERVICE DETAILS</span><DetailRow label="Runtime" value={service.runtime} icon={<Code2 size={14} />} /><DetailRow label="Region" value={service.region} icon={<Globe2 size={14} />} /><DetailRow label="Build time" value={service.buildTime} icon={<Clock3 size={14} />} /><DetailRow label="Memory" value={`${service.memory || 0} MB`} icon={<Cpu size={14} />} /><DetailRow label="Source" value={service.repoUrl.replace("https://github.com/", "") } icon={<Github size={14} />} /></div><div className="drawer-section logs-preview"><div className="panel-head"><span className="section-kicker">RUNTIME OUTPUT</span><button className="text-button">Open logs <ArrowUpRight size={13} /></button></div><pre><span className="log-muted">12:41:08</span> <span className="log-green">INFO</span> health check passed{`\n`}<span className="log-muted">12:41:12</span> <span className="log-blue">GET</span> /api/health 200 84ms{`\n`}<span className="log-muted">12:41:18</span> <span className="log-green">INFO</span> worker heartbeat ok</pre></div><button className="danger-button" onClick={() => onRemove(service)}><Trash2 size={14} /> Remove service</button></aside></div>; }
function DetailRow({ label, value, icon }: { label: string; value: string; icon: React.ReactNode }) { return <div className="detail-row"><span>{icon}{label}</span><strong>{value}</strong></div>; }

export default App;
