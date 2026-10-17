import { useEffect, useRef, useState, type ReactNode } from "react";
import * as THREE from "three";
import { animate, stagger } from "animejs";
import {
  Activity,
  ArrowUpRight,
  Check,
  Clock3,
  Cpu,
  HeartPulse,
  LayoutDashboard,
  Menu,
  Plus,
  RefreshCw,
  Server,
  ShieldCheck,
} from "lucide-react";
import { KeepAliveModal, KeepAlivePage } from "./components/KeepAliveManager";
import type { ActivityItem, KeepAliveMonitor, Metrics } from "./types";
import "./styles.css";

const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? "").trim().replace(/\/+$/, "");
const apiPath = (route: string) => `${API_BASE_URL}${route}`;
type ApiStatus = "connecting" | "online" | "offline";
type OverviewPayload = { activities: ActivityItem[]; keepAlives: KeepAliveMonitor[]; metrics: Metrics };

function App() {
  const [activities, setActivities] = useState<ActivityItem[]>([]);
  const [keepAlives, setKeepAlives] = useState<KeepAliveMonitor[]>([]);
  const [metrics, setMetrics] = useState<Metrics | null>(null);
  const [activeSection, setActiveSection] = useState("Overview");
  const [showKeepAlive, setShowKeepAlive] = useState(false);
  const [notice, setNotice] = useState("");
  const [apiStatus, setApiStatus] = useState<ApiStatus>("connecting");
  const [streamConnected, setStreamConnected] = useState(false);
  const [checkingMonitor, setCheckingMonitor] = useState<string | null>(null);
  const [lastUpdatedAt, setLastUpdatedAt] = useState<number | null>(null);
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
  const noticeTimer = useRef<number | null>(null);

  function applyOverview(data: OverviewPayload) {
    setActivities(Array.isArray(data.activities) ? data.activities : []);
    setKeepAlives(Array.isArray(data.keepAlives) ? data.keepAlives : []);
    setMetrics(data.metrics);
    setApiStatus("online");
    setLastUpdatedAt(Date.now());
  }

  async function refresh(): Promise<boolean> {
    try {
      const response = await fetch(apiPath("/api/overview"), { cache: "no-store" });
      if (!response.ok) throw new Error("Control-plane API unavailable");
      const data = await response.json() as OverviewPayload;
      applyOverview(data);
      return true;
    } catch {
      setApiStatus("offline");
      return false;
    }
  }

  useEffect(() => {
    let disposed = false;
    let source: EventSource | null = null;

    async function refreshAndConnect() {
      const online = await refresh();
      if (!online || disposed || source) return;
      source = new EventSource(apiPath("/api/events"));
      source.addEventListener("overview", (event) => {
        try {
          applyOverview(JSON.parse((event as MessageEvent).data) as OverviewPayload);
          setStreamConnected(true);
        } catch {
          setStreamConnected(false);
        }
      });
      source.onopen = () => setStreamConnected(true);
      source.onerror = () => setStreamConnected(false);
    }

    void refreshAndConnect();
    const timer = window.setInterval(() => void refreshAndConnect(), 15_000);
    return () => {
      disposed = true;
      window.clearInterval(timer);
      source?.close();
      if (noticeTimer.current !== null) window.clearTimeout(noticeTimer.current);
    };
  }, []);

  useEffect(() => {
    animate(".animate-in", { opacity: [0, 1], translateY: [12, 0], delay: stagger(55), duration: 520, ease: "outExpo" });
  }, [activeSection]);

  const apiOnline = apiStatus === "online";
  const failingMonitors = keepAlives.filter((monitor) => monitor.enabled && monitor.status === "down").length;
  const systemStatus = apiStatus === "connecting"
    ? "Connecting to control plane"
    : apiStatus === "offline"
      ? "Control plane offline"
      : failingMonitors > 0
        ? `${failingMonitors} target${failingMonitors === 1 ? "" : "s"} failing`
        : streamConnected ? "Live telemetry connected" : "API online · polling";

  function showNotice(message: string) {
    setNotice(message);
    if (noticeTimer.current !== null) window.clearTimeout(noticeTimer.current);
    noticeTimer.current = window.setTimeout(() => setNotice(""), 3000);
  }

  async function createKeepAlive(payload: { name: string; url: string; intervalMinutes: number }) {
    if (!apiOnline) {
      showNotice("The control-plane API is offline. No monitor was created.");
      return;
    }
    try {
      const response = await fetch(apiPath("/api/keepalives"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...payload, name: payload.name.trim() || undefined }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "Unable to create monitor");
      setShowKeepAlive(false);
      showNotice(`Monitoring ${result.name}`);
      await refresh();
    } catch (error) {
      showNotice(error instanceof Error ? error.message : "Could not add monitor");
    }
  }

  async function checkKeepAlive(monitor: KeepAliveMonitor) {
    if (!apiOnline) return showNotice("The control-plane API is offline. No check was run.");
    setCheckingMonitor(monitor.id);
    try {
      const response = await fetch(apiPath(`/api/keepalives/${monitor.id}/check`), { method: "POST" });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "Check failed");
      await refresh();
      showNotice(result.status === "up" ? `${monitor.name} is responding` : `${monitor.name} did not respond`);
    } catch (error) {
      showNotice(error instanceof Error ? error.message : "Check failed");
    } finally {
      setCheckingMonitor(null);
    }
  }

  async function toggleKeepAlive(monitor: KeepAliveMonitor, enabled: boolean) {
    if (!apiOnline) return showNotice("The control-plane API is offline. No setting was changed.");
    try {
      const response = await fetch(apiPath(`/api/keepalives/${monitor.id}`), {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "Unable to update monitor");
      await refresh();
      showNotice(`${monitor.name} ${enabled ? "resumed" : "paused"}`);
    } catch (error) {
      showNotice(error instanceof Error ? error.message : "Unable to update monitor");
    }
  }

  async function removeKeepAlive(monitor: KeepAliveMonitor) {
    if (!apiOnline) return showNotice("The control-plane API is offline. No monitor was removed.");
    try {
      const response = await fetch(apiPath(`/api/keepalives/${monitor.id}`), { method: "DELETE" });
      if (!response.ok) throw new Error("Unable to remove monitor");
      await refresh();
      showNotice(`${monitor.name} removed`);
    } catch (error) {
      showNotice(error instanceof Error ? error.message : "Unable to remove monitor");
    }
  }

  function navigate(section: string) {
    setActiveSection(section);
    setMobileMenuOpen(false);
  }

  return (
    <div className="app-shell">
      <AmbientScene />
      <aside className={`sidebar ${mobileMenuOpen ? "open" : ""}`}>
        <div className="brand"><div className="brand-mark"><span>H</span></div><div><strong>hosting<span className="accent">.</span>bot</strong><small>CONTROL PLANE</small></div></div>
        <div className="workspace"><div className="workspace-icon"><Server size={14} /></div><div><span>INSTANCE</span><strong>Hosting Bot</strong></div></div>
        <nav className="nav-list">
          <span className="nav-label">MONITORING</span>
          <NavItem icon={<LayoutDashboard size={16} />} label="Overview" active={activeSection === "Overview"} onClick={() => navigate("Overview")} />
          <NavItem icon={<HeartPulse size={16} />} label="Keep-alive" count={keepAlives.length} active={activeSection === "Keep-alive"} onClick={() => navigate("Keep-alive")} />
          <NavItem icon={<Activity size={16} />} label="Activity" active={activeSection === "Activity"} onClick={() => navigate("Activity")} />
        </nav>
        <div className="sidebar-bottom">
          <div className="instance-card">
            <span className="section-kicker">CONTROL PLANE</span>
            <strong className={apiOnline ? "online" : "offline"}>{apiStatus === "connecting" ? "Connecting" : apiOnline ? "Connected" : "Offline"}</strong>
            <small>{metrics ? `Process uptime ${formatUptime(metrics.uptimeSeconds)}` : "Waiting for API telemetry"}</small>
          </div>
        </div>
      </aside>

      <main className="main-content">
        <header className="topbar">
          <button className="mobile-menu" aria-label="Toggle navigation" onClick={() => setMobileMenuOpen((open) => !open)}><Menu size={18} /></button>
          <div className="breadcrumb"><span>Hosting Bot</span><span>/</span><strong>{activeSection}</strong></div>
          <div className="top-actions">
            <div className={`system-pill ${apiOnline ? "online" : "offline"}`}><span className="pulse" /> {systemStatus}</div>
            <button className="icon-button" onClick={() => void refresh()} title="Refresh live data" aria-label="Refresh live data"><RefreshCw size={16} /></button>
          </div>
        </header>

        <div className="content-wrap">
          {activeSection === "Overview" && (
            <Overview
              metrics={metrics}
              monitors={keepAlives}
              activities={activities}
              apiStatus={apiStatus}
              streamConnected={streamConnected}
              lastUpdatedAt={lastUpdatedAt}
              onAddMonitor={() => setShowKeepAlive(true)}
              onNavigate={navigate}
            />
          )}
          {activeSection === "Keep-alive" && (
            <KeepAlivePage
              monitors={keepAlives}
              apiOnline={apiOnline}
              onAdd={() => setShowKeepAlive(true)}
              onCheck={checkKeepAlive}
              onToggle={toggleKeepAlive}
              onRemove={removeKeepAlive}
              checkingId={checkingMonitor}
            />
          )}
          {activeSection === "Activity" && <ActivityPage activities={activities} />}
        </div>
      </main>
      {showKeepAlive && <KeepAliveModal onClose={() => setShowKeepAlive(false)} onCreate={createKeepAlive} />}
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
    for (let index = 0; index < positions.length; index += 3) {
      positions[index] = (Math.random() - 0.5) * 11;
      positions[index + 1] = (Math.random() - 0.5) * 7;
      positions[index + 2] = (Math.random() - 0.5) * 5;
    }
    geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
    const points = new THREE.Points(geometry, new THREE.PointsMaterial({ color: 0x9ac9ff, size: 0.012, transparent: true, opacity: 0.5 }));
    scene.add(points);
    const resize = () => {
      if (!ref.current) return;
      const { width, height } = ref.current.getBoundingClientRect();
      renderer.setSize(width, height, false);
      camera.aspect = width / height;
      camera.updateProjectionMatrix();
    };
    resize();
    window.addEventListener("resize", resize);
    let frame = 0;
    const animateScene = () => {
      frame = requestAnimationFrame(animateScene);
      points.rotation.y += 0.00035;
      points.rotation.x = Math.sin(Date.now() * 0.00012) * 0.06;
      renderer.render(scene, camera);
    };
    animateScene();
    return () => {
      cancelAnimationFrame(frame);
      window.removeEventListener("resize", resize);
      geometry.dispose();
      points.material.dispose();
      renderer.dispose();
      renderer.domElement.remove();
    };
  }, []);
  return <div ref={ref} className="ambient-scene" aria-hidden="true" />;
}

