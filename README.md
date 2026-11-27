# NAS Download v4

面向自托管 NAS 的五平台自动化下载与媒体归档中心，统一管理 **Telegram**、**小红书**、**X**、**Pixiv** 与 **抖音** 的增量采集、浏览器直推、去重对账、工作区转码与媒体预览。

v4 采用五容器解耦架构，将控制台与调度状态集中在核心服务，并与固定上游版本的平台下载引擎及独立媒体转码工作区隔离：

| 服务 | 镜像文件 | 主要职责 |
|---|---|---|
| `console` | [containers/console.Dockerfile](containers/console.Dockerfile) | 唯一 Web 控制台与 REST API（端口 `14001`）、SQLite WAL 状态与历史记录、定时调度、浏览器推送校验、`Pillow` WEBP 缩略图缓存、上游版本巡检与 Bark 告警 |
| `engines` | [containers/engines.Dockerfile](containers/engines.Dockerfile) | 执行 X / Pixiv / 抖音平台任务与 X 浏览器直推清单下载；独立子进程执行与分钟上限控制 |
| `xhs` | [containers/xhs.Dockerfile](containers/xhs.Dockerfile) | 隔离运行固定版 XHS-Downloader，并承接小红书链接入队与浏览器直推清单下载（`direct`） |
| `telegram-worker` | [containers/telegram.Dockerfile](containers/telegram.Dockerfile) | 长期在线维护 Telethon 连接、同步持久化下载历史与交付恢复（仅通过内部网络通信） |
| `media` | [containers/media.Dockerfile](containers/media.Dockerfile) | 工作区文件转码、完整性校验与原子入库（Intel N95 核显 `hevc_qsv`、PNG→JPEG 质量 95、防膨胀） |

---

## 控制台功能总览

Web 界面划分为八个功能页，支持可选控制台密码登录：

- **总览**：查看各执行服务心跳、最近成功同步时间、任务数量、最近任务摘要，以及固定上游依赖的更新检测状态。
- **浏览器推送**：查看并轮换浏览器推送授权 `X-NAS-Submit-Token`，统计 X / 小红书浏览器端推送记录与 X 基线库存量。
- **下载**：手动投放下载链接、触发增量同步、查看任务进度与取消/重试，以及管理 Telegram 下载控制、速率限制（`limit_enabled` / `limit_mb`）与分页历史。
- **工作区**：管理经工作区的待处理/失败转码文件并统计累计节省空间（`saved_bytes_total`）。
  - 视频优先使用 `hevc_qsv` 转码，PNG 在实际可缩小且非透明/非动画时转为 `quality=95` 的 JPEG；不符合更小体积要求时自动保留原件。
  - 媒体服务不可用时支持自动旁路（bypass）并在恢复后核对交付与入库收据。
- **媒体记录**：按内容聚合展示已归档记录与全部文件。
  - 支持 **瀑布流 / 列表双视图切换**（默认瀑布流，偏好持久化在浏览器 `localStorage`）。
  - 图片自动请求服务端缓存接口 `GET /api/records/{platform}/{identifier}/thumb`（由 `Pillow` 生成 WEBP 缩略图并按文件 mtime/体积缓存于 `/state/core/thumbs`），视频使用 `<video preload="metadata">` 预览，多文件条目显示 `P` 角标。
- **错误列表**：统一汇总五个平台尚未解决的下载失败项，支持按平台、链接或原因过滤及隐藏条目（隐藏仅移除列表显示，保留已下载文件与原始数据库记录）。
- **平台与设置**：配置五平台凭证/保存目录、工作区开关、同步间隔（小时）、单次最大运行时长（分钟）、可选控制台登录密码以及 **Bark 静默告警**。
  - Bark 推送地址 `https://api.day.app/密钥/` 单独保存在核心状态目录的私有 `bark.json`（接口不回显、不入库）。每 5 分钟检查一次异常，故障持续超过 10 分钟才以 `passive` 级别发出摘要；滚动 24 小时最多 1 条、同类问题间隔至少 72 小时，内容仅包含平台/数量与服务或磁盘健康状态，不泄露标题、链接、路径或凭证。
- **日志**：实时查看系统运行日志（默认展示最近配置行数，支持完整日志分页与导出）。

---

## 浏览器直推、增量对账与错误重下

针对高风控站点（**X** 与 **小红书**），推荐使用配套 Tampermonkey 油猴脚本在已登录浏览器内完成列表核对与媒体提取，尽量减少或避免服务器端依赖 Cookie：

