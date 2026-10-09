import { useEffect, useState, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import {
  House,
  Download,
  Layers3,
  Images,
  SlidersHorizontal,
  ScrollText,
  Search,
  Plus,
  ArrowUpRight,
  ArrowRight,
  RefreshCw,
  ChevronLeft,
  ChevronRight,
  X as Close,
  Menu,
  Check,
  TriangleAlert,
  ShieldCheck,
  Cpu,
  FolderOpen,
  Pause,
  Play,
  RotateCcw,
  LogOut,
} from "lucide-react";
import { definitions, hidden, type Spec } from "./fields";
import "./styles.css";

const names: Record<string, string> = {
  telegram: "Telegram",
  xhs: "小红书",
  x: "X",
  pixiv: "Pixiv",
  douyin: "抖音",
};
const initials: Record<string, string> = {
  telegram: "TG",
  xhs: "红",
  x: "X",
  pixiv: "Px",
  douyin: "抖",
};
const stateNames: Record<string, string> = {
  queued: "排队中",
  running: "运行中",
  processing: "处理中",
  publishing: "正在入库",
  complete: "已完成",
  failed: "失败",
  interrupted: "已中断",
  limited: "达到时长上限",
  cancelled: "已取消",
  deleted: "已删除",
  downloading: "下载中",
  retrying: "重试中",
  paused: "下载已暂停",
  verifying: "校验中",
};
const stopNames: Record<string, string> = {
  known_limit: "连续已下载达到阈值",
  checkpoint: "到达上次同步边界",
  marker: "到达指定停止标记",
  end: "已到列表末尾",
  manual: "链接处理完成",
  timeout: "达到分钟上限，本次未完成",
  cancelled: "用户停止",
  baseline: "已建立队列基线",
};
const connectionNames: Record<string, string> = {
  online: "已连接",
  connecting: "连接中",
  retry_wait: "等待重试",
  waiting_config: "等待配置",
  credential_error: "凭证异常",
};
function stopText(value: any) {
  if (!value?.stop_reason) return "";
  return `${stopNames[value.stop_reason] || value.stop_reason}${value.stop_reason === "known_limit" ? ` · ${value.known_consecutive}/${value.known_threshold}` : ""}`;
}
const kinds: Record<string, string> = {
  sync: "增量同步",
  links: "链接下载",
  retry: "重试失败下载",
  test: "连接测试",
  "oauth-start": "Pixiv 登录授权",
  "oauth-finish": "保存 Pixiv 授权",
};
const nav = [
  ["home", "总览", House],
  ["downloads", "下载", Download],
  ["workspace", "工作区", Layers3],
  ["records", "媒体记录", Images],
  ["settings", "平台与设置", SlidersHorizontal],
  ["logs", "日志", ScrollText],
] as const;
const stamp = (v: number | string) =>
  !v
    ? "—"
    : new Date(typeof v === "number" ? v * 1000 : v).toLocaleString("zh-CN", {
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
      });
const bytes = (n: number) =>
  !n
    ? "—"
    : n >= 1024 ** 3
      ? (n / 1024 ** 3).toFixed(2) + " GB"
      : n >= 1024 ** 2
        ? (n / 1024 ** 2).toFixed(1) + " MB"
        : (n / 1024).toFixed(1) + " KB";
function refresh() {
  window.dispatchEvent(new Event("nas-refresh"));
}
async function api(
  path: string,
  body?: unknown,
  method = "POST",
): Promise<any> {
  const response = await fetch(path, {
    cache: "no-store",
    ...(body === undefined
      ? {}
      : {
          method,
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }),
  });
  const data = await response.json().catch(() => ({}));
  if (response.status === 401)
    window.dispatchEvent(new Event("nas-unauthorized"));
  if (!response.ok)
    throw new Error(
      typeof data.detail === "string"
        ? data.detail
        : response.status === 401
          ? "请登录"
          : `请求失败（${response.status}）`,
    );
  return data;
}
function useData(path: string | null, interval = 5000) {
  const [value, setValue] = useState<any>(null),
    [error, setError] = useState("");
  useEffect(() => {
    if (!path) {
      setValue(null);
      return;
    }
    let alive = true;
    const load = () =>
      api(path)
        .then((v) => {
          if (alive) {
            setValue(v);
            setError("");
          }
        })
        .catch((e) => {
          if (alive) setError(e.message);
        });
    setValue(null);
    load();
    const timer = window.setInterval(load, interval);
    window.addEventListener("nas-refresh", load);
    return () => {
      alive = false;
      clearInterval(timer);
      window.removeEventListener("nas-refresh", load);
    };
  }, [path, interval]);
  return { value, error };
}
function Badge({ state }: { state: string }) {
  return (
    <span className={`badge ${state}`}>
      <i />
      {stateNames[state] || state}
    </span>
  );
}
function Mark({ platform }: { platform: string }) {
  return (
    <span className={`platform-mark ${platform}`}>
      {initials[platform] || "N"}
    </span>
  );
}
function Empty({
  title = "暂时没有记录",
  text = "新的任务开始后会显示在这里。",
}: {
  title?: string;
  text?: string;
}) {
  return (
    <div className="empty">
      <FolderOpen size={28} />
      <strong>{title}</strong>
      <p>{text}</p>
    </div>
  );
}
function Panel({
  title,
  extra,
  children,
  className = "",
}: {
  title: string;
  extra?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`panel ${className}`}>
      <div className="panel-heading">
        <h2>{title}</h2>
        {extra}
      </div>
      {children}
    </section>
  );
}
function Switch({
  checked,
  onChange,
  label,
  hint,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label: string;
  hint?: string;
}) {
  return (
    <label className="switch-row">
      <span>
        <strong>{label}</strong>
        {hint && <small>{hint}</small>}
      </span>
      <input
        type="checkbox"
        role="switch"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
      />
      <span className="switch-track" />
    </label>
  );
}
type Act = (path: string, body?: unknown, method?: string) => Promise<any>;
type Open = (kind: string, row: any) => void;

