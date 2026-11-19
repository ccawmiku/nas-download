// ==UserScript==
// @name         小红书 · NAS 下载工作台
// @namespace    xhs_downloader
// @version      3.2.2
// @description  点赞/收藏自动保存最近五作品，按顺序增量截止且点赞与收藏分目录独立归档，支持每日一次后台静默直推、NAS 推送回执及自动关闭标签
// @match        https://www.xiaohongshu.com/*
// @storageName  nas_prod_xhs_v1
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM.getValue
// @grant        GM.setValues
// @grant        GM_addValueChangeListener
// @grant        GM_removeValueChangeListener
// @grant        GM_openInTab
// @grant        GM_closeInTab
// @grant        GM.closeInTab
// @grant        GM_xmlhttpRequest
// @grant        GM_registerMenuCommand
// @grant        GM_notification
// @grant        unsafeWindow
// @grant        window.close
// @connect      *
// @license      GPL-3.0
// @run-at       document-end
// @noframes
// ==/UserScript==

// Rebuilt for ccawmiku/nas-download; source project: JoeanAmier/XHS-Downloader (GPL-3.0).
(() => {
    'use strict';

    const STORAGE_KEYS = {
        CONFIG: 'nas_prod_xhs_config_v1',
        DAILY_STATE: 'nas_prod_xhs_daily_state',
        BG_RESULT: 'nas_prod_xhs_bg_result',
    };

    const unwrap = value => value?._rawValue ?? value?.value ?? value;
    const noteId = value =>
        String(value || '').match(
            /(?:\/explore\/|\/discovery\/item\/|\/user\/profile\/[^/?#]+\/|^)([a-f\d]{24})(?:[/?#]|$)/i
        )?.[1] || '';
    const accountId = path => String(path || '').match(/\/user\/profile\/([^/?#]+)/)?.[1] || '';

    const range = (items, cutoff) => {
        if (!cutoff) return items;
        const index = items.findIndex(item => item.id === cutoff);
        if (index < 0) throw new Error('未找到截止作品。请继续加载、重新选择或清除截止点，本次没有发送。');
        return items.slice(0, index);
    };

    // Match the surviving saved IDs in their original order; missing/deleted IDs
    // are allowed, but one survivor from a multi-note checkpoint is insufficient.
    const checkpointMatch = (items, anchors) => {
        const positions = new Map(items.map((item, index) => [item.id, index]));
        const found = anchors.filter(item => positions.has(item.id)).map(item => positions.get(item.id));
        const ordered = found.every((position, index) => index === 0 || position > found[index - 1]);
        const matched = anchors.length > 0 && found.length >= Math.min(2, anchors.length) && ordered;
        return {matched, index: matched ? found[0] : -1, count: found.length, ordered};
    };

    const batchRange = (items, cutoff, anchors = []) => {
        const auto = checkpointMatch(items, anchors);
        const manual = cutoff ? items.findIndex(item => item.id === cutoff) : -1;
        const stops = [manual, auto.index].filter(index => index >= 0);
        if (stops.length) return items.slice(0, Math.min(...stops));
        if (anchors.length) {
            throw new Error(
                '自动截止尚未匹配：需至少两个已保存作品保持原顺序（初次仅保存一个时匹配一个）。请继续加载、设置手动截止或清除自动记录，本次没有发送。'
            );
        }
        return range(items, cutoff);
    };

    // A short incremental batch carries forward older confirmed IDs to keep five
    // anchors available when only one or two new notes have been added.
    const nextCheckpoint = (sent, previous = []) => {
        const unique = new Map();
        for (const item of [...sent, ...previous]) {
            if (!unique.has(item.id)) unique.set(item.id, {id: item.id, title: item.title || item.id});
            if (unique.size === 5) break;
        }
        return [...unique.values()];
    };

    const endpoint = (host, port, path) => {
        const url = new URL(/^https?:\/\//i.test(host) ? host : `http://${host}`);
        if (port && !url.port) url.port = String(port);
        url.pathname = String(path || '').startsWith('/') ? path : `/${path || ''}`;
        url.search = '';
        url.hash = '';
        return url.href;
    };

    const redactXhsUrl = rawUrl =>
        String(rawUrl || '').replace(/([?&]xsec_token=)([^&#]*)/gi, (_m, prefix, val) =>
            /^<已隐藏:/.test(val || '')
                ? `${prefix}${val}`
                : `${prefix}<已隐藏:长度${decodeURIComponent(val || '').length}>`
        );

    const parseBgRunHash = hash => {
        const h = String(hash || '');
        const runMatch = h.match(/nas_bg_run=([a-zA-Z0-9_-]+)/);
        const modeMatch = h.match(/nas_bg_mode=(liked|saved)/);
        return {
            runId: runMatch ? runMatch[1] : '',
            mode: modeMatch ? modeMatch[1] : 'liked',
        };
    };

    const makeBatchId = (prefix = 'xhs') => {
        try {
            if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
                return `${prefix}_${crypto.randomUUID()}`;
            }
        } catch {}
        return `${prefix}_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`;
    };

    const REPORT_PLATFORM = 'xhs';

    const extractErrorMessage = (statusCode, responseText) => {
        let detail = '';
        const raw = String(responseText || '').trim();
        if (raw) {
            try {
                const parsed = JSON.parse(raw);
                if (typeof parsed?.detail === 'string') {
                    detail = parsed.detail.trim();
                } else if (Array.isArray(parsed?.detail) && parsed.detail.length) {
                    detail = parsed.detail.map(item => item?.msg || JSON.stringify(item)).join('; ');
                } else if (typeof parsed?.message === 'string') {
                    detail = parsed.message.trim();
                }
            } catch {
                detail = raw.slice(0, 160);
            }
        }
        return detail
            ? `Docker HTTP ${statusCode}：${detail}`
            : `Docker HTTP ${statusCode}，请检查地址、授权和服务版本。`;
    };

    // Pure helpers are also exercised by the Node regression tests.
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = {
            STORAGE_KEYS,
            REPORT_PLATFORM,
            noteId,
            accountId,
            range,
            endpoint,
            checkpointMatch,
            batchRange,
            nextCheckpoint,
            redactXhsUrl,
            parseBgRunHash,
            extractErrorMessage,
        };
        return;
    }

    const modes = {liked: '点赞', saved: '收藏', published: '发布', page: '当前页面'};
    const indices = {published: 0, saved: 1, liked: 2};
    const TAB_NAMES = {
        liked: ['点赞', '赞过', 'Liked', 'Likes'],
        saved: ['收藏', 'Collected', 'Saved'],
        published: ['笔记', '发布', 'Notes', 'Posts'],
    };

    const gmSetAll = async obj => {
        if (typeof GM !== 'undefined' && typeof GM.setValues === 'function') return GM.setValues(obj);
        for (const [k, v] of Object.entries(obj)) GM_setValue(k, v);
    };

    const loadConfig = () => {
        const saved = GM_getValue(STORAGE_KEYS.CONFIG, null) || {};
        const modesList = Array.isArray(saved.bgModes) && saved.bgModes.length
            ? saved.bgModes.filter(m => m === 'liked' || m === 'saved')
            : ['liked', 'saved'];
        return {
            host: String(saved.host ?? GM_getValue('dockerHost', '192.168.1.20')),
            port: String(saved.port ?? GM_getValue('dockerPort', '14001')),
            path: String(saved.path ?? GM_getValue('dockerPath', '/api/xhs/links')),
            token: String(saved.token ?? GM_getValue('nasSubmitToken', '')),
            max: Number(saved.max ?? GM_getValue('nasMaxScrolls', 100)) || 100,
            maxBgScrolls: Number(saved.maxBgScrolls ?? 25) || 25,
            dailyBgEnabled: saved.dailyBgEnabled !== false,
            bgModes: modesList.length ? modesList : ['liked', 'saved'],
            bgProfileUrl: String(saved.bgProfileUrl || ''),
        };
    };

    let config = loadConfig();
    const saveConfig = async nextCfg => {
        config = {...config, ...nextCfg};
        await gmSetAll({
            [STORAGE_KEYS.CONFIG]: config,
            dockerHost: config.host,
            dockerPort: config.port,
            dockerPath: config.path,
            nasSubmitToken: config.token,
            nasMaxScrolls: config.max,
        });
    };

    const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
    const pageWin = typeof unsafeWindow !== 'undefined' ? unsafeWindow : window;

    const findVueRef = name => {
        if (pageWin?.__INITIAL_STATE__?.[name]) {
            return unwrap(pageWin.__INITIAL_STATE__[name]);
        }
        const app = document.querySelector('#app')?.__vue_app__;
        const pinia = app?.config?.globalProperties?.$pinia?._s;
        if (!pinia) return null;
        for (const store of pinia.values()) {
            if (store && Object.prototype.hasOwnProperty.call(store, name)) {
                return unwrap(store[name]);
            }
        }
        return null;
    };

    const loggedInUserId = () => {
        const u = findVueRef('user');
        const info = unwrap(u?.userInfo) || {};
        return String(info.userId || info.user_id || info.id || '');
    };

    const detectXhsAnomaly = () => {
        if (/\/login\b/.test(location.pathname)) return '小红书跳转至登录页，请重新登录';
        const loginModal = document.querySelector('.login-modal, [class*="login-container"]');
        const bodyText = document.body?.innerText || '';
        if (loginModal && /扫码登录|手机号登录/.test(bodyText)) {
            return '小红书弹出登录验证窗口，会话可能已过期';
        }
        if (/请先验证|滑动验证|安全验证/.test(bodyText)) {
            return '触发小红书安全/滑块风控验证，请人工在浏览器完成验证后重试';
        }
        return '';
    };

    const collectDomLinkMap = () => {
        const linkMap = new Map();
        const nodes = document.querySelectorAll(
            'a[href*="/explore/"], a[href*="/discovery/item/"], a[href*="/user/profile/"][href*="xsec_token"]'
        );
        for (const a of nodes) {
            const href = a.getAttribute('href') || '';
            const id = noteId(href);
            if (!id) continue;
            try {
                const u = new URL(href, location.origin);
                const token = u.searchParams.get('xsec_token') || '';
                const source = u.searchParams.get('xsec_source') || 'pc_user';
                const cardNode = a.closest('section, .note-item, [data-index]');
                const titleText =
                    cardNode?.querySelector('.title, .note-title, span')?.textContent?.trim() ||
                    a.textContent?.trim() ||
                    '';
                const canonical = new URL(`/explore/${id}`, location.origin);
                if (token) canonical.searchParams.set('xsec_token', token);
                canonical.searchParams.set('xsec_source', source);
                linkMap.set(id, {
                    id,
                    token,
                    source,
                    url: canonical.href,
                    title: titleText,
                    cover: cardNode?.querySelector('img')?.getAttribute('src') || '',
                });
            } catch {}
        }
        return linkMap;
    };

    function normalizeNote(raw, domMap) {
        const card = raw.noteCard || raw.note_card || raw;
        const id = noteId(raw.id || raw.noteId || card.noteId || card.id || '');
        if (!id) return null;
        const dom = domMap?.get(id);
        const token =
            raw.xsecToken || raw.xsec_token || card.xsecToken || card.xsec_token || dom?.token || '';
        const url = new URL(`/explore/${id}`, location.origin);
        if (token) url.searchParams.set('xsec_token', token);
        url.searchParams.set('xsec_source', dom?.source || 'pc_user');
        return {
            id,
            url: url.href,
            title: card.displayTitle || card.title || dom?.title || id,
            author: card.user?.nickName || card.user?.nickname || '',
            cover: card.cover?.urlDefault || card.cover?.url || dom?.cover || '',
        };
    }

    const readNotesForMode = activeMode => {
        const domMap = collectDomLinkMap();
        let raw;
        if (activeMode in indices) {
            if (!accountId(location.pathname)) throw new Error('请先打开要收录的小红书账号主页。');
            raw = unwrap(unwrap(findVueRef('user')?.notes)?.[indices[activeMode]]);
        } else {
            if (/\/user\/profile\//.test(location.pathname)) {
                throw new Error('账号主页请使用点赞、收藏或发布分类。');
            }
            if (location.pathname.includes('board')) {
                const id = location.pathname.split('/board/')[1]?.split('/')[0];
                raw = unwrap(findVueRef('board')?.boardFeedsMap)?.[id]?.notes;
            } else {
                raw = unwrap(
                    (location.pathname.includes('search_result') ? findVueRef('search') : findVueRef('feed'))
                        ?.feeds
                );
            }
        }
        if (Array.isArray(raw)) {
            const found = raw.map(item => normalizeNote(item, domMap)).filter(Boolean);
            if (found.length > 0 || !domMap.size) {
                return [...new Map(found.map(item => [item.id, item])).values()];
            }
        }
        if (domMap.size > 0) {
            return [...domMap.values()].map(d => ({
                id: d.id,
                url: d.url,
                title: d.title || d.id,
                author: '',
                cover: d.cover || '',
            }));
        }
        throw new Error('暂未读取到列表。请登录并打开对应分类，等作品出现后重试。');
    };

    const activateTabForMode = async activeMode => {
        if (!(activeMode in indices)) return;
        if (!accountId(location.pathname)) throw new Error('请先打开账号主页。');
        const names = TAB_NAMES[activeMode] || [];
        const tab = [...document.querySelectorAll('.reds-tab-item, [role="tab"], .tab-item')].find(node => {
            const label = node.textContent.trim().replace(/\s*[\d.,万千wWkK+]+\s*$/, '').trim();
            return names.includes(label) || names.some(n => label.includes(n));
        });
        if (!tab) throw new Error(`未找到「${modes[activeMode]}」标签，请在账号主页打开该分类后重试。`);
        tab.click();
        await sleep(1000);
    };

    const cutoffKeyFor = (acc, activeMode) => `nas:cutoff:v1:${acc}:${activeMode}`;
    const checkpointKeyFor = (acc, activeMode) => `nas:checkpoint:v1:${acc}:${activeMode}`;
    const getCutoff = (acc, activeMode) => GM_getValue(cutoffKeyFor(acc, activeMode), null);
    const getCheckpoint = (acc, activeMode) => {
        const v = GM_getValue(checkpointKeyFor(acc, activeMode), []);
        return Array.isArray(v) ? v : [];
    };

    const gmGetJson = (targetUrl, token) =>
        new Promise((resolve, reject) =>
            GM_xmlhttpRequest({
                method: 'GET',
                url: targetUrl,
                headers: {
                    'X-NAS-Submit-Token': token || '',
                },
                timeout: 15000,
                onload(response) {
                    if (response.status === 401) {
                        reject(new Error('NAS 授权失败 (401)'));
                        return;
                    }
                    if (response.status < 200 || response.status >= 300) {
                        reject(new Error(`HTTP ${response.status}`));
                        return;
                    }
                    try {
                        resolve(JSON.parse(response.responseText || '{}'));
                    } catch {
                        reject(new Error('JSON parse error'));
                    }
                },
                onerror: () => reject(new Error('network error')),
                ontimeout: () => reject(new Error('timeout')),
            })
        );

    const fetchNasBgRun = async (cfg, runId) => {
        if (!cfg?.token || !runId) return null;
        try {
            const url = endpoint(cfg.host, cfg.port, `/api/browser-push/runs/${encodeURIComponent(runId)}`);
            const data = await gmGetJson(url, cfg.token);
            if (data?.found && data.run) return {...data.run, runId};
        } catch {}
        return null;
    };

    const writeBgResult = async payload => {
        try {
            if (typeof GM_setValue === 'function') GM_setValue(STORAGE_KEYS.BG_RESULT, payload);
        } catch {}
        await gmSetAll({[STORAGE_KEYS.BG_RESULT]: payload});
    };

    const awaitBgResult = ({cfg, storageKey, runId, openTab, timeoutMs, timeoutMessage}) =>
        new Promise((resolve, reject) => {
            let settled = false;
            let timer = null;
            let localPoll = null;
            let nasPoll = null;
            let closeTimer = null;
            let listenerId = null;

            const cleanup = () => {
                if (timer) clearTimeout(timer);
                if (localPoll) clearInterval(localPoll);
                if (nasPoll) clearInterval(nasPoll);
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
                    const cur =
                        typeof GM !== 'undefined' && typeof GM.getValue === 'function'
                            ? await GM.getValue(storageKey, null)
                            : GM_getValue(storageKey, null);
                    if (cur && cur.runId === runId) {
                        finish(null, cur);
                        return true;
                    }
                } catch {}
                const nasRun = await fetchNasBgRun(cfg, runId);
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
            localPoll = setInterval(async () => {
                try {
                    const cur =
                        typeof GM !== 'undefined' && typeof GM.getValue === 'function'
                            ? await GM.getValue(storageKey, null)
                            : GM_getValue(storageKey, null);
                    if (cur && cur.runId === runId) finish(null, cur);
                } catch {}
            }, 800);
            nasPoll = setInterval(async () => {
                const nasRun = await fetchNasBgRun(cfg, runId);
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
                            finish(new Error('后台标签页已提前关闭，未收到 NAS 或本地回执'));
                        }, 1500);
                    }, 800);
                };
            }
        });

    const gmPostJson = (targetUrl, token, payload) =>
        new Promise((resolve, reject) =>
            GM_xmlhttpRequest({
                method: 'POST',
                url: targetUrl,
                headers: {
                    'Content-Type': 'application/json',
                    'X-NAS-Submit-Token': token || '',
                },
                data: JSON.stringify(payload),
                timeout: 30000,
                onload(response) {
                    if (response.status === 401) {
                        reject(new Error('NAS 授权失败 (401)：Token 无效或已轮换，请在控制台重新复制。'));
                        return;
                    }
                    if (response.status < 200 || response.status >= 300) {
                        reject(new Error(extractErrorMessage(response.status, response.responseText)));
                        return;
                    }
                    try {
                        resolve(JSON.parse(response.responseText || '{}'));
                    } catch {
                        reject(new Error('Docker 返回格式无法解析，请检查服务是否正常。'));
                    }
                },
                onerror: () => reject(new Error('无法连接 Docker，请检查主机地址、端口和局域网连通性。')),
                ontimeout: () => reject(new Error('Docker 请求超时，请先检查任务队列，避免重复发送。')),
            })
        );

    const requestSubmit = async (cfg, payload) => {
        const body = await gmPostJson(endpoint(cfg.host, cfg.port, cfg.path), cfg.token, payload);
        if (
            !body.ok ||
            body.valid !== payload.urls.length ||
            Number(body.accepted || 0) + Number(body.skipped || 0) !== payload.urls.length ||
            (Array.isArray(body.invalid) && body.invalid.length > 0)
        ) {
            throw new Error('Docker 未完整确认接收，请检查任务队列后重试。');
        }
        return body;
    };

    const reportToNas = async (cfg, report, {strict = false} = {}) => {
        try {
            return await gmPostJson(
                endpoint(cfg.host, cfg.port, '/api/browser-push/report'),
                cfg.token,
                {
                    platform: REPORT_PLATFORM,
                    source_mode: report.source_mode || 'workbench',
                    run_id: report.run_id || '',
                    batch_id: report.batch_id || '',
                    account: report.account || '',
                    status: report.status || 'ok',
                    submitted: Number(report.submitted || 0),
                    accepted: Number(report.accepted || 0),
                    skipped: Number(report.skipped || 0),
                    discovered: Number(report.discovered || 0),
                    message: String(report.message || '').slice(0, 300),
                }
            );
        } catch (err) {
            if (strict) throw err;
            return null;
        }
    };

    const scrollContainers = () => {
        const containers = [
            document.scrollingElement,
            ...document.querySelectorAll('.scroll-container, .feeds-container'),
        ].filter(Boolean);
        for (const node of containers) {
            try {
                node.scrollTo({top: node.scrollHeight, behavior: 'instant'});
                node.dispatchEvent(new Event('scroll'));
            } catch {}
        }
        try {
            window.dispatchEvent(new Event('scroll'));
        } catch {}
    };

    const pushNotes = async (
        notes,
        {
            destination,
            activeMode,
            account,
            discovered = notes.length,
            sourceMode = 'workbench',
            runId = '',
            onStatus = () => {},
        }
    ) => {
        if (!notes.length) throw new Error('范围内没有作品，本次不发送。');
        const limited = activeMode === 'liked' || activeMode === 'saved';
        const autoKey =
            destination === 'batch' && limited && account ? checkpointKeyFor(account, activeMode) : '';
        const previous = autoKey ? getCheckpoint(account, activeMode) : [];
        let sent = 0;
        let accepted = 0;
        let skipped = 0;
        let lastTaskId = '';
        const batchRoot = makeBatchId(`xhs_${activeMode}`);
        const chunkCount = Math.ceil(notes.length / 500);

        for (let offset = 0, part = 1; offset < notes.length; offset += 500, part += 1) {
            const chunk = notes.slice(offset, offset + 500);
            const chunkBatchId = chunkCount > 1 ? `${batchRoot}_p${part}` : batchRoot;
            onStatus(`正在发送 ${sent}/${notes.length}…`);
            try {
                const body = await requestSubmit(config, {
                    urls: chunk.map(item => item.url),
                    destination,
                    ...(limited ? {category: activeMode} : {}),
                    ...(runId ? {run_id: runId} : {}),
                    batch_id: chunkBatchId,
                    source_mode: sourceMode,
                    account: account ? `${account}:${activeMode}` : activeMode,
                    discovered,
                });
                accepted += Number(body.accepted || 0);
                skipped += Number(body.skipped || 0);
                if (body.task_id) lastTaskId = String(body.task_id);
            } catch (error) {
                throw new Error(`已确认 ${sent}/${notes.length}。${error.message}`);
            }
            sent += chunk.length;
        }

        let saved = false;
        if (autoKey && sent === notes.length) {
            GM_setValue(autoKey, nextCheckpoint(notes, previous));
            saved = true;
        }
        return {sent, accepted, skipped, saved, taskId: lastTaskId, batchId: batchRoot};
    };

    // ==================== 后台自动任务执行路径 (#nas_bg_run=...) ====================
    const bgRun = parseBgRunHash(location.hash);
    if (bgRun.runId) {
        (async () => {
            const targetMode = bgRun.mode === 'saved' ? 'saved' : 'liked';
            let anchorUpdated = false;
            let discoveredCount = 0;
            const acc = accountId(location.pathname);

            try {
                await sleep(2200);
                let anomaly = detectXhsAnomaly();
                if (anomaly) throw new Error(anomaly);
                if (!acc) throw new Error('当前后台页面不是小红书个人主页 (/user/profile/<id>)，请检查配置。');

                await activateTabForMode(targetMode);
                for (let wait = 0; wait < 20; wait += 1) {
                    anomaly = detectXhsAnomaly();
                    if (anomaly) throw new Error(anomaly);
                    try {
                        const probe = readNotesForMode(targetMode);
                        if (probe.length > 0) break;
                    } catch {}
                    const emptyEl = document.body?.innerText || '';
                    if (/还没有(?:赞过|点赞|收藏)/.test(emptyEl) && wait >= 4) break;
                    await sleep(500);
                }

                let loaded = [];
                try {
                    loaded = readNotesForMode(targetMode);
                } catch (err) {
                    const emptyEl = document.body?.innerText || '';
                    if (/还没有(?:赞过|点赞|收藏)/.test(emptyEl)) loaded = [];
                    else throw err;
                }

                const collected = new Map(loaded.map(it => [it.id, it]));
                const anchors = getCheckpoint(acc, targetMode);
                const cutoffMark = getCutoff(acc, targetMode)?.id || '';
                const maxBg = Math.max(1, Math.min(200, Number(config.maxBgScrolls || 25)));
                let idle = 0;

                for (let step = 0; step < maxBg; step += 1) {
                    anomaly = detectXhsAnomaly();
                    if (anomaly) throw new Error(anomaly);
                    const snapshot = [...collected.values()];
                    const matched = checkpointMatch(snapshot, anchors);
                    if ((cutoffMark && collected.has(cutoffMark)) || matched.matched) break;
                    const before = collected.size;
                    scrollContainers();
                    await sleep(1400);
                    try {
                        readNotesForMode(targetMode).forEach(it => collected.set(it.id, it));
                    } catch {}
                    idle = collected.size === before ? idle + 1 : 0;
                    if (idle >= 4) break;
                }

                const finalItems = [...collected.values()];
                discoveredCount = finalItems.length;
                if (!finalItems.length) {
                    await reportToNas(config, {
                        source_mode: 'bg',
                        run_id: bgRun.runId,
                        account: `${acc}:${targetMode}`,
                        status: 'empty',
                        discovered: 0,
                        message: `后台定时扫描小红书(${modes[targetMode]})为空，无新增作品`,
                    });
                    await writeBgResult({
                        runId: bgRun.runId,
                        platform: 'xiaohongshu',
                        mode: targetMode,
                        status: 'ok',
                        reason: 'empty_list',
                        itemCount: 0,
                        anchorUpdated: false,
                        finishedAt: new Date().toISOString(),
                    });
                    return;
                }

                const selected = batchRange(finalItems, cutoffMark, anchors);
                if (!selected.length) {
                    await reportToNas(config, {
                        source_mode: 'bg',
                        run_id: bgRun.runId,
                        account: `${acc}:${targetMode}`,
                        status: 'empty',
                        discovered: finalItems.length,
                        message: `后台定时检查小红书(${modes[targetMode]}) ${finalItems.length} 条，命中断点无新增`,
                    });
                    await writeBgResult({
                        runId: bgRun.runId,
                        platform: 'xiaohongshu',
                        mode: targetMode,
                        status: 'ok',
                        reason: 'no_new_items',
                        itemCount: 0,
                        anchorUpdated: false,
                        finishedAt: new Date().toISOString(),
                    });
                    return;
                }

                const summary = await pushNotes(selected, {
                    destination: 'batch',
                    activeMode: targetMode,
                    account: acc,
                    discovered: finalItems.length,
                    sourceMode: 'bg',
                    runId: bgRun.runId,
                });
                anchorUpdated = summary.saved;
                await writeBgResult({
                    runId: bgRun.runId,
                    platform: 'xiaohongshu',
                    mode: targetMode,
                    status: 'ok',
                    reason: 'pushed',
                    itemCount: summary.sent,
                    accepted: summary.accepted,
                    skipped: summary.skipped,
                    taskId: summary.taskId,
                    anchorUpdated,
                    finishedAt: new Date().toISOString(),
                });
            } catch (err) {
                const msg = err?.message || String(err);
                await reportToNas(config, {
                    source_mode: 'bg',
                    run_id: bgRun.runId,
                    account: `${acc || 'unknown'}:${targetMode}`,
                    status: 'error',
                    discovered: discoveredCount,
                    message: `小红书(${modes[targetMode]})后台同步失败：${msg}`,
                });
                await writeBgResult({
                    runId: bgRun.runId,
                    platform: 'xiaohongshu',
                    mode: targetMode,
                    status: 'failed',
                    reason: msg,
                    anchorUpdated,
                    finishedAt: new Date().toISOString(),
                });
            } finally {
                // 等待存储与 NAS 回执落盘后主动自关标签页，解决后台运行完成后不自动关闭的问题
                await sleep(1200);
                try { window.onbeforeunload = null; } catch {}
                try { pageWin.onbeforeunload = null; } catch {}
                try {
                    if (typeof window.close === 'function') window.close();
                } catch {}
            }
        })();
        return;
    }

    // ==================== 前台工作台 UI ====================
    let mode = 'liked';
    let items = [];
    let busy = false;
    let stopped = false;
    let context = '';

    const activeAccount = () => accountId(location.pathname);
    const cutoffKey = () => cutoffKeyFor(activeAccount(), mode);
    const cutoff = () => GM_getValue(cutoffKey(), null);
    const checkpointKey = () => checkpointKeyFor(activeAccount(), mode);
    const checkpoint = () => getCheckpoint(activeAccount(), mode);
    const limitedMode = () => ['liked', 'saved'].includes(mode);
    const currentRange = () => (limitedMode() ? batchRange(items, cutoff()?.id, checkpoint()) : items);
    const remember = item => {
        GM_setValue(cutoffKey(), item ? {id: item.id, title: item.title} : null);
        render();
    };

    const syncOwnProfileUrl = () => {
        const acc = activeAccount();
        if (!acc) return;
        const me = loggedInUserId();
        if (!config.bgProfileUrl || (me && me === acc)) {
            const cleanUrl = `${location.origin}/user/profile/${acc}`;
            if (config.bgProfileUrl !== cleanUrl) {
                saveConfig({bgProfileUrl: cleanUrl}).catch(() => {});
            }
        }
    };

    const host = document.createElement('div');
    host.id = 'nas-xhs-workbench';
    const shadow = host.attachShadow({mode: 'open'});
    shadow.innerHTML = `<style>
:host{all:initial;font:14px/1.5 system-ui,-apple-system,"Segoe UI","PingFang SC",sans-serif;color:#202736}
*{box-sizing:border-box}button,input,select{font:inherit}
button{cursor:pointer;border:1px solid #e2e6ee;background:#fff;color:#384153;border-radius:10px;padding:9px 12px;transition:background .15s}
button:hover:not(:disabled){background:#f1f4f9}button:disabled{opacity:.45;cursor:default}
button.primary{background:#e94465;color:white;border-color:#e94465}button.primary:hover:not(:disabled){background:#d63757}
button.quiet{border:0;background:transparent;padding:4px 8px;color:#64748b}
input,select{border:1px solid #dce1e9;border-radius:9px;padding:9px;background:white;color:#273045;min-width:0}
input:focus,button:focus-visible,select:focus{outline:2px solid #eb718b;outline-offset:2px}
.launch{position:fixed;left:24px;bottom:28px;z-index:2147483646;background:#e94465;color:white;border:0;box-shadow:0 6px 24px #63263730;border-radius:18px;padding:13px 18px;font-weight:650}
.panel{position:fixed;left:24px;bottom:88px;width:450px;max-width:calc(100vw - 24px);max-height:calc(100vh - 112px);z-index:2147483646;border:1px solid #e3e7ee;border-radius:22px;background:#f8fafc;box-shadow:0 20px 80px #1c294630;overflow:auto}
.hidden{display:none!important}
header{padding:19px 22px 15px;display:flex;align-items:center;justify-content:space-between;background:#fff;position:sticky;top:0;z-index:2;border-bottom:1px solid #edf0f5}
h2{font-size:18px;letter-spacing:-.4px;margin:2px 0}
small,.muted{color:#64748b;font-size:12px}
.brand{font-size:10px;letter-spacing:2px;color:#e94465;font-weight:750}
.body{padding:16px;display:grid;gap:14px}
.box{background:white;border:1px solid #e7ebf1;border-radius:15px;padding:14px}
.row{display:flex;gap:8px;align-items:center}.spread{justify-content:space-between}.grow{flex:1;min-width:0}
.tabs{display:flex;gap:5px;margin-top:8px}.tabs button{flex:1;padding:8px 4px}
.active{background:#fdebf0!important;color:#cf3655!important;border-color:#f8ccd6!important}
h3{font-size:14px;margin:0 0 9px}p{margin:6px 0 12px}
label{display:grid;gap:5px;margin:8px 0;font-size:12px;color:#475569}
label.inline{display:flex;align-items:center;gap:8px;margin:6px 0}
.cut{background:#fff4f6;border:1px solid #f9dde4;border-radius:10px;padding:10px;margin:10px 0;overflow-wrap:anywhere}
.list{max-height:310px;overflow:auto;margin-top:10px}
.note{display:flex;gap:10px;padding:10px 0;border-top:1px solid #edf0f5;align-items:center}
.cover{width:48px;height:60px;border-radius:8px;object-fit:cover;background:#eff2f6}
.title{font-size:13px;overflow-wrap:anywhere}.note button{font-size:11px;padding:5px 7px}
.actions{display:flex;gap:5px;margin-top:6px}.tag{font-size:10px;color:#df4564}
#status{white-space:pre-wrap;overflow-wrap:anywhere;padding:11px 13px;border-radius:10px;background:#edf2f8;font-size:12px;color:#334155}
#status.error{color:#b72b43;background:#fff0f2}
details summary{cursor:pointer;color:#475569;font-size:13px;font-weight:600}
progress{width:100%;height:5px;accent-color:#e94465}a{color:#ce3859;text-decoration:none}
@media(max-width:500px){.panel{left:12px;bottom:80px}.launch{left:12px;bottom:18px}}
</style>
<button class="launch">↓ NAS 工作台</button>
<section class="panel hidden" aria-label="NAS 下载工作台">
<header><div><div class="brand">REDNOTE / NAS</div><h2>把喜欢的，收进 NAS</h2><small>浏览器选作品 · Docker 继续下载 · 每日后台增量直推</small></div><button class="quiet" id="close" aria-label="关闭">✕</button></header>
<div class="body">
<div class="box"><h3>单个作品</h3><p class="muted">发送正在浏览的作品，或粘贴完整作品链接。独立保存到 single 目录。</p><div class="row"><input id="single" class="grow" placeholder="作品链接（保留 xsec_token）" aria-label="单个作品链接"><button id="send-single" class="primary">发送</button></div><button id="current" class="quiet">↗ 使用当前作品</button></div>
<div class="box">
  <div class="row spread"><h3>批量收录</h3><small id="account"></small></div>
  <div class="tabs">${Object.entries(modes).map(([value, label]) => `<button data-mode="${value}">${label}</button>`).join('')}</div>
  <div class="cut"><div class="row spread"><strong style="font-size:12px">手动截止 · 不包含</strong><button id="clear" class="quiet">清除</button></div><div id="cutoff" class="muted"></div><div class="row"><input id="cutoff-input" class="grow" placeholder="也可粘贴作品链接或 ID" aria-label="截止作品"><button id="save-cutoff">保存</button></div></div>
  <div id="auto-cut" class="cut"><div class="row spread"><strong style="font-size:12px">自动增量截止 · 最近 5 个作品</strong><button id="clear-auto" class="quiet">清除记录</button></div><div id="auto-summary" class="muted"></div><ol id="auto-notes" class="muted" style="margin:8px 0;padding-left:20px"></ol><small>发送成功后自动记住；至少两个作品顺序一致即可截止，允许作品被删除。</small></div>
  <div class="row"><button id="scan" class="grow">读取已加载</button><button id="load" class="grow">自动加载</button><button id="stop" class="hidden">停止</button></div>
  <p class="muted">自动记录按账号和点赞/收藏分类分别保存。手动与自动截止同时存在时，在先遇到的位置停止，不包含截止作品。</p>
  <div class="row spread"><small id="count">尚未读取作品</small><button id="send-batch" class="primary">发送范围内作品</button></div>
  <div id="list" class="list"></div>
</div>
<div class="box">
  <div class="row spread"><h3>每日后台同步</h3><button id="set-bg-profile" class="quiet">设为我的后台主页</button></div>
  <div id="daily-summary" class="muted" style="margin-bottom:8px"></div>
  <div class="row">
    <button id="bg-test-liked" class="grow">后台试跑(点赞)</button>
    <button id="bg-test-saved" class="grow">后台试跑(收藏)</button>
    <button id="reset-daily" class="quiet">重置今日锁</button>
  </div>
</div>
<div id="status" role="status" aria-live="polite">准备就绪。先设置 Docker 地址与授权 Token，再选择作品。</div><progress class="hidden" id="progress"></progress>
<details class="box"><summary>NAS 连接、授权与后台计划设置</summary>
  <label>Docker 主机<input id="host" placeholder="192.168.1.20 或 https://nas.example.com"></label>
  <div class="row"><label class="grow">端口<input id="port"></label><label class="grow">手动滚动上限<input id="max" type="number" min="1" max="1000"></label><label class="grow">后台滚动上限<input id="max-bg" type="number" min="1" max="200"></label></div>
  <label>接收路径<input id="path"></label>
  <label>推送授权 (X-NAS-Submit-Token)<input id="token" type="password" autocomplete="off"></label>
  <label>每日后台个人主页 URL<input id="bg-url" placeholder="https://www.xiaohongshu.com/user/profile/..."></label>
  <label class="inline"><input id="daily-enabled" type="checkbox"> 启用「小红书 · 每日一次」后台同步</label>
  <div class="row" style="margin:6px 0">
    <label class="inline" style="margin:0"><input id="bg-mode-liked" type="checkbox"> 同步点赞</label>
    <label class="inline" style="margin:0"><input id="bg-mode-saved" type="checkbox"> 同步收藏</label>
  </div>
  <div class="row">
    <button id="settings" class="primary">保存设置</button>
    <button id="test-conn" class="grow">测试 NAS 连接与授权</button>
  </div>
  <p class="muted">授权从 NAS 控制台「浏览器直推 / 设置」复制；测试结果也会同步显示在 NAS 控制台。</p>
</details>
</div></section>`;
    document.body.append(host);
    const $ = selector => shadow.querySelector(selector);
    const status = (message, error = false) => {
        $('#status').textContent = message;
        $('#status').classList.toggle('error', error);
    };
    const guard = () => {
        if (context !== `${activeAccount()}:${mode}:${location.pathname}`) {
            throw new Error('页面或账号已切换，请重新读取作品。');
        }
    };

    const read = () => {
        const unique = readNotesForMode(mode);
        context = `${activeAccount()}:${mode}:${location.pathname}`;
        return unique;
    };

    function render() {
        syncOwnProfileUrl();
        const acc = activeAccount();
        $('#account').textContent = acc ? `账号 …${acc.slice(-6)}` : '未在账号主页';
        shadow.querySelectorAll('[data-mode]').forEach(button => button.classList.toggle('active', button.dataset.mode === mode));
        const limited = limitedMode();
        $('.cut').classList.toggle('hidden', !limited);
        $('#auto-cut').classList.toggle('hidden', !limited);

        const mark = cutoff();
        $('#cutoff').textContent = mark ? `${mark.title || mark.id} · ${mark.id}` : '未设置手动截止；自动截止记录仍然生效。';
        $('#cutoff-input').placeholder = mark ? `已保存：${mark.id}` : '也可粘贴作品链接或 ID';

        const anchors = checkpoint();
        const match = checkpointMatch(items, anchors);
        $('#auto-summary').textContent = !anchors.length
            ? '首次发送后自动保存，无需手动设置。'
            : match.matched
            ? `已按顺序匹配 ${match.count}/${anchors.length} 个作品，将在第一个匹配作品前停止。`
            : `已保存 ${anchors.length} 个，当前出现 ${match.count} 个${match.ordered ? '，等待足够作品' : '，顺序不一致'}。`;
        $('#auto-notes').replaceChildren();
        for (const anchor of anchors) {
            const row = document.createElement('li');
            row.textContent = anchor.title || anchor.id;
            row.title = anchor.id;
            $('#auto-notes').append(row);
        }

        let eligible = items;
        try { eligible = currentRange(); } catch { eligible = []; }
        $('#count').textContent = `已加载 ${items.length} · 范围内 ${eligible.length}`;
        $('#list').replaceChildren();
        const selected = new Set(eligible.map(item => item.id));

        for (const item of items) {
            const row = document.createElement('div');
            row.className = 'note';
            if (/^https?:\/\//.test(item.cover)) {
                const image = document.createElement('img');
                image.className = 'cover';
                image.src = item.cover;
                image.loading = 'lazy';
                image.referrerPolicy = 'no-referrer';
                image.alt = '';
                row.append(image);
            }
            const body = document.createElement('div');
            body.className = 'grow';
            const title = document.createElement('a');
            title.className = 'title';
            title.href = item.url;
            title.target = '_blank';
            title.rel = 'noopener noreferrer';
            title.textContent = item.title;
            const desc = document.createElement('div');
            desc.className = 'muted';
            desc.textContent = item.author;
            const actions = document.createElement('div');
            actions.className = 'actions';
            if (limited) {
                const set = document.createElement('button');
                set.textContent = mark?.id === item.id ? '✓ 截止作品' : '设为截止';
                set.disabled = busy;
                set.onclick = () => execute(async () => {
                    guard();
                    remember(item);
                    status('截止作品已保存，发送时不包含它。');
                });
                actions.append(set);
            }
            const send = document.createElement('button');
            send.textContent = '单独发送';
            send.disabled = busy;
            send.onclick = () => execute(async () => {
                guard();
                await push(
                    [item],
                    'single'
                );
            });
            actions.append(send);
            const tag = document.createElement('span');
            tag.className = 'tag';
            tag.textContent = selected.has(item.id) ? '待发送' : '截止范围外';
            body.append(title, desc, actions);
            row.append(body, tag);
            $('#list').append(row);
        }

        const daily = GM_getValue(STORAGE_KEYS.DAILY_STATE, {}) || {};
        const d = new Date();
        const today = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
        const doneList = daily.doneModesDate === today && Array.isArray(daily.doneModes) ? daily.doneModes : [];
        let dailyText = '';
        if (!config.bgProfileUrl) {
            dailyText = '尚未设定您的个人主页 URL：请打开自己的小红书主页或点击右上角「设为我的后台主页」。';
        } else if (daily.lastSuccessDate === today) {
            dailyText = `今日已完成自动同步 (${daily.lastSummary || '全部开启分类'})`;
        } else if (daily.failDate === today && Number(daily.failCount || 0) >= 3) {
            dailyText = `今日已触发 3 次失败暂停：${daily.lastError || '未知原因'}`;
        } else {
            dailyText = `后台主页已绑定 · 今日已完成分类：[${doneList.join(', ') || '无'}]`;
        }
        $('#daily-summary').textContent = dailyText;

        shadow.querySelectorAll('button').forEach(button => {
            if (!button.matches('.launch,#close,#stop')) button.disabled = busy;
        });
        $('#stop').classList.toggle('hidden', !busy);
        $('#progress').classList.toggle('hidden', !busy);
    }

    async function execute(action) {
        if (busy) return;
        busy = true;
        stopped = false;
        render();
        try {
            await action();
        } catch (error) {
            status(error.message || String(error), true);
        } finally {
            busy = false;
            render();
        }
    }

    async function scan(auto) {
        await activateTabForMode(mode);
        items = read();
        const snapshot = context;
        const collected = new Map(items.map(item => [item.id, item]));
        if (auto) {
            let idle = 0;
            for (let step = 0; step < config.max && !stopped; step++) {
                guard();
                const mark = limitedMode() ? cutoff()?.id : '';
                const autoHit = limitedMode() ? checkpointMatch([...collected.values()], checkpoint()) : {matched: false};
                if ((mark && collected.has(mark)) || autoHit.matched) break;
                const count = collected.size;
                scrollContainers();
                await sleep(1200);
                guard();
                read().forEach(item => collected.set(item.id, item));
                if (context !== snapshot) throw new Error('页面已切换，请重新读取。');
                idle = collected.size === count ? idle + 1 : 0;
                items = [...collected.values()];
                render();
                status(`正在加载 ${modes[mode]} · ${items.length} 个作品 · 第 ${step + 1} 次滚动`);
                if (idle >= 5) break;
            }
        }
        items = [...collected.values()];
        currentRange();
        status(`${stopped ? '已停止加载' : '读取完成'}：${items.length} 个作品，范围内 ${currentRange().length} 个。`);
    }

    async function push(notes, destination) {
        const res = await pushNotes(notes, {
            destination,
            activeMode: mode,
            account: activeAccount(),
            discovered: items.length || notes.length,
            sourceMode: destination === 'single' ? 'single' : 'workbench',
            onStatus: msg => status(msg),
        });
        status(
            `Docker 已确认 ${res.sent}/${notes.length} 个作品（新入队 ${res.accepted}，重复跳过 ${res.skipped}）。${
                destination === 'single'
                    ? '保存到单个作品目录。'
                    : res.saved
                    ? '已更新最近 5 个作品的自动截止记录，手动截止保持不变。'
                    : '自动截止记录保持不变。'
            } 可在 NAS 控制台「浏览器推送 / 下载记录」查看进度。`
        );
    }

    function currentNote() {
        const state = findVueRef('note');
        const id = unwrap(state?.currentNoteId) || noteId(location.pathname);
        const detail = unwrap(state?.noteDetailMap)?.[id];
        const token = detail?.note?.xsecToken || detail?.xsecToken;
        const url = new URL(location.href);
        if (!id || !noteId(id)) throw new Error('请先打开一个作品，或粘贴作品完整链接。');
        const result = new URL(`/explore/${id}`, location.origin);
        if (url.searchParams.get('xsec_token') || token) {
            result.searchParams.set('xsec_token', url.searchParams.get('xsec_token') || token);
        }
        return result.href;
    }

    function parseLink(value) {
        const url = new URL(value.trim());
        if (
            !['www.xiaohongshu.com', 'xiaohongshu.com'].includes(url.hostname) ||
            !['http:', 'https:'].includes(url.protocol) ||
            !noteId(url.href)
        ) {
            throw new Error('请粘贴小红书完整作品链接，保留 xsec_token 参数。');
        }
        return {id: noteId(url.href), url: url.href};
    }

    const triggerManualBgTab = async targetMode => {
        if (!config.token) throw new Error('请先在设置中填写并保存 NAS Token。');
        const profile = config.bgProfileUrl || (activeAccount() ? `${location.origin}/user/profile/${activeAccount()}` : '');
        if (!profile) throw new Error('请先打开自己的小红书主页，或填写「每日后台个人主页 URL」。');
        const runId = `xhs_manual_${targetMode}_${Date.now()}`;
        const tabUrl = `${profile.split('#')[0]}#nas_bg_run=${encodeURIComponent(runId)}&nas_bg_mode=${targetMode}`;
        status(`正在打开静默后台标签页测试（${modes[targetMode]}）…`);
        let tab = null;
        try {
            const res = await awaitBgResult({
                cfg: config,
                storageKey: STORAGE_KEYS.BG_RESULT,
                runId,
                timeoutMs: 150000,
                timeoutMessage: '后台标签页测试超时 (150s)',
                openTab: () => {
                    tab = GM_openInTab(tabUrl, {active: false, insert: true});
                    return tab;
                },
            });
            if (res.status === 'ok') {
                status(`后台试跑(${modes[targetMode]})成功：reason=${res.reason}，提交 ${res.itemCount || 0} 条。`);
            } else {
                status(`后台试跑(${modes[targetMode]})失败：${res.reason || '未知错误'}`, true);
            }
        } finally {
            const closeTabSafe = () => {
                try { tab?.close?.(); } catch {}
            };
            closeTabSafe();
            setTimeout(closeTabSafe, 400);
        }
    };

    const syncFormFields = () => {
        $('#host').value = config.host;
        $('#port').value = config.port;
        $('#path').value = config.path;
        $('#token').value = config.token;
        $('#max').value = String(config.max);
        $('#max-bg').value = String(config.maxBgScrolls);
        $('#bg-url').value = config.bgProfileUrl;
        $('#daily-enabled').checked = config.dailyBgEnabled !== false;
        $('#bg-mode-liked').checked = config.bgModes.includes('liked');
        $('#bg-mode-saved').checked = config.bgModes.includes('saved');
    };
    syncFormFields();

    $('.launch').onclick = () => { $('.panel').classList.toggle('hidden'); render(); };
    $('#close').onclick = () => $('.panel').classList.add('hidden');
    shadow.querySelectorAll('[data-mode]').forEach(button => {
        button.onclick = () => { mode = button.dataset.mode; items = []; context = ''; render(); };
    });
    $('#scan').onclick = () => execute(() => scan(false));
    $('#load').onclick = () => execute(() => scan(true));
    $('#stop').onclick = () => { stopped = true; status('正在停止，等待当前操作完成…'); };
    $('#clear').onclick = () => { remember(null); status('已清除当前账号此分类的截止作品。'); };
    $('#clear-auto').onclick = () => {
        GM_setValue(checkpointKey(), []);
        render();
        status('已清除当前账号此分类的自动截止记录。');
    };
    $('#save-cutoff').onclick = () => execute(async () => {
        if (!activeAccount()) throw new Error('请先打开账号主页。');
        const id = noteId($('#cutoff-input').value.trim());
        if (!id) throw new Error('请输入有效的作品链接或 24 位作品 ID。');
        remember(items.find(item => item.id === id) || {id, title: ''});
        status('截止作品已长期保存。');
    });
    $('#send-batch').onclick = () => execute(async () => { guard(); await push(currentRange(), 'batch'); });
    $('#current').onclick = () => execute(async () => {
        $('#single').value = currentNote();
        status('已填入当前作品，点击发送即可单独归档。');
    });
    $('#send-single').onclick = () => execute(() => push([parseLink($('#single').value || currentNote())], 'single'));
    $('#set-bg-profile').onclick = () => execute(async () => {
        const acc = activeAccount();
        if (!acc) throw new Error('请先浏览进入您的个人主页（网址包含 /user/profile/...）。');
        const cleanUrl = `${location.origin}/user/profile/${acc}`;
        await saveConfig({bgProfileUrl: cleanUrl});
        syncFormFields();
        status(`已将当前主页设为每日后台采集账号：${cleanUrl}`);
    });
    $('#bg-test-liked').onclick = () => execute(() => triggerManualBgTab('liked'));
    $('#bg-test-saved').onclick = () => execute(() => triggerManualBgTab('saved'));
    $('#reset-daily').onclick = () => execute(async () => {
        await gmSetAll({[STORAGE_KEYS.DAILY_STATE]: {}});
        status('已清空今日完成锁与失败计数，下次后台定时到达即可再次运行。');
    });
    $('#settings').onclick = () => execute(async () => {
        const nextHost = $('#host').value.trim();
        const nextPort = $('#port').value.trim();
        const nextPath = $('#path').value.trim();
        endpoint(nextHost, nextPort, nextPath);
        const nextMax = Number($('#max').value);
        const nextBgMax = Number($('#max-bg').value);
        if (!Number.isInteger(nextMax) || nextMax < 1 || nextMax > 1000) {
            throw new Error('手动滚动次数应为 1–1000 的整数。');
        }
        if (!Number.isInteger(nextBgMax) || nextBgMax < 1 || nextBgMax > 200) {
            throw new Error('后台滚动次数应为 1–200 的整数。');
        }
        const selectedModes = [];
        if ($('#bg-mode-liked').checked) selectedModes.push('liked');
        if ($('#bg-mode-saved').checked) selectedModes.push('saved');
        await saveConfig({
            host: nextHost,
            port: nextPort,
            path: nextPath,
            token: $('#token').value.trim(),
            max: nextMax,
            maxBgScrolls: nextBgMax,
            bgProfileUrl: $('#bg-url').value.trim().split('#')[0],
            dailyBgEnabled: $('#daily-enabled').checked,
            bgModes: selectedModes.length ? selectedModes : ['liked'],
        });
        status('连接、授权与每日后台设置已保存。');
    });
    $('#test-conn').onclick = () => execute(async () => {
        const cfgNow = {
            ...config,
            host: $('#host').value.trim(),
            port: $('#port').value.trim(),
            path: $('#path').value.trim(),
            token: $('#token').value.trim(),
        };
        endpoint(cfgNow.host, cfgNow.port, '/api/browser-push/report');
        await reportToNas(
            cfgNow,
            {
                source_mode: 'workbench',
                account: activeAccount() || 'manual-test',
                status: 'ok',
                discovered: items.length,
                message: '小红书工作台连接与授权验证成功',
            },
            {strict: true}
        );
        status('NAS 连接与授权 Token 验证通过！已同步记录至 NAS 控制台。');
    });

    let lastUrl = location.href;
    setInterval(() => {
        if (lastUrl === location.href) return;
        lastUrl = location.href;
        stopped = true;
        items = [];
        context = '';
        syncFormFields();
        render();
        status('页面已切换，请重新读取作品。');
    }, 700);
    GM_registerMenuCommand('打开小红书 NAS 下载工作台', () => {
        $('.panel').classList.remove('hidden');
        render();
    });
    render();
})();
