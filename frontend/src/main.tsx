import { useCallback, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Activity,
  AlertCircle,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  Clock3,
  Download,
  ExternalLink,
  Home,
  Menu,
  RefreshCw,
  Server,
  Settings2,
  TerminalSquare,
} from "lucide-react";
import "./styles.css";
import { TelegramPanel } from "./telegram-panel";
import { ServicePanel } from "./service-panel";

export type Service = {
  key: string;
  name: string;
  path: string;
  port: number;
  ready: boolean;
  running: boolean;
  next_run_at: string;
  current: string;
  extra: string;
};

type Status = {
  version: string;
  services: Service[];
  processes: { name: string; pid: number; returncode: number | null }[];
  logs: string[];
};

const serviceIcons: Record<string, string> = {
  xhs: "小红",
  x: "X",
  pixiv: "Px",
  douyin: "抖",
  telegram: "TG",
};

const navItems = [
  { key: "dashboard", label: "总览", icon: Home },
  { key: "tasks", label: "任务中心", icon: Download },
  { key: "logs", label: "运行日志", icon: TerminalSquare },
];

function formatCountdown(value: string) {
  if (!value) return "未排程";
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) return value;
  const seconds = Math.max(0, Math.floor((timestamp - Date.now()) / 1000));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (hours > 0) return `${hours}小时${minutes}分`;
  if (minutes > 0) return `${minutes}分${seconds % 60}秒`;
  return `${seconds}秒`;
}

function App() {
  const [status, setStatus] = useState<Status>({ version: "", services: [], processes: [], logs: [] });
  const [active, setActive] = useState("dashboard");
  const [collapsed, setCollapsed] = useState(false);
  const [mobileOpen, setMobileOpen] = useState(false);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [now, setNow] = useState(Date.now());

  const refresh = useCallback(async () => {
    try {
      const response = await fetch("/api/status", { cache: "no-store" });
      if (!response.ok) throw new Error("status request failed");
      setStatus(await response.json());
      setLastUpdated(new Date());
    } catch {
      // The status cards retain their last known state while a worker restarts.
    }
  }, []);

  useEffect(() => {
    refresh();
    const poll = window.setInterval(refresh, 10000);
    const ticker = window.setInterval(() => setNow(Date.now()), 1000);
    return () => {
      window.clearInterval(poll);
      window.clearInterval(ticker);
    };
  }, [refresh]);

  const readyCount = status.services.filter((service) => service.ready).length;
  const runningCount = status.services.filter((service) => service.running).length;
  const activeService = status.services.find((service) => service.key === active);
  const currentTitle = active === "dashboard" ? "运行总览" : active === "tasks" ? "任务中心" : active === "logs" ? "运行日志" : activeService?.name ?? "服务";

  const select = (key: string) => {
    setActive(key);
    setMobileOpen(false);
  };

  return (
    <div className={`app-shell ${collapsed ? "is-collapsed" : ""}`}>
      {mobileOpen && <button className="mobile-scrim" aria-label="关闭导航" onClick={() => setMobileOpen(false)} />}
      <aside className={`sidebar ${mobileOpen ? "is-open" : ""}`}>
        <div className="brand-row">
          <div className="brand-mark"><Activity size={19} /></div>
          <div className="brand-copy"><strong>NAS Download</strong><span>多源下载控制台</span></div>
          <button className="icon-button collapse-button" aria-label="折叠侧边栏" onClick={() => setCollapsed(!collapsed)}>
            {collapsed ? <ChevronRight size={18} /> : <ChevronLeft size={18} />}
          </button>
        </div>
        <div className="sidebar-caption">工作区</div>
        <nav className="main-nav" aria-label="主导航">
          {navItems.map(({ key, label, icon: Icon }) => (
            <button key={key} className={`nav-item ${active === key ? "is-active" : ""}`} onClick={() => select(key)} title={label}>
              <Icon size={18} /><span>{label}</span>
            </button>
          ))}
        </nav>
        <div className="sidebar-caption service-caption">下载服务</div>
        <nav className="main-nav" aria-label="下载服务">
          {status.services.map((service) => (
            <button key={service.key} className={`nav-item ${active === service.key ? "is-active" : ""}`} onClick={() => select(service.key)} title={service.name}>
              <span className="service-avatar">{serviceIcons[service.key] ?? service.name.slice(0, 1)}</span>
              <span className="nav-label">{service.name}</span>
              <span className={`status-dot ${service.ready ? "is-ready" : ""}`} />
            </button>
          ))}
        </nav>
        <div className="sidebar-footer">
          <button className="nav-item" onClick={() => select("settings")} title="设置"><Settings2 size={18} /><span>设置</span></button>
          <div className="version">{status.version || "v3.0.1"}</div>
        </div>
      </aside>

      <main className="main-content">
        <header className="topbar">
          <button className="icon-button mobile-menu" aria-label="打开导航" onClick={() => setMobileOpen(true)}><Menu size={21} /></button>
          <div><p className="eyebrow">NAS DOWNLOAD</p><h1>{currentTitle}</h1></div>
          <div className="topbar-actions">
            <div className="system-chip"><span className="status-dot is-ready" />{readyCount}/{status.services.length || 5} 服务在线</div>
            <button className="icon-button" aria-label="刷新状态" onClick={refresh}><RefreshCw size={18} /></button>
          </div>
        </header>

        {active === "dashboard" && <Dashboard status={status} readyCount={readyCount} runningCount={runningCount} now={now} lastUpdated={lastUpdated} select={select} />}
        {active === "tasks" && <TaskView status={status} select={select} />}
        {active === "logs" && <LogView logs={status.logs} />}
        {active === "settings" && <SettingsView />}
        {activeService && (activeService.key === "telegram" ? <TelegramPanel service={activeService} /> : <ServicePanel service={activeService} />)}
      </main>
    </div>
  );
}

