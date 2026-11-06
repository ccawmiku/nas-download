type Counts = { downloaded?: number; processing?: number; failed?: number };
type Task = { state: string; result?: Counts; progress?: Counts };

export function taskOutcome(task: Task) {
  // A final receipt is authoritative; progress remains useful after an exception.
  const counts = task.result?.downloaded !== undefined
    ? task.result
    : task.progress;
  const downloaded = counts?.downloaded || 0;
  const processing = counts?.processing || 0;
  const failed = counts?.failed || 0;
  let state = task.state;
  if (state === "failed") {
    state = downloaded + processing > 0
      ? "partial_failed"
      : failed > 0
        ? "all_failed"
        : "execution_failed";
  }
  const summary = counts?.downloaded !== undefined
    ? `已下载 ${downloaded}${processing ? ` · 待处理 ${processing}` : ""} · 失败 ${failed}`
    : "";
  return { state, summary };
}
