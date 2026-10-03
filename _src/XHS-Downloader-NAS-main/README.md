# XHS Queue Downloader for NAS

这是 JoeanAmier/XHS-Downloader API 的轻量队列辅助服务。

当前版本不再在 NAS 端打开小红书账号页面，也不再导入旧的无头浏览器 Cookie。推荐流程是：

```text
电脑浏览器 userscript 获取作品链接
  -> POST 到 NAS Auto Download 的 /api/xhs/links
  -> 写入 /queue/links.txt 并触发 worker
  -> worker 串行提交到 xhs-api /xhs/detail
  -> xhs-api 下载到 /xhs
```

## Cookie

如果 JoeanAmier/XHS-Downloader 2.7 需要 Cookie，可在本服务网页里粘贴 Cookie Header。保存后会写入：

```text
/xhs-volume/settings.json
```

字段名为：

```json
{
  "cookie": "a1=...; web_session=..."
}
```

这个 Cookie 只给 XHS-Downloader API 下载使用，不再和 NAS 无头浏览器或统一 Cookie 导入共用。

## 日志

compose 会把 `xhs-api` 输出追加到：

```text
/app/Volume/xhs-api.log
```

worker 页面会同时显示队列日志和 `xhs-api` 日志。