function Dashboard({ status, readyCount, runningCount, now, lastUpdated, select }: { status: Status; readyCount: number; runningCount: number; now: number; lastUpdated: Date | null; select: (key: string) => void }) {
  return <div className="page-body">
    <section className="welcome-panel">
      <div><span className="section-kicker">今日概览</span><h2>所有下载服务，一眼掌握</h2><p>查看服务状态、运行中的任务和最近活动。平台配置仍由各自的服务页面负责。</p></div>
      <div className="welcome-orb"><Activity size={52} strokeWidth={1.4} /></div>
    </section>
    <section className="metric-grid" aria-label="运行指标">
      <Metric icon={<Server size={18} />} label="在线服务" value={`${readyCount}/${status.services.length || 5}`} tone="blue" />
      <Metric icon={<Download size={18} />} label="运行中的任务" value={String(runningCount)} tone="violet" />
      <Metric icon={<CheckCircle2 size={18} />} label="平台总数" value={String(status.services.length || 5)} tone="green" />
      <Metric icon={<Clock3 size={18} />} label="状态更新" value={lastUpdated ? `${Math.max(0, Math.floor((now - lastUpdated.getTime()) / 1000))} 秒前` : "等待中"} tone="orange" />
    </section>
    <div className="content-grid">
      <section className="card service-card-panel"><div className="card-heading"><div><span className="section-kicker">服务状态</span><h3>下载服务</h3></div><span className="muted">自动刷新</span></div><div className="service-list">{status.services.map((service) => <ServiceRow key={service.key} service={service} select={select} />)}</div></section>
      <section className="card activity-panel"><div className="card-heading"><div><span className="section-kicker">最近活动</span><h3>系统日志</h3></div><button className="text-button" onClick={() => select("logs")}>查看全部 <ChevronRight size={15} /></button></div><div className="activity-list">{status.logs.slice(-6).reverse().map((line, index) => <div className="activity-item" key={`${line}-${index}`}><span className="activity-dot" /><span>{line}</span></div>)}{status.logs.length === 0 && <EmptyState label="暂时没有新的系统日志" />}</div></section>
    </div>
  </div>;
}

