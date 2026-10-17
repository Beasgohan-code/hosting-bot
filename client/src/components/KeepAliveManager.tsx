import { useState } from "react";
import {
  ArrowUpRight,
  CircleAlert,
  Clock3,
  HeartPulse,
  LoaderCircle,
  Pause,
  Play,
  Plus,
  RefreshCw,
  ShieldCheck,
  Trash2,
  X,
  XCircle,
} from "lucide-react";
import type { KeepAliveMonitor } from "../types";

type CreatePayload = { name: string; url: string; intervalMinutes: number };

type KeepAlivePageProps = {
  monitors: KeepAliveMonitor[];
  apiOnline: boolean;
  checkingId: string | null;
  onAdd: () => void;
  onCheck: (monitor: KeepAliveMonitor) => void;
  onToggle: (monitor: KeepAliveMonitor, enabled: boolean) => void;
  onRemove: (monitor: KeepAliveMonitor) => void;
};

function relativeTime(value: number | null) {
  if (!value) return "Never checked";
  const minutes = Math.max(0, Math.floor((Date.now() - value) / 60_000));
  if (minutes < 1) return "Just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

function formatCheckTime(value: number) {
  return new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" }).format(value);
}

export function KeepAlivePage({ monitors, apiOnline, checkingId, onAdd, onCheck, onToggle, onRemove }: KeepAlivePageProps) {
  const enabled = monitors.filter((monitor) => monitor.enabled);
  const up = enabled.filter((monitor) => monitor.status === "up").length;
  const down = enabled.filter((monitor) => monitor.status === "down").length;
  const pending = enabled.filter((monitor) => monitor.status === "pending").length;

  return (
    <>
      <div className="page-header animate-in">
        <div>
          <span className="section-kicker">UPTIME & KEEP-ALIVE</span>
          <h1>Keep-alive</h1>
          <p>Probe public health endpoints on a schedule and review their real response history.</p>
        </div>
        <button className="button primary" onClick={onAdd} disabled={!apiOnline}>
          <Plus size={16} /> Add monitor
        </button>
      </div>

      {!apiOnline && (
        <div className="monitor-notice">
          <CircleAlert size={16} />
          <div>
            <strong>Control-plane API is offline</strong>
            <span>Monitor data and scheduled checks are served by the persistent Node control-plane service. Reconnect it to view current readings or make changes.</span>
          </div>
        </div>
      )}

      <div className="monitor-summary animate-in">
        <div><span>MONITORS</span><strong>{monitors.length}</strong><small>{enabled.length} scheduled</small></div>
        <div className="summary-up"><span>RESPONDING</span><strong>{up}</strong><small>Healthy targets</small></div>
        <div className={down ? "summary-down" : ""}><span>FAILING</span><strong>{down}</strong><small>{pending} awaiting first check</small></div>
      </div>

      {monitors.length ? (
        <div className="monitor-grid">
          {monitors.map((monitor) => {
            const running = checkingId === monitor.id;
            const status = !monitor.enabled ? "paused" : monitor.status;
            const latestChecks = monitor.history.slice(0, 12).reverse();
            return (
              <article className={`monitor-card status-${status}`} key={monitor.id}>
                <div className="monitor-card-head">
                  <div className="monitor-title-icon"><HeartPulse size={17} /></div>
                  <div className="monitor-title-copy"><h3>{monitor.name}</h3><span>{monitor.intervalMinutes} minute interval</span></div>
                  <span className={`monitor-badge ${status}`}><i />{status}</span>
                </div>
                <a className="monitor-url" href={monitor.url} target="_blank" rel="noreferrer">
                  <span>{monitor.url}</span><ArrowUpRight size={13} />
                </a>
                <div className="monitor-stats">
                  <div><span>LAST CHECK</span><strong>{relativeTime(monitor.lastCheckedAt)}</strong></div>
                  <div><span>RESPONSE</span><strong>{monitor.lastLatencyMs === null ? "—" : `${monitor.lastLatencyMs} ms`}</strong></div>
                  <div><span>HTTP</span><strong>{monitor.lastStatusCode ?? "—"}</strong></div>
                </div>
                {monitor.lastError && monitor.enabled && monitor.status === "down" && <div className="monitor-error"><XCircle size={13} />{monitor.lastError}</div>}
                <div className="monitor-history" aria-label="Recent checks">
                  <div className="monitor-history-bars">
                    {latestChecks.length ? latestChecks.map((check) => (
                      <span key={check.id} className={check.ok ? "ok" : "fail"} title={`${formatCheckTime(check.checkedAt)} · ${check.statusCode ?? check.error ?? "No response"}`} />
                    )) : <span className="empty" />}
                  </div>
                  <span>{monitor.successCount} ok · {monitor.failureCount} failed</span>
                </div>
                <div className="monitor-actions">
                  <button className="button secondary compact" onClick={() => onCheck(monitor)} disabled={!apiOnline || !monitor.enabled || running}>
                    {running ? <LoaderCircle className="spin" size={14} /> : <RefreshCw size={14} />}{running ? "Checking" : "Check now"}
                  </button>
                  <button className="icon-button monitor-action-icon" title={monitor.enabled ? "Pause checks" : "Resume checks"} onClick={() => onToggle(monitor, !monitor.enabled)} disabled={!apiOnline}>
                    {monitor.enabled ? <Pause size={15} /> : <Play size={15} />}
                  </button>
                  <button className="icon-button monitor-action-icon danger" title="Remove monitor" onClick={() => onRemove(monitor)} disabled={!apiOnline}>
                    <Trash2 size={15} />
                  </button>
                </div>
              </article>
            );
          })}
        </div>
      ) : (
        <div className="monitor-empty animate-in">
          <div className="monitor-empty-icon"><HeartPulse size={22} /></div>
          <span className="section-kicker">NO TARGETS YET</span>
          <h2>Keep your endpoints on your radar.</h2>
          <p>Add a public health URL. Hosting Bot will check it at your chosen interval, keep recent response history, and record state changes in the activity feed.</p>
          <button className="button primary" onClick={onAdd} disabled={!apiOnline}><Plus size={15} /> Add your first monitor</button>
        </div>
      )}
    </>
  );
}

export function KeepAliveModal({ onClose, onCreate }: { onClose: () => void; onCreate: (payload: CreatePayload) => Promise<void> | void }) {
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [intervalMinutes, setIntervalMinutes] = useState(5);
  const [saving, setSaving] = useState(false);

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSaving(true);
    try {
      await onCreate({ name: name.trim(), url: url.trim(), intervalMinutes });
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="modal-backdrop" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <form className="modal" onSubmit={submit}>
        <div className="modal-head">
          <div><span className="section-kicker">UPTIME MONITOR</span><h2>Add keep-alive target</h2><p>Schedule safe HTTP checks and record actual responses over time.</p></div>
          <button type="button" className="icon-button" onClick={onClose}><X size={17} /></button>
        </div>
        <label>Display name <span className="optional">optional</span><input value={name} onChange={(event) => setName(event.target.value)} placeholder="e.g. production health" maxLength={48} /></label>
        <label>Public health URL<input required type="url" value={url} onChange={(event) => setUrl(event.target.value)} placeholder="https://your-domain.com/health" /></label>
        <label>Check interval<select value={intervalMinutes} onChange={(event) => setIntervalMinutes(Number(event.target.value))}><option value={1}>Every minute</option><option value={5}>Every 5 minutes</option><option value={10}>Every 10 minutes</option><option value={15}>Every 15 minutes</option><option value={30}>Every 30 minutes</option><option value={60}>Every hour</option></select></label>
        <div className="modal-note"><ShieldCheck size={15} /><span>Only public HTTP(S) targets are allowed. Local/private IPs, embedded credentials, and URL query strings are blocked to protect your host.</span></div>
        <div className="modal-actions"><button type="button" className="button secondary" onClick={onClose}>Cancel</button><button className="button primary" disabled={saving}>{saving ? <LoaderCircle className="spin" size={15} /> : <Clock3 size={15} />}{saving ? "Adding…" : "Start monitoring"}</button></div>
      </form>
    </div>
  );
}
