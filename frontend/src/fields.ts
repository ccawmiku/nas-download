export type Spec = {
  key: string;
  label: string;
  type?: "number" | "boolean" | "list" | "password" | "text";
  hint?: string;
  value?: unknown;
};
export const definitions: Record<string, Record<string, Spec[]>> = {
  telegram: {
    账号与访问: [
      { key: "api_id", label: "Telegram API ID", type: "number" },
      {
        key: "api_hash",
        label: "API Hash",
        type: "password",
        hint: "留空保留现有值",
      },
      {
        key: "bot_token",
        label: "Bot Token",
        type: "password",
        hint: "留空保留现有值",
      },
      {
        key: "allowed_user_ids",
        label: "允许使用的用户 ID",
        type: "list",
        hint: "每行一个；留空允许所有用户",
      },
      { key: "admin_user_ids", label: "机器人管理员 ID", type: "list" },
    ],
    下载与队列: [
      { key: "queue_maxsize", label: "最大排队数", type: "number", value: 100 },
      {
        key: "max_auto_retries",
        label: "下载自动重试次数",
        type: "number",
        value: 3,
      },
      {
        key: "progress_interval_seconds",
        label: "进度更新间隔（秒）",
        type: "number",
        value: 3,
      },
      {
        key: "max_filename_stem_length",
        label: "文件名最大长度",
        type: "number",
        value: 120,
      },
    ],
    归档目录: [
      {
        key: "image_download_dir",
        label: "图片目录",
        value: "/downloads/images",
      },
      {
        key: "video_download_dir",
        label: "视频目录",
        value: "/downloads/videos",
      },
      {
        key: "file_download_dir",
        label: "其他文件目录",
        value: "/downloads/files",
      },
    ],
  },
  xhs: {
    下载内容: [
      {
        key: "upstream.image_download",
        label: "下载图片",
        type: "boolean",
        value: true,
      },
      {
        key: "upstream.video_download",
        label: "下载视频",
        type: "boolean",
        value: true,
      },
      {
        key: "upstream.live_download",
        label: "下载 Live Photo 视频",
        type: "boolean",
        value: true,
      },
      {
        key: "upstream.video_cover_download",
        label: "下载视频封面",
        type: "boolean",
        value: false,
      },
      {
        key: "upstream.image_format",
        label: "上游图片格式",
        value: "auto",
        hint: "auto 保持来源格式；工作区仅将 PNG 转为 JPEG",
      },
      {
        key: "upstream.video_preference",
        label: "视频版本偏好",
        value: "resolution",
      },
    ],
    归档与命名: [
      { key: "upstream.work_path", label: "归档根目录", value: "/xhs" },
      { key: "upstream.folder_name", label: "归档子目录", value: "Download" },
      {
        key: "upstream.name_format",
        label: "文件命名格式",
        value: "发布时间 作者昵称 作品标题",
      },
      {
        key: "upstream.folder_mode",
        label: "每个作品单独建目录",
        type: "boolean",
        value: true,
      },
      {
        key: "upstream.author_archive",
        label: "按作者建目录",
        type: "boolean",
        value: false,
      },
      {
        key: "upstream.write_mtime",
        label: "保留发布时间",
        type: "boolean",
        value: false,
      },
    ],
    请求与重试: [
      {
        key: "request_delay_seconds",
        label: "作品间隔（秒）",
        type: "number",
        value: 1,
      },
      {
        key: "upstream.timeout",
        label: "请求超时（秒）",
        type: "number",
        value: 30,
      },
      {
        key: "upstream.max_retry",
        label: "上游下载重试次数",
        type: "number",
        value: 5,
      },
      {
        key: "retry_failed",
        label: "自动重试新内容的失败下载",
        type: "boolean",
        value: true,
        hint: "旧记录不自动重试；可在下载页手动重试",
      },
      {
        key: "max_download_attempts",
        label: "最大下载尝试次数",
        type: "number",
        value: 0,
        hint: "0 为不限次数",
      },
      {
        key: "network_retry_delay_seconds",
        label: "失败后重试等待（秒）",
        type: "number",
        value: 300,
      },
    ],
  },
  x: {
    同步停止: [
      {
        key: "known_stop_consecutive",
        label: "连续已下载停止数",
        type: "number",
        value: 10,
        hint: "首次同步也生效；按唯一作品计数",
      },
      {
        key: "stop_marker.enabled",
        label: "启用指定停止标记",
        type: "boolean",
        value: true,
      },
      { key: "stop_marker.url", label: "停止推文链接", value: "" },
    ],
    收藏来源: [
      {
        key: "browser.screen_name",
        label: "X 用户名",
        hint: "不包含 @；可从 Cookie 自动获取",
      },
      {
        key: "browser.likes_url",
        label: "收藏页面地址",
        hint: "留空使用用户名的 likes 页面",
      },
      {
        key: "request_delay_seconds",
        label: "请求间隔（秒）",
        type: "number",
        value: 3,
      },
      {
        key: "jitter_seconds",
        label: "随机等待（秒）",
        type: "number",
        value: 2,
      },
    ],
    媒体与重试: [
      {
        key: "retry_failed",
        label: "自动重试失败下载",
        type: "boolean",
        value: true,
      },
      {
        key: "max_download_attempts",
        label: "最大下载尝试次数",
        type: "number",
        value: 0,
        hint: "0 为不限次数",
      },
      { key: "media.video_format", label: "视频下载格式", value: "bv*+ba/b" },
      {
        key: "media.convert_gif",
        label: "将动图转为 GIF",
        type: "boolean",
        value: true,
      },
      {
        key: "redownload_missing_files",
        label: "重下已记录但丢失的文件",
        type: "boolean",
        value: false,
      },
    ],
  },
  pixiv: {
    同步停止: [
      {
        key: "stop_after_consecutive_done",
        label: "连续已下载停止数",
        type: "number",
        value: 20,
        hint: "公开和私密收藏分别计数；首次同步也生效",
      },
      {
        key: "stop_marker.enabled",
        label: "启用指定停止标记",
        type: "boolean",
        value: true,
      },
      { key: "stop_marker.url", label: "停止作品链接", value: "" },
    ],
    收藏来源: [
      {
        key: "restrict",
        label: "收藏范围",
        type: "list",
        value: ["public", "private"],
        hint: "public 为公开收藏，private 为非公开收藏",
      },
      {
        key: "request_delay_seconds",
        label: "请求间隔（秒）",
        type: "number",
        value: 1,
      },
      {
        key: "download_delay_seconds",
        label: "下载间隔（秒）",
        type: "number",
        value: 1,
      },
    ],
    媒体与重试: [
      {
        key: "media.download_images",
        label: "下载插画和漫画",
        type: "boolean",
        value: true,
      },
      {
        key: "media.download_ugoira",
        label: "下载动图",
        type: "boolean",
        value: true,
      },
      { key: "media.ugoira_format", label: "动图保存格式", value: "gif" },
      {
        key: "retry_failed",
        label: "自动重试失败下载",
        type: "boolean",
        value: true,
      },
      {
        key: "max_download_attempts",
        label: "最大下载尝试次数",
        type: "number",
        value: 0,
      },
      {
        key: "network.api_timeout_seconds",
        label: "API 超时（秒）",
        type: "number",
        value: 60,
      },
      {
        key: "network.api_retries",
        label: "API 重试次数",
        type: "number",
        value: 4,
      },
      {
        key: "network.download_retries",
        label: "文件下载重试次数",
        type: "number",
        value: 4,
      },
    ],
  },
  douyin: {
    同步停止: [
      {
        key: "fallback_stop_consecutive_skipped",
        label: "连续已下载停止数",
        type: "number",
        value: 50,
        hint: "图集只计一个作品；达到阈值后继续下一个来源",
      },
    ],
    媒体与命名: [
      {
        key: "defaults.folderize",
        label: "每个作品单独建目录",
        type: "boolean",
        value: true,
      },
      {
        key: "defaults.naming",
        label: "命名格式",
        value: "{create}-{nickname}-{aweme_id}",
      },
      {
        key: "defaults.cover",
        label: "下载封面",
        type: "boolean",
        value: false,
      },
      {
        key: "defaults.desc",
        label: "保存文案",
        type: "boolean",
        value: false,
      },
      {
        key: "defaults.music",
        label: "下载原声",
        type: "boolean",
        value: false,
      },
    ],
    请求与重试: [
      {
        key: "defaults.page_counts",
        label: "每页作品数",
        type: "number",
        value: 20,
      },
      {
        key: "defaults.timeout",
        label: "请求间隔 / 超时（秒）",
        type: "number",
        value: 10,
      },
      {
        key: "defaults.max_retries",
        label: "上游重试次数",
        type: "number",
        value: 5,
      },
    ],
  },
};
export const hidden =
  /^(web\.|stop_marker\.|stop_after_|known_stop_|run_interval_|run_timeout_|max_job_runtime_|fallback_stop_|browser\.(max_scrolls|safety_max_scrolls|max_idle_scrolls)|max_pages_per_restrict|api_url|api_skip_existing|xhs_api_log_file|sync_settings\.|queue_files|settings_path|download_record|upstream.cookie|upstream.download_record)/;
