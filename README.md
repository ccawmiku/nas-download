# NAS Download

统一管理小红书、X、Pixiv、抖音和 Telegram 的 NAS 下载控制台。

当前版本：`v3.0.0`。项目由原 NAS 下载集成服务 v2.0.2 和 Telegram v1.9 迁移而来，保留已有平台实现、配置与状态格式。

## 功能

- 五个平台的服务状态、运行任务与日志。
- Telegram 原控制台密码登录，下载队列、进度、速率、ETA、历史和媒体预览。
- Telegram 启动、停止、重启、暂停、恢复、限速、取消与失败重试。
- 原有小红书链接队列、X 点赞、Pixiv 收藏和抖音 f2 下载。
- 平台 worker 退出后的退避重启，流式媒体代理及视频 Range 请求。
- Telegram 独立容器，单实例复用现有 session，集成 cryptg 加速。

## 访问

- 统一控制台：`http://NAS_IP:14001`
- Telegram 统一入口：在控制台选择 Telegram；完整设置页为 `/telegram/`
- 原 Telegram 独立端口继续保留：`http://NAS_IP:12010`
- 小红书 API 保留端口 13001

## 已有部署迁移

请先阅读 [迁移说明](docs/MIGRATION.md)。运行数据集中在 `/volume2/docker/nas-download`，素材目录及容器内路径保持原样。

新项目不会用空配置替代已有 Cookie、数据库、历史或 Telegram session。公开仓库只包含源码、示例和文档，NAS 的私有运行配置单独保存。

## 部署和更新

准备私有 `.env`，可参考 `.env.example`。发布后的两个镜像分别为：

```text
ghcr.io/ccawmiku/nas-download:v3.0.0
ghcr.io/ccawmiku/nas-download-telegram:v3.0.0
```

```bash
docker compose -f docker-compose.yml pull
docker compose -f docker-compose.yml up -d
```

已有部署先执行迁移流程；上述命令不能替代迁移。NAS 运行文件命名为 `compose.yaml`。

## 开发与验证

```bash
python -m pip install -r services/telegram/requirements-dev.txt requests PyYAML pixivpy3 gallery-dl playwright
python -m unittest test_integrated.py test_integration.py
(cd services/telegram && python -m pytest -q)
(cd frontend && npm ci && npm run build)
```

`.github/workflows/ci.yml` 在 PR 上验证 Python 与前端，正式版本标签验证通过后发布两个镜像。平台运行依赖按照迁移前实际版本锁定，f2 使用明确 commit。

## 代码结构

- `frontend/`：统一 React 控制台。
- `_integrated/`：Web 服务、平台适配、进程监督和流式代理。
- `_src/`：沿用的四个平台下载器。
- `services/telegram/`：沿用的 Telegram 实现与测试。
- `scripts/`、`docs/`：迁移及运维说明。

来源与基线 commit 见 [PROVENANCE.md](PROVENANCE.md)。原平台中的参考文档和示例保留供开发使用，部署以根目录 Compose 与迁移说明为准。
