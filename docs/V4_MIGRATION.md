# v3.0.3 → v4

当前分支为重建预览。先使用隔离数据和端口验证，再决定切换；不要直接运行旧的 v2/v1 迁移脚本。

## 已有 NAS

数据根仍为 /volume2/docker/nas-download。v4 新增 v4/core、v4/execution；媒体工作区为 /volume1/.nas-workspace。配置、Cookie、refresh token、F2 数据库、XHS Volume 和 Telegram session 保持原路径。既有素材目录和容器内媒体路径继续使用。

prepare-v4.sh 只创建新状态/工作区目录，不替换凭证、不修改已有媒体。以 root 执行；随后用 compose.v4.yml build 构建全部镜像。暂不提供 v4 发布标签，不能用不存在的 v4 GHCR 标签执行 pull。

## 切换前

1. 完成隔离验证，确认网页、归档路径、上游依赖与核显运行正常。
2. 等当前生产下载完成。停止旧的 nas-download、nas-download-telegram、nas-download-xhs-api，避免两个实例使用同一 session 或并发写平台数据库。
3. 保存旧 Compose、私有 .env、容器定义及全部 config/state/session/Volume 的一致性备份。备份只留在 NAS 私有目录。
4. 保留旧内部令牌；v4 的 INTERNAL_API_TOKEN 从私有 .env 读取。旧 TELEGRAM_ADMIN_PASSWORD 不会自动开启统一控制台登录：v4 按约定默认关闭，在设置中添加密码并启用。
5. 运行准备脚本，再 docker compose -f compose.v4.yml up -d。新版本只发布控制台 14001，Telegram/XHS 仅内部访问。
6. 首次启动只读取旧记录数据库，并将 Telegram JSON 一次性导入 SQLite。首轮收藏同步正常下载未完成内容，按连续已下载记录结束，并保存来源边界；不扫描旧媒体。
   旧小红书队列数据库没有文件清单，迁移保留来源/状态，按约定不补扫历史素材；历史文件预览不会凭空补齐。
7. 验证新链接/新收藏下载、Bot 接收、工作区转换、直接旁路、记录与文件预览、失败重试、分钟上限、日志导出和密码设置。

## 回退

先停止 v4 所有执行服务，确认没有媒体转换或入库进程；使用备份的旧 Compose 和已保留 v3.0.3 镜像启动。新旧 Telegram 不得同时启动。

Telegram v4 不会继续重写旧 downloads.json；v4 期间产生的新历史存于 downloads.sqlite3。回退后的旧网页不会显示这部分新历史，媒体本身已归档的文件仍在原目录。新工作区未完成原件及收据应保留，不能把工作区整体删掉。

需要回退到切换前数据库时，先另存 v4 运行期间的数据，再恢复备份。不要把新数据库直接覆盖为旧副本，否则新文件的记录会失去对应关系。

正式切换尚未执行。隔离预览不挂载生产素材、不使用生产账号/session、不自动同步。
