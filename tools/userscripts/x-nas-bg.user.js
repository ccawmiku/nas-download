// ==UserScript==
// @name         X · 每日一次 NAS 后台自动采集
// @namespace    https://yaemiko.lol/nas-download
// @version      1.0.1
// @description  每天自动执行一次 X 点赞增量直推 NAS（当天成功一次后不再重复运行，随机错峰 + 失败限次退避）
// @crontab      */30 * * * *
// @storageName  nas_prod_x_v1
// @grant        GM.getValue
// @grant        GM.setValues
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_addValueChangeListener
// @grant        GM_removeValueChangeListener
// @grant        GM_openInTab
// @grant        GM_closeInTab
// @grant        GM.closeInTab
// @grant        GM_notification
// @noframes
// ==/UserScript==

(() => {
  'use strict';

  const STORAGE_KEYS = {
    CONFIG: 'nas_prod_x_config_v1',
    DAILY_STATE: 'nas_prod_x_daily_state',
    BG_RESULT: 'nas_prod_x_bg_result',
  };

  const DEFAULT_MAX_DAILY_FAILS = 3;
  const RETRY_COOLDOWN_MS = 25 * 60 * 1000;
  const TAB_TIMEOUT_MS = 150 * 1000;

  const localDateKey = (nowMs = Date.now()) => {
    const d = new Date(nowMs);
    const yyyy = d.getFullYear();
    const mm = String(d.getMonth() + 1).padStart(2, '0');
    const dd = String(d.getDate()).padStart(2, '0');
    return `${yyyy}-${mm}-${dd}`;
  };

  const shouldRunDailyBg = ({ config, dailyState, nowMs = Date.now(), ignoreLock = false }) => {
    const enabled = config?.dailyBgEnabled !== false;
    if (!enabled && !ignoreLock) {
      return { run: false, reason: 'disabled' };
    }
    const targetUrl = String(config?.bgTargetUrl || 'https://x.com/i/history/likes').split('#')[0];
    if (!targetUrl) {
      return { run: false, reason: 'missing_target_url' };
    }
    const today = localDateKey(nowMs);
    const state = dailyState && typeof dailyState === 'object' ? dailyState : {};
    if (!ignoreLock) {
      if (state.lastSuccessDate === today) {
        return { run: false, reason: 'already_succeeded_today', today };
      }
      if (state.failDate === today && Number(state.failCount || 0) >= DEFAULT_MAX_DAILY_FAILS) {
        return { run: false, reason: 'daily_fail_limit_reached', today };
      }
      if (state.cooldownUntil && Number(state.cooldownUntil) > nowMs) {
        return { run: false, reason: 'cooldown_active', today };
      }
    }
    return { run: true, reason: 'ready', today, targetUrl };
  };

  const markDailySuccess = (dailyState, nowMs = Date.now(), summary = '') => ({
    ...(dailyState && typeof dailyState === 'object' ? dailyState : {}),
    lastSuccessDate: localDateKey(nowMs),
    lastSuccessAt: new Date(nowMs).toISOString(),
    failDate: '',
    failCount: 0,
    cooldownUntil: 0,
    lastSummary: String(summary || ''),
  });

  const markDailyFailure = (dailyState, nowMs = Date.now(), error = '') => {
    const today = localDateKey(nowMs);
    const prev = dailyState && typeof dailyState === 'object' ? dailyState : {};
    const count = prev.failDate === today ? Number(prev.failCount || 0) + 1 : 1;
    return {
      ...prev,
      failDate: today,
      failCount: count,
      cooldownUntil: nowMs + RETRY_COOLDOWN_MS,
      lastError: String(error || ''),
      lastAttemptAt: new Date(nowMs).toISOString(),
    };
  };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
      STORAGE_KEYS,
      DEFAULT_MAX_DAILY_FAILS,
      RETRY_COOLDOWN_MS,
      localDateKey,
      shouldRunDailyBg,
      markDailySuccess,
      markDailyFailure,
    };
    return;
  }

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const notify = (title, text) => {
    if (typeof GM_notification === 'function') {
      try {
        GM_notification({ title, text, timeout: 6000 });
      } catch {}
    }
  };
  const getValue = async (key, fallback) => {
    if (typeof GM !== 'undefined' && typeof GM.getValue === 'function') {
      return GM.getValue(key, fallback);
    }
    return GM_getValue(key, fallback);
  };
  const setValues = async (obj) => {
    if (typeof GM !== 'undefined' && typeof GM.setValues === 'function') return GM.setValues(obj);
    for (const [k, v] of Object.entries(obj)) GM_setValue(k, v);
  };
  const awaitBgResult = ({ storageKey, runId, timeoutMs, timeoutMessage }) =>
    new Promise((resolve, reject) => {
      let settled = false;
      let timer = null;
      let pollTimer = null;
      let listenerId = null;

      const cleanup = () => {
        if (timer) clearTimeout(timer);
        if (pollTimer) clearInterval(pollTimer);
        if (listenerId !== null && typeof GM_removeValueChangeListener === 'function') {
          try { GM_removeValueChangeListener(listenerId); } catch {}
        }
      };

      const finish = (err, val) => {
        if (settled) return;
        settled = true;
        cleanup();
        if (err) reject(err);
        else resolve(val);
      };

      timer = setTimeout(() => finish(new Error(timeoutMessage)), timeoutMs);
      listenerId = GM_addValueChangeListener(storageKey, (_n, _o, val) => {
        if (val && val.runId === runId) finish(null, val);
      });
      pollTimer = setInterval(async () => {
        try {
          const cur = await getValue(storageKey, null);
          if (cur && cur.runId === runId) finish(null, cur);
        } catch {}
      }, 800);
    });

  return (async () => {
    const config = await getValue(STORAGE_KEYS.CONFIG, {});
    const dailyState = await getValue(STORAGE_KEYS.DAILY_STATE, {});

    if (!config?.token) {
      return 'skipped:no_nas_token';
    }
    const decision = shouldRunDailyBg({ config, dailyState, nowMs: Date.now() });
    if (!decision.run) {
      return `skipped:${decision.reason}`;
    }

    // 错峰随机延迟 6-20 秒
    await sleep(6000 + Math.floor(Math.random() * 14000));

    const runId = `x_daily_${Date.now()}`;
    const tabUrl = `${decision.targetUrl}#nas_bg_run=${encodeURIComponent(runId)}`;
    let tab = null;

    try {
      const resultPromise = awaitBgResult({
        storageKey: STORAGE_KEYS.BG_RESULT,
        runId,
        timeoutMs: TAB_TIMEOUT_MS,
        timeoutMessage: 'X 后台采集超时 (150s)',
      });
      tab = GM_openInTab(tabUrl, { active: false, insert: true });
      const result = await resultPromise;

      const latestState = await getValue(STORAGE_KEYS.DAILY_STATE, dailyState);
      if (result.status === 'ok') {
        const summary = `${result.reason || 'ok'} (新增 ${result.itemCount || 0})`;
        await setValues({
          [STORAGE_KEYS.DAILY_STATE]: markDailySuccess(latestState, Date.now(), summary),
        });
        if (Number(result.itemCount || 0) > 0) {
          notify('X 今日自动同步完成', `已向 NAS 提交 ${result.itemCount} 条新推文清单。`);
        }
        return `ok:${summary}`;
      }
      throw new Error(result.reason || 'X 后台采集返回失败');
    } catch (error) {
      const msg = error?.message || String(error);
      const latestState = await getValue(STORAGE_KEYS.DAILY_STATE, dailyState);
      await setValues({
        [STORAGE_KEYS.DAILY_STATE]: markDailyFailure(latestState, Date.now(), msg),
      });
      notify('X 自动同步异常（未推进断点）', msg);
      return `failed:${msg}`;
    } finally {
      const closeTabSafe = () => {
        try { tab?.close?.(); } catch {}
      };
      closeTabSafe();
      await sleep(300);
      closeTabSafe();
    }
  })();
})();
