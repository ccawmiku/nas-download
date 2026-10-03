from __future__ import annotations

COMMON_APP_CSS = """
:root {
  color-scheme: light;
  --bg:#f6f8fb;
  --panel:#fff;
  --panel-soft:#f8fafb;
  --line:#e7ebf0;
  --line-strong:#cbd4e1;
  --text:#182230;
  --muted:#7b8797;
  --accent:#5574eb;
  --accent-strong:#4867dc;
  --accent-soft:#edf1ff;
  --ink:#182230;
  --ok:#16805d;
  --ok-bg:#ecfdf5;
  --ok-line:#bbf7d0;
  --warn:#a15c07;
  --warn-bg:#fff7ed;
  --danger:#b42318;
  --danger-bg:#fff1f0;
  --shadow:0 8px 30px rgba(28,42,68,.07);
  --shadow-soft:0 4px 18px rgba(28,42,68,.04);
}
* { box-sizing:border-box; }
body {
  margin:0;
  font-family:Inter,ui-sans-serif,system-ui,-apple-system,Segoe UI,"PingFang SC","Microsoft YaHei",sans-serif;
  background:var(--bg);
  color:var(--text);
  line-height:1.48;
}
header {
  min-height:64px;
  display:flex;
  align-items:center;
  justify-content:space-between;
  gap:12px;
  padding:0 24px;
  background:#fff;
  color:var(--text);
  border-bottom:1px solid var(--line);
}
main {
  max-width:1440px;
  margin:0 auto;
  padding:30px;
  display:grid;
  gap:16px;
}
section,
.card {
  background:var(--panel);
  border:1px solid var(--line);
  border-radius:15px;
  padding:20px;
  box-shadow:var(--shadow-soft);
}
h1 { font-size:20px; margin:0; letter-spacing:-.025em; }
h2 { font-size:16px; margin:0 0 12px; letter-spacing:-.015em; }
label { display:block; color:var(--muted); font-size:13px; margin:10px 0 5px; }
code { color:var(--accent-strong); background:var(--accent-soft); border-radius:5px; padding:1px 5px; }
input,
textarea,
select {
  width:100%;
  box-sizing:border-box;
  border:1px solid var(--line);
  border-radius:9px;
  padding:10px 11px;
  font:inherit;
  background:white;
  color:var(--text);
  transition:border-color .15s, box-shadow .15s, background .15s;
}
textarea { min-height:120px; resize:vertical; }
input:focus,
textarea:focus,
select:focus {
  outline:0;
  border-color:var(--accent);
  box-shadow:0 0 0 3px rgba(85,116,235,.14);
}
button {
  border:0;
  background:var(--accent);
  color:white;
  border-radius:9px;
  padding:10px 14px;
  cursor:pointer;
  font:inherit;
  font-weight:650;
  line-height:1.2;
  transition:background .15s, box-shadow .15s, transform .12s, opacity .15s;
}
button:hover { background:var(--accent-strong); box-shadow:0 6px 16px rgba(85,116,235,.18); }
button:active { transform:translateY(1px); }
button.secondary { background:#eef2f6; color:var(--text); border:1px solid var(--line); }
button.secondary:hover { background:#e3e8ee; box-shadow:none; }
button:disabled { opacity:.55; cursor:not-allowed; }
.grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:14px; }
.auto-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:12px; }
.actions { display:flex; gap:8px; flex-wrap:wrap; margin-top:12px; }
.status { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
.pill {
  display:inline-flex;
  align-items:center;
  gap:5px;
  border-radius:999px;
  background:#edf2f7;
  color:#334155;
  border:1px solid #dce3ea;
  padding:4px 9px;
  font-size:12px;
  font-weight:650;
}
.pill.ok { background:var(--ok-bg); color:var(--ok); }
.pill.warn { background:var(--warn-bg); color:var(--warn); }
.muted { color:var(--muted); }
.help { color:var(--muted); font-size:13px; line-height:1.65; }
.ok { background:var(--ok-bg); border-color:var(--ok-line); }
.card {
  display:grid;
  grid-template-columns:1fr auto;
  gap:6px 12px;
  text-decoration:none;
  color:inherit;
  transition:border-color .15s, box-shadow .15s, transform .12s;
}
.card:hover { border-color:var(--line-strong); box-shadow:var(--shadow); transform:translateY(-1px); }
.card strong { font-size:17px; }
.card span { color:var(--muted); grid-column:1/-1; }
.card em {
  font-style:normal;
  border-radius:999px;
  padding:2px 8px;
  background:#eef2f7;
  color:var(--muted);
  font-size:12px;
}
.card.ready em { background:var(--ok-bg); color:var(--ok); }
.card.starting em { background:var(--warn-bg); color:var(--warn); }
table { width:100%; border-collapse:collapse; font-size:13px; }
th,
td { border-bottom:1px solid var(--line); padding:9px 8px; text-align:left; vertical-align:top; }
th { color:var(--muted); font-weight:700; background:var(--panel-soft); }
pre {
  margin:0;
  background:#11151d;
  color:#dce7f3;
  padding:12px;
  border-radius:9px;
  overflow:auto;
  max-height:520px;
  white-space:pre-wrap;
  overflow-wrap:anywhere;
  word-break:break-word;
}
progress { width:100%; height:16px; accent-color:var(--accent); }
.progress-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; margin-top:12px; }
.metric { border:1px solid var(--line); border-radius:8px; padding:12px; background:var(--panel-soft); box-shadow:var(--shadow-soft); }
.metric strong { display:block; font-size:18px; margin-top:4px; }
.metric .label { color:var(--muted); font-size:13px; }
.metric .value { font-size:20px; font-weight:750; overflow-wrap:anywhere; }
@media (max-width:760px) {
  header { padding:10px 14px; align-items:flex-start; flex-direction:column; }
  main { padding:12px; }
  .grid,
  .progress-grid { grid-template-columns:1fr; }
}
""".strip()


def app_css(extra: str = "") -> str:
    parts = [COMMON_APP_CSS]
    if extra.strip():
        parts.append(extra.strip())
    return "\n".join(parts)
