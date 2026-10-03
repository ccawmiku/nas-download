# 迁移已有部署

迁移基线为原下载集成服务 v2.0.2 与 Telegram v1.9。新项目保留原平台代码、测试及所有配置和状态格式，Telegram 使用独立 worker。

## 目标目录

```text
/volume2/docker/nas-download/
├─ compose.yaml
├─ .env                 私有运行参数
├─ platforms/           原 nas-auto-download-integrated 目录的全部内容
│  ├─ xhs/
│  ├─ x/
│  ├─ pixiv/
│  └─ douyin/
├─ telegram/            原 telethon-media-bot 目录的全部内容
│  ├─ config/           配置、downloads.json 和预览缓存
│  ├─ sessions/         现有 Telethon session
│  └─ download-root/    原匿名下载根卷的内容
├─ backup-location
└─ migration-complete
```

媒体目录沿用部署前的宿主和容器映射。迁移脚本只移动 `/volume2/docker` 下的两个服务数据目录，不移动或整理图片、视频、抖音及其他素材目录。

## 执行顺序

1. 准备正式发布的两个新镜像及新 Compose。
2. 从旧 Telegram 容器迁移 ADMIN_PASSWORD 环境值到 NAS 私有 `.env`，同时生成内部服务令牌。不要把该文件提交到仓库。
3. 确认下载器没有需要等待完成的运行任务，然后以 root 执行 `scripts/migrate-existing.sh`。
4. 脚本先停止旧服务，保存容器定义并复制完整备份，然后把两个目录移动到新目录中；启动前用 rsync 校验文件内容一致。
5. 备份位于 `/volume2/docker/.nas-download-backups/<时间>`，包含配置和 session，只在 NAS 保存。
6. 新服务接管后验证五个平台状态与 Telegram 连接、历史、文件读取、媒体 Range 请求及既有路径。完成验证后移除旧的停止容器，备份继续保留。

同一 session 始终只由一个 Telegram 实例使用。素材文件由既有下载器按原有逻辑继续读写；迁移本身不对素材目录执行复制、移动或删除。

## 回退

迁移脚本在新服务验收失败时自动停止新项目、把数据目录移回原路径，并启动原容器。

完成切换后的人工回退应先停止新项目，再将当前的 `platforms` 和 `telegram` 移回原路径，使用旧目录中的 Compose 启动原服务。若需要恢复到切换时状态，先另存当前数据，再恢复 `backup-location` 指向的备份。不能让新旧 bot 同时使用 session。

## 验收范围

- 五个平台服务 ready，Telegram bot 连接成功。
- Telegram 控制台直接访问，现有下载历史和失败项仍可查询。
- 既有 session、Cookie、配置与数据库完成迁移；没有创建全新账号状态。
- Telegram 原图、预览与视频 Range 请求通过统一入口可访问。
- 所有素材挂载与迁移前完全一致，旧运行数据目录不再作为活动服务目录。
- 测试中的新消息、取消和重试使用替身客户端；上线检查不发送测试消息或重新下载素材。