### 1. X（零 Cookie 优先 · Quote 方案 A）
- **实时增量对账与流式直推**：浏览 `/i/history/likes` 或个人点赞页时，油猴脚本拦截 GraphQL 响应直接解析无水印原图（`pbs.twimg.com`）与最高码率 MP4（`video.twimg.com`），并发起 `POST /api/x/reconcile` 按 `tweet_id` 与 NAS 归档及基线库对账，达到连续已归档早停阈值时自动终止加载，同时通过 `POST /api/x/direct` 流式推送缺失媒体（支持外贴推文与引用推文 Quote 双重归档）。
- **离线与浏览历史基线导入**：
  - 浏览器端可在工作台执行「已捕获导入基线」或「边滚边导基线（不下载）」，调用 `POST /api/x/baseline` 将已收录推文 ID 写入 `x_download_baseline` 表。
  - 服务器端可通过离线脚本 [tools/import_x_baseline.py](tools/import_x_baseline.py) 扫描本地已归档媒体目录及旧版历史库（默认仅依据实际媒体文件名/元数据按需导入，零 Cookie、零外网请求）。
- **旧版 Cookie 入口隔离与防误触**：控制台发起 X 重试或全量操作时会弹出二次确认，默认推荐使用「仅重试直推记录（免 Cookie）」（`direct_only`），防止误触发旧版 Cookie 无头浏览器链路。

### 2. 小红书（作品 ID 对账 · 滚动追捕失败项 · 详情抓图直推）
- **增量对账与保鲜入队**：[tools/userscripts/xhs-docker-queue.user.js](tools/userscripts/xhs-docker-queue.user.js) 读取点赞与收藏列表，通过 `POST /api/xhs/reconcile` 按 24 位作品 ID 核对状态（`archived` / `pending` / `deferred` / `missing`），流式提交缺失作品携带完整 `xsec_token` 的链接至 `POST /api/xhs/links`，支持分类保存与单个作品独立保存（`destination: "single"`）。
- **滚动追捕失败项（`12/18` → `18/18`）与增量/后台共用**：工作台在页面打开及滚动点赞/收藏列表时，实时对比 NAS 错误列表并显示最新 `xsec_token` 的捕获进度（如 `12/18`、`18/18`）。前台点击「自动加载」或每日后台任务运行期间，开启追捕开关后在未捕获齐当前分类失败项之前不触发连续早停，并把捕获到的失败作品新链接与增量新作品一并携带 `force_retry: true` 推回 NAS 重下，也支持在面板一键推送已捕获的失败项。
- **详情抓图直推（方式 A）**：当服务端解析遇到页面限制时，可在小红书作品详情页通过 `/feed` API Hook、Vue/Pinia 状态或 DOM 媒体兜底直接提取官方 CDN 媒体直链（`xhscdn.com` / `xhscdn.net` / `xiaohongshu.com`，后端 [core/xhs_direct.py](core/xhs_direct.py) 执行严格域名白名单、HTTPS 443 端口校验与防 SSRF 重定向检查）并提交至 `POST /api/xhs/direct` 下载归档。

### 3. 统一失败列表联动与重下闭环
- X 与小红书油猴工作台均内置「NAS 失败重试」区块，通过 `GET /api/browser-push/errors` 拉取当前未解决失败记录。
- 后台定时运行会在扫流期间顺带重推或刷新命中错误列表的作品；用户也可在油猴工作台逐条点开失败记录页面，脚本在详情页自动提取高清直链并直推 NAS 治愈错误，或经 `POST /api/browser-push/errors/dismiss` 在列表侧隐藏无需再试的条目。

---

## 油猴脚本一览

全部脚本位于 [tools/userscripts/](tools/userscripts)，需配合 Tampermonkey（建议支持 `@crontab` 与 `GM_openInTab` 的桌面版浏览器）使用：

| 脚本文件 | 当前版本 | 用途与主要能力 |
|---|---|---|
| [x-nas-queue.user.js](tools/userscripts/x-nas-queue.user.js) | `1.3.1` | X 浏览器直推工作台：GraphQL 直链拦截、Quote 方案 A 归档、实时 `tweet_id` 对账与早停、基线导入、NAS 失败列表同步与详情自动重推 |
| [x-nas-bg.user.js](tools/userscripts/x-nas-bg.user.js) | `1.1.0` | X 每日一次后台自动采集调度脚本（`@crontab * * once * *`）：自动打开点赞页增量推流、轮询 NAS 入队与错误回执 |
| [xhs-docker-queue.user.js](tools/userscripts/xhs-docker-queue.user.js) | `3.7.1` | 小红书 NAS 下载工作台：点赞/收藏增量对账、滚动实时追捕失败项（`12/18`→`18/18`）并与增量/后台共用早停与重推、单条 `single` 发送、详情抓图直推（方式 A）与 NAS 失败列表联动 |
| [xhs-nas-bg.user.js](tools/userscripts/xhs-nas-bg.user.js) | `1.1.0` | 小红书每日一次后台自动采集调度脚本（`@crontab * * once * *`）：按配置分类依次巡检点赞与收藏并上报结果 |

详细使用说明另见 [小红书浏览器工作台文档](docs/XHS_BROWSER_WORKBENCH.md)。

