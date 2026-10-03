import { useCallback, useEffect, useState } from "react";
import { Download, LockKeyhole, Pause, Play, RefreshCw, RotateCcw } from "lucide-react";
import type { Service } from "./main";

type Record = { id: string; file_name: string; status: string; progress: number; downloaded_bytes: number;
  total_bytes: number; speed_bytes_per_second: number; eta_seconds: number | null; error: string;
  url: string | null; preview_url: string | null };
type State = { version: string; downloads: Record[]; bot: { running: boolean; queue_size: number;
  queue_maxsize: number; active: Record | null; controls: { paused: boolean; speed_limit_text: string } } };
const labels: { [key: string]: string } = { queued: "排队中", downloading: "下载中", paused: "已暂停",
  retrying: "重试中", verifying: "校验中", complete: "已完成", failed: "失败", interrupted: "已中断", cancelled: "已取消" };
const retryable = new Set(["failed", "interrupted", "cancelled"]);
const activeStatuses = new Set(["queued", "downloading", "paused", "retrying", "verifying"]);
function size(value: number) { return value >= 1024 ** 3 ? `${(value / 1024 ** 3).toFixed(2)} GiB` : `${(value / 1024 ** 2).toFixed(1)} MiB`; }

export function TelegramPanel({ service }: { service: Service }) {
  const [authenticated, setAuthenticated] = useState(false);
  const [password, setPassword] = useState("");
  const [state, setState] = useState<State | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [limit, setLimit] = useState("2");
  const [tab, setTab] = useState("downloads");
  const [logs, setLogs] = useState<string[]>([]);
  const api = useCallback(async (path: string, body?: unknown) => {
    const response = await fetch(`${service.path}${path}`, { cache: "no-store",
      ...(body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }) });
    const data = await response.json();
    if (response.status === 401) setAuthenticated(false);
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : `HTTP ${response.status}`);
    return data;
  }, [service.path]);
  const refresh = useCallback(async () => {
    try {
      const auth = await api("api/auth/status");
      setAuthenticated(auth.authenticated);
      if (!auth.authenticated) { setState(null); return; }
      setState(await api("api/state"));
      if (tab === "logs") setLogs((await api("api/logs")).logs.map((line: { message?: string } | string) => typeof line === "string" ? line : line.message ?? JSON.stringify(line)));
    } catch (reason) { setError(reason instanceof Error ? reason.message : "服务暂不可用"); }
  }, [api, tab]);
  useEffect(() => { refresh(); const timer = window.setInterval(refresh, 3000); return () => window.clearInterval(timer); }, [refresh]);
  async function action(path: string, body: unknown = {}) {
    setBusy(true); setError("");
    try { await api(path, body); await refresh(); } catch (reason) { setError(reason instanceof Error ? reason.message : "操作失败"); }
    finally { setBusy(false); }
  }
  const bot = state?.bot;
  const current = bot?.active;
  const failed = state?.downloads.filter(record => retryable.has(record.status)).length ?? 0;
  return <div className="page-body platform-page">
    <div className="page-intro"><div><span className="section-kicker">平台管理 / Telegram</span><h2>Telegram 下载</h2><p>发送图片、视频或文件到现有机器人，下载任务会显示在这里。</p></div><a className="secondary-button" href={service.path} target="_blank" rel="noreferrer">完整设置</a></div>
    {error && <div className="platform-alert" role="alert">{error}<button onClick={() => { setError(""); refresh(); }}>刷新</button></div>}
    {!authenticated ? <section className="card telegram-login"><LockKeyhole size={30} /><h3>登录 Telegram 控制台</h3><p>使用原来 Telegram 控制台的密码。</p><form onSubmit={async event => { event.preventDefault(); await action("api/auth/login", { password }); setPassword(""); }}><input aria-label="控制台密码" type="password" autoComplete="current-password" value={password} onChange={event => setPassword(event.target.value)} required /><button className="primary-button" disabled={busy}>登录</button></form></section> : <>
      <section className="platform-hero card"><div className="platform-hero-title"><div className="service-avatar large">TG</div><div><span className="section-kicker">连接状态</span><h3>{bot?.running ? "机器人已连接" : "机器人未连接"}</h3><p>{current?.file_name || "等待新的媒体消息"}</p></div></div><div className="platform-actions">
        <button className="primary-button" disabled={busy} onClick={() => action("api/bot/start")}><Play size={14} />启动</button>
        <button className="secondary-button" disabled={busy} onClick={() => action("api/bot/stop")}><Pause size={14} />停止</button>
        <button className="secondary-button" disabled={busy} onClick={() => action("api/bot/restart")}><RotateCcw size={14} />重启</button>
        <button className="icon-button" aria-label="刷新 Telegram" onClick={refresh}><RefreshCw size={16} /></button>
      </div></section>
      <section className="platform-metrics">{[["队列", `${bot?.queue_size ?? 0} / ${bot?.queue_maxsize ?? 100}`], ["当前下载", current ? `${current.progress.toFixed(1)}%` : "空闲"], ["下载速度", current ? `${size(current.speed_bytes_per_second)}/s` : "—"], ["失败 / 中断", String(failed)]].map(([title, value]) => <div className="metric-card platform-metric" key={title}><div className="metric-icon"><Download size={16} /></div><div><span>{title}</span><strong>{value}</strong></div></div>)}</section>
      <div className="platform-columns"><section className="card platform-main-card"><div className="platform-tabs"><button className={tab === "downloads" ? "is-active" : ""} onClick={() => setTab("downloads")}>下载历史</button><button className={tab === "logs" ? "is-active" : ""} onClick={() => setTab("logs")}>运行日志</button></div>
        {tab === "logs" ? <div className="platform-log"><pre>{logs.join("\n") || "暂无日志"}</pre></div> : <div className="record-list">{state?.downloads.map(record => <div className="record-row telegram-record" key={record.id}>
          {record.preview_url ? <a href={record.url || undefined} target="_blank" rel="noreferrer"><img className="telegram-preview" loading="lazy" src={record.preview_url} alt="媒体预览" /></a> : <div className="record-symbol"><Download size={16} /></div>}
          <div className="record-main"><strong>{record.url ? <a href={record.url} target="_blank" rel="noreferrer">{record.file_name}</a> : record.file_name}</strong><small>{record.error || `${size(record.downloaded_bytes)} / ${size(record.total_bytes)} · ${record.progress.toFixed(1)}%`}</small></div><span className={`record-status ${record.status === "complete" ? "done" : record.status}`}>{labels[record.status] || record.status}</span>
          {retryable.has(record.status) && <button className="text-button" disabled={busy} onClick={() => action(`api/downloads/${record.id}/retry`)}>重试</button>}
          {activeStatuses.has(record.status) && <button className="text-button" disabled={busy} onClick={() => action(`api/downloads/${record.id}/cancel`)}>取消</button>}
        </div>)}{!state?.downloads.length && <div className="platform-empty">暂无下载记录</div>}</div>}
      </section><aside className="platform-side"><section className="card platform-info"><span className="section-kicker">下载控制</span><h3>暂停与限速</h3><p>{bot?.controls.paused ? "下载已暂停" : "下载已恢复"} · {bot?.controls.speed_limit_text ?? "不限速"}</p><button className="secondary-button" disabled={busy} onClick={() => action(bot?.controls.paused ? "api/controls/resume" : "api/controls/pause")}>{bot?.controls.paused ? "恢复下载" : "暂停下载"}</button><form className="telegram-limit" onSubmit={event => { event.preventDefault(); action("api/controls/limit", { megabytes_per_second: Number(limit) }); }}><label>限速（MB/s）<input type="number" min="1" max="100" step="0.5" value={limit} onChange={event => setLimit(event.target.value)} /></label><button className="secondary-button" disabled={busy}>应用</button></form><button className="text-button" disabled={busy} onClick={() => action("api/controls/limit", { megabytes_per_second: null })}>取消限速</button></section>
      <section className="card platform-info"><span className="section-kicker">任务恢复</span><h3>失败任务</h3><p>共有 {failed} 条失败、中断或取消记录。</p><button className="secondary-button" disabled={busy || !failed} onClick={() => action("api/downloads/retry-failed")}>重试全部失败任务</button></section>
      {current && <section className="card platform-info"><h3>当前任务</h3><p>{current.file_name}</p><progress max="100" value={current.progress} /><p>剩余约 {current.eta_seconds === null ? "—" : `${Math.ceil(current.eta_seconds)} 秒`}</p><button className="text-button" disabled={busy} onClick={() => action(`api/downloads/${current.id}/cancel`)}>取消当前任务</button></section>}
      </aside></div>
    </>}
  </div>;
}
