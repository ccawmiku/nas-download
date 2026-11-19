// ==UserScript==
// @name         X · 每日一次 NAS 后台自动采集
// @namespace    https://yaemiko.lol/nas-download
// @version      1.0.2
// @description  每天自动执行一次 X 点赞增量直推 NAS（采用原生 once 每日锁 + 手动运行一次必执行，支持 NAS 接口回执轮询与失败自动重试）
// @crontab      * * once * *
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
// @grant        GM_xmlhttpRequest
// @grant        GM_notification
// @grant        GM_log
// @connect      *
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

  const endpoint = (host, port, path) => {
    const rawHost = String(host || '192.168.1.20').trim();
    const rawPort = String(port || '14001').trim();
    const url = new URL(/^https?:\/\//i.test(rawHost) ? rawHost : `http://${rawHost}`);
    if (rawPort && !url.port) url.port = rawPort;
    url.pathname = String(path || '').startsWith('/') ? path : `/${path || ''}`;
    url.search = '';
    url.hash = '';
    return url.href;
  };

  const canScheduleRetry = (dailyState, nowMs = Date.now()) => {
    const today = localDateKey(nowMs);
    const prev = dailyState && typeof dailyState === 'object' ? dailyState : {};
    const count = prev.failDate === today ? Number(prev.failCount || 0) : 0;
    return count < DEFAULT_MAX_DAILY_FAILS;
  };

  const shouldRunDailyBg = ({ config, nowMs = Date.now() }) => {
    const targetUrl = String(config?.bgTargetUrl || 'https://x.com/i/history/likes').split('#')[0];
    if (!targetUrl) {
      return { run: false, reason: 'missing_target_url' };
    }
    return { run: true, reason: 'ready', today: localDateKey(nowMs), targetUrl };
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
      endpoint,
      canScheduleRetry,
      shouldRunDailyBg,
      markDailySuccess,
      markDailyFailure,
    };
    return;
  }

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const log = (msg) => {
    if (typeof GM_log === 'function') {
      try { GM_log(String(msg)); } catch {}
    }
  };
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
  const gmGetJson = (url, token) =>
    new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method: 'GET',
        url,
        headers: { 'X-NAS-Submit-Token': token || '' },
        timeout: 15000,
        onload(resp) {
          if (resp.status < 200 || resp.status >= 300) {
            reject(new Error(`HTTP ${resp.status}`));
            return;
          }
          try {
            resolve(JSON.parse(resp.responseText || '{}'));
          } catch (err) {
            reject(err);
          }
        },
        onerror: () => reject(new Error('network error')),
        ontimeout: () => reject(new Error('timeout')),
      });
    });

  const fetchNasBgRun = async (config, runId) => {
    if (!config?.token || !runId) return null;
    try {
      const url = endpoint(config.host, config.port, `/api/browser-push/runs/${encodeURIComponent(runId)}`);
      const data = await gmGetJson(url, config.token);
      if (data?.found && data.run) return { ...data.run, runId };
    } catch {}
    return null;
  };

  const awaitBgResult = ({ config, storageKey, runId, openTab, timeoutMs, timeoutMessage }) =>
    new Promise((resolve, reject) => {
      let settled = false;
      let timer = null;
      let pollTimer = null;
      let nasTimer = null;
      let closeTimer = null;
      let listenerId = null;

      const cleanup = () => {
        if (timer) clearTimeout(timer);
        if (pollTimer) clearInterval(pollTimer);
        if (nasTimer) clearInterval(nasTimer);
        if (closeTimer) clearTimeout(closeTimer);
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

      const checkOnce = async () => {
        try {
          const cur = await getValue(storageKey, null);
          if (cur && cur.runId === runId) {
            finish(null, cur);
            return true;
          }
        } catch {}
        const nasRun = await fetchNasBgRun(config, runId);
        if (nasRun) {
          finish(null, nasRun);
          return true;
        }
        return false;
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
      }, 900);
      nasTimer = setInterval(async () => {
        const nasRun = await fetchNasBgRun(config, runId);
        if (nasRun) finish(null, nasRun);
      }, 2000);

      const tab = openTab();
      if (tab && typeof tab === 'object') {
        tab.onclose = () => {
          if (settled || closeTimer) return;
          closeTimer = setTimeout(async () => {
            if (await checkOnce()) return;
            setTimeout(async () => {
              if (await checkOnce()) return;
              finish(new Error('X 后台标签页已关闭，未收到 NAS 或本地回执'));
            }, 1500);
          }, 800);
        };
      }
    });

  return (async () => {
    const config = await getValue(STORAGE_KEYS.CONFIG, {});
    const dailyState = await getValue(STORAGE_KEYS.DAILY_STATE, {});

    if (!config?.token) {
      log('[X-NAS-BG] skipped:no_nas_token');
      return 'skipped:no_nas_token';
    }
    const decision = shouldRunDailyBg({ config, nowMs: Date.now() });
    if (!decision.run) {
      log(`[X-NAS-BG] skipped:${decision.reason}`);
      return `skipped:${decision.reason}`;
    }

    log('[X-NAS-BG] starting background collection');
    // 轻微错峰延迟 2-5 秒，兼顾手点“运行一次”响应速度与定时错峰
    await sleep(2000 + Math.floor(Math.random() * 3000));

    const runId = `x_daily_${Date.now()}`;
    const tabUrl = `${decision.targetUrl}#nas_bg_run=${encodeURIComponent(runId)}&nas_bg_ignore_lock=1`;
    let tab = null;

    try {
      const result = await awaitBgResult({
        config,
        storageKey: STORAGE_KEYS.BG_RESULT,
        runId,
        timeoutMs: TAB_TIMEOUT_MS,
        timeoutMessage: 'X 后台采集超时 (150s)',
        openTab: () => {
          tab = GM_openInTab(tabUrl, { active: false, insert: true });
          return tab;
        },
      });

      const latestState = await getValue(STORAGE_KEYS.DAILY_STATE, dailyState);
      if (result.status === 'ok') {
        const summary = `${result.reason || 'ok'} (新增 ${result.itemCount || 0})`;
        await setValues({
          [STORAGE_KEYS.DAILY_STATE]: markDailySuccess(latestState, Date.now(), summary),
        });
        log(`[X-NAS-BG] ok:${summary}`);
        if (Number(result.itemCount || 0) > 0) {
          notify('X 今日自动同步完成', `已向 NAS 提交 ${result.itemCount} 条新推文清单。`);
        }
        return `ok:${summary}`;
      }
      throw new Error(result.reason || 'X 后台采集返回失败');
    } catch (error) {
      const msg = error?.message || String(error);
      const latestState = await getValue(STORAGE_KEYS.DAILY_STATE, dailyState);
      const allowRetry = canScheduleRetry(latestState, Date.now());
      await setValues({
        [STORAGE_KEYS.DAILY_STATE]: markDailyFailure(latestState, Date.now(), msg),
      });
      log(`[X-NAS-BG] failed:${msg}`);
      notify('X 自动同步异常（未推进断点）', msg);
      if (allowRetry && typeof CATRetryError === 'function') {
        throw new CATRetryError(msg, RETRY_COOLDOWN_MS / 1000);
      }
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
