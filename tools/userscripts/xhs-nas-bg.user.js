// ==UserScript==
// @name         小红书 · 每日一次 NAS 后台自动采集
// @namespace    xhs_downloader
// @version      1.0.1
// @description  每天自动完成一次小红书点赞/收藏增量采集并提交至 NAS（当天全部完成即锁定跳过，分段完成持久化，失败限次退避）
// @crontab      */30 * * * *
// @storageName  nas_prod_xhs_v1
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
    CONFIG: 'nas_prod_xhs_config_v1',
    DAILY_STATE: 'nas_prod_xhs_daily_state',
    BG_RESULT: 'nas_prod_xhs_bg_result',
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

  const normalizeModes = (rawModes) => {
    const list = Array.isArray(rawModes)
      ? rawModes.filter((m) => m === 'liked' || m === 'saved')
      : [];
    return [...new Set(list.length ? list : ['liked', 'saved'])];
  };

  const pendingModesForToday = (configuredModes, dailyState, today) => {
    const modes = normalizeModes(configuredModes);
    const state = dailyState && typeof dailyState === 'object' ? dailyState : {};
    const doneToday =
      state.doneModesDate === today && Array.isArray(state.doneModes)
        ? new Set(state.doneModes)
        : new Set();
    return modes.filter((m) => !doneToday.has(m));
  };

  const shouldRunDailyBg = ({ config, dailyState, nowMs = Date.now(), ignoreLock = false }) => {
    const enabled = config?.dailyBgEnabled !== false;
    if (!enabled && !ignoreLock) {
      return { run: false, reason: 'disabled' };
    }
    const targetUrl = String(config?.bgProfileUrl || '').split('#')[0];
    if (!targetUrl || !/xiaohongshu\.com\/user\/profile\//i.test(targetUrl)) {
      return { run: false, reason: 'missing_profile_url' };
    }
    const today = localDateKey(nowMs);
    const state = dailyState && typeof dailyState === 'object' ? dailyState : {};
    const configuredModes = normalizeModes(config?.bgModes);
    const pendingModes = ignoreLock
      ? configuredModes
      : pendingModesForToday(configuredModes, state, today);

    if (!ignoreLock) {
      if (state.lastSuccessDate === today || pendingModes.length === 0) {
        return { run: false, reason: 'already_succeeded_today', today };
      }
      if (state.failDate === today && Number(state.failCount || 0) >= DEFAULT_MAX_DAILY_FAILS) {
        return { run: false, reason: 'daily_fail_limit_reached', today };
      }
      if (state.cooldownUntil && Number(state.cooldownUntil) > nowMs) {
        return { run: false, reason: 'cooldown_active', today };
      }
    }

    return {
      run: true,
      reason: 'ready',
      today,
      targetUrl,
      configuredModes,
      pendingModes,
    };
  };

  const markModeSuccess = (dailyState, mode, configuredModes, nowMs = Date.now(), summary = '') => {
    const today = localDateKey(nowMs);
    const prev = dailyState && typeof dailyState === 'object' ? dailyState : {};
    const targetModes = normalizeModes(configuredModes);
    const existingDone =
      prev.doneModesDate === today && Array.isArray(prev.doneModes) ? prev.doneModes : [];
    const doneModes = [...new Set([...existingDone, mode])].filter((m) =>
      targetModes.includes(m)
    );
    const allDone = targetModes.every((m) => doneModes.includes(m));
    return {
      ...prev,
      doneModesDate: today,
      doneModes,
      lastSuccessDate: allDone ? today : prev.lastSuccessDate === today ? today : '',
      lastSuccessAt: allDone ? new Date(nowMs).toISOString() : prev.lastSuccessAt || '',
      failDate: '',
      failCount: 0,
      cooldownUntil: 0,
      lastSummary: String(summary || ''),
    };
  };

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

  const buildRunTabUrl = (baseUrl, runId, mode = 'liked') => {
    const cleanBase = String(baseUrl || '').split('#')[0];
    if (!cleanBase) return '';
    const safeMode = mode === 'saved' ? 'saved' : 'liked';
    return `${cleanBase}#nas_bg_run=${encodeURIComponent(String(runId))}&nas_bg_mode=${safeMode}`;
  };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
      STORAGE_KEYS,
      DEFAULT_MAX_DAILY_FAILS,
      RETRY_COOLDOWN_MS,
      localDateKey,
      normalizeModes,
      pendingModesForToday,
      shouldRunDailyBg,
      markModeSuccess,
      markDailyFailure,
      buildRunTabUrl,
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

  const runSingleMode = async (targetUrl, mode) => {
    const runId = `xhs_daily_${mode}_${Date.now()}`;
    const tabUrl = buildRunTabUrl(targetUrl, runId, mode);
    let tab = null;

    try {
      const resultPromise = awaitBgResult({
        storageKey: STORAGE_KEYS.BG_RESULT,
        runId,
        timeoutMs: TAB_TIMEOUT_MS,
        timeoutMessage: `小红书(${mode})后台采集超时 (150s)`,
      });
      tab = GM_openInTab(tabUrl, { active: false, insert: true });
      const result = await resultPromise;

      if (result.status !== 'ok') {
        throw new Error(result.reason || `小红书(${mode})后台采集返回失败`);
      }
      return result;
    } finally {
      const closeTabSafe = () => {
        try {
          tab?.close?.();
        } catch {}
      };
      closeTabSafe();
      await sleep(300);
      closeTabSafe();
    }
  };

  return (async () => {
    const config = await getValue(STORAGE_KEYS.CONFIG, {});
    let dailyState = await getValue(STORAGE_KEYS.DAILY_STATE, {});

    if (!config?.token) {
      return 'skipped:no_nas_token';
    }
    const decision = shouldRunDailyBg({ config, dailyState, nowMs: Date.now() });
    if (!decision.run) {
      return `skipped:${decision.reason}`;
    }

    // 与 X 后台错开 18-38 秒启动，避免同一分钟唤醒并发抢占桌面标签资源
    await sleep(18000 + Math.floor(Math.random() * 20000));

    let totalSubmitted = 0;
    try {
      for (const mode of decision.pendingModes) {
        const res = await runSingleMode(decision.targetUrl, mode);
        totalSubmitted += Number(res.itemCount || 0);
        const summary = `${mode}:${res.reason || 'ok'} (+${res.itemCount || 0})`;
        dailyState = markModeSuccess(
          dailyState,
          mode,
          decision.configuredModes,
          Date.now(),
          summary
        );
        await setValues({ [STORAGE_KEYS.DAILY_STATE]: dailyState });
        await sleep(2500);
      }
      if (totalSubmitted > 0) {
        notify('小红书今日自动同步完成', `已向 NAS 提交 ${totalSubmitted} 个新作品。`);
      }
      return `ok:${totalSubmitted}`;
    } catch (error) {
      const msg = error?.message || String(error);
      await setValues({
        [STORAGE_KEYS.DAILY_STATE]: markDailyFailure(dailyState, Date.now(), msg),
      });
      notify('小红书自动同步异常（未推进断点）', msg);
      return `failed:${msg}`;
    }
  })();
})();