function Metric({ icon, label, value, tone }: { icon: React.ReactNode; label: string; value: string; tone: string }) {
  return <div className={`metric-card tone-${tone}`}><div className="metric-icon">{icon}</div><div><span>{label}</span><strong>{value}</strong></div></div>;
}

function ServiceRow({ service, select }: { service: Service; select: (key: string) => void }) {
  return <button className="service-row" onClick={() => select(service.key)}><span className="service-avatar large">{serviceIcons[service.key] ?? service.name.slice(0, 1)}</span><span className="service-row-main"><strong>{service.name}</strong><small>{service.current || service.extra || "暂无运行任务"}</small></span><span className={`service-state ${service.ready ? "ready" : "starting"}`}><span className="status-dot" />{service.running ? "运行中" : service.ready ? "空闲" : "启动中"}</span><ChevronRight size={17} className="row-arrow" /></button>;
}

function TaskView({ status, select }: { status: Status; select: (key: string) => void }) {
  return <div className="page-body"><div className="page-intro"><div><span className="section-kicker">统一视图</span><h2>任务中心</h2><p>每个平台继续由自己的 Worker 管理状态，这里提供统一的运行概览和快捷入口。</p></div><button className="primary-button" onClick={() => select("dashboard")}><Home size={16} />返回总览</button></div><section className="card task-table-card task-table-wrap"><table className="task-table"><thead><tr><th>服务</th><th>状态</th><th>当前任务</th><th>补充</th><th /></tr></thead><tbody>{status.services.map((service) => <tr key={service.key}><td><span className="table-service"><span className="service-avatar">{serviceIcons[service.key] ?? service.name.slice(0, 1)}</span><strong>{service.name}</strong></span></td><td><span className={`service-state ${service.ready ? "ready" : "starting"}`}><span className="status-dot" />{service.running ? "运行中" : service.ready ? "空闲" : "启动中"}</span></td><td>{service.current || "暂无运行任务"}</td><td className="table-muted">{service.extra || "—"}</td><td><button className="text-button" onClick={() => select(service.key)}>打开 <ChevronRight size={15} /></button></td></tr>)}</tbody></table></section></div>;
}

function LogView({ logs }: { logs: string[] }) {
  return <div className="page-body"><div className="page-intro"><div><span className="section-kicker">系统活动</span><h2>运行日志</h2><p>统一服务的最近日志会自动刷新，平台详细日志仍保留在各自的服务页面。</p></div><div className="log-count">{logs.length} 条</div></div><section className="card full-log-card"><pre>{logs.length ? logs.join("\n") : "暂无日志"}</pre></section></div>;
}

function SettingsView() {
  return <div className="page-body"><div className="page-intro"><div><span className="section-kicker">系统信息</span><h2>设置</h2><p>平台 Cookie、OAuth 和下载参数继续在对应服务页面中维护。</p></div></div><section className="card settings-card"><div className="settings-row"><span className="settings-icon"><Settings2 size={18} /></span><div><strong>配置隔离</strong><p>小红书、X、Pixiv、抖音和 Telegram 继续使用各自的配置目录与状态文件。</p></div></div><div className="settings-row"><span className="settings-icon"><Server size={18} /></span><div><strong>统一服务</strong><p>统一控制台只负责汇总状态、导航和平台适配，不读取或重写其他平台的内部状态格式。</p></div></div></section></div>;
}

function EmptyState({ label }: { label: string }) { return <div className="empty-state"><AlertCircle size={17} />{label}</div>; }

export default App;

const rootElement = document.getElementById("root");
if (rootElement) createRoot(rootElement).render(<App />);