### 安装与授权步骤
1. 在桌面版浏览器的 Tampermonkey 中，从本仓库 `main` 分支的 [tools/userscripts/](tools/userscripts) 导入对应平台的 `queue`（工作台）与 `bg`（每日后台定时）脚本。
2. 登录 NAS 控制台进入 **「浏览器推送」** 页面复制授权密钥 `X-NAS-Submit-Token`。
3. 打开 X 或小红书页面点击右下/左下悬浮按钮打开工作台设置，填入 NAS 主机地址、端口（默认 `14001`）与 `X-NAS-Submit-Token`，保存后测试连通性即可。

---

## 部署、构建与升级

### 目录与环境准备
1. 编排文件为 [compose.v4.yml](compose.v4.yml)，仅向外发布控制台端口 `14001`。运行配置、凭证、Cookie、session、SQLite 状态库与媒体目录全部留在宿主 NAS 私有路径，不提交至仓库。
2. 参考 [.env.v4.example](.env.v4.example) 创建私有 `.env`，设置 `INTERNAL_API_TOKEN`、`NAS_DOWNLOAD_ROOT`、`NAS_WORKSPACE_HOST`、`NAS_DOWNLOAD_PORT`、`NAS_DOWNLOAD_VERSION` 与 `NAS_SCHEDULER` 等变量。
3. 全新切换 v4 时以 root 执行一次 [scripts/prepare-v4.sh](scripts/prepare-v4.sh) 初始化 `v4/core`、`v4/execution` 与工作区目录。

### 启动与源码重建
- **拉取已发布 GHCR 镜像运行**：
  `compose.v4.yml` 默认拉取 `NAS_DOWNLOAD_VERSION=v4.0.14` 对应的五个 GHCR 镜像（推送 `v*` 标签会触发 [.github/workflows/ci.yml](.github/workflows/ci.yml) 构建并发布新标签镜像）：
  ```sh
  docker compose -f compose.v4.yml pull
  docker compose -f compose.v4.yml up -d --no-build
  ```
- **直接从 `main` 源码重建运行（包含最新功能与修复）**：
  `main` 源码包含控制台 `Pillow==13.0.0` WEBP 缩略图缓存与媒体记录瀑布流视图、小红书 `run_direct` 直推、X Quote 方案 A 直推及多项增量对账/失败重下修复。若不等待新发布标签而直接使用最新源码，可本地构建运行；升级已有容器时建议先停并清除旧容器再重建 `console`、`xhs` 与 `engines`：
  ```sh
  docker compose -f compose.v4.yml stop console xhs engines
  docker compose -f compose.v4.yml rm -f console xhs engines
  docker compose -f compose.v4.yml up -d --build console xhs engines
  ```
  首次全量从源码构建启动全部服务可执行 `docker compose -f compose.v4.yml up -d --build`。

### 核心固定依赖与兼容说明
- 核心与下载引擎固定版本：`Pillow 13.0.0`、`pillow-heif 1.8.1`、`yt-dlp 2026.10.26`、`gallery-dl 1.32.19`，以及固定版本 F2 与 XHS-Downloader（`JoeanAmier/XHS-Downloader` `master`）（来源信息见 [PROVENANCE.md](PROVENANCE.md)）。
- **Instagram 已于 v4 移除**：不再提供网页配置入口、采集适配器或媒体目录挂载；升级时会自动忽略旧定时配置，已有历史文件与数据库记录保留只读历史。

---

## 相关文档

- [v4 架构与数据流程](docs/V4_ARCHITECTURE.md)
- [v3 → v4 升级与迁移指南](docs/V4_MIGRATION.md)
- [小红书浏览器工作台使用说明](docs/XHS_BROWSER_WORKBENCH.md)
- [上游来源与固定修订说明](PROVENANCE.md)

---

## 本地验证与测试

在提交或打包前可执行以下验证命令：

```sh
# 1. Python 语法与单元测试
python -m compileall -q core workers _common _src tools services/telegram/app
python -m pytest tests -q
python -m unittest test_integration.py

# 2. 需完整集成依赖（含 pixivpy3 / PyYAML）时补充运行
python -m unittest test_integrated.py

# 3. 油猴脚本语法与核心逻辑测试
node -c tools/userscripts/x-nas-queue.user.js
node -c tools/userscripts/x-nas-bg.user.js
node -c tools/userscripts/xhs-docker-queue.user.js
node -c tools/userscripts/xhs-nas-bg.user.js
node --test tests/test_xhs_userscript.cjs tests/test_x_userscript.cjs
node --experimental-strip-types --test tests/test_task_outcome.mjs

# 4. 前端构建校验
cd frontend && npm ci && npm run build

# 5. 可选：安装 Node playwright 与 chromium 后运行浏览器冒烟测试
node tests/xhs_browser_smoke.cjs
```
