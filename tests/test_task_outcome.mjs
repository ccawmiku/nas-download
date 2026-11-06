import test from "node:test";
import assert from "node:assert/strict";
import { taskOutcome } from "../frontend/src/taskOutcome.ts";

test("mixed results show partial failure using the final receipt", () => {
  assert.deepEqual(taskOutcome({ state: "failed", result: { downloaded: 72, failed: 2 } }), {
    state: "partial_failed", summary: "已下载 72 · 失败 2",
  });
});

test("only failed downloads show total failure; skipped old media do not count as success", () => {
  assert.equal(taskOutcome({ state: "failed", result: { downloaded: 0, failed: 3, skipped: 20 } }).state, "all_failed");
});

test("media handed to the workspace count as partial progress without claiming completed downloads", () => {
  assert.deepEqual(taskOutcome({ state: "failed", result: { downloaded: 0, processing: 2, failed: 1 } }), {
    state: "partial_failed", summary: "已下载 0 · 待处理 2 · 失败 1",
  });
});

test("execution exceptions with missing or empty counters do not claim all downloads failed", () => {
  for (const result of [undefined, {}, { downloaded: 0, failed: 0 }]) {
    assert.equal(taskOutcome({ state: "failed", result }).state, "execution_failed");
  }
});

test("exceptions use the last reported progress when the receipt is missing", () => {
  assert.equal(taskOutcome({ state: "failed", result: {}, progress: { downloaded: 3, failed: 0 } }).state, "partial_failed");
});

test("a final receipt takes precedence over stale progress", () => {
  assert.deepEqual(taskOutcome({ state: "failed", result: { downloaded: 0, failed: 2 }, progress: { downloaded: 5, failed: 1 } }), {
    state: "all_failed", summary: "已下载 0 · 失败 2",
  });
});

test("active, limited, cancelled, interrupted and complete states keep their meaning", () => {
  for (const state of ["queued", "running", "limited", "cancelled", "interrupted", "complete"]) {
    assert.equal(taskOutcome({ state, progress: { downloaded: 2, failed: 1 } }).state, state);
  }
});