function Home({
  act,
  go,
  open,
}: {
  act: Act;
  go: (s: string) => void;
  open: Open;
}) {
  const { value: data, error } = useData("/api/overview"),
    workspace = useData("/api/workspace");
  if (!data)
    return <div className="loading">{error || "正在读取 NAS 状态…"}</div>;
  const task = data.counts.tasks,
    assets = data.counts.assets;
  const pending = (task.queued || 0) + (task.running || 0),
    working =
      (assets.queued || 0) +
      (assets.processing || 0) +
      (assets.publishing || 0),
    attention =
      (task.failed || 0) +
      (task.interrupted || 0) +
      (task.limited || 0) +
      (assets.failed || 0) +
      (assets.interrupted || 0);
  return (
    <>
      {error && <div className="notice warning">{error}</div>}
      <div className="overview-intro">
        <div>
          <span className="eyebrow">YOUR MEDIA, IN ORDER</span>
          <h1>下载有进度，归档有结果。</h1>
          <p>五个平台，一个队列视图。新媒体在工作区处理后自动入库。</p>
        </div>
        <div className="system-pill">
          <Cpu size={17} />
          <span>
            Intel N95 · HEVC 硬编
            <small>
              {workspace.value?.available
                ? "工作区可用 · 单任务半速处理"
                : "工作区未连接 · 新下载直接入库"}
            </small>
          </span>
        </div>
      </div>
      <div className="metrics">
        {[
          [Download, "下载队列", pending, "待运行与正在运行"],
          [Layers3, "工作区", working, "等待转换与正在入库"],
          [Check, "已入库", assets.complete || 0, "工作区处理完成"],
          [TriangleAlert, "需要处理", attention, "失败、中断或时长上限"],
        ].map(([Icon, title, n, sub]: any) => (
          <div className="metric" key={title}>
            <span className="metric-label">
              <Icon size={16} />
              {title}
            </span>
            <strong>{n}</strong>
            <small>{sub}</small>
          </div>
        ))}
      </div>
      <Panel
        title="平台状态"
        extra={
          <button className="link-button" onClick={() => go("settings")}>
            管理平台 <ArrowRight size={14} />
          </button>
        }
      >
        <div className="platform-grid">
          {data.platforms.map((p: any) => {
            const online = data.workers.some(
              (w: any) =>
                w.roles.includes(p.key) && Date.now() / 1000 - w.updated < 60,
            );
            return (
              <div className="platform-tile" key={p.key}>
                <div className="platform-top">
                  <Mark platform={p.key} />
                  <div>
                    <h3>{p.name}</h3>
                    <span className={`connection ${online ? "online" : ""}`}>
                      {p.key === "telegram"
                        ? data.workers.some(
                            (w: any) =>
                              w.roles.includes("telegram") &&
                              w.details.running &&
                              Date.now() / 1000 - w.updated < 60,
                          )
                          ? "机器人已连接"
                          : "机器人尚未连接"
                        : online
                          ? "执行服务在线"
                          : "执行服务未连接"}
                    </span>
                  </div>
                  <button
                    aria-label={`打开${p.name}设置`}
                    className="icon-button"
                    onClick={() => go("settings:" + p.key)}
                  >
                    <ArrowUpRight size={17} />
                  </button>
                </div>
                <div className="tile-info">
                  <span>{p.workspace ? "经过工作区" : "直接归档"}</span>
                  <span>
                    {p.max_minutes
                      ? `上限 ${p.max_minutes} 分钟`
                      : "按队列处理"}
                  </span>
                </div>
                <div className="tile-bottom">
                  {p.latest ? (
                    <Badge state={p.latest.state} />
                  ) : (
                    <span className="muted">等待新内容</span>
                  )}
                  <button
                    className="link-button"
                    onClick={() =>
                      p.key === "telegram"
                        ? go("downloads:telegram")
                        : act("/api/tasks", { platform: p.key })
                    }
                  >
                    {p.key === "xhs"
                      ? "处理链接队列"
                      : p.key === "telegram"
                        ? "查看下载"
                        : "立即同步"}
                  </button>
                </div>
                {p.next_run && (
                  <small className="next-run">
                    下次同步 {stamp(p.next_run)}
                  </small>
                )}
              </div>
            );
          })}
        </div>
      </Panel>
      <div className="split-grid">
        <Panel
          title="最近任务"
          extra={
            <button className="link-button" onClick={() => go("downloads")}>
              查看全部 <ArrowRight size={14} />
            </button>
          }
        >
          {data.recent.length ? (
            data.recent.map((r: any) => (
              <button
                className="activity-row"
                key={r.id}
                onClick={() => open("task", r)}
              >
                <Mark platform={r.platform} />
                <span className="activity-main">
                  <strong>
                    {names[r.platform]} · {kinds[r.kind]}
                  </strong>
                  <small>{r.error || stamp(r.created)}</small>
                </span>
                <Badge state={r.state} />
              </button>
            ))
          ) : (
            <Empty
              title="准备接收新内容"
              text="添加链接或启动平台同步，即可查看完整进度。"
            />
          )}
        </Panel>
        <Panel
          title="上游版本"
          extra={
            <button
              className="icon-button"
              aria-label="检查上游更新"
              onClick={() => act("/api/updates/check", {})}
            >
              <RefreshCw size={16} />
            </button>
          }
        >
          <p className="panel-description">
            每 6 小时自动检查。显示可评估的版本，更新由你决定。
          </p>
          {data.updates.items?.map((r: any) => (
            <a
              className="upstream-row"
              key={r.key}
              href={r.url}
              target="_blank"
              rel="noreferrer"
            >
              <div>
                <strong>{r.key === "xhs" ? "XHS-Downloader" : r.key}</strong>
                <small>
                  当前 {r.installed} {r.candidate ? "→ " + r.candidate : ""}
                </small>
                {r.note && <small>{r.note}</small>}
              </div>
              <span className={`update-label ${r.status}`}>
                {(
                  {
                    review: "可评估",
                    current: "已是当前版本",
                    unavailable: "检查失败",
                  } as any
                )[r.status] || "尚未检查"}{" "}
                <ArrowUpRight size={13} />
              </span>
            </a>
          )) || <Empty title="正在检查版本" text="首次检查需要连接 GitHub。" />}
          <div className="panel-footer">
            最近检查 {stamp(data.updates.checked_at)}
          </div>
        </Panel>
      </div>
    </>
  );
}
function Pager({
  page,
  total,
  limit,
  onPage,
}: {
  page: number;
  total: number;
  limit: number;
  onPage: (n: number) => void;
}) {
  return (
    <div className="pager">
      <span>共 {total} 条</span>
      <div>
        <button
          className="icon-button"
          aria-label="上一页"
          disabled={page <= 1}
          onClick={() => onPage(page - 1)}
        >
          <ChevronLeft size={17} />
        </button>
        <span>
          {page} / {Math.max(1, Math.ceil(total / limit))}
        </span>
        <button
          className="icon-button"
          aria-label="下一页"
          disabled={page * limit >= total}
          onClick={() => onPage(page + 1)}
        >
          <ChevronRight size={17} />
        </button>
      </div>
    </div>
  );
}
function FilterBar({
  platform,
  setPlatform,
  q,
  setQ,
  state,
  setState,
  states,
}: {
  platform: string;
  setPlatform: (s: string) => void;
  q: string;
  setQ: (s: string) => void;
  state: string;
  setState: (s: string) => void;
  states: string[];
}) {
  return (
    <div className="filters">
      <div className="chips">
        <button
          className={!platform ? "selected" : ""}
          onClick={() => setPlatform("")}
        >
          全部平台
        </button>
        {Object.entries(names).map(([key, name]) => (
          <button
            className={platform === key ? "selected" : ""}
            key={key}
            onClick={() => setPlatform(key)}
          >
            {name}
          </button>
        ))}
      </div>
      <div className="filter-inputs">
        <label className="search">
          <Search size={15} />
          <input
            aria-label="搜索"
            placeholder="搜索名称、路径或内容"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
        </label>
        <select
          aria-label="按状态筛选"
          value={state}
          onChange={(e) => setState(e.target.value)}
        >
          <option value="">全部状态</option>
          {states.map((s) => (
            <option key={s} value={s}>
              {stateNames[s]}
            </option>
          ))}
        </select>
      </div>
    </div>
  );
}
function Downloads({
  act,
  open,
  initial = "",
  add,
}: {
  act: Act;
  open: Open;
  initial?: string;
  add: () => void;
}) {
  const [tab, setTab] = useState(initial === "telegram" ? "telegram" : "tasks"),
    [platform, setPlatform] = useState(""),
    [q, setQ] = useState(""),
    [state, setState] = useState(""),
    [page, setPage] = useState(1);
  const { value: data, error } = useData(
    `/api/tasks?page=${page}&platform=${platform}&state=${state}&q=${encodeURIComponent(q)}`,
  );
  useEffect(() => setPage(1), [platform, q, state]);
  return (
    <>
      <div className="page-title">
        <div>
          <h1>下载</h1>
          <p>同步任务与机器人下载，共用一套管理界面。</p>
        </div>
        <button className="primary" onClick={add}>
          <Plus size={16} />
          添加链接
        </button>
      </div>
      <div className="tabs">
        <button
          className={tab === "tasks" ? "selected" : ""}
          onClick={() => setTab("tasks")}
        >
          平台任务
        </button>
        <button
          className={tab === "telegram" ? "selected" : ""}
          onClick={() => setTab("telegram")}
        >
          Telegram 实时下载
        </button>
      </div>
      {tab === "telegram" ? (
        <Telegram act={act} open={open} />
      ) : (
        <Panel
          title="任务列表"
          extra={
            platform !== "telegram" && (
              <button
                className="link-button"
                onClick={async () => {
                  for (const key of platform
                    ? [platform]
                    : ["x", "pixiv", "douyin", "xhs"])
                    await act("/api/tasks", { platform: key, kind: "retry" });
                }}
              >
                重试{names[platform] || "全部"}失败下载
              </button>
            )
          }
        >
          <FilterBar
            {...{ platform, setPlatform, q, setQ, state, setState }}
            states={[
              "queued",
              "running",
              "complete",
              "failed",
              "limited",
              "interrupted",
              "cancelled",
            ]}
          />
          {error && <p className="inline-error">{error}</p>}
          {data?.items.length ? (
            <div className="data-list">
              {data.items.map((r: any) => (
                <div className="data-row" key={r.id}>
                  <button
                    className="row-content"
                    onClick={() => open("task", r)}
                  >
                    <Mark platform={r.platform} />
                    <div>
                      <strong>
                        {names[r.platform]} · {kinds[r.kind]}
                      </strong>
                      <small>
                        {r.error ||
                          r.result?.message ||
                          stopText(r.result) ||
                          stamp(r.created)}
                      </small>
                      {r.progress?.downloaded !== undefined && (
                        <small>
                          已下载 {r.progress.downloaded} · 失败{" "}
                          {r.progress.failed || 0}
                          {r.progress.known_threshold
                            ? ` · 连续已下载 ${r.progress.known_consecutive || 0}/${r.progress.known_threshold}`
                            : ""}
                        </small>
                      )}
                    </div>
                  </button>
                  <Badge state={r.state} />
                  <div className="row-actions">
                    {["queued", "running"].includes(r.state) && (
                      <button
                        onClick={() => act(`/api/tasks/${r.id}/cancel`, {})}
                      >
                        停止
                      </button>
                    )}
                    {["failed", "limited", "interrupted", "cancelled"].includes(
                      r.state,
                    ) && (
                      <button
                        onClick={() => act(`/api/tasks/${r.id}/retry`, {})}
                      >
                        重试
                      </button>
                    )}
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <Empty />
          )}
          {data && (
            <Pager page={page} total={data.total} limit={30} onPage={setPage} />
          )}
        </Panel>
      )}
    </>
  );
}
function Telegram({ act, open }: { act: Act; open: Open }) {
  const { value: data, error } = useData("/api/telegram/api/state", 3000),
    [limit, setLimit] = useState("2"),
    [page, setPage] = useState(1);
  const downloads = useData(
    `/api/telegram/api/downloads/history?page=${page}`,
    3000,
  );
  const bot = data?.bot,
    connected = !!bot?.running && bot?.connection_status === "online",
    current = bot?.active,
    rows = downloads.value?.items || [];
  useEffect(
    () => setLimit(String(bot?.controls.limit_mb ?? 2)),
    [bot?.controls.limit_mb],
  );
  const applyLimit = (enabled: boolean) =>
    act("/api/telegram/api/controls/limit", {
      megabytes_per_second: enabled ? Number(limit) : null,
      limit_mb: Number(limit),
    });
  return (
    <>
      <Panel
        title="机器人与下载控制"
        extra={
          <span className={`connection ${connected ? "online" : ""}`}>
            {connectionNames[bot?.connection_status] || "未连接"}
          </span>
        }
      >
        {error && <div className="notice warning">{error}</div>}
        {!connected && bot?.last_error && (
          <div className="notice warning">{bot.last_error}</div>
        )}
        <p className="muted">机器人自动保持在线；暂停下载时仍可接收命令。</p>
        <div className="telegram-controls">
          <div className="button-group">
            <button onClick={() => act("/api/telegram/api/bot/restart", {})}>
              <RotateCcw size={15} />
              重新连接
            </button>
          </div>
          <div className="button-group">
            <button
              onClick={() =>
                act(
                  `/api/telegram/api/controls/${bot?.controls.paused ? "resume" : "pause"}`,
                  {},
                )
              }
            >
              {bot?.controls.paused ? "恢复下载" : "暂停下载"}
            </button>
            <Switch
              checked={!!bot?.controls.limit_enabled}
              onChange={applyLimit}
              label="下载限速"
              hint={
                bot?.controls.limit_enabled
                  ? `${bot.controls.limit_mb} MB/s`
                  : "不限速"
              }
            />
            <label>
              MB/s
              <input
                className="tiny-input"
                type="number"
                min="1"
                max="10000"
                step="0.5"
                value={limit}
                onChange={(e) => setLimit(e.target.value)}
                onBlur={() => applyLimit(!!bot?.controls.limit_enabled)}
              />
            </label>
          </div>
        </div>
        {current ? (
          <div className="current-download">
            <div>
              <strong>{current.file_name}</strong>
              <span>
                {current.progress.toFixed(1)}% ·{" "}
                {bytes(current.speed_bytes_per_second)}/s
              </span>
            </div>
            <progress value={current.progress} max={100} />
            <small>
              {bytes(current.downloaded_bytes)} / {bytes(current.total_bytes)} ·
              剩余 {current.eta_seconds ?? "—"} 秒
            </small>
          </div>
        ) : (
          <p className="panel-description">
            等待媒体消息或链接 · 排队 {bot?.queue_size || 0} 个 ·{" "}
            {bot?.controls.speed_limit_text || "不限速"}
          </p>
        )}
      </Panel>
      <Panel
        title="下载记录"
        extra={
          <button
            className="link-button"
            onClick={() => act("/api/telegram/api/downloads/retry-failed", {})}
          >
            重试全部失败
          </button>
        }
      >
        {rows.length ? (
          rows.map((r: any) => (
            <div className="data-row" key={r.id}>
              <button
                className="row-content"
                onClick={() => open("telegram", r)}
              >
                <span className="file-icon">
                  <Download size={18} />
                </span>
                <div>
                  <strong>{r.file_name}</strong>
                  <small>
                    {r.error ||
                      `${bytes(r.size_bytes || r.downloaded_bytes)} · ${stamp(r.updated_at)}`}
                  </small>
                </div>
              </button>
              <Badge state={r.status} />
              <div className="row-actions">
                {["failed", "cancelled", "interrupted"].includes(r.status) && (
                  <button
                    onClick={() =>
                      act(`/api/telegram/api/downloads/${r.id}/retry`, {})
                    }
                  >
                    重试
                  </button>
                )}
                {["queued", "downloading", "paused", "retrying"].includes(
                  r.status,
                ) && (
                  <button
                    onClick={() =>
                      act(`/api/telegram/api/downloads/${r.id}/cancel`, {})
                    }
                  >
                    取消
                  </button>
                )}
              </div>
            </div>
          ))
        ) : (
          <Empty
            title="等待新的 Telegram 媒体"
            text="向机器人发送图片、视频、文件或 t.me 消息链接。"
          />
        )}
        <Pager
          page={page}
          total={downloads.value?.total || 0}
          limit={30}
          onPage={setPage}
        />
      </Panel>
    </>
  );
}
function Collection({
  kind,
  act,
  open,
}: {
  kind: "assets" | "records";
  act: Act;
  open: Open;
}) {
  const [platform, setPlatform] = useState(""),
    [state, setState] = useState(""),
    [q, setQ] = useState(""),
    [page, setPage] = useState(1);
  const { value: data, error } = useData(
    `/api/${kind}?page=${page}&platform=${platform}&state=${state}&q=${encodeURIComponent(q)}`,
  );
  const status = useData("/api/workspace");
  useEffect(() => setPage(1), [platform, state, q]);
  const workspace = kind === "assets";
  return (
    <>
      <div className="page-title">
        <div>
          <h1>{workspace ? "工作区" : "媒体记录"}</h1>
          <p>
            {workspace
              ? "新媒体完成转换和校验后自动入库；此处没有暂停操作。"
              : "独立于同步停止边界，记录每份内容的下载和归档结果。"}
          </p>
        </div>
        {workspace && (
          <span
            className={`health-pill ${status.value?.available ? "online" : ""}`}
          >
            <i />
            {status.value?.available ? "自动处理中" : "不可用时直接入库"}
          </span>
        )}
      </div>
      {workspace && (
        <div className="workspace-savings">
          <strong>
            累计已减少存储占用{" "}
            {((status.value?.saved_bytes_total || 0) / 1_000_000_000).toFixed(
              2,
            )}{" "}
            GB
          </strong>
          <span>从新版处理记录开始累计，入库成功后计入</span>
        </div>
      )}
      {workspace && (
        <div className="workflow-strip">
          <span>
            <Download size={17} />
            下载完成
          </span>
          <ArrowRight size={15} />
          <span>
            <Cpu size={17} />
            HEVC / JPEG 95
          </span>
          <ArrowRight size={15} />
          <span>
            <ShieldCheck size={17} />
            校验与防膨胀
          </span>
          <ArrowRight size={15} />
          <span>
            <FolderOpen size={17} />
            自动入库
          </span>
        </div>
      )}
      <Panel title={workspace ? "文件处理列表" : "内容与归档"}>
        <FilterBar
          {...{ platform, setPlatform, q, setQ, state, setState }}
          states={
            workspace
              ? [
                  "queued",
                  "processing",
                  "publishing",
                  "complete",
                  "failed",
                  "interrupted",
                  "deleted",
                ]
              : ["processing", "complete", "failed"]
          }
        />
        {error && <p className="inline-error">{error}</p>}
        {data?.items.length ? (
          data.items.map((r: any) => {
            const file = workspace
              ? (r.target || r.source).split(/[\\/]/).pop()
              : r.metadata.title || r.source_id;
            return (
              <div
                className="data-row"
                key={r.id || r.platform + ":" + r.source_id}
              >
                <button
                  className="row-content"
                  onClick={() => open(workspace ? "asset" : "record", r)}
                >
                  <Mark platform={r.platform} />
                  <div>
                    <strong>{file}</strong>
                    <small>
                      {workspace
                        ? r.reason || `${bytes(r.source_bytes)} · 等待处理`
                        : `${names[r.platform]} · ${r.files.length} 个文件 · ${stamp(r.updated)}`}
                    </small>
                    {workspace && r.output_bytes > 0 && (
                      <small>
                        {bytes(r.source_bytes)} → {bytes(r.output_bytes)}
                        {r.output_bytes < r.source_bytes
                          ? ` · 节省 ${((1 - r.output_bytes / r.source_bytes) * 100).toFixed(1)}%`
                          : " · 保留原件"}
                      </small>
                    )}
                  </div>
                </button>
                <Badge state={r.state} />
                {workspace && (
                  <div className="row-actions">
                    {["failed", "interrupted"].includes(r.state) && (
                      <button
                        onClick={() => act(`/api/assets/${r.id}/retry`, {})}
                      >
                        重试
                      </button>
                    )}
                    {["queued", "failed", "interrupted"].includes(r.state) && (
                      <button
                        className="danger-text"
                        onClick={() => open("delete", r)}
                      >
                        删除
                      </button>
                    )}
                  </div>
                )}
                {!workspace &&
                  r.state === "failed" &&
                  r.platform !== "telegram" &&
                  r.metadata.url && (
                    <div className="row-actions">
                      <button
                        onClick={() =>
                          act("/api/tasks", {
                            platform: r.platform,
                            kind: "retry",
                            payload: { urls: [r.metadata.url] },
                          })
                        }
                      >
                        重试下载
                      </button>
                    </div>
                  )}
              </div>
            );
          })
        ) : (
          <Empty
            title={workspace ? "工作区已清空" : "还没有媒体记录"}
            text={
              workspace
                ? "Telegram 与小红书默认进入工作区；其他平台可在设置中开启。"
                : "首次同步建立新内容边界，不扫描历史媒体。"
            }
          />
        )}
        {data && (
          <Pager page={page} total={data.total} limit={30} onPage={setPage} />
        )}
      </Panel>
    </>
  );
}
function Logs() {
  const [platform, setPlatform] = useState(""),
    [q, setQ] = useState(""),
    [full, setFull] = useState(false),
    [page, setPage] = useState(1);
  const prefs = useData("/api/settings", 30000),
    limit = full ? 100 : prefs.value?.log_preview_lines || 30;
  const { value: data, error } = useData(
    `/api/logs?page=${page}&limit=${limit}&platform=${platform}&q=${encodeURIComponent(q)}`,
  );
  useEffect(() => setPage(1), [platform, q, full]);
  return (
    <>
      <div className="page-title">
        <div>
          <h1>日志</h1>
          <p>默认仅显示最近 {limit} 行。全部记录按页查看或下载排查。</p>
        </div>
        <a
          className="button"
          href={`/api/logs/export/all?platform=${platform}&q=${encodeURIComponent(q)}`}
        >
          <Download size={16} />
          下载全部日志
        </a>
      </div>
      <Panel
        title={full ? "全部日志" : "最近日志"}
        extra={
          <button className="link-button" onClick={() => setFull(!full)}>
            {full ? "返回短预览" : "查看全部记录"} <ArrowRight size={14} />
          </button>
        }
      >
        <div className="filters">
          <select
            aria-label="日志平台"
            value={platform}
            onChange={(e) => setPlatform(e.target.value)}
          >
            <option value="">全部平台</option>
            {Object.entries(names).map(([k, v]) => (
              <option key={k} value={k}>
                {v}
              </option>
            ))}
            <option value="system">系统</option>
          </select>
          <label className="search">
            <Search size={15} />
            <input
              placeholder="搜索日志内容"
              value={q}
              onChange={(e) => setQ(e.target.value)}
            />
          </label>
        </div>
        {error && <p className="inline-error">{error}</p>}
        <div className="log-view">
          {data?.items.length ? (
            [...data.items].reverse().map((r: any) => (
              <div className={`log-line ${r.level}`} key={r.id}>
                <time>{stamp(r.created)}</time>
                <span className="log-platform">
                  {names[r.platform] || r.platform}
                </span>
                <code>{r.message}</code>
              </div>
            ))
          ) : (
            <Empty title="暂无日志" text="任务运行和服务异常会保留在这里。" />
          )}
        </div>
        {full && data && (
          <Pager
            page={page}
            total={data.total}
            limit={limit}
            onPage={setPage}
          />
        )}
      </Panel>
    </>
  );
}

function Field({
  spec,
  value,
  onChange,
}: {
  spec: Spec;
  value: any;
  onChange: (v: any) => void;
}) {
  if (spec.type === "boolean")
    return (
      <Switch
        label={spec.label}
        hint={spec.hint}
        checked={!!value}
        onChange={onChange}
      />
    );
  return (
    <label className="field">
      <span>{spec.label}</span>
      {spec.type === "list" ? (
        <textarea
          rows={3}
          value={Array.isArray(value) ? value.join("\n") : value || ""}
          onChange={(e) => onChange(e.target.value.split("\n").filter(Boolean))}
        />
      ) : (
        <input
          type={
            spec.type === "password"
              ? "password"
              : spec.type === "number"
                ? "number"
                : "text"
          }
          autoComplete={spec.type === "password" ? "new-password" : undefined}
          value={value ?? ""}
          step={spec.type === "number" ? "any" : undefined}
          onChange={(e) =>
            onChange(
              spec.type === "number"
                ? e.target.value === ""
                  ? ""
                  : Number(e.target.value)
                : e.target.value,
            )
          }
        />
      )}
      <small>{spec.hint}</small>
    </label>
  );
}
function BrowserPush() {
  const [token, setToken] = useState("");
  const [copyLabel, setCopyLabel] = useState("复制授权");
  async function copyToken() {
    try {
      if (navigator.clipboard) await navigator.clipboard.writeText(token);
      else {
        const field = document.createElement("textarea");
        field.value = token;
        field.style.position = "fixed";
        field.style.opacity = "0";
        document.body.append(field);
        field.select();
        try {
          if (!document.execCommand("copy")) throw new Error("copy");
        } finally {
          field.remove();
        }
      }
      setCopyLabel("已复制");
    } catch {
      setCopyLabel("复制失败，请手动复制");
    }
  }
  return (
    <Panel title="小红书浏览器推送">
      <p className="panel-description">
        现有用户脚本继续使用
        /api/xhs/links。启用控制台密码后，在脚本设置中填写推送授权。
      </p>
      <div className="oauth-row">
        {token ? (
          <>
            <input
              aria-label="浏览器推送授权"
              type="password"
              readOnly
              value={token}
            />
            <button onClick={copyToken}>{copyLabel}</button>
          </>
        ) : (
          <button
            onClick={async () =>
              setToken((await api("/api/browser-push/token")).token)
            }
          >
            显示推送授权
          </button>
        )}
      </div>
    </Panel>
  );
}
function Settings({ initial, act }: { initial: string; act: Act }) {
  const [tab, setTab] = useState(initial || "telegram");
  const prefs = useData("/api/settings", 30000);
  const config = useData(
    "/api/platforms/" + (names[tab] ? tab : "telegram") + "/settings",
    30000,
  );
  const [draft, setDraft] = useState<Record<string, any>>({}),
    [preferences, setPreferences] = useState<any>(null),
    [credential, setCredential] = useState(""),
    [password, setPassword] = useState(""),
    [callback, setCallback] = useState(""),
    [testUrl, setTestUrl] = useState("");
  useEffect(() => {
    if (prefs.value) setPreferences(structuredClone(prefs.value));
  }, [JSON.stringify(prefs.value)]);
  useEffect(() => {
    setCredential("");
    if (config.value) {
      const fields = { ...config.value.fields };
      Object.values(definitions[tab] || {})
        .flat()
        .forEach((s) => {
          if (fields[s.key] === undefined)
            fields[s.key] =
              s.value ??
              (s.type === "boolean" ? false : s.type === "list" ? [] : "");
        });
      setDraft(fields);
    }
  }, [JSON.stringify(config.value?.fields), tab]);
  const fieldChange = (key: string, v: any) =>
    setDraft((d) => ({ ...d, [key]: v }));
  const prefChange = (key: string, v: any) =>
    setPreferences((p: any) => ({ ...p, [key]: v }));
  async function save() {
    if (tab === "general") {
      const result = await act(
        "/api/settings",
        { ...preferences, password },
        "PATCH",
      );
      if (result) setPassword("");
      return;
    }
    const values = Object.fromEntries(
      Object.entries(draft).filter(
        ([k, v]) =>
          !k.endsWith("_set") &&
          !hidden.test(k) &&
          (!["api_hash", "bot_token"].includes(k) || !!v),
      ),
    );
    if (tab === "telegram")
      for (const key of ["allowed_user_ids", "admin_user_ids"])
        values[key] = (values[key] || []).map(Number);
    const saved = await act(
      "/api/platforms/" + tab + "/settings",
      { fields: values, credential },
      "PATCH",
    );
    if (!saved) return;
    await act(
      "/api/settings",
      {
        workspace: preferences.workspace,
        max_minutes: preferences.max_minutes,
        schedule: preferences.schedule,
      },
      "PATCH",
    );
    setCredential("");
  }
  const known = new Set(
    Object.values(definitions[tab] || {})
      .flat()
      .map((s) => s.key),
  );
  const advanced = Object.entries(draft).filter(
    ([k]) =>
      !known.has(k) &&
      !k.endsWith("_set") &&
      !hidden.test(k) &&
      ![
        "jobs",
        "api_hash",
        "bot_token",
        "admin_password_hash",
        "cookie",
        "upstream.cookie",
      ].includes(k),
  );
  return (
    <>
      <div className="page-title">
        <div>
          <h1>平台与设置</h1>
          <p>账号、下载行为和归档策略都在这里管理。</p>
        </div>
        <button
          className="primary"
          onClick={save}
          disabled={!preferences || !config.value}
        >
          <Check size={16} />
          保存设置
        </button>
      </div>
      <div className="settings-layout">
        <aside className="settings-nav">
          {Object.entries(names).map(([k, v]) => (
            <button
              className={tab === k ? "selected" : ""}
              key={k}
              onClick={() => setTab(k)}
            >
              <Mark platform={k} />
              {v}
            </button>
          ))}
          <button
            className={tab === "general" ? "selected" : ""}
            onClick={() => setTab("general")}
          >
            <SlidersHorizontal size={19} />
            通用设置
          </button>
        </aside>
        <div className="settings-main">
          {(config.error || prefs.error) && (
            <div className="notice warning">{config.error || prefs.error}</div>
          )}
          {preferences && tab === "general" ? (
            <>
              <Panel title="登录与访问">
                <Switch
                  label="启用密码登录"
                  hint="默认关闭。设置密码后可开启单用户登录。"
                  checked={preferences.auth_enabled}
                  onChange={(v) => prefChange("auth_enabled", v)}
                />
                <Field
                  spec={{
                    key: "password",
                    label: prefs.value.password_set ? "修改密码" : "设置密码",
                    type: "password",
                    hint: "留空保留现有密码；密码只保存加盐摘要",
                  }}
                  value={password}
                  onChange={setPassword}
                />
              </Panel>
              <BrowserPush />
              <Panel title="日志预览">
                <Field
                  spec={{
                    key: "log_preview_lines",
                    label: "默认显示行数",
                    type: "number",
                    hint: "5–100 行；全部日志始终可以分页和下载",
                  }}
                  value={preferences.log_preview_lines}
                  onChange={(v) => prefChange("log_preview_lines", v)}
                />
              </Panel>
              <Panel title="工作区策略">
                <dl className="policy-list">
                  <div>
                    <dt>视频</dt>
                    <dd>N95 核显 HEVC · 高画质 · 不改变分辨率与帧率</dd>
                  </div>
                  <div>
                    <dt>图片</dt>
                    <dd>PNG → JPEG 质量 95；透明 PNG 保留</dd>
                  </div>
                  <div>
                    <dt>防膨胀</dt>
                    <dd>转换后不更小，则保留原件</dd>
                  </div>
                  <div>
                    <dt>失败</dt>
                    <dd>自动重试一次，仍失败则入库有效原件</dd>
                  </div>
                  <div>
                    <dt>检查</dt>
                    <dd>视频封装、流信息与首尾短片段；不做全长解码</dd>
                  </div>
                  <div>
                    <dt>负载</dt>
                    <dd>单任务、半速输入、低进程与磁盘优先级</dd>
                  </div>
                  <div>
                    <dt>删除原件</dt>
                    <dd>归档与记录确认后删除</dd>
                  </div>
                </dl>
              </Panel>
              <div className="save-bar">
                <span>登录和日志设置保存后生效。</span>
                <button className="primary" onClick={save}>
                  <Check size={16} />
                  保存设置
                </button>
              </div>
            </>
          ) : preferences && config.value && names[tab] ? (
            <>
              <Panel title={names[tab] + " · 处理方式"}>
                <Switch
                  label="通过工作区处理新媒体"
                  hint="不可用时直接入库。此开关不扫描或转换历史文件。"
                  checked={preferences.workspace[tab]}
                  onChange={(v) =>
                    prefChange("workspace", {
                      ...preferences.workspace,
                      [tab]: v,
                    })
                  }
                />
                {preferences.max_minutes[tab] !== undefined && (
                  <Field
                    spec={{
                      key: "max",
                      label: "单次最大运行时长（分钟）",
                      type: "number",
                      hint: "1–1440 分钟。达到上限会标记本次未完成，可重试。",
                    }}
                    value={preferences.max_minutes[tab]}
                    onChange={(v) =>
                      prefChange("max_minutes", {
                        ...preferences.max_minutes,
                        [tab]: v,
                      })
                    }
                  />
                )}
                {preferences.schedule[tab] && (
                  <>
                    <Switch
                      label="定时增量同步"
                      checked={preferences.schedule[tab].enabled}
                      onChange={(v) =>
                        prefChange("schedule", {
                          ...preferences.schedule,
                          [tab]: { ...preferences.schedule[tab], enabled: v },
                        })
                      }
                    />
                    <Field
                      spec={{
                        key: "hours",
                        label: "同步间隔（小时）",
                        type: "number",
                      }}
                      value={preferences.schedule[tab].hours}
                      onChange={(v) =>
                        prefChange("schedule", {
                          ...preferences.schedule,
                          [tab]: { ...preferences.schedule[tab], hours: v },
                        })
                      }
                    />
                  </>
                )}
              </Panel>
              {tab !== "telegram" && (
                <Panel title="账号凭证">
                  <div className="credential-status">
                    <ShieldCheck size={17} />
                    {config.value.credential_set
                      ? "已保存凭证"
                      : "尚未保存凭证"}
                  </div>
                  <label className="field">
                    <span>{tab === "pixiv" ? "Refresh Token" : "Cookie"}</span>
                    <textarea
                      rows={3}
                      spellCheck={false}
                      value={credential}
                      onChange={(e) => setCredential(e.target.value)}
                      placeholder="留空保留现有凭证；已保存的内容不会回显"
                    />
                    <small>
                      {tab === "pixiv"
                        ? "可粘贴已有 Refresh Token，也可使用下方登录流程。"
                        : "支持浏览器导出的 Cookie；保存后用于下一次任务。"}
                    </small>
                  </label>
                  <div className="button-group">
                    {tab === "pixiv" && (
                      <button
                        onClick={() =>
                          act("/api/tasks", {
                            platform: "pixiv",
                            kind: "oauth-start",
                          })
                        }
                      >
                        生成 Pixiv 登录链接
                      </button>
                    )}
                    {["pixiv", "douyin"].includes(tab) && (
                      <button
                        onClick={() =>
                          act("/api/tasks", { platform: tab, kind: "test" })
                        }
                      >
                        测试连接
                      </button>
                    )}
                  </div>
                  {tab === "x" && (
                    <div className="oauth-row">
                      <input
                        aria-label="X 测试推文链接"
                        placeholder="填写一条可访问的推文链接"
                        value={testUrl}
                        onChange={(e) => setTestUrl(e.target.value)}
                      />
                      <button
                        disabled={!testUrl}
                        onClick={() =>
                          act("/api/tasks", {
                            platform: "x",
                            kind: "test",
                            payload: { url: testUrl },
                          })
                        }
                      >
                        测试 Cookie
                      </button>
                    </div>
                  )}
                  {tab === "pixiv" && (
                    <div className="oauth-row">
                      <input
                        aria-label="Pixiv 授权回调"
                        placeholder="粘贴登录后的 callback URL 或 code"
                        value={callback}
                        onChange={(e) => setCallback(e.target.value)}
                      />
                      <button
                        disabled={!callback}
                        onClick={() =>
                          act("/api/tasks", {
                            platform: tab,
                            kind: "oauth-finish",
                            payload: { callback },
                          })
                        }
                      >
                        完成授权
                      </button>
                      <small>
                        生成后的登录链接在下载页的授权任务详情里查看。
                      </small>
                    </div>
                  )}
                </Panel>
              )}
              {tab === "douyin" && (
                <Panel title="同步来源">
                  <p className="panel-description">
                    每个来源单独记录同步边界。默认不回查历史。
                  </p>
                  {(draft.jobs || []).map((j: any, i: number) => (
                    <div className="source-row" key={i}>
                      <input
                        aria-label="来源名称"
                        placeholder="名称"
                        value={j.name || ""}
                        onChange={(e) =>
                          fieldChange(
                            "jobs",
                            draft.jobs.map((v: any, n: number) =>
                              n === i ? { ...v, name: e.target.value } : v,
                            ),
                          )
                        }
                      />
                      <select
                        aria-label="抖音同步模式"
                        value={j.mode}
                        onChange={(e) =>
                          fieldChange(
                            "jobs",
                            draft.jobs.map((v: any, n: number) =>
                              n === i ? { ...v, mode: e.target.value } : v,
                            ),
                          )
                        }
                      >
                        {[
                          "like",
                          "collection",
                          "post",
                          "mix",
                          "collects",
                          "live",
                        ].map((v) => (
                          <option key={v}>{v}</option>
                        ))}
                      </select>
                      <input
                        aria-label="来源地址"
                        placeholder="抖音来源地址"
                        value={j.url || ""}
                        onChange={(e) =>
                          fieldChange(
                            "jobs",
                            draft.jobs.map((v: any, n: number) =>
                              n === i ? { ...v, url: e.target.value } : v,
                            ),
                          )
                        }
                      />
                      <label>
                        <input
                          type="checkbox"
                          checked={j.enabled !== false}
                          onChange={(e) =>
                            fieldChange(
                              "jobs",
                              draft.jobs.map((v: any, n: number) =>
                                n === i
                                  ? { ...v, enabled: e.target.checked }
                                  : v,
                              ),
                            )
                          }
                        />
                        启用
                      </label>
                      <button
                        className="icon-button"
                        aria-label="移除来源"
                        onClick={() =>
                          fieldChange(
                            "jobs",
                            draft.jobs.filter((_: any, n: number) => n !== i),
                          )
                        }
                      >
                        <Close size={16} />
                      </button>
                    </div>
                  ))}
                  <button
                    className="button"
                    onClick={() =>
                      fieldChange("jobs", [
                        ...(draft.jobs || []),
                        {
                          name: "新来源",
                          mode: "like",
                          url: "",
                          enabled: true,
                        },
                      ])
                    }
                  >
                    <Plus size={15} />
                    添加来源
                  </button>
                </Panel>
              )}
              {Object.entries(definitions[tab] || {}).map(([group, specs]) => (
                <Panel key={group} title={group}>
                  <div className="fields-grid">
                    {specs.map((s) => (
                      <Field
                        key={s.key}
                        spec={{
                          ...s,
                          hint:
                            s.type === "password" && draft[s.key + "_set"]
                              ? "已保存，留空保留现有值"
                              : s.hint,
                        }}
                        value={draft[s.key]}
                        onChange={(v) => fieldChange(s.key, v)}
                      />
                    ))}
                  </div>
                </Panel>
              ))}
              {!!advanced.length && (
                <details className="advanced">
                  <summary>高级下载参数与目录</summary>
                  <p className="panel-description">
                    同页管理较少使用的参数；内部 Web 服务和旧的停止条件已移除。
                  </p>
                  <div className="fields-grid">
                    {advanced.map(([k, v]) => (
                      <Field
                        key={k}
                        spec={{
                          key: k,
                          label: k,
                          type:
                            typeof v === "boolean"
                              ? "boolean"
                              : typeof v === "number"
                                ? "number"
                                : Array.isArray(v)
                                  ? "list"
                                  : "text",
                        }}
                        value={
                          typeof v === "object" && !Array.isArray(v)
                            ? JSON.stringify(v)
                            : v
                        }
                        onChange={(n) => fieldChange(k, n)}
                      />
                    ))}
                  </div>
                </details>
              )}
              <div className="save-bar">
                <span>更改在保存后生效；已运行任务使用启动时的设置。</span>
                <button className="primary" onClick={save}>
                  <Check size={16} />
                  保存设置
                </button>
              </div>
            </>
          ) : (
            <div className="loading">正在读取设置…</div>
          )}
        </div>
      </div>
    </>
  );
}
function Drawer({
  selection,
  close,
  act,
}: {
  selection: { kind: string; row: any } | null;
  close: () => void;
  act: Act;
}) {
  const live = useData(
    selection && ["asset", "task"].includes(selection.kind)
      ? `/api/detail/${selection.kind === "asset" ? "assets" : "tasks"}/${selection.row.id}`
      : null,
  );
  useEffect(() => {
    if (!selection) return;
    const listener = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    document.addEventListener("keydown", listener);
    return () => document.removeEventListener("keydown", listener);
  }, [selection, close]);
  if (!selection) return null;
  const { kind } = selection;
  const row = live.value || selection.row;
  const title =
    kind === "task"
      ? names[row.platform] + " · " + kinds[row.kind]
      : row.file_name ||
        row.metadata?.title ||
        row.target?.split(/[\\/]/).pop() ||
        row.source_id;
  const mediaUrl =
    kind === "asset"
      ? `/api/assets/${row.id}/file`
      : kind === "record" && row.files.length
        ? `/api/records/${row.platform}/${encodeURIComponent(row.source_id)}/file`
        : kind === "telegram" && row.url
          ? row.url.replace(/^\/telegram\//, "/api/telegram/")
          : null;
  const path =
    kind === "asset"
      ? row.target
      : kind === "record"
        ? row.files[0]
        : row.file_name || "";
  const video = /\.(mp4|mkv|mov|webm|m4v)$/i.test(path || ""),
    image = /\.(jpg|jpeg|png|webp|gif)$/i.test(path || "");
  return (
    <div
      className="modal-backdrop"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) close();
      }}
    >
      <section
        className="drawer"
        role="dialog"
        aria-modal="true"
        aria-label="记录详情"
      >
        <div className="drawer-heading">
          <span>{kind === "delete" ? "删除工作区文件" : "详情"}</span>
          <button className="icon-button" aria-label="关闭详情" onClick={close}>
            <Close size={20} />
          </button>
        </div>
        <div className="drawer-body">
          <h2>{title}</h2>
          {kind === "delete" ? (
            <>
              <p>
                删除此文件及其工作区记录，下载记录会标记为已丢弃。此操作无法恢复。
              </p>
              <button
                className="danger"
                onClick={async () => {
                  if (await act(`/api/assets/${row.id}/delete`, {})) close();
                }}
              >
                确认删除
              </button>
            </>
          ) : (
            <>
              <Badge state={row.state || row.status} />
              {mediaUrl && (
                <div className="media-preview">
                  {video ? (
                    <video controls preload="metadata" src={mediaUrl} />
                  ) : image ? (
                    <img src={mediaUrl} alt={title} />
                  ) : (
                    <a
                      className="button"
                      href={mediaUrl}
                      target="_blank"
                      rel="noreferrer"
                    >
                      打开文件 <ArrowUpRight size={15} />
                    </a>
                  )}
                </div>
              )}
              <dl className="detail-list">
                {[
                  ["时间", stamp(row.created || row.created_at || row.updated)],
                  ["平台", names[row.platform] || "Telegram"],
                  ["说明", row.reason || row.error || row.metadata?.error],
                  ["原大小", bytes(row.source_bytes || row.total_bytes)],
                  ["入库大小", bytes(row.output_bytes || row.size_bytes)],
                  ["来源", row.source || row.metadata?.url],
                  ["归档", row.target || row.path],
                ]
                  .filter(([, v]) => v && v !== "—")
                  .map(([k, v]) => (
                    <div key={k}>
                      <dt>{k}</dt>
                      <dd>{v}</dd>
                    </div>
                  ))}
              </dl>
              {row.result?.url && (
                <a
                  className="primary button"
                  href={row.result.url}
                  target="_blank"
                  rel="noreferrer"
                >
                  打开 Pixiv 授权页面 <ArrowUpRight size={15} />
                </a>
              )}
              {!!row.files?.length && (
                <Panel title="文件列表">
                  {row.files.map((file: string, index: number) => (
                    <a
                      className="file-link"
                      key={file}
                      href={`/api/records/${row.platform}/${encodeURIComponent(row.source_id)}/file?index=${index}`}
                      target="_blank"
                      rel="noreferrer"
                    >
                      <FolderOpen size={15} />
                      {file.split(/[\\/]/).pop()}
                      <ArrowUpRight size={13} />
                    </a>
                  ))}
                </Panel>
              )}
              {kind === "task" && (
                <Panel title="运行结果">
                  <div className="result-grid">
                    {Object.entries(row.result || {})
                      .filter(([, v]) => typeof v !== "object")
                      .map(([k, v]) => (
                        <div key={k}>
                          <span>
                            {(
                              {
                                discovered: "发现",
                                downloaded: "下载",
                                skipped: "跳过",
                                failed: "失败",
                                stop_reason: "停止原因",
                                known_consecutive: "连续已下载",
                                known_threshold: "停止阈值",
                                source: "当前来源",
                                last_id: "最后作品",
                                message: "说明",
                              } as any
                            )[k] || k}
                          </span>
                          <strong>
                            {k === "stop_reason"
                              ? stopNames[String(v)] || String(v)
                              : String(v)}
                          </strong>
                        </div>
                      ))}
                  </div>
                  {row.result?.sources?.map((source: any, index: number) => (
                    <p key={index}>
                      {source.source} · {stopText(source)}
                    </p>
                  ))}
                </Panel>
              )}
              <details className="raw-details">
                <summary>诊断数据</summary>
                <pre>{JSON.stringify(row, null, 2)}</pre>
              </details>
            </>
          )}
        </div>
      </section>
    </div>
  );
}
function AddLinks({ close, act }: { close: () => void; act: Act }) {
  const [platform, setPlatform] = useState("xhs"),
    [text, setText] = useState(""),
    [pending, setPending] = useState(false);
  return (
    <div className="modal-backdrop">
      <form
        className="dialog"
        onSubmit={async (e) => {
          e.preventDefault();
          setPending(true);
          try {
            const result =
              platform === "telegram"
                ? await act("/api/telegram/api/downloads/links", {
                    links: text,
                  })
                : await act("/api/tasks", {
                    platform,
                    kind: "links",
                    payload: { urls: text.match(/https?:\/\/[^\s<>]+/g) || [] },
                  });
            if (result) close();
          } finally {
            setPending(false);
          }
        }}
      >
        <div className="drawer-heading">
          <h2>添加链接下载</h2>
          <button
            type="button"
            className="icon-button"
            aria-label="关闭添加链接"
            onClick={close}
          >
            <Close size={20} />
          </button>
        </div>
        <label className="field">
          <span>平台</span>
          <select
            value={platform}
            onChange={(e) => setPlatform(e.target.value)}
          >
            {Object.entries(names).map(([k, v]) => (
              <option key={k} value={k}>
                {v}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>作品或消息链接</span>
          <textarea
            autoFocus
            rows={6}
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="每行一个链接，也可粘贴包含链接的分享文本"
            required
          />
        </label>
        <p className="panel-description">
          重复的运行任务会合并；已下载内容以持久记录判断。
        </p>
        <button
          className="primary"
          disabled={pending || !text.match(/https?:\/\//)}
        >
          <Plus size={16} />
          {pending ? "正在提交…" : "加入下载队列"}
        </button>
      </form>
    </div>
  );
}
function App() {
  const [route, setRoute] = useState(location.hash.slice(1) || "home"),
    [menu, setMenu] = useState(false),
    [auth, setAuth] = useState<any>(null),
    [password, setPassword] = useState(""),
    [notice, setNotice] = useState<{ text: string; error: boolean } | null>(
      null,
    ),
    [selection, setSelection] = useState<{ kind: string; row: any } | null>(
      null,
    ),
    [add, setAdd] = useState(false),
    [busy, setBusy] = useState(0);
  const [active, parameter] = route.split(":");
  function go(next: string) {
    location.hash = next;
    setMenu(false);
    setSelection(null);
  }
  useEffect(() => {
    const change = () => setRoute(location.hash.slice(1) || "home");
    window.addEventListener("hashchange", change);
    const check = () =>
      api("/api/auth/status")
        .then(setAuth)
        .catch((e) => setNotice({ text: e.message, error: true }));
    check();
    window.addEventListener("nas-unauthorized", check);
    return () => {
      window.removeEventListener("hashchange", change);
      window.removeEventListener("nas-unauthorized", check);
    };
  }, []);
  useEffect(() => {
    if (!notice) return;
    const t = setTimeout(() => setNotice(null), 8000);
    return () => clearTimeout(t);
  }, [notice]);
  const act: Act = async (path, body = {}, method = "POST") => {
    setBusy((n) => n + 1);
    try {
      const data = await api(path, body, method);
      setNotice({
        text: data.id
          ? data.created === false
            ? "相同任务已在队列中"
            : "任务已加入队列"
          : "操作已完成",
        error: false,
      });
      refresh();
      return data;
    } catch (e: any) {
      setNotice({ text: e.message, error: true });
      return null;
    } finally {
      setBusy((n) => n - 1);
    }
  };
  const open: Open = (kind, row) => setSelection({ kind, row });
  if (!auth)
    return (
      <div className="boot-screen">
        <span className="brand-symbol">
          <Layers3 size={23} />
        </span>
        <h1>NAS Download</h1>
        <p>{notice?.text || "正在连接控制台…"}</p>
      </div>
    );
  if (!auth.authenticated)
    return (
      <div className="login-screen">
        <form
          className="dialog"
          onSubmit={async (e) => {
            e.preventDefault();
            try {
              await api("/api/auth/login", { password });
              setPassword("");
              setAuth(await api("/api/auth/status"));
            } catch (e: any) {
              setNotice({ text: e.message, error: true });
            }
          }}
        >
          <span className="brand-symbol">
            <Layers3 size={23} />
          </span>
          <h1>登录 NAS Download</h1>
          <p>使用你在设置中保存的单用户密码。</p>
          <input
            aria-label="登录密码"
            type="password"
            autoComplete="current-password"
            autoFocus
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
          />
          <button className="primary">登录</button>
          {notice && <p className="inline-error">{notice.text}</p>}
        </form>
      </div>
    );
  return (
    <div className="app-shell">
      <aside className={`sidebar ${menu ? "open" : ""}`}>
        <a className="brand" href="#home">
          <span className="brand-symbol">
            <Layers3 size={22} />
          </span>
          <span>
            NAS Download<small>媒体下载与归档</small>
          </span>
        </a>
        <nav>
          {nav.map(([key, label, Icon]) => (
            <button
              className={active === key ? "active" : ""}
              key={key}
              onClick={() => go(key)}
            >
              <Icon size={19} />
              {label}
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <span>
            <i />
            个人 NAS
          </span>
          <small>v4 · 工作区重建版</small>
          {auth.enabled && (
            <button
              className="link-button"
              onClick={async () => {
                await api("/api/auth/logout", {});
                setAuth(await api("/api/auth/status"));
              }}
            >
              <LogOut size={15} />
              退出登录
            </button>
          )}
        </div>
      </aside>
      {menu && <div className="nav-overlay" onClick={() => setMenu(false)} />}
      <div className="main-shell">
        <header className="topbar">
          <div>
            <button
              className="icon-button mobile-menu"
              aria-label="打开导航"
              onClick={() => setMenu(!menu)}
            >
              <Menu size={21} />
            </button>
            <span>{nav.find((n) => n[0] === active)?.[1] || "总览"}</span>
            <span className="topbar-divider">/</span>
            <small>NAS 媒体中心</small>
          </div>
          <div>
            <span className="refresh-status">
              {busy ? "正在执行…" : "每 5 秒更新"}
            </span>
            <button
              className="icon-button"
              aria-label="刷新数据"
              onClick={refresh}
            >
              <RefreshCw size={17} className={busy ? "spinning" : ""} />
            </button>
            <button className="top-add" onClick={() => setAdd(true)}>
              <Plus size={17} />
              <span>添加链接</span>
            </button>
          </div>
        </header>
        <main>
          {active === "home" ? (
            <Home {...{ act, go, open }} />
          ) : active === "downloads" ? (
            <Downloads
              key={route}
              {...{ act, open }}
              initial={parameter}
              add={() => setAdd(true)}
            />
          ) : active === "workspace" ? (
            <Collection kind="assets" {...{ act, open }} />
          ) : active === "records" ? (
            <Collection kind="records" {...{ act, open }} />
          ) : active === "settings" ? (
            <Settings key={route} initial={parameter} act={act} />
          ) : active === "logs" ? (
            <Logs />
          ) : (
            <Home {...{ act, go, open }} />
          )}
        </main>
        <footer className="app-footer">
          NAS Download <span>新内容增量同步 · 硬件处理 · 自动归档</span>
        </footer>
      </div>
      {notice && (
        <div
          className={`toast ${notice.error ? "error" : ""}`}
          role={notice.error ? "alert" : "status"}
        >
          {notice.error ? <TriangleAlert size={18} /> : <Check size={18} />}
          <span>{notice.text}</span>
          <button
            aria-label="关闭提示"
            className="icon-button"
            onClick={() => setNotice(null)}
          >
            <Close size={16} />
          </button>
        </div>
      )}
      <Drawer {...{ selection, act }} close={() => setSelection(null)} />
      {add && <AddLinks close={() => setAdd(false)} act={act} />}
    </div>
  );
}
createRoot(document.getElementById("root")!).render(<App />);
