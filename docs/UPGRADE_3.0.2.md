# v3.0.2 升级说明

## 小红书

直接使用 `ghcr.io/joeanamier/xhs-downloader:2.8`，没有修改、挂载覆盖或运行时补丁。取消自定义图片格式回退，新配置默认 JPEG，超时 30 秒，队列间隔 1 秒。现有配置仍按原值加载；希望应用这些设置时，需同时修改 worker 的 `image_format`、`request_delay_seconds` 和 `sync_settings.defaults.timeout`，以及上游 `settings.json` 的 `image_format`、`timeout`。

Docker 2.8 的数据目录仍为 `/app/Volume`，原 NAS 挂载保持不变，Cookie、下载记录、缓存和队列继续复用。[2.8 升级提醒](https://github.com/JoeanAmier/XHS-Downloader/releases/tag/2.8)中的 `_internal/Volume` 搬迁针对独立程序版：将旧目录复制到新主程序旁的 `Volume`。

API 容器配置两个公共 DNS 上游。worker 识别原来的 httpx DNS 错误和 2.8 curl 的 DNS、超时、传输中断错误，按原有队列重试机制处理。不会清理删除、私密或失效笔记。

2.8 虽修复部分解析错误，其 `Converter.YAML_ILLEGAL` 没有覆盖 U+0083。不能据发布说明断言日志中的特定 ReaderError 已修复。本次遵照要求保留原版上游，遇到该字符仍可能失败；未通过清除队列掩盖问题。

## f2

继续固定自有 fork 的 `5828ea7edb52e8148cbd8dff2c778cf305152fc9`。2026-10-06 核实：fork 已包含官方 main `7dab3e2f`，新增开发集中在 `v0.0.1.8-pw3` 分支 `f6be8c0f`，与当前基线相差 458 个文件。该分支涉及下载器、配置和请求框架变动，直接合并不适合此次兼容性修复。现有 Argus/uifid 定制保留；仅外围 worker 与控制台改善新 Cookie 字段保留及认证门户错误诊断。

## 发布与部署

自有控制台、Telegram 镜像更新至 v3.0.2。先备份 NAS Compose、私有配置、状态数据库和 session，再替换镜像，保持所有素材目录与状态挂载。Telegram 短进程预览及连接退避优化随正式镜像发布，替代 NAS 上的临时 memory-opt 镜像。

X 图片、Pixiv 帧路径、Telegram 文件列表缓存等修复详见 [验证记录](FIX_X_IMAGES_ISSUE.md)。
