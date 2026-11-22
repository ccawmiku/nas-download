# 小红书 NAS 下载工作台

安装或更新两个脚本。工作台为 `tools/userscripts/xhs-docker-queue.user.js`，每日调度脚本为 `tools/userscripts/xhs-nas-bg.user.js`。两个脚本沿用已有账号、连接和调度配置。

## 点赞与收藏增量同步

1. 登录小红书，打开账号主页，在「NAS 工作台」选择点赞或收藏。
2. 点击「读取已加载」核对当前列表，或者用「自动加载」继续读取后续页面。
3. 工作台将作品 ID 分批发给 NAS 核对，并为各作品显示归档和处理状态。
4. 点击「发送待下载作品」。工作台发送缺失作品的完整链接，保留 `xsec_token`。NAS 再按 ID 检查一次并原子入队，点赞、收藏和手动提交共用下载记录。

每日后台脚本使用同一工作台采集与提交，按所选分类读取，直到页面没有新增或达到设置的滚动上限。每次后台运行及前台打开点赞/收藏页面时，都会先同步 `GET /api/browser-push/errors?platform=xhs` 识别待处理的失败/暂缓作品：
- **滚动追捕与增量共用（`12/18` → `18/18`）**：在点赞/收藏页向下滚动或自动加载时，悬浮按钮与面板实时统计失败项的新鲜 `xsec_token` 捕获进度；开启「追捕全部失败项」后，当前分类未追齐前不会提前触发连续已归档早停，追齐或合并提交时与常规增量作品一并携带 `force_retry: true` 发给 NAS，也可在面板点击「一键推送已捕获失败项」或直接使用升级为新鲜链接的「打开新鲜链接」。
- 已有归档或正在处理的作品会跳过，正常下载完成由 NAS 文件处理和归档状态确认。

## 失败重试与详情抓图直推（方式 A）

- 工作台「NAS 失败补抓与重试」支持与错误列表联动：除了直接在点赞/收藏流中滚动刷回新鲜链接重推，开启「打开失败作品页时自动抓图直推 NAS（方式 A）」后，点击任意条目的详情页链接，脚本会依次尝试 `/feed` API Hook、Vue/Pinia `noteDetailMap` 与详情页 DOM 媒体提取官方 CDN 原图/无水印视频，并通过 `POST /api/xhs/direct` 直推 NAS 下载。
- 也可在任意作品详情页手动点击「详情抓图直推 NAS」提交直推清单；若某个失败作品无需再重下，可点击「忽略」调用 `POST /api/browser-push/errors/dismiss`（或控制台错误列表）隐藏条目。

## 单个作品与 Docker 目录

- 打开作品后点击「使用当前作品」再发送，或粘贴带签名参数的完整作品链接。
- 列表中每张卡片也可「单独发送」，由 NAS 按作品 ID 检查是否需要下载。
- Docker 控制台 → 小红书 → 归档与命名 → 单个作品保存目录，对应配置键 `single_work_path`。
- 留空默认使用上游 `work_path` 下的 `single`（例如 `/xhs/single`）；上游的作品子目录和文件命名规则继续生效。
- 自定义值必须是容器内绝对路径。使用现有 `/xhs` 挂载内的路径无需修改 Compose；如选择挂载外的新路径，必须将其同样挂载到 xhs 和 media 服务，且加入控制台可浏览的媒体根目录。
- 单个发送同样按作品 ID 查重。尚未归档且没有在处理的作品可以按单个任务入队。
- 下载、工作区处理及失败重试都保留 single 保存类型。旧脚本不传保存类型时维持批量目录。

接口保持 `POST /api/xhs/links`，请求示例：

```json
{"urls":["https://www.xiaohongshu.com/explore/作品ID?xsec_token=签名"],"destination":"single"}
```

`destination` 可为 `batch`（默认）或 `single`；single 请求只能包含一条链接。浏览器授权仍使用 `X-NAS-Submit-Token`。目录由 Docker 设置决定，浏览器不能提交任意保存路径。

## 验证

```sh
node --test tests/test_xhs_userscript.cjs
python -m pytest tests/test_v4.py -q
cd frontend && npm ci && npm run build
```

可选浏览器回归：安装 Node 的 `playwright` 包并执行 `npx playwright install chromium` 后运行 `node tests/xhs_browser_smoke.cjs`；非默认浏览器路径可用 `PLAYWRIGHT_EXECUTABLE_PATH` 指定。

浏览器验证使用模拟页面状态和 NAS 响应，覆盖按 ID 核对、只发送缺失作品、保留签名、分类路由、单个作品类型和后台回执。真实小红书页面依赖其 DOM 标签和 `__INITIAL_STATE__` 结构；平台改版或不可见的点赞/收藏列表会显示读取错误，需在登录页面实测。
