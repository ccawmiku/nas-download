import { useCallback, useEffect, useMemo, useState } from "react";
import { AlertCircle, CheckCircle2, Clock3, ExternalLink, Pause, Play, RefreshCw, RotateCcw, TerminalSquare } from "lucide-react";
import type { Service } from "./main";

type PlatformStatus = {
  running?: boolean;
  next_run_at?: string;
  current_job?: string;
  last_run_message?: string;
  notice?: string;
  counts?: Record<string, number>;
  progress?: Record<string, unknown>;
  logs?: string[];
  xhs_api_logs?: string[];
  runs?: Record<string, unknown>[];
  notes?: Record<string, unknown>[];
  error_notes?: Record<string, unknown>[];
  tweets?: Record<string, unknown>[];
  artworks?: Record<string, unknown>[];
  last_results?: Record<string, unknown>[];
  cookie_present?: boolean;
  token_present?: boolean;
  settings_cookie?: Record<string, unknown>;
  cookie_summary?: Record<string, unknown>;
  f2_version?: Record<string, unknown>;
  db?: Record<string, unknown>;
};

const STATUS_LABELS: Record<string, string> = {
  pending: "待处理",
  running: "运行中",
  done: "已完成",
  completed: "已完成",
  success: "成功",
  failed: "失败",
  retry: "待重试",
  skipped: "已跳过",
  cancelled: "已取消",
};

function textValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") return value ? "是" : "否";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function displayDate(value: string | undefined): string {
  if (!value) return "未排程";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN");
}

