# v3.0.3 升级说明

## X 手动失败重试

“需要手动处理的视频失败链接”在独立 Worker 页面和统一控制台均支持单条重试、全部重试和原有删除操作。重试通过保存的 URL 重新打开详情页识别媒体，再强制下载，不依赖该推文仍出现在 Likes 列表，也不受自动重试次数限制。不会把未识别媒体的推文强行标记为视频。

全部重试逐条处理完整失败列表，不受页面显示 200 条的限制；开始时预留任务锁，正在采集、下载或测试 Cookie 时返回 409。成功项自动退出失败列表；失败项保留新错误、次数和已下载文件。一条失败不会中止后面的重试；开始前已删除或完成的记录会跳过。删除只移除记录，沿用原行为。

## 小红书 fork

使用 [ccawmiku/XHS-Downloader](https://github.com/ccawmiku/XHS-Downloader) 的 `ghcr.io/ccawmiku/xhs-downloader:2.8-nas.2`，基于上游 2.8。应用代码只扩展 `Converter.YAML_ILLEGAL` 的字符范围，覆盖 PyYAML 拒绝的 U+007F–U+0084 和 U+0086–U+009F，保留合法的 U+0085。继续沿用 YAML 解析与原有 JavaScript 规范化，兼容 API、JPEG 设置和 `/app/Volume` 数据目录。没有图片格式回退补丁。

fork 固定修复提交：`5336169fe5d2f3a6dcb171309a14d273b917286d`。回归覆盖原 U+0083 最小复现、全部非法 C1 字符、合法 Unicode/空白、PC/移动网页数据和 JavaScript 特殊值；发布前分别在源码及实际容器运行测试。fork 使用独立 GHCR 构建，不依赖上游作者的 Docker Hub 凭证；上游原发布工作流在自有 fork 中禁用。

## NAS 更新

自有服务镜像 v3.0.3，小红书 API 2.8-nas.2。先备份现有配置、数据库、队列和 Telegram session，再按原目录挂载切换镜像。保留 Cookie、历史、下载素材及已有 JPEG/超时/间隔设置。直接推送自有仓库，未向上游发 PR。