function NavItem({ icon, label, count, active, onClick }: { icon: ReactNode; label: string; count?: number; active: boolean; onClick: () => void }) {
  return <button className={`nav-item ${active ? "active" : ""}`} onClick={onClick}>{icon}<span>{label}</span>{count !== undefined && <em>{count}</em>}</button>;
}

function formatUptime(seconds: number) {
  const days = Math.floor(seconds / 86_400);
  const hours = Math.floor((seconds % 86_400) / 3_600);
  const minutes = Math.floor((seconds % 3_600) / 60);
  if (days) return `${days}d ${hours}h`;
  if (hours) return `${hours}h ${minutes}m`;
  if (minutes) return `${minutes}m`;
  return `${seconds}s`;
}

function timeAgo(value: number | string) {
  const delta = Math.max(0, Date.now() - new Date(value).getTime());
  const minutes = Math.floor(delta / 60_000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

function Overview({ metrics, monitors, activities, apiStatus, streamConnected, lastUpdatedAt, onAddMonitor, onNavigate }: {
  metrics: Metrics | null;
  monitors: KeepAliveMonitor[];
  activities: ActivityItem[];
  apiStatus: ApiStatus;
  streamConnected: boolean;
  lastUpdatedAt: number | null;
  onAddMonitor: () => void;
  onNavigate: (section: string) => void;
}) {
  const activeMonitors = monitors.filter((monitor) => monitor.enabled);
  return (
    <>
      <div className="hero animate-in">
        <div>
          <div className="eyebrow"><HeartPulse size={13} /> LIVE OPERATIONS</div>
          <h1>Real data.<br /><span>No guesswork.</span></h1>
          <p>Control-plane telemetry and verified HTTP checks from your configured endpoints.</p>
          <div className="overview-sync">{lastUpdatedAt ? `Last synchronized ${new Date(lastUpdatedAt).toLocaleTimeString()}` : "Waiting for the control-plane API"}</div>
        </div>
        <div className="hero-actions">
          <button className="button primary" onClick={onAddMonitor} disabled={apiStatus !== "online"}><Plus size={16} /> Add monitor</button>
        </div>
      </div>

      {apiStatus === "offline" && (
        <div className="monitor-notice">
          <ShieldCheck size={16} />
          <div><strong>Live API data is unavailable</strong><span>No sample or cached demo records are displayed. Start the persistent control-plane service to load real measurements and manage monitors.</span></div>
        </div>
      )}

      <div className="runner-notice animate-in">
        <ShieldCheck size={15} />
        <span><strong>Repository deployments are not enabled.</strong> A restricted deployment runner is not configured, so this dashboard does not report simulated builds or service states.</span>
      </div>

      <div className="metric-grid animate-in">
        <MetricCard icon={<HeartPulse />} label="Healthy targets" value={metrics ? `${metrics.healthyMonitors} / ${metrics.activeMonitors}` : "—"} detail={metrics ? `${metrics.totalMonitors} configured monitor${metrics.totalMonitors === 1 ? "" : "s"}` : "Waiting for live telemetry"} tone="lime" />
        <MetricCard icon={<Activity />} label="Probe success rate" value={!metrics || metrics.probeSuccessRate === null ? "—" : `${metrics.probeSuccessRate.toFixed(2)}%`} detail={metrics ? `${metrics.totalChecks.toLocaleString()} checks recorded` : "Waiting for live telemetry"} tone="blue" />
        <MetricCard icon={<Cpu />} label="Control-plane RSS" value={metrics ? `${metrics.processMemoryMb} MB` : "—"} detail="Current Node.js process" tone="violet" />
        <MetricCard icon={<Clock3 />} label="API process uptime" value={metrics ? formatUptime(metrics.uptimeSeconds) : "—"} detail={metrics ? `${metrics.apiRequests.toLocaleString()} requests since start · SSE excluded` : "Waiting for live telemetry"} tone="orange" />
      </div>

      <div className="section-head animate-in">
        <div><div className="section-kicker"><span className="live-pulse" /> MONITORED TARGETS</div><h2>Keep-alive checks <span>{monitors.length}</span></h2></div>
        <button className="text-button" onClick={() => onNavigate("Keep-alive")}>Manage monitors <ArrowUpRight size={13} /></button>
      </div>
      {activeMonitors.length ? (
        <div className="monitor-overview-grid">
          {activeMonitors.slice(0, 4).map((monitor) => <MonitorOverviewCard key={monitor.id} monitor={monitor} />)}
        </div>
      ) : (
        <div className="empty-state"><HeartPulse size={23} /><h3>{monitors.length ? "No active monitors" : "No monitored targets yet"}</h3><p>{apiStatus === "online" ? "Add a public health URL to start collecting real response status, latency, and history." : "Connect the control-plane API to view and manage endpoint monitors."}</p><button className="button primary compact" onClick={onAddMonitor} disabled={apiStatus !== "online"}><Plus size={14} /> Add monitor</button></div>
      )}

      <div className="lower-grid animate-in">
        <div className="panel activity-panel">
          <div className="panel-head"><div><span className="section-kicker">AUDIT EVENTS</span><h3>Recent activity</h3></div><button className="text-button" onClick={() => onNavigate("Activity")}>View all <ArrowUpRight size={13} /></button></div>
          {activities.length ? activities.slice(0, 5).map((item) => <ActivityRow key={item.id} item={item} />) : <div className="activity-empty">No monitor events have been recorded.</div>}
        </div>
        <div className="panel quick-panel">
          <div className="panel-head"><div><span className="section-kicker">QUICK ACCESS</span><h3>Monitoring</h3></div><Activity size={17} className="muted-icon" /></div>
          <div className="quick-links">
            <button onClick={onAddMonitor} disabled={apiStatus !== "online"}><div className="quick-icon cyan"><Plus size={16} /></div><div><strong>Add a health check</strong><span>Register a public HTTP endpoint</span></div><ArrowUpRight size={14} /></button>
            <button onClick={() => onNavigate("Keep-alive")}><div className="quick-icon purple"><HeartPulse size={16} /></div><div><strong>Manage monitors</strong><span>Inspect checks and response history</span></div><ArrowUpRight size={14} /></button>
            <button onClick={() => onNavigate("Activity")}><div className="quick-icon orange"><Clock3 size={16} /></div><div><strong>Review audit events</strong><span>See recorded monitor changes</span></div><ArrowUpRight size={14} /></button>
          </div>
        </div>
      </div>
      <div className="telemetry-footnote">Telemetry stream: {streamConnected ? "connected" : "polling / reconnecting"} · API data refreshes every 15 seconds</div>
    </>
  );
}

function MetricCard({ icon, label, value, detail, tone }: { icon: ReactNode; label: string; value: string; detail: string; tone: string }) {
  return <div className={`metric-card ${tone}`}><div className="metric-icon">{icon}</div><div className="metric-label">{label}</div><strong>{value}</strong><span>{detail}</span></div>;
}

function MonitorOverviewCard({ monitor }: { monitor: KeepAliveMonitor }) {
  const status = monitor.enabled ? monitor.status : "paused";
  return (
    <article className={`monitor-card status-${status}`}>
      <div className="monitor-card-head"><div className="monitor-title-icon"><HeartPulse size={17} /></div><div className="monitor-title-copy"><h3>{monitor.name}</h3><span>{monitor.intervalMinutes} minute interval</span></div><span className={`monitor-badge ${status}`}><i />{status}</span></div>
      <a className="monitor-url" href={monitor.url} target="_blank" rel="noreferrer"><span>{monitor.url}</span><ArrowUpRight size={13} /></a>
      <div className="monitor-stats"><div><span>LAST CHECK</span><strong>{monitor.lastCheckedAt ? timeAgo(monitor.lastCheckedAt) : "Not checked"}</strong></div><div><span>RESPONSE</span><strong>{monitor.lastLatencyMs === null ? "—" : `${monitor.lastLatencyMs} ms`}</strong></div><div><span>HTTP</span><strong>{monitor.lastStatusCode ?? "—"}</strong></div></div>
    </article>
  );
}

function ActivityRow({ item }: { item: ActivityItem }) {
  return <div className="activity-row"><div className={`activity-icon ${item.tone}`}><span>{item.tone === "success" ? "✓" : item.tone === "warning" ? "!" : "i"}</span></div><div className="activity-copy"><strong>{item.action}</strong><span><b>{item.targetName}</b> · {item.detail}</span></div><time>{timeAgo(item.createdAt)}</time></div>;
}

function ActivityPage({ activities }: { activities: ActivityItem[] }) {
  return <>
    <PageHeader eyebrow="RECORDED MONITOR EVENTS" title="Activity" description="Events generated by actual monitor checks and configuration changes." />
    <div className="panel activity-full">
      {activities.length ? activities.map((item) => <ActivityRow key={item.id} item={item} />) : <div className="activity-empty">No events have been recorded yet.</div>}
    </div>
  </>;
}

function PageHeader({ eyebrow, title, description }: { eyebrow: string; title: string; description: string }) {
  return <div className="page-header animate-in"><div><span className="section-kicker">{eyebrow}</span><h1>{title}</h1><p>{description}</p></div></div>;
}

export default App;
