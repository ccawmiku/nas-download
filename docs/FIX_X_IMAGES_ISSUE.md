# X 图片下载修复与兼容性验证

## 问题与修复

列表采集只检查图片 `src`，通过正文中的 Play/Watch 判断视频，媒体下载为空时强制调用 yt-dlp；详情解析失败也会人为设置 `has_video=True`。这些逻辑会漏掉图片并把图片或纯文字推文误报为视频失败。

`_src/x-auto-download-nas-main/x_auto_worker.py` 已调整为：

- 未启用截图时，用有效的 1×1 透明 GIF 响应浏览器图片请求，让图片加载回调成功，同时保留低资源采集。截图开启时仍加载真实图片。该处理使用 [Playwright route.fulfill](https://playwright.dev/python/docs/api/class-route#route-fulfill)。
- 从 `currentSrc`、`src`、`srcset`、`data-src`、`picture source` 和照片容器提取、去重媒体 ID；只在媒体容器或含内容警告的推文内展开明确的显示按钮。多次滚动合并时保持图片顺序。
- 视频识别只使用视频元素、播放器组件和视频缩略图。
- 列表没有下载到媒体时，最多再探测一次详情页；详情采集短暂等待异步挂载。只有确认视频时才调用 yt-dlp，媒体仍为空则报告 `no downloadable media found in tweet`。
- 复用 requests Session 并在任务结束关闭；新默认候选优先原图，已有自定义候选顺序仍有效。失败或空响应不留下 `.part` 文件。
- 多图部分失败保持 `failed`，保留成功文件供重试复用；混合媒体的视频失败也保留已下载图片。
- 数据库每次操作结束关闭连接，保持原有事务提交和回滚行为。

原有配置键、SQLite 表结构、下载目录、Cookie 文件格式、视频下载命令、GIF 转换及独立部署入口均保留。

## 实际推文验收（2026-10-06）

使用已保存的账号 Cookie 在本地浏览器打开详情页，运行修改后的 Downloader，输出写入本机 Codex 临时测试目录下的 `downloads/images`。NAS 仅用于读取 Cookie，没有替换线上代码或修改线上下载记录；临时 Cookie 在测试结束删除。

| 推文 | 提取的媒体 ID | 结果 | 解码尺寸 |
| --- | --- | --- | --- |
| `https://x.com/_Nag1chan/status/2104881580913426735` | `HTYKoXTbMAEToXI` | done，1 张图片，无视频误判 | 2731×4096 |
| `https://x.com/Etocha_cos/status/2103481797493600303` | `HTERi7cboAAgtEs` | done，1 张图片，无视频误判 | 1536×2048 |

两张输出均通过 Pillow 解码校验。浏览器回归另覆盖多图、响应式图片、图片 onerror 移除行为、敏感内容展开、正文关键词、播放器组件和响应式视频缩略图。下载回归覆盖漏图详情探测、空媒体、已有完成记录、部分下载失败和中断清理。真实账号 Likes 长列表与实际 MP4/HLS 下载尚未做整轮验收，视频路径通过回归检查保持原有调用行为。

## 其他核实与修复

- 抖音：保留参考 Cookie 顺序，将新的 ASCII Cookie 字段追加到末尾，覆盖 worker 和统一控制台的导入及 YAML 输出路径。日志出现明确重定向或认证门户特征时给出可能的网络原因；普通 login 文本不会触发告警，也不直接认定宽带欠费。
- Pixiv：按 [FFmpeg 引号规则](https://ffmpeg.org/ffmpeg-utils.html#Quoting-and-escaping) 转义 concat 帧路径，使用绝对路径避免相对路径被重复拼接，并验证元数据帧位于解压目录。默认 GIF 与已有 palettegen/paletteuse 优化保留，没有引入新输出格式。
- Telegram：在已有 scandir/top-200 和线程扫描改动上增加一个有锁的 30 秒有界缓存，目录配置变更立即重扫，返回结果独立复制；外部新增、删除文件最多延迟 30 秒显示。原有短进程图片预览改动保留。
- 工程：保留 `_src` 的独立构建文件，CI 的两个 Python 服务分别安装各自镜像的锁定依赖，避免 f2 与 Telegram 开发依赖的 pytest/pytest-asyncio 锁定版本冲突；增加集成与 Telegram 镜像构建及导入检查，不迁移 Python 包结构。没有更换 X 图片下载为 gallery-dl，以保持文件命名、重试和目录行为。
- 前端：`npm audit` 发现 source-map-js 1.2.1 的高危拒绝服务问题，只更新锁文件到补丁版本 1.2.2；详见 [GitHub 安全公告](https://github.com/advisories/GHSA-68fv-2mgg-jv7q)。

## 验证边界

本地检查：Python 单元与集成测试 48 项通过，Telegram pytest 56 项通过，前端构建与 npm audit 通过（0 项漏洞），另完成依赖及编译检查。Windows 的 Docker 服务没有运行，新增镜像构建检查需要在 CI 执行，不能把本地测试当作镜像构建已通过。修复尚未发布镜像或部署到 NAS。