export function ServicePanel({ service }: { service: Service }) {
  const [data, setData] = useState<PlatformStatus | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [tab, setTab] = useState<"records" | "logs">("records");
  const base = service.path;

  const refresh = useCallback(async () => {
    try {
      const response = await fetch(`${base}api/status`, { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      setData(await response.json());
      setError("");
    } catch (reason) {
      setError(`无法读取 ${service.name} 状态：${reason instanceof Error ? reason.message : String(reason)}`);
    }
  }, [base, service.name]);

  useEffect(() => {
    setData(null);
    setTab("records");
    refresh();
    const timer = window.setInterval(refresh, 10000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const runAction = async (path: string, label: string) => {
    setBusy(label);
    try {
      const response = await fetch(`${base}${path}`, {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: "",
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      await refresh();
    } catch (reason) {
      setError(`${label}失败：${reason instanceof Error ? reason.message : String(reason)}`);
    } finally {
      setBusy("");
    }
  };

  const records = useMemo(() => {
    if (!data) return [];
    if (service.key === "xhs") return data.notes ?? [];
    if (service.key === "x") return data.tweets ?? [];
    if (service.key === "pixiv") return data.artworks ?? [];
    return data.last_results ?? [];
  }, [data, service.key]);

  const logs = data?.logs ?? [];
  const pending = Number(data?.counts?.pending ?? 0);
  const failed = Number(data?.counts?.failed ?? 0);
  const retry = Number(data?.counts?.retry ?? 0);
  const credentialState = service.key === "pixiv" ? data?.token_present : service.key === "xhs" ? data?.settings_cookie?.present : data?.cookie_present;

  return <div className="page-body platform-page">
    <div className="page-intro">
      <div><span className="section-kicker">平台管理 / {service.name}</span><h2>{service.name} 下载</h2><p>查看运行状态与最近任务。详细配置仍由独立 Worker 页面处理。</p></div>
      <a className="secondary-button" href={base} target="_blank" rel="noreferrer">完整设置 <ExternalLink size={14} /></a>
    </div>

    {error && <div className="platform-alert"><AlertCircle size={16} />{error}<button onClick={refresh}>重试</button></div>}
    <section className="platform-hero card">
      <div className="platform-hero-title"><div className={`platform-indicator ${service.ready ? "online" : "offline"}`}><span className="status-dot" /></div><div><span className="section-kicker">服务概况</span><h3>{service.name} Worker</h3><p>{data?.last_run_message || data?.notice || (service.ready ? "服务在线，等待任务" : "服务正在启动")}</p></div></div>
      <div className="platform-actions">
        <button className="primary-button" disabled={!!busy || !service.ready} onClick={() => runAction("run", "立即运行")}><Play size={15} />立即运行</button>
        {service.key === "douyin" && <button className="secondary-button" disabled={!!busy} onClick={() => runAction("stop", "停止任务")}><Pause size={15} />停止</button>}
        <button className="secondary-button" disabled={!!busy} onClick={() => runAction("reload", "重新读取配置")}><RotateCcw size={15} />重新读取配置</button>
        <button className="icon-button" aria-label="刷新平台状态" onClick={refresh}><RefreshCw size={17} /></button>
      </div>
    </section>

    <section className="platform-metrics">
      <PlatformMetric label="运行状态" value={data?.running ? "运行中" : service.ready ? "空闲" : "启动中"} icon={<CheckCircle2 size={18} />} />
      <PlatformMetric label="待处理" value={String(pending)} icon={<Clock3 size={18} />} />
      <PlatformMetric label="待重试 / 失败" value={`${retry} / ${failed}`} icon={<AlertCircle size={18} />} />
      <PlatformMetric label={service.key === "pixiv" ? "授权状态" : "Cookie 状态"} value={credentialState === undefined ? "请到完整设置查看" : credentialState ? "已配置" : "未配置"} icon={<CheckCircle2 size={18} />} />
    </section>

    <div className="platform-columns">
      <section className="card platform-main-card">
        <div className="platform-tabs"><button className={tab === "records" ? "is-active" : ""} onClick={() => setTab("records")}>最近任务</button><button className={tab === "logs" ? "is-active" : ""} onClick={() => setTab("logs")}>运行日志</button></div>
        {tab === "records" && <RecordList records={records} />}
        {tab === "logs" && <div className="platform-log"><pre>{logs.length ? logs.slice(-1000).join("\n") : "暂无日志"}</pre></div>}
      </section>
      <aside className="platform-side">
        <section className="card platform-info"><span className="section-kicker">当前运行</span><h3>任务详情</h3><div className="info-line"><span>当前任务</span><strong>{data?.current_job || textValue(data?.progress?.current_url)}</strong></div><div className="info-line"><span>下次运行</span><strong>{displayDate(data?.next_run_at)}</strong></div><div className="info-line"><span>最近记录</span><strong>{records.length} 条</strong></div></section>
        {service.key === "xhs" && <section className="card platform-info"><span className="section-kicker">小红书队列</span><h3>失败与重试</h3><p>失败记录 {data?.error_notes?.length ?? 0} 条。重试和队列清理可以在完整设置中操作。</p><button className="secondary-button" disabled={!!busy} onClick={() => runAction("api/retry-errors", "重试失败任务")}>重试全部失败任务</button></section>}
        {service.key === "douyin" && <section className="card platform-info"><span className="section-kicker">依赖版本</span><h3>f2 状态</h3><div className="info-line"><span>当前</span><strong>{textValue(data?.f2_version?.installed)}</strong></div><div className="info-line"><span>最新</span><strong>{textValue(data?.f2_version?.latest)}</strong></div><button className="secondary-button" disabled={!!busy} onClick={() => runAction("check-version", "检查 f2 版本")}>检查版本</button></section>}
        <section className="card platform-info"><span className="section-kicker">平台边界</span><h3>独立维护</h3><p>此页面只通过公开状态接口读取数据。下载器配置与状态文件仍保留在对应平台目录中。</p><a className="text-button" href={base} target="_blank" rel="noreferrer">打开完整管理页面 <ExternalLink size={14} /></a></section>
      </aside>
    </div>
  </div>;
}

function PlatformMetric({ label, value, icon }: { label: string; value: string; icon: React.ReactNode }) {
  return <div className="metric-card platform-metric"><div className="metric-icon">{icon}</div><div><span>{label}</span><strong>{value}</strong></div></div>;
}

function RecordList({ records }: { records: Record<string, unknown>[] }) {
  if (!records.length) return <div className="platform-empty"><TerminalSquare size={24} /><strong>暂无最近任务</strong><span>任务启动后会在这里显示最新记录。</span></div>;
  return <div className="record-list">{records.slice(0, 1000).map((record, index) => {
    const title = textValue(record.title ?? record.name ?? record.url ?? record.note_id ?? record.tweet_id ?? record.artwork_id ?? record.aweme_id ?? record.job ?? `任务 ${index + 1}`);
    const status = String(record.status ?? record.result ?? "");
    const detail = textValue(record.last_error ?? record.error ?? record.updated_at ?? record.downloaded_at ?? record.created_at ?? "");
    return <div className="record-row" key={`${title}-${index}`}><div className="record-symbol"><TerminalSquare size={16} /></div><div className="record-main"><strong title={title}>{title}</strong><small title={detail}>{detail === "—" ? "暂无补充信息" : detail}</small></div><span className={`record-status ${status.toLowerCase()}`}>{STATUS_LABELS[status.toLowerCase()] ?? status ?? "—"}</span></div>;
  })}</div>;
}
