# NAS Download

统一管理小红书、X、Pixiv、抖音和 Telegram 的 NAS 下载控制台。

当前版本：`v3.0.2`。项目由原 NAS 下载集成服务 v2.0.2 和 Telegram v1.9 迁移而来，保留已有平台实现、配置与状态格式。

## 功能

- 五个平台的服务状态、运行任务与日志。
- Telegram 直接访问，下载队列、进度、速率、ETA、历史和媒体预览。
- Telegram 启动、停止、重启、暂停、恢复、限速、取消与失败重试。
- 原有小红书链接队列、X 点赞、Pixiv 收藏和抖音 f2 下载。
- 平台 worker 退出后的退避重启，流式媒体代理及视频 Range 请求。
- Telegram 独立容器，单实例复用现有 session，集成 cryptg 加速。
- 小红书保留自动重试，页面只展示失败记录与重试状态。

## 访问

- 统一控制台：`http://NAS_IP:14001`
- Telegram 统一入口：在控制台选择 Telegram；完整设置页为 `/telegram/`
- 原 Telegram 独立端口继续保留：`http://NAS_IP:12010`
- 小红书 API 保留端口 13001

## 已有部署迁移

请先阅读 [迁移说明](docs/MIGRATION.md)。运行数据集中在 `/volume2/docker/nas-download`，素材目录及容器内路径保持原样。

新项目不会用空配置替代已有 Cookie、数据库、历史或 Telegram session。公开仓库只包含源码、示例和文档，NAS 的私有运行配置单独保存。

## 部署和更新

准备私有 `.env`，可参考 `.env.example`。发布镜像分别为：

```text
ghcr.io/ccawmiku/nas-download:v3.0.2
ghcr.io/ccawmiku/nas-download-telegram:v3.0.2
ghcr.io/joeanamier/xhs-downloader:2.8
```

```bash
docker compose -f docker-compose.yml pull
docker compose -f docker-compose.yml up -d
```

已有部署先执行迁移流程；上述命令不能替代迁移。NAS 运行文件命名为 `compose.yaml`。

小红书直接使用未修改的上游 2.8 镜像，默认 JPEG。Docker 数据目录仍为 `/app/Volume`，升级保留现有挂载；独立程序版需把旧 `_internal/Volume` 复制至新程序旁。新增配置默认超时 30 秒、队列间隔 1 秒；已有显式配置继续保留。

## 开发与验证

集成服务：在仓库根目录的独立 Python 3.12 虚拟环境中运行。

```bash
python -m pip install -r _integrated/requirements.txt PyYAML
python -m playwright install chromium
python -m unittest test_integrated.py test_integration.py
```

Telegram：另建独立 Python 3.12 虚拟环境，在 `services/telegram` 中运行；f2 固定的 pytest/pytest-asyncio 版本与 Telegram 开发依赖不同，不要混装两个服务的依赖。

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

前端：在 `frontend` 中运行。

```bash
npm ci
npm run build
npm audit --audit-level=high
```

`.github/workflows/ci.yml` 在 PR 上用独立作业验证两个 Python 环境、真实浏览器采集与前端，并构建两个自有镜像检查 worker 和工具能否导入。正式版本标签全部验证通过后发布。测试直接使用对应镜像的锁定依赖，f2 使用明确 commit。

X 图片下载和其他兼容性修复的验证记录见 [修复说明](docs/FIX_X_IMAGES_ISSUE.md)。

## 代码结构

- `frontend/`：统一 React 控制台。
- `_integrated/`：Web 服务、平台适配、进程监督和流式代理。
- `_src/`：沿用的四个平台下载器。
- `services/telegram/`：沿用的 Telegram 实现与测试。
- `scripts/`、`docs/`：迁移及运维说明。

来源与基线 commit 见 [PROVENANCE.md](PROVENANCE.md)。原平台中的参考文档和示例保留供开发使用，部署以根目录 Compose 与迁移说明为准。
