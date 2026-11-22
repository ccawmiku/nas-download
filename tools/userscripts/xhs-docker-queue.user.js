// ==UserScript==
// @name         小红书 · NAS 下载工作台
// @namespace    xhs_downloader
// @version      3.7.1
// @description  点赞和收藏按作品 ID 向 NAS 增量对账、实时追捕失败作品新鲜链接（如 12/18→18/18）、连续已归档早停与流式推送，支持失败作品自动重推、详情抓图直推与每日后台回执
// @match        https://www.xiaohongshu.com/*
// @match        https://xiaohongshu.com/*
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
// @run-at       document-start
// @noframes
// ==/UserScript==

// Rebuilt for ccawmiku/nas-download; source project: JoeanAmier/XHS-Downloader (GPL-3.0).
(() => {
    'use strict';

    const STORAGE_KEYS = {
        CONFIG: 'nas_prod_xhs_config_v1',
        DAILY_STATE: 'nas_prod_xhs_daily_state',
        BG_RESULT: 'nas_prod_xhs_bg_result',
        ERROR_CACHE: 'nas_prod_xhs_error_cache',
    };

    const unwrap = value => value?._rawValue ?? value?.value ?? value;
    const noteId = value =>
        String(value || '').match(
            /(?:\/explore\/|\/discovery\/item\/|\/user\/profile\/[^/?#]+\/|^)([a-f\d]{24})(?:[/?#]|$)/i
        )?.[1] || '';
    const accountId = path => String(path || '').match(/\/user\/profile\/([^/?#]+)/)?.[1] || '';

    const selectMissing = (items, states) => items.filter(item =>
        !item.id || states[item.id.toLowerCase()] === 'missing'
    );

    const endpoint = (host, port, path) => {
        const url = new URL(/^https?:\/\//i.test(host) ? host : `http://${host}`);
        if (port && !url.port) url.port = String(port);
        const rawPath = String(path || '').split('#')[0];
        const qIndex = rawPath.indexOf('?');
        const pathPart = qIndex >= 0 ? rawPath.slice(0, qIndex) : rawPath;
        const searchPart = qIndex >= 0 ? rawPath.slice(qIndex) : '';
        url.pathname = pathPart.startsWith('/') ? pathPart : `/${pathPart}`;
        url.search = searchPart;
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

    const extractXhsApiNotes = payload => {
        const rawList = payload?.data?.notes;
        if (!Array.isArray(rawList)) return [];
        const baseOrigin =
            typeof location !== 'undefined' && location?.origin
                ? location.origin
                : 'https://www.xiaohongshu.com';
        const out = [];
        for (const raw of rawList) {
            if (!raw || typeof raw !== 'object') continue;
            const card = raw.note_card || raw.noteCard || raw;
            const id = noteId(
                raw.note_id || raw.id || raw.noteId || card.note_id || card.noteId || card.id || ''
            );
            if (!id) continue;
            const token = String(
                raw.xsec_token || raw.xsecToken || card.xsec_token || card.xsecToken || ''
            );
            const url = new URL(`/explore/${id}`, baseOrigin);
            if (token) url.searchParams.set('xsec_token', token);
            url.searchParams.set('xsec_source', 'pc_user');
            out.push({
                id,
                url: url.href,
                title: String(
                    card.display_title || card.displayTitle || card.title || raw.display_title || raw.title || id
                ),
                author: String(
                    card.user?.nick_name || card.user?.nickName || card.user?.nickname || raw.user?.nickname || ''
                ),
                cover: String(
                    card.cover?.url_default || card.cover?.urlDefault || card.cover?.url || ''
                ),
            });
        }
        return out;
    };

    const evaluateXhsEarlyStop = (orderedIds, statesMap = {}, stopThreshold = 30, opts = {}) => {
        const threshold = Math.max(0, Number(stopThreshold || 0));
        const pendingFailedTargetCount = Math.max(0, Number(opts?.pendingFailedTargetCount || 0));
        const settledSet = new Set(['archived', 'pending', 'deferred']);
        let consecutiveSettled = 0;
        let stopAtId = '';
        let reachedThreshold = false;
        for (const rawId of Array.isArray(orderedIds) ? orderedIds : []) {
            const id = String(rawId || '').toLowerCase();
            if (!id) continue;
            if (settledSet.has(statesMap[id])) {
                consecutiveSettled += 1;
            } else {
                consecutiveSettled = 0;
            }
            if (threshold > 0 && consecutiveSettled >= threshold) {
                reachedThreshold = true;
                stopAtId = id;
                break;
            }
        }
        return {
            shouldStop: reachedThreshold && pendingFailedTargetCount <= 0,
            reachedThreshold,
            consecutiveSettled,
            pendingFailedTargetCount,
            stopAtId,
        };
    };

    const toHttpsUrl = raw => {
        const text = String(raw || '').trim();
        if (!text) return '';
        if (/^\/\//.test(text)) return `https:${text}`;
        return /^http:\/\//i.test(text) ? `https://${text.slice(7)}` : text;
    };

    const computeFailedCaptureProgress = (errorRows, capturedItems, activeMode = '') => {
        const byId = new Map();
        const iter =
            capturedItems instanceof Map
                ? capturedItems.values()
                : Array.isArray(capturedItems)
                  ? capturedItems
                  : capturedItems && typeof capturedItems === 'object'
                    ? Object.values(capturedItems)
                    : [];
        for (const item of iter) {
            if (!item || typeof item !== 'object') continue;
            const id = noteId(item.id || item.note_id || item.url || '').toLowerCase();
            if (!id) continue;
            const url = String(item.url || '');
            const prev = byId.get(id);
            if (!prev) {
                byId.set(id, {...item, id});
            } else {
                const useNewUrl = url.includes('xsec_token=') || !String(prev.url || '').includes('xsec_token=');
                byId.set(id, {
                    ...prev,
                    ...item,
                    id,
                    url: useNewUrl ? url || prev.url : prev.url,
                    category: item.category || prev.category || '',
                });
            }
        }

        const uniqueErrors = new Map();
        for (const row of Array.isArray(errorRows) ? errorRows : []) {
            if (!row || typeof row !== 'object') continue;
            const id = noteId(row.source_id || row.id || row.url || '').toLowerCase();
            if (!id || uniqueErrors.has(id)) continue;
            uniqueErrors.set(id, row);
        }

        let total = 0;
        let capturedTotal = 0;
        let relevantTotal = 0;
        let capturedRelevant = 0;
        const capturedMap = {};
        const modeNorm = String(activeMode || '').trim().toLowerCase();

        for (const [id, row] of uniqueErrors.entries()) {
            total += 1;
            const rowCat = String(row.category || '').trim().toLowerCase();
            const isRelevant = !modeNorm || !rowCat || rowCat === modeNorm;
            if (isRelevant) relevantTotal += 1;
            const matched = byId.get(id);
            const hasFreshUrl = Boolean(matched?.url && String(matched.url).includes('xsec_token='));
            if (hasFreshUrl) {
                capturedTotal += 1;
                if (isRelevant) capturedRelevant += 1;
                capturedMap[id] = {
                    ...matched,
                    category: matched.category || rowCat || '',
                };
            }
        }

        const remainingRelevant = Math.max(0, relevantTotal - capturedRelevant);
        return {
            total,
            capturedTotal,
            relevantTotal,
            capturedRelevant,
            remainingRelevant,
            allRelevantCaptured: relevantTotal === 0 || capturedRelevant >= relevantTotal,
            allTotalCaptured: total === 0 || capturedTotal >= total,
            capturedMap,
        };
    };

    const selectPushCandidates = (items, states = {}, failedErrorMap = {}, opts = {}) => {
        const requireFreshDiff = Boolean(opts?.requireFreshDiff);
        const errMap = failedErrorMap && typeof failedErrorMap === 'object' ? failedErrorMap : {};
        const missingItems = [];
        const failedRefreshItems = [];
        const allItems = [];
        const seen = new Set();

        for (const item of Array.isArray(items) ? items : []) {
            if (!item || typeof item !== 'object') continue;
            const id = noteId(item.id || item.note_id || item.url || '').toLowerCase();
            const dedupKey = id || String(item.url || '');
            if (dedupKey && seen.has(dedupKey)) continue;
            if (dedupKey) seen.add(dedupKey);

            const st = id ? states[id] : 'missing';
            if (st === 'archived' || st === 'pending') continue;

            const errRow = id ? errMap[id] : null;
            const url = String(item.url || '');
            const hasToken = url.includes('xsec_token=');
            const qualifiesRefresh =
                Boolean(errRow) &&
                hasToken &&
                (!requireFreshDiff || url !== String(errRow.url || '') || !errRow.retry_exhausted);

            if (qualifiesRefresh) {
                failedRefreshItems.push(item);
                allItems.push(item);
            } else if (!id || st === 'missing') {
                missingItems.push(item);
                allItems.push(item);
            }
        }
        return {missingItems, failedRefreshItems, allItems};
    };

    const extractXhsNoteMedia = (noteObj, fallbackUrl = '') => {
        if (!noteObj || typeof noteObj !== 'object') return null;
        const firstFeedCard = noteObj?.data?.items?.[0]?.note_card || noteObj?.items?.[0]?.note_card;
        const node =
            noteObj.note && typeof noteObj.note === 'object'
                ? noteObj.note
                : noteObj.note_card && typeof noteObj.note_card === 'object'
                  ? noteObj.note_card
                  : noteObj.noteCard && typeof noteObj.noteCard === 'object'
                    ? noteObj.noteCard
                    : firstFeedCard && typeof firstFeedCard === 'object'
                      ? firstFeedCard
                      : noteObj;
        const id = noteId(
            node.note_id || node.noteId || node.id || noteObj.id || fallbackUrl || ''
        ).toLowerCase();
        if (!id) return null;

        const media = [];
        const seen = new Set();
        const pushMedia = (type, rawUrl) => {
            const clean = toHttpsUrl(rawUrl);
            if (!clean || !/^https:\/\//i.test(clean) || seen.has(clean)) return;
            seen.add(clean);
            media.push({type, url: clean});
        };

        const streamRoot = node.video?.media?.stream || {};
        const streamCandidates = [];
        for (const codec of ['h265', 'h264', 'av1', 'h266']) {
            const list = streamRoot[codec];
            if (Array.isArray(list)) {
                for (const s of list) {
                    const mUrl = s?.master_url || s?.masterUrl || s?.backup_urls?.[0] || s?.backupUrls?.[0] || '';
                    if (mUrl) {
                        streamCandidates.push({
                            url: mUrl,
                            bitrate: Number(s?.video_bitrate || s?.avg_bitrate || s?.bitrate || 0) || 0,
                            preferH264: codec === 'h264' ? 1 : 0,
                        });
                    }
                }
            }
        }
        streamCandidates.sort((a, b) => b.bitrate - a.bitrate || b.preferH264 - a.preferH264);
        if (streamCandidates.length > 0) {
            pushMedia('video', streamCandidates[0].url);
        } else {
            const originKey = node.video?.consumer?.origin_video_key || node.video?.consumer?.originVideoKey || '';
            if (originKey) {
                pushMedia('video', `https://sns-video-bd.xhscdn.com/${String(originKey).replace(/^\/+/, '')}`);
            }
        }

        if (media.length === 0) {
            const images = node.image_list || node.imageList || [];
            if (Array.isArray(images)) {
                for (const img of images) {
                    if (!img || typeof img !== 'object') continue;
                    const infoList = img.info_list || img.infoList || [];
                    let chosen = '';
                    if (Array.isArray(infoList) && infoList.length) {
                        const dft = infoList.find(
                            x => (x?.image_scene || x?.imageScene || x?.scene) === 'WB_DFT' && (x?.url || '')
                        );
                        chosen = dft?.url || infoList[infoList.length - 1]?.url || infoList[0]?.url || '';
                    }
                    if (!chosen) {
                        chosen = img.url_default || img.urlDefault || img.url_pre || img.urlPre || img.url || '';
                    }
                    pushMedia('image', chosen);
                }
            }
        }

        if (!media.length) return null;
        const baseOrigin =
            typeof location !== 'undefined' && location?.origin
                ? location.origin
                : 'https://www.xiaohongshu.com';
        let noteUrl = `${baseOrigin}/explore/${id}`;
        if (fallbackUrl && noteId(fallbackUrl).toLowerCase() === id) {
            try {
                const parsed = new URL(fallbackUrl, baseOrigin);
                if (parsed.hostname.endsWith('xiaohongshu.com')) {
                    noteUrl = parsed.href.split('#')[0];
                }
            } catch {}
        }
        return {
            note_id: id,
            url: noteUrl,
            title: String(node.title || node.display_title || node.displayTitle || '').trim(),
            author: String(
                node.user?.nickname || node.user?.nick_name || node.user?.nickName || ''
            ).trim(),
            media,
        };
    };

    const pickFailedRefreshIds = (orderedNotes, failedErrorMap = {}) => {
        const refreshIds = [];
        const errMap = failedErrorMap && typeof failedErrorMap === 'object' ? failedErrorMap : {};
        for (const item of Array.isArray(orderedNotes) ? orderedNotes : []) {
            const id = String(item?.id || '').toLowerCase();
            if (!id) continue;
            const errRow = errMap[id];
            if (!errRow) continue;
            const url = String(item?.url || '');
            if (url.includes('xsec_token=') && url !== String(errRow.url || '')) {
                refreshIds.push(id);
            }
        }
        return refreshIds;
    };

    // Pure helpers are also exercised by the Node regression tests.
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = {
            STORAGE_KEYS,
            REPORT_PLATFORM,
            noteId,
            accountId,
            selectMissing,
            endpoint,
            redactXhsUrl,
            parseBgRunHash,
            extractErrorMessage,
            extractXhsApiNotes,
            evaluateXhsEarlyStop,
            toHttpsUrl,
            extractXhsNoteMedia,
            pickFailedRefreshIds,
            computeFailedCaptureProgress,
            selectPushCandidates,
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
            maxBgScrolls: saved.idSync ? Number(saved.maxBgScrolls) || 300 : Math.max(300, Number(saved.maxBgScrolls) || 0),
            stopConsecutive: saved.stopConsecutive !== undefined ? Math.max(0, Number(saved.stopConsecutive) || 0) : 30,
            streamBatchSize: Math.max(10, Math.min(500, Number(saved.streamBatchSize || 50))),
            dailyBgEnabled: saved.dailyBgEnabled !== false,
            autoRetryErrorDetail: saved.autoRetryErrorDetail !== false,
            huntErrorsBeforeStop: saved.huntErrorsBeforeStop !== false,
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

    const apiCaptured = {
        liked: new Map(),
        saved: new Map(),
        published: new Map(),
    };
    const detailCaptured = new Map();
    const freshCapturedById = new Map();
    let onLiveCapture = null;

    const recordFreshCaptured = (rawList, forcedCategory = '') => {
        if (!Array.isArray(rawList) || !rawList.length) return 0;
        let changed = 0;
        for (const entry of rawList) {
            if (!entry || typeof entry !== 'object') continue;
            const id = noteId(entry.id || entry.note_id || entry.url || '').toLowerCase();
            if (!id) continue;
            const cat = String(forcedCategory || entry.category || '').trim().toLowerCase();
            const cleanCat = cat === 'liked' || cat === 'saved' ? cat : '';
            const nextUrl = String(entry.url || '');
            const prev = freshCapturedById.get(id);
            if (!prev) {
                freshCapturedById.set(id, {
                    id,
                    url: nextUrl,
                    title: entry.title || id,
                    author: entry.author || '',
                    cover: entry.cover || '',
                    ...(cleanCat ? {category: cleanCat} : {}),
                });
                changed += 1;
            } else {
                const prevUrl = String(prev.url || '');
                const shouldTakeUrl = nextUrl.includes('xsec_token=')
                    ? nextUrl !== prevUrl
                    : !prevUrl.includes('xsec_token=') && Boolean(nextUrl) && nextUrl !== prevUrl;
                const nextCat = cleanCat || prev.category || '';
                const nextObj = {
                    ...prev,
                    ...entry,
                    id,
                    url: shouldTakeUrl ? nextUrl : prevUrl,
                    title: entry.title && entry.title !== id ? entry.title : prev.title || entry.title || id,
                    author: entry.author || prev.author || '',
                    cover: entry.cover || prev.cover || '',
                    ...(nextCat ? {category: nextCat} : {}),
                };
                if (nextObj.url !== prevUrl || nextObj.category !== prev.category) changed += 1;
                freshCapturedById.set(id, nextObj);
            }
        }
        return changed;
    };

    const detectXhsApiMode = reqUrl => {
        const u = String(reqUrl || '');
        if (/\/api\/sns\/web\/v\d+\/note\/like\/page(?:[/?#]|$)/.test(u)) return 'liked';
        if (/\/api\/sns\/web\/v\d+\/note\/collect\/page(?:[/?#]|$)/.test(u)) return 'saved';
        if (/\/api\/sns\/web\/v\d+\/user_posted(?:[/?#]|$)/.test(u)) return 'published';
        return '';
    };

    const ingestXhsApiPayload = (reqUrl, json) => {
        const u = String(reqUrl || '');
        if (/\/api\/sns\/web\/v\d+\/feed(?:[/?#]|$)/.test(u)) {
            const list = json?.data?.items;
            if (Array.isArray(list)) {
                for (const entry of list) {
                    const extracted = extractXhsNoteMedia(entry);
                    if (extracted?.note_id) {
                        detailCaptured.set(extracted.note_id, {
                            raw: entry?.note_card || entry?.noteCard || entry,
                            extracted,
                        });
                    }
                }
            }
            return;
        }
        const apiMode = detectXhsApiMode(reqUrl);
        if (!apiMode) return;
        const parsed = extractXhsApiNotes(json);
        if (!parsed.length) return;
        const bucket = apiCaptured[apiMode];
        for (const item of parsed) {
            const key = item.id.toLowerCase();
            if (!bucket.has(key)) bucket.set(key, item);
            else {
                const prev = bucket.get(key);
                bucket.set(key, {
                    ...prev,
                    ...item,
                    url: item.url.includes('xsec_token=') ? item.url : prev.url,
                });
            }
        }
        if (recordFreshCaptured(parsed, apiMode === 'liked' || apiMode === 'saved' ? apiMode : '') > 0) {
            try {
                if (typeof onLiveCapture === 'function') onLiveCapture(apiMode);
            } catch {}
        }
    };

    try {
        const rawFetch = pageWin.fetch;
        if (typeof rawFetch === 'function') {
            pageWin.fetch = async function (...args) {
                const response = await rawFetch.apply(this, args);
                try {
                    const reqUrl = typeof args[0] === 'string' ? args[0] : args[0]?.url || response?.url || '';
                    if (detectXhsApiMode(reqUrl) || /\/api\/sns\/web\/v\d+\/feed(?:[/?#]|$)/.test(String(reqUrl || ''))) {
                        response.clone().json().then(json => ingestXhsApiPayload(reqUrl, json)).catch(() => {});
                    }
                } catch {}
                return response;
            };
        }
        const RawXhrOpen = pageWin.XMLHttpRequest?.prototype?.open;
        const RawXhrSend = pageWin.XMLHttpRequest?.prototype?.send;
        if (RawXhrOpen && RawXhrSend) {
            pageWin.XMLHttpRequest.prototype.open = function (method, url, ...rest) {
                this.__nasXhsUrl = String(url || '');
                return RawXhrOpen.call(this, method, url, ...rest);
            };
            pageWin.XMLHttpRequest.prototype.send = function (...args) {
                this.addEventListener('load', () => {
                    try {
                        const reqUrl = this.responseURL || this.__nasXhsUrl || '';
                        if (
                            this.status >= 200 &&
                            this.status < 300 &&
                            this.responseText &&
                            (detectXhsApiMode(reqUrl) || /\/api\/sns\/web\/v\d+\/feed(?:[/?#]|$)/.test(String(reqUrl || '')))
                        ) {
                            ingestXhsApiPayload(reqUrl, JSON.parse(this.responseText));
                        }
                    } catch {}
                });
                return RawXhrSend.apply(this, args);
            };
        }
    } catch {}

    const getPiniaStores = () => {
        try {
            const app = document.querySelector('#app')?.__vue_app__;
            const pinia = app?.config?.globalProperties?.$pinia?._s;
            return pinia instanceof Map || typeof pinia?.entries === 'function' ? pinia : null;
        } catch {
            return null;
        }
    };

    const findVueRef = name => {
        const pinia = getPiniaStores();
        if (pinia) {
            if (typeof pinia.get === 'function' && pinia.get(name)) {
                return unwrap(pinia.get(name));
            }
            for (const [sid, store] of pinia.entries()) {
                if (!store) continue;
                if (sid === name || store.$id === name) return unwrap(store);
                if (Object.prototype.hasOwnProperty.call(store, name)) {
                    return unwrap(store[name]);
                }
            }
        }
        if (pageWin?.__INITIAL_STATE__?.[name]) {
            return unwrap(pageWin.__INITIAL_STATE__[name]);
        }
        return null;
    };

    const lookupNoteDetailInMap = (rawMap, targetId) => {
        const mapObj = unwrap(rawMap);
        if (!mapObj || typeof mapObj !== 'object') return null;
        const lowerTarget = String(targetId || '').toLowerCase();
        if (!lowerTarget) return null;
        if (mapObj[targetId]) return unwrap(mapObj[targetId]);
        if (mapObj[lowerTarget]) return unwrap(mapObj[lowerTarget]);
        for (const [k, v] of Object.entries(mapObj)) {
            if (String(k || '').toLowerCase() === lowerTarget && v) return unwrap(v);
            const val = unwrap(v);
            const node = val?.note || val?.note_card || val?.noteCard || val;
            const nestedId = noteId(node?.noteId || node?.note_id || node?.id || '').toLowerCase();
            if (nestedId === lowerTarget && val) return val;
        }
        return null;
    };

    const findVueNoteDetail = targetId => {
        const lowerId = String(targetId || '').toLowerCase();
        if (!lowerId) return null;
        const fromHook = detailCaptured.get(lowerId);
        if (fromHook?.extracted?.media?.length) return fromHook.extracted;

        const maps = [];
        const pinia = getPiniaStores();
        if (pinia) {
            const noteStore = typeof pinia.get === 'function' ? pinia.get('note') : null;
            if (noteStore?.noteDetailMap) maps.push(noteStore.noteDetailMap);
            if (noteStore?.$state?.noteDetailMap) maps.push(noteStore.$state.noteDetailMap);
            for (const store of pinia.values()) {
                if (!store) continue;
                if (store.noteDetailMap) maps.push(store.noteDetailMap);
                if (store.$state?.noteDetailMap) maps.push(store.$state.noteDetailMap);
                if (store.note?.noteDetailMap) maps.push(store.note.noteDetailMap);
            }
        }
        if (pageWin?.__INITIAL_STATE__?.note?.noteDetailMap) {
            maps.push(pageWin.__INITIAL_STATE__.note.noteDetailMap);
        }
        for (const m of maps) {
            const candidate = lookupNoteDetailInMap(m, lowerId);
            if (!candidate) continue;
            const extracted = extractXhsNoteMedia(candidate, location.href);
            if (extracted?.media?.length) return extracted;
        }
        return null;
    };

    const extractXhsDomDetailMedia = (targetId, fallbackUrl = location.href) => {
        const id = noteId(targetId || location.pathname).toLowerCase();
        if (!id) return null;
        const modal = document.querySelector('#noteContainer, .note-detail-mask, .note-container');
        const inNotePage = /\/(?:explore|discovery\/item)\/[a-f\d]{24}/i.test(location.pathname);
        if (!modal && !inNotePage) return null;
        const scope = modal || document.querySelector('main') || document.body;
        if (!scope) return null;

        const media = [];
        const seen = new Set();
        const pushDomMedia = (type, rawSrc) => {
            const clean = toHttpsUrl(rawSrc);
            if (!clean || !/^https:\/\//i.test(clean)) return;
            try {
                const u = new URL(clean);
                if (!/(?:xhscdn\.com|xhscdn\.net)$/i.test(u.hostname)) return;
            } catch {
                return;
            }
            if (type === 'image' && /(?:avatar|sns-avatar|emoji|favicon|icon|logo)/i.test(clean)) return;
            if (seen.has(clean)) return;
            seen.add(clean);
            media.push({type, url: clean});
        };

        for (const v of scope.querySelectorAll('video')) {
            pushDomMedia('video', v.currentSrc || v.getAttribute('src') || '');
            for (const s of v.querySelectorAll('source')) {
                pushDomMedia('video', s.getAttribute('src') || '');
            }
        }
        if (media.length === 0) {
            const imgSelectors =
                '.swiper-slide:not(.swiper-slide-duplicate) img, .note-slider-img img, .media-container img, .img-container img';
            for (const img of scope.querySelectorAll(imgSelectors)) {
                pushDomMedia('image', img.currentSrc || img.getAttribute('src') || '');
            }
        }
        if (!media.length) return null;

        const title = String(
            scope.querySelector('#detail-title, .note-content .title, h1')?.textContent || ''
        ).trim();
        const author = String(
            scope.querySelector('.author-wrapper .username, .user-nickname')?.textContent || ''
        ).trim();
        let noteUrl = `${location.origin}/explore/${id}`;
        if (fallbackUrl && noteId(fallbackUrl).toLowerCase() === id) {
            noteUrl = String(fallbackUrl).split('#')[0];
        }
        return {note_id: id, url: noteUrl, title, author, media};
    };

    const loggedInUserId = () => {
        const u = findVueRef('user') || unwrap(pageWin?.__INITIAL_STATE__?.user);
        const info = unwrap(u?.userInfo) || unwrap(u?.$state?.userInfo) || {};
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
        const apiBucket = apiCaptured[activeMode];
        let raw;
        if (activeMode in indices) {
            if (!accountId(location.pathname)) throw new Error('请先打开要收录的小红书账号主页。');
            const userRef = findVueRef('user');
            const fallbackUser = unwrap(pageWin?.__INITIAL_STATE__?.user);
            const notesArr = unwrap(userRef?.notes) || unwrap(userRef?.$state?.notes) || unwrap(fallbackUser?.notes);
            raw = unwrap(notesArr?.[indices[activeMode]]);
        } else {
            if (/\/user\/profile\//.test(location.pathname)) {
                throw new Error('账号主页请使用点赞、收藏或发布分类。');
            }
            if (location.pathname.includes('board')) {
                const id = location.pathname.split('/board/')[1]?.split('/')[0];
                const boardRef = findVueRef('board') || unwrap(pageWin?.__INITIAL_STATE__?.board);
                raw = unwrap(boardRef?.boardFeedsMap || boardRef?.$state?.boardFeedsMap)?.[id]?.notes;
            } else {
                const feedKey = location.pathname.includes('search_result') ? 'search' : 'feed';
                const feedRef = findVueRef(feedKey) || unwrap(pageWin?.__INITIAL_STATE__?.[feedKey]);
                raw = unwrap(feedRef?.feeds || feedRef?.$state?.feeds);
            }
        }
        const merged = new Map();
        if (apiBucket && apiBucket.size > 0) {
            for (const item of apiBucket.values()) merged.set(item.id.toLowerCase(), item);
        }
        const needsDom =
            !Array.isArray(raw) ||
            raw.length === 0 ||
            raw.some(item => !(item?.xsecToken || item?.xsec_token || item?.noteCard?.xsecToken || item?.note_card?.xsec_token));
        const domMap = needsDom ? collectDomLinkMap() : null;
        if (Array.isArray(raw)) {
            for (const entry of raw) {
                const item = normalizeNote(entry, domMap);
                if (!item) continue;
                const key = item.id.toLowerCase();
                if (!merged.has(key) || (item.url.includes('xsec_token=') && !merged.get(key).url.includes('xsec_token='))) {
                    merged.set(key, item);
                }
            }
        }
        if (merged.size > 0) {
            const out = [...merged.values()];
            recordFreshCaptured(out, activeMode === 'liked' || activeMode === 'saved' ? activeMode : '');
            return out;
        }
        if (domMap && domMap.size > 0) {
            const out = [...domMap.values()].map(d => ({
                id: d.id,
                url: d.url,
                title: d.title || d.id,
                author: '',
                cover: d.cover || '',
            }));
            recordFreshCaptured(out, activeMode === 'liked' || activeMode === 'saved' ? activeMode : '');
            return out;
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

    const fetchNasErrors = async cfg => {
        if (!cfg?.token) {
            throw new Error('请先设置并保存「推送授权 (X-NAS-Submit-Token)」。');
        }
        const url = endpoint(cfg.host, cfg.port, '/api/browser-push/errors?platform=xhs&limit=200');
        const data = await gmGetJson(url, cfg.token);
        const list = Array.isArray(data?.items) ? data.items : [];
        await gmSetAll({[STORAGE_KEYS.ERROR_CACHE]: list});
        return list;
    };

    const dismissNasError = async (cfg, sourceId) => {
        if (!cfg?.token) {
            throw new Error('请先设置并保存「推送授权 (X-NAS-Submit-Token)」。');
        }
        return gmPostJson(endpoint(cfg.host, cfg.port, '/api/browser-push/errors/dismiss'), cfg.token, {
            platform: 'xhs',
            source_id: String(sourceId || ''),
        });
    };

    const pushXhsDirect = async (cfg, items, opts = {}) => {
        if (!cfg?.token) {
            throw new Error('请先设置并保存「推送授权 (X-NAS-Submit-Token)」。');
        }
        return gmPostJson(endpoint(cfg.host, cfg.port, '/api/xhs/direct'), cfg.token, {
            items,
            destination: opts.destination || 'batch',
            ...(opts.category ? {category: opts.category} : {}),
            source_mode: opts.sourceMode || 'error_retry',
            account: opts.account || '',
            batch_id: makeBatchId('xhs_direct'),
            discovered: items.length,
        });
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
                    ...(report.item_count !== undefined ? {item_count: Number(report.item_count)} : {}),
                    ...(report.category ? {category: report.category} : {}),
                    ...(report.task_id ? {task_id: report.task_id} : {}),
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

    const reconcileNotes = async (notes, cfg = config) => {
        const ids = [...new Set(notes.map(item => item.id?.toLowerCase()).filter(Boolean))];
        const states = {};
        for (let offset = 0; offset < ids.length; offset += 500) {
            const part = ids.slice(offset, offset + 500);
            const data = await gmPostJson(endpoint(cfg.host, cfg.port, '/api/xhs/reconcile'), cfg.token, {ids: part});
            for (const id of part) {
                const state = data?.states?.[id];
                if (!['archived', 'pending', 'deferred', 'missing'].includes(state)) {
                    throw new Error('NAS 未完整返回作品 ID 的状态。');
                }
                states[id] = state;
            }
        }
        return states;
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
            failedErrorMap = {},
            requireFreshDiff = false,
            onStatus = () => {},
        }
    ) => {
        const limited = activeMode === 'liked' || activeMode === 'saved';
        const originalCount = notes.length;
        const reconciledStates = await reconcileNotes(notes);
        const picked = selectPushCandidates(notes, reconciledStates, failedErrorMap, {
            requireFreshDiff,
        });
        const refreshSet = new Set(picked.failedRefreshItems);
        const missingSet = new Set(picked.missingItems);
        notes = picked.allItems;
        let sent = 0;
        let accepted = 0;
        let skipped = originalCount - notes.length;
        let lastTaskId = '';
        const batchRoot = makeBatchId(`xhs_${activeMode}`);
        const chunkCount = Math.ceil(notes.length / 500);

        for (let offset = 0, part = 1; offset < notes.length; offset += 500, part += 1) {
            const chunk = notes.slice(offset, offset + 500);
            const chunkHasRefresh = chunk.some(it => refreshSet.has(it));
            const chunkHasMissing = chunk.some(it => missingSet.has(it));
            const chunkSourceMode =
                chunkHasRefresh && !chunkHasMissing && sourceMode === 'workbench'
                    ? 'error_retry'
                    : sourceMode;
            const chunkBatchId = chunkCount > 1 ? `${batchRoot}_p${part}` : batchRoot;
            onStatus(`正在发送 ${sent}/${notes.length}…`);
            try {
                const body = await requestSubmit(config, {
                    urls: chunk.map(item => item.url),
                    destination,
                    ...(limited ? {category: activeMode} : {}),
                    ...(chunkHasRefresh ? {force_retry: true} : {}),
                    ...(runId && part === chunkCount ? {run_id: runId} : {}),
                    batch_id: chunkBatchId,
                    source_mode: chunkSourceMode,
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

        return {
            sent,
            accepted,
            skipped,
            refreshed: picked.failedRefreshItems.length,
            pushedIds: notes.map(n => noteId(n.id || n.url).toLowerCase()).filter(Boolean),
            taskId: lastTaskId,
            batchId: batchRoot,
        };
    };

    // ==================== 后台自动任务执行路径 (#nas_bg_run=...) ====================
    const initialBgRun = parseBgRunHash(location.hash);
    const onDomReady = fn => {
        if (document.readyState !== 'loading' && document.body) {
            fn();
            return;
        }
        let ran = false;
        let timer = null;
        const trigger = () => {
            if (ran || !document.body) return;
            ran = true;
            if (timer) clearInterval(timer);
            fn();
        };
        document.addEventListener('DOMContentLoaded', trigger, {once: true});
        timer = setInterval(() => {
            if (document.body && document.readyState !== 'loading') trigger();
        }, 150);
    };

    onDomReady(() => {
    const bgRun = initialBgRun.runId ? initialBgRun : parseBgRunHash(location.hash);
    if (bgRun.runId) {
        (async () => {
            const targetMode = bgRun.mode === 'saved' ? 'saved' : 'liked';
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

                const collected = new Map();
                const orderedIds = [];
                const checkedStates = {};
                const effectiveStates = {};
                const refreshIds = new Set();
                const capturedFailedIds = new Set();
                const seenTargetFailedIds = new Set();
                const handledIds = new Set();
                const pushedIds = new Set();
                const pendingBuffer = [];
                const failedErrors = await fetchNasErrors(config).catch(() => []);
                const failedErrorMap = Object.fromEntries(
                    failedErrors
                        .filter(row => row?.source_id)
                        .map(row => [String(row.source_id).toLowerCase(), row])
                );
                const targetFailedIds = new Set(
                    failedErrors
                        .filter(row => {
                            const cat = String(row?.category || '').trim().toLowerCase();
                            return Boolean(row?.source_id) && (!cat || cat === targetMode);
                        })
                        .map(row => String(row.source_id).toLowerCase())
                );
                const maxBg = Math.max(1, Math.min(1000, Number(config.maxBgScrolls || 300)));
                const stopThreshold = Math.max(0, Number(config.stopConsecutive ?? 30));
                const streamBatchSize = Math.max(10, Math.min(500, Number(config.streamBatchSize || 50)));
                const limited = targetMode === 'liked' || targetMode === 'saved';
                const batchRoot = makeBatchId(`xhs_${targetMode}`);
                let batchSeq = 0;
                let totalDiscovered = 0;
                let totalSent = 0;
                let totalAccepted = 0;
                let totalSkipped = 0;
                let totalRefreshed = 0;
                let lastTaskId = '';
                let soleBatchSentRunId = false;
                let stoppedEarly = false;

                const ingestList = list => {
                    let addedHere = 0;
                    for (const it of list) {
                        const key = it?.id?.toLowerCase();
                        if (!key) continue;
                        if (!handledIds.has(key)) {
                            handledIds.add(key);
                            orderedIds.push(key);
                            collected.set(key, it);
                            totalDiscovered += 1;
                            addedHere += 1;
                        } else if (!pushedIds.has(key) && collected.has(key)) {
                            const prev = collected.get(key);
                            if (it.url.includes('xsec_token=') && !prev.url.includes('xsec_token=')) {
                                collected.set(key, it);
                            }
                        }
                    }
                    return addedHere;
                };

                const flushBatch = async (chunkKeys, sendRunId) => {
                    const chunk = chunkKeys.map(key => collected.get(key)).filter(Boolean);
                    if (!chunk.length) return;
                    batchSeq += 1;
                    const refreshedHere = chunk.filter(item =>
                        refreshIds.has(item.id.toLowerCase())
                    ).length;
                    const body = await requestSubmit(config, {
                        urls: chunk.map(item => item.url),
                        destination: 'batch',
                        ...(limited ? {category: targetMode} : {}),
                        ...(refreshedHere > 0 ? {force_retry: true} : {}),
                        ...(sendRunId ? {run_id: bgRun.runId} : {}),
                        batch_id: `${batchRoot}_p${batchSeq}`,
                        source_mode: 'bg',
                        account: acc ? `${acc}:${targetMode}` : targetMode,
                        discovered: totalDiscovered,
                    });
                    if (sendRunId) soleBatchSentRunId = true;
                    totalSent += chunk.length;
                    totalAccepted += Number(body.accepted || 0);
                    totalSkipped += Number(body.skipped || 0);
                    if (refreshedHere > 0) totalRefreshed += refreshedHere;
                    if (body.task_id) lastTaskId = String(body.task_id);
                    for (const item of chunk) {
                        const key = item.id.toLowerCase();
                        pushedIds.add(key);
                        collected.delete(key);
                    }
                };

                const reconcileAndFlush = async (forceFinal = false) => {
                    do {
                        const needIds = orderedIds.filter(id => !(id in checkedStates));
                        if (needIds.length > 0) {
                            const chunkItems = needIds.map(id => collected.get(id) || {id});
                            const freshStates = await reconcileNotes(chunkItems, config);
                            for (const id of needIds) {
                                const st = freshStates[id];
                                checkedStates[id] = st;
                                effectiveStates[id] = st;
                                const item = collected.get(id);
                                const errRow = failedErrorMap[id];
                                if (targetFailedIds.has(id) && (st === 'archived' || st === 'pending' || item?.url?.includes('xsec_token='))) {
                                    seenTargetFailedIds.add(id);
                                }
                                const shouldRefresh =
                                    Boolean(errRow) &&
                                    st !== 'archived' &&
                                    st !== 'pending' &&
                                    Boolean(item?.url?.includes('xsec_token=')) &&
                                    (item.url !== String(errRow.url || '') || !errRow.retry_exhausted);
                                if ((st === 'missing' || shouldRefresh) && !pushedIds.has(id) && collected.has(id)) {
                                    if (shouldRefresh) {
                                        refreshIds.add(id);
                                        effectiveStates[id] = 'missing';
                                        if (targetFailedIds.has(id)) capturedFailedIds.add(id);
                                    }
                                    pendingBuffer.push(id);
                                } else {
                                    collected.delete(id);
                                }
                            }
                        }

                        while (
                            (!forceFinal && pendingBuffer.length >= streamBatchSize) ||
                            (forceFinal && pendingBuffer.length > 0)
                        ) {
                            const isSoleRunBatch =
                                forceFinal && batchSeq === 0 && pendingBuffer.length <= 500;
                            const chunkKeys = forceFinal
                                ? pendingBuffer.splice(0, 500)
                                : pendingBuffer.splice(0, streamBatchSize);
                            await flushBatch(chunkKeys, isSoleRunBatch);
                        }

                        if (!forceFinal && stopThreshold > 0) {
                            const pendingFailedTargetCount =
                                config.huntErrorsBeforeStop !== false
                                    ? Math.max(0, targetFailedIds.size - seenTargetFailedIds.size)
                                    : 0;
                            const early = evaluateXhsEarlyStop(orderedIds, effectiveStates, stopThreshold, {
                                pendingFailedTargetCount,
                            });
                            if (early.shouldStop) {
                                stoppedEarly = true;
                            }
                        }
                    } while (
                        forceFinal &&
                        (pendingBuffer.length > 0 || orderedIds.some(id => !(id in checkedStates)))
                    );
                };

                ingestList(loaded);
                if (orderedIds.length > 0) {
                    await reconcileAndFlush(false);
                }

                let idle = 0;
                for (let step = 0; step < maxBg && !stoppedEarly; step += 1) {
                    anomaly = detectXhsAnomaly();
                    if (anomaly) throw new Error(anomaly);
                    const before = totalDiscovered;
                    scrollContainers();
                    await sleep(1400);
                    try {
                        ingestList(readNotesForMode(targetMode));
                    } catch {}
                    const delta = totalDiscovered - before;
                    if (delta > 0) {
                        await reconcileAndFlush(false);
                    }
                    idle = delta === 0 ? idle + 1 : 0;
                    if (idle >= 8) break;
                }

                await reconcileAndFlush(true);
                discoveredCount = totalDiscovered;
                if (!totalDiscovered) {
                    await reportToNas(config, {
                        source_mode: 'bg',
                        run_id: bgRun.runId,
                        account: `${acc}:${targetMode}`,
                        status: 'empty',
                        discovered: 0,
                        message: `后台定时扫描小红书(${modes[targetMode]})为空，无新增作品`,
                    }, {strict: true});
                    await writeBgResult({
                        runId: bgRun.runId,
                        platform: 'xiaohongshu',
                        mode: targetMode,
                        status: 'ok',
                        reason: 'empty_list',
                        itemCount: 0,
                        finishedAt: new Date().toISOString(),
                    });
                    return;
                }

                const failSummary = targetFailedIds.size > 0
                    ? `，失败补抓 ${capturedFailedIds.size}/${targetFailedIds.size}`
                    : '';
                if (!totalSent) {
                    await reportToNas(config, {
                        source_mode: 'bg',
                        run_id: bgRun.runId,
                        account: `${acc}:${targetMode}`,
                        status: 'empty',
                        discovered: totalDiscovered,
                        message: `已向 NAS 核对 ${totalDiscovered} 个小红书作品 ID，没有待下载项${failSummary}`,
                    }, {strict: true});
                } else if (!soleBatchSentRunId || batchSeq > 1) {
                    await reportToNas(config, {
                        source_mode: 'bg',
                        run_id: bgRun.runId,
                        account: `${acc}:${targetMode}`,
                        ...(limited ? {category: targetMode} : {}),
                        status: 'ok',
                        discovered: totalDiscovered,
                        item_count: totalSent,
                        accepted: totalAccepted,
                        submitted: totalSent,
                        skipped: totalSkipped,
                        ...(lastTaskId ? {task_id: lastTaskId} : {}),
                        message: `流式核对完成：共扫 ${totalDiscovered} 个作品，提交 ${totalSent} 个（新增 ${totalAccepted}${totalRefreshed ? `，含重推失败作品 ${totalRefreshed} 个` : ''}${failSummary}）`,
                    }, {strict: true});
                }
                await writeBgResult({
                    runId: bgRun.runId,
                    platform: 'xiaohongshu',
                    mode: targetMode,
                    status: 'ok',
                    reason: totalSent ? 'pushed' : 'no_new_items',
                    itemCount: totalSent,
                    accepted: totalAccepted,
                    skipped: totalSkipped,
                    refreshed: totalRefreshed,
                    failedTargetCount: targetFailedIds.size,
                    failedCapturedCount: capturedFailedIds.size,
                    taskId: lastTaskId,
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
    let states = {};
    let busy = false;
    let stopped = false;
    let context = '';
    let lastErrorSyncAt = 0;
    let errorSyncInFlight = false;
    let liveRefreshTimer = null;
    let errorCache = Array.isArray(GM_getValue(STORAGE_KEYS.ERROR_CACHE, []))
        ? GM_getValue(STORAGE_KEYS.ERROR_CACHE, [])
        : [];
    const autoSubmittedErrorIds = new Set();
    const submittedErrIds = new Set();

    const buildErrorMap = rows =>
        Object.fromEntries(
            (Array.isArray(rows) ? rows : [])
                .filter(row => row?.source_id)
                .map(row => [String(row.source_id).toLowerCase(), row])
        );

    const activeAccount = () => accountId(location.pathname);

    const detectCurrentDomMode = () => {
        if (!activeAccount()) return 'page';
        try {
            const activeTab = document.querySelector(
                '.reds-tab-item.active, .reds-tab-item[aria-selected="true"], [role="tab"][aria-selected="true"], .tab-item.active'
            );
            const label = String(activeTab?.textContent || '')
                .trim()
                .replace(/\s*[\d.,万千wWkK+]+\s*$/, '')
                .trim();
            if (label) {
                for (const [k, names] of Object.entries(TAB_NAMES)) {
                    if (names.includes(label) || names.some(n => label.includes(n))) return k;
                }
            }
            const tabParam = new URLSearchParams(location.search).get('tab') || '';
            if (/^like/i.test(tabParam)) return 'liked';
            if (/^(?:collect|save|sub)/i.test(tabParam)) return 'saved';
            if (/^(?:note|post|publish)/i.test(tabParam)) return 'published';
        } catch {}
        return mode;
    };

    const captureVisiblePassive = () => {
        let changed = 0;
        for (const [apiCat, bucket] of Object.entries(apiCaptured)) {
            if (bucket && bucket.size > 0) {
                changed += recordFreshCaptured(
                    [...bucket.values()],
                    apiCat === 'liked' || apiCat === 'saved' ? apiCat : ''
                );
            }
        }
        const domMap = collectDomLinkMap();
        if (activeAccount()) {
            const domTab = detectCurrentDomMode();
            const userRef = findVueRef('user');
            const fallbackUser = unwrap(pageWin?.__INITIAL_STATE__?.user);
            const notesArr = unwrap(userRef?.notes) || unwrap(userRef?.$state?.notes) || unwrap(fallbackUser?.notes);
            for (const [tabKey, idx] of Object.entries(indices)) {
                const rawList = unwrap(notesArr?.[idx]);
                if (Array.isArray(rawList) && rawList.length > 0) {
                    const normalized = rawList
                        .map(entry => normalizeNote(entry, tabKey === domTab ? domMap : null))
                        .filter(Boolean);
                    changed += recordFreshCaptured(
                        normalized,
                        tabKey === 'liked' || tabKey === 'saved' ? tabKey : ''
                    );
                }
            }
            if (domMap.size > 0) {
                const domCat = domTab === 'liked' || domTab === 'saved' ? domTab : '';
                const domNotes = [...domMap.values()].map(d => ({
                    id: d.id,
                    url: d.url,
                    title: d.title || d.id,
                    author: '',
                    cover: d.cover || '',
                }));
                changed += recordFreshCaptured(domNotes, domCat);
            }
        } else if (domMap.size > 0) {
            const domNotes = [...domMap.values()].map(d => ({
                id: d.id,
                url: d.url,
                title: d.title || d.id,
                author: '',
                cover: d.cover || '',
            }));
            changed += recordFreshCaptured(domNotes, '');
        }
        return changed;
    };

    const refreshErrorsSilently = async (force = false) => {
        if (!config.token || errorSyncInFlight) return errorCache;
        const now = Date.now();
        if (!force && now - lastErrorSyncAt < 30000) return errorCache;
        errorSyncInFlight = true;
        try {
            errorCache = await fetchNasErrors(config);
            lastErrorSyncAt = Date.now();
            return errorCache;
        } catch {
            return errorCache;
        } finally {
            errorSyncInFlight = false;
        }
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

    document.querySelectorAll('#nas-xhs-workbench').forEach(node => node.remove());
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
.launch{position:fixed;left:24px;bottom:28px;z-index:2147483646;background:#e94465;color:white;border:0;box-shadow:0 6px 24px #63263730;border-radius:18px;padding:13px 18px;font-weight:650;transition:background .2s}
.launch.pill-done{background:#059669}
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
.err-prog{border-radius:10px;padding:9px 11px;font-size:12px;margin:6px 0 8px;background:#fff7ed;color:#9a3412;border:1px solid #fed7aa}
.err-prog.done{background:#ecfdf5;color:#047857;border-color:#a7f3d0}
.err-prog.idle{background:#f1f5f9;color:#475569;border-color:#e2e8f0}
.err-badge{display:inline-block;font-size:11px;padding:1px 7px;border-radius:999px;font-weight:600;margin-left:6px}
.err-badge.ready{background:#dcfce7;color:#166534}.err-badge.pending{background:#e0f2fe;color:#0369a1}.err-badge.wait{background:#f1f5f9;color:#64748b}
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
  <div class="row"><button id="scan" class="grow">读取已加载</button><button id="load" class="grow">自动加载</button><button id="stop" class="hidden">停止</button></div>
  <label class="inline" style="margin:6px 0 2px"><input id="full-scan" type="checkbox"> 全量扫描（不早停，滚到底核对历史遗漏）</label>
  <label class="inline" style="margin:2px 0 6px"><input id="hunt-errors" type="checkbox"> 自动加载/后台追捕全部失败项（未抓齐前不早停）</label>
  <p class="muted">按作品 ID 与 NAS 实时核对，已归档与正在处理跳过，已捕获失败作品会随增量一并重推。</p>
  <div class="row spread"><small id="count">尚未读取作品</small><button id="send-batch" class="primary">发送待下载作品</button></div>
  <div id="list" class="list"></div>
</div>
<div class="box">
  <div class="row spread"><h3>NAS 失败补抓与重试</h3><button id="sync-errors" class="quiet">同步 NAS 错误列表</button></div>
  <div id="failed-progress" class="err-prog idle"></div>
  <div class="row" style="margin-bottom:6px">
    <button id="push-captured-errors" class="primary grow">一键推送已捕获失败项</button>
    <button id="capture-detail-direct">详情抓图直推 NAS</button>
  </div>
  <label class="inline" style="margin:4px 0"><input id="auto-retry-error" type="checkbox"> 打开失败作品页时自动抓图直推 NAS（方式 A）</label>
  <div id="error-summary" class="muted"></div>
  <div id="error-list" class="list"></div>
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
  <div class="row"><label class="grow">端口<input id="port"></label><label class="grow">手动滚动上限<input id="max" type="number" min="1" max="1000"></label><label class="grow">后台滚动上限<input id="max-bg" type="number" min="1" max="1000"></label></div>
  <div class="row"><label class="grow">早停连续已归档数（0=全量不早停）<input id="stop-consecutive" type="number" min="0" max="500"></label></div>
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
    const ensureMounted = () => {
        if (!document.body || bgRun.runId) return;
        if (!host.isConnected) document.body.append(host);
    };
    ensureMounted();
    setTimeout(ensureMounted, 500);
    setTimeout(ensureMounted, 1500);
    setTimeout(ensureMounted, 3000);
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

    const updatePillOnly = () => {
        const errRows = Array.isArray(errorCache) ? errorCache : [];
        const domMode = detectCurrentDomMode();
        const filterMode = domMode === 'liked' || domMode === 'saved' ? domMode : '';
        const prog = computeFailedCaptureProgress(errRows, freshCapturedById, filterMode);
        const pill = $('.launch');
        if (!pill) return;
        if (prog.total > 0) {
            pill.textContent = prog.allTotalCaptured
                ? `✓ NAS · 失败 ${prog.capturedTotal}/${prog.total}`
                : `↓ NAS · 失败 ${prog.capturedTotal}/${prog.total}`;
            pill.classList.toggle('pill-done', prog.allTotalCaptured);
        } else {
            pill.textContent = '↓ NAS 工作台';
            pill.classList.remove('pill-done');
        }
        return prog;
    };

    function render() {
        syncOwnProfileUrl();
        const acc = activeAccount();
        $('#account').textContent = acc ? `账号 …${acc.slice(-6)}` : '未在账号主页';
        shadow.querySelectorAll('[data-mode]').forEach(button => button.classList.toggle('active', button.dataset.mode === mode));
        const errRows = Array.isArray(errorCache) ? errorCache : [];
        const errMap = buildErrorMap(errRows);
        const checked = Object.keys(states).length;
        const missing = checked
            ? selectPushCandidates(items, states, errMap, {requireFreshDiff: false}).allItems.length
            : 0;
        $('#count').textContent = checked
            ? `已加载 ${items.length} · 待提交 ${missing}`
            : `已加载 ${items.length} · 等待 NAS 核对`;
        $('#list').replaceChildren();
        for (const item of items.slice(0, 120)) {
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
            const lowerId = String(item.id || '').toLowerCase();
            const st = states[lowerId];
            if (errMap[lowerId] && st !== 'archived' && st !== 'pending') {
                tag.textContent = '失败待补';
            } else {
                tag.textContent = {missing: '待下载', archived: '已归档', pending: '处理中', deferred: '暂缓重试'}[st] || '待核对';
            }
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

        const progMode = mode === 'liked' || mode === 'saved' ? mode : '';
        const prog = computeFailedCaptureProgress(errRows, freshCapturedById, progMode);
        const pill = $('.launch');
        if (prog.total > 0) {
            pill.textContent = prog.allTotalCaptured
                ? `✓ NAS · 失败 ${prog.capturedTotal}/${prog.total}`
                : `↓ NAS · 失败 ${prog.capturedTotal}/${prog.total}`;
            pill.classList.toggle('pill-done', prog.allTotalCaptured);
        } else {
            pill.textContent = '↓ NAS 工作台';
            pill.classList.remove('pill-done');
        }

        const progBox = $('#failed-progress');
        if (!prog.total) {
            progBox.className = 'err-prog idle';
            progBox.textContent = 'NAS 当前没有待重下的失败记录。';
        } else if (prog.allTotalCaptured) {
            progBox.className = 'err-prog done';
            progBox.textContent = `已捕获全部 ${prog.capturedTotal}/${prog.total} 条失败作品的新鲜链接！可点「一键推送已捕获失败项」或「发送待下载作品」交给 NAS。`;
        } else if (progMode) {
            progBox.className = 'err-prog';
            const modeHint =
                prog.relevantTotal > 0
                    ? `当前「${modes[progMode]}」${prog.capturedRelevant}/${prog.relevantTotal}${
                          prog.remainingRelevant > 0
                              ? `，还差 ${prog.remainingRelevant} 条，向下滚动或点「自动加载」`
                              : '，当前分类已追齐'
                      }`
                    : '当前分类暂无失败记录，请检查另一分类';
            progBox.textContent = `后台失败已获取：${prog.capturedTotal}/${prog.total}（${modeHint}）`;
        } else {
            progBox.className = 'err-prog';
            progBox.textContent = `后台失败已获取：${prog.capturedTotal}/${prog.total}（打开账号主页的点赞或收藏并向下滚动即可自动追捕）`;
        }

        const pushErrBtn = $('#push-captured-errors');
        pushErrBtn.textContent =
            prog.capturedTotal > 0
                ? `一键推送已捕获失败项 (${prog.capturedTotal}/${prog.total})`
                : '一键推送已捕获失败项';

        $('#error-summary').textContent = errRows.length
            ? `NAS 失败共 ${errRows.length} 条 · 已捕新链接 ${prog.capturedTotal} 条（滚动列表自动升级链接，点开详情也能直抓）`
            : '点击右上角同步 NAS 失败列表';

        const errListEl = $('#error-list');
        errListEl.replaceChildren();
        const sortedErrRows = [...errRows].sort((a, b) => {
            const aId = String(a?.source_id || '').toLowerCase();
            const bId = String(b?.source_id || '').toLowerCase();
            const aReady = prog.capturedMap[aId] ? 1 : 0;
            const bReady = prog.capturedMap[bId] ? 1 : 0;
            return bReady - aReady;
        });
        for (const errRow of sortedErrRows.slice(0, 60)) {
            const sid = String(errRow.source_id || '').toLowerCase();
            const fresh = prog.capturedMap[sid] || freshCapturedById.get(sid);
            const hasFreshToken = Boolean(fresh?.url && String(fresh.url).includes('xsec_token='));
            const isQueued = submittedErrIds.has(sid) || states[sid] === 'pending';

            const rowEl = document.createElement('div');
            rowEl.className = 'note';
            const body = document.createElement('div');
            body.className = 'grow';
            const titleLine = document.createElement('div');
            const titleLink = document.createElement('a');
            titleLink.className = 'title';
            titleLink.href = hasFreshToken
                ? fresh.url
                : errRow.url || `${location.origin}/explore/${errRow.source_id}`;
            titleLink.target = '_blank';
            titleLink.rel = 'noopener noreferrer';
            titleLink.textContent =
                (fresh?.title && fresh.title !== sid ? fresh.title : errRow.title) || errRow.source_id;
            const badge = document.createElement('span');
            badge.className = `err-badge ${isQueued ? 'pending' : hasFreshToken ? 'ready' : 'wait'}`;
            badge.textContent = isQueued
                ? '⏳ 排队重下中'
                : hasFreshToken
                  ? '✓ 已获取新链接'
                  : '待滚动捕获';
            titleLine.append(titleLink, badge);

            const reasonEl = document.createElement('div');
            reasonEl.className = 'muted';
            reasonEl.textContent = `${errRow.source_id} · ${errRow.error || '下载失败'}`;
            const actionsEl = document.createElement('div');
            actionsEl.className = 'actions';
            const openBtn = document.createElement('a');
            openBtn.href = titleLink.href;
            openBtn.target = '_blank';
            openBtn.rel = 'noopener noreferrer';
            openBtn.textContent = hasFreshToken ? '打开新鲜链接' : '打开作品';
            const dismissBtn = document.createElement('button');
            dismissBtn.textContent = '忽略';
            dismissBtn.disabled = busy;
            dismissBtn.onclick = () => execute(async () => {
                await dismissNasError(config, errRow.source_id);
                errorCache = errorCache.filter(x => x.source_id !== errRow.source_id);
                await gmSetAll({[STORAGE_KEYS.ERROR_CACHE]: errorCache});
                status(`已忽略失败记录 ${errRow.source_id}。`);
            });
            actionsEl.append(openBtn, dismissBtn);
            body.append(titleLine, reasonEl, actionsEl);
            rowEl.append(body);
            errListEl.append(rowEl);
        }

        shadow.querySelectorAll('button').forEach(button => {
            if (!button.matches('.launch,#close,#stop')) button.disabled = busy;
        });
        if (!busy && prog.capturedTotal === 0) pushErrBtn.disabled = true;
        $('#stop').classList.toggle('hidden', !busy);
        $('#progress').classList.toggle('hidden', !busy);
    }

    const scheduleLiveRefresh = () => {
        if (bgRun.runId || liveRefreshTimer) return;
        liveRefreshTimer = setTimeout(() => {
            liveRefreshTimer = null;
            captureVisiblePassive();
            const panelOpen = !$('.panel')?.classList?.contains('hidden');
            if (!busy && panelOpen) render();
            else updatePillOnly();
        }, 280);
    };
    onLiveCapture = () => scheduleLiveRefresh();

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
        if (config.token) await refreshErrorsSilently(false);
        await activateTabForMode(mode);
        items = read();
        states = {};
        const snapshot = context;
        const collected = new Map(items.map(item => [item.id, item]));
        const fullScan = Boolean($('#full-scan')?.checked);
        const huntErrors = !fullScan && Boolean($('#hunt-errors')?.checked);
        const stopThreshold = fullScan ? 0 : Math.max(0, Number(config.stopConsecutive ?? 30));

        const reconcileUnchecked = async () => {
            const currentList = [...collected.values()];
            const need = currentList.filter(it => !(it.id?.toLowerCase() in states));
            if (need.length > 0 && config.token) {
                Object.assign(states, await reconcileNotes(need));
            }
            return currentList;
        };

        if (auto) {
            let idle = 0;
            if (items.length > 0 && config.token) {
                await reconcileUnchecked();
                render();
            }
            for (let step = 0; step < config.max && !stopped; step++) {
                guard();
                const errMapNow = buildErrorMap(errorCache);
                const errProgNow = computeFailedCaptureProgress(errorCache, freshCapturedById, mode);
                if (stopThreshold > 0 && config.token) {
                    const early = evaluateXhsEarlyStop([...collected.keys()], states, stopThreshold, {
                        pendingFailedTargetCount: huntErrors ? errProgNow.remainingRelevant : 0,
                    });
                    if (early.shouldStop) {
                        const pendingCnt = selectPushCandidates(items, states, errMapNow, {
                            requireFreshDiff: false,
                        }).allItems.length;
                        const failDoneHint =
                            errProgNow.relevantTotal > 0
                                ? ` · 失败补抓 ${errProgNow.capturedRelevant}/${errProgNow.relevantTotal}`
                                : '';
                        status(
                            `已命中连续 ${stopThreshold} 个已处理作品${failDoneHint}，停止加载：${items.length} 个作品，${pendingCnt} 个待提交。`
                        );
                        break;
                    }
                }
                const count = collected.size;
                scrollContainers();
                await sleep(1200);
                guard();
                const freshList = read();
                freshList.forEach(item => collected.set(item.id, item));
                if (context !== snapshot) throw new Error('页面已切换，请重新读取。');
                const delta = collected.size - count;
                idle = delta === 0 ? idle + 1 : 0;
                items = await reconcileUnchecked();
                render();

                const errMapAfter = buildErrorMap(errorCache);
                const errProgAfter = computeFailedCaptureProgress(errorCache, freshCapturedById, mode);
                const pendingCntAfter = Object.keys(states).length
                    ? selectPushCandidates(items, states, errMapAfter, {requireFreshDiff: false}).allItems.length
                    : items.length;
                const early = stopThreshold > 0 && Object.keys(states).length
                    ? evaluateXhsEarlyStop([...collected.keys()], states, stopThreshold, {
                          pendingFailedTargetCount: huntErrors ? errProgAfter.remainingRelevant : 0,
                      })
                    : {consecutiveSettled: 0, shouldStop: false, reachedThreshold: false};
                const failHint =
                    errProgAfter.relevantTotal > 0
                        ? ` · 失败补抓 ${errProgAfter.capturedRelevant}/${errProgAfter.relevantTotal}`
                        : '';
                const stopHint = early.reachedThreshold && !early.shouldStop
                    ? ` · 已达归档阈值，继续追捕剩余 ${errProgAfter.remainingRelevant} 条失败项`
                    : stopThreshold > 0
                      ? ` · 连续已归档 ${early.consecutiveSettled}/${stopThreshold}`
                      : '';
                status(`正在加载 ${modes[mode]} · ${items.length} 个作品 · 待提交 ${pendingCntAfter}${failHint}${stopHint} · 第 ${step + 1} 次滚动`);
                if (early.shouldStop) {
                    status(`已命中连续 ${stopThreshold} 个已处理作品${failHint}，停止加载：${items.length} 个作品，${pendingCntAfter} 个待提交。`);
                    break;
                }
                if (idle >= 5) break;
            }
        }
        items = [...collected.values()];
        const remaining = items.filter(it => !(it.id?.toLowerCase() in states));
        if (remaining.length > 0 || !Object.keys(states).length) {
            Object.assign(states, await reconcileNotes(remaining.length ? remaining : items));
        }
        guard();
        const finalMap = buildErrorMap(errorCache);
        const finalPicked = selectPushCandidates(items, states, finalMap, {requireFreshDiff: false});
        const finalProg = computeFailedCaptureProgress(errorCache, freshCapturedById, mode);
        const refreshNote = finalPicked.failedRefreshItems.length
            ? `（含可重推失败作品 ${finalPicked.failedRefreshItems.length} 条）`
            : '';
        const leftoverHint =
            !stopped && finalProg.remainingRelevant > 0
                ? `；当前分类仍有 ${finalProg.remainingRelevant} 条未出现在列表（可能位于另一分类或已被作者删除）`
                : '';
        status(
            `${stopped ? '已停止加载' : '读取完成'}：${items.length} 个作品，${finalPicked.allItems.length} 个待提交${refreshNote}${leftoverHint}。`
        );
    }

    async function push(notes, destination) {
        const res = await pushNotes(notes, {
            destination,
            activeMode: mode,
            account: activeAccount(),
            discovered: items.length || notes.length,
            sourceMode: destination === 'single' ? 'single' : 'workbench',
            failedErrorMap: buildErrorMap(errorCache),
            requireFreshDiff: false,
            onStatus: msg => status(msg),
        });
        for (const sid of res.pushedIds || []) submittedErrIds.add(sid);
        const refreshHint = res.refreshed ? `（含顺带重推失败作品 ${res.refreshed} 条）` : '';
        status(`NAS 新入队 ${res.accepted} 个，跳过 ${res.skipped} 个${refreshHint}。可在下载记录查看进度。`);
        if (config.token) await refreshErrorsSilently(true);
        if (items.length && context === `${activeAccount()}:${mode}:${location.pathname}`) {
            states = await reconcileNotes(items);
        }
    }

    function currentNote() {
        const state = findVueRef('note') || unwrap(pageWin?.__INITIAL_STATE__?.note);
        const id = noteId(unwrap(state?.currentNoteId) || unwrap(state?.$state?.currentNoteId) || location.pathname);
        if (!id) throw new Error('请先打开一个作品，或粘贴作品完整链接。');
        const detail = findVueNoteDetail(id);
        let token = '';
        if (detail?.url && detail.url.includes('xsec_token=')) {
            try {
                token = new URL(detail.url).searchParams.get('xsec_token') || '';
            } catch {}
        }
        const url = new URL(location.href);
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
                timeoutMs: config.maxBgScrolls * 2200 + 120000,
                timeoutMessage: '后台标签页扫描超时',
                openTab: () => {
                    tab = GM_openInTab(tabUrl, {active: false, insert: true});
                    return tab;
                },
            });
            if (res.status === 'ok') {
                const bgFailHint = Number(res.failedTargetCount || 0) > 0
                    ? `，失败补抓 ${res.failedCapturedCount || 0}/${res.failedTargetCount}`
                    : '';
                status(`后台试跑(${modes[targetMode]})成功：reason=${res.reason}，提交 ${res.itemCount || 0} 条${bgFailHint}。`);
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
        $('#stop-consecutive').value = String(config.stopConsecutive ?? 30);
        $('#bg-url').value = config.bgProfileUrl;
        $('#daily-enabled').checked = config.dailyBgEnabled !== false;
        $('#auto-retry-error').checked = config.autoRetryErrorDetail !== false;
        $('#hunt-errors').checked = config.huntErrorsBeforeStop !== false;
        $('#bg-mode-liked').checked = config.bgModes.includes('liked');
        $('#bg-mode-saved').checked = config.bgModes.includes('saved');
    };
    syncFormFields();

    $('.launch').onclick = () => {
        captureVisiblePassive();
        $('.panel').classList.toggle('hidden');
        render();
        if (!$('.panel').classList.contains('hidden')) {
            refreshErrorsSilently(false).then(() => render()).catch(() => {});
        }
    };
    $('#close').onclick = () => $('.panel').classList.add('hidden');
    shadow.querySelectorAll('[data-mode]').forEach(button => {
        button.onclick = () => { mode = button.dataset.mode; items = []; states = {}; context = ''; render(); };
    });
    $('#scan').onclick = () => execute(() => scan(false));
    $('#load').onclick = () => execute(() => scan(true));
    $('#stop').onclick = () => { stopped = true; status('正在停止，等待当前操作完成…'); };
    $('#send-batch').onclick = () => execute(async () => {
        const expectedCtx = `${activeAccount()}:${mode}:${location.pathname}`;
        if (!items.length || context !== expectedCtx) {
            items = read();
        }
        guard();
        await push(items, 'batch');
    });
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

    const extractCurrentDetailDirectItem = targetId => {
        const vueNote = findVueRef('note') || unwrap(pageWin?.__INITIAL_STATE__?.note);
        const curVueId = unwrap(vueNote?.currentNoteId) || unwrap(vueNote?.$state?.currentNoteId);
        const id = noteId(targetId || curVueId || location.pathname).toLowerCase();
        if (!id) return null;
        const extracted = findVueNoteDetail(id) || extractXhsDomDetailMedia(id, location.href);
        if (!extracted || !extracted.media?.length) return null;
        const matchedErr = (Array.isArray(errorCache) ? errorCache : []).find(
            x => String(x?.source_id || '').toLowerCase() === id
        );
        return {
            ...extracted,
            ...(matchedErr?.category ? {category: matchedErr.category} : {}),
        };
    };

    const submitDetailDirectNote = async (targetId, sourceMode = 'error_retry') => {
        const manifest = extractCurrentDetailDirectItem(targetId);
        if (!manifest) {
            throw new Error('暂未从当前页面捕获到原图或视频直链，请确认已打开作品详情页并等待加载完成。');
        }
        const res = await pushXhsDirect(config, [manifest], {
            sourceMode,
            category: manifest.category || '',
            account: activeAccount(),
        });
        submittedErrIds.add(manifest.note_id);
        errorCache = (Array.isArray(errorCache) ? errorCache : []).filter(
            x => String(x?.source_id || '').toLowerCase() !== manifest.note_id
        );
        await gmSetAll({[STORAGE_KEYS.ERROR_CACHE]: errorCache});
        status(
            `浏览器抓图直推成功！作品 ${manifest.note_id}（${manifest.media.length} 份媒体）→ NAS 新增 ${res.accepted} · 跳过 ${res.skipped}`
        );
        return {manifest, res};
    };

    const checkAutoRetryDetailPage = async () => {
        if (config.autoRetryErrorDetail === false || !config.token) return;
        const curId = noteId(location.pathname).toLowerCase();
        if (!curId || autoSubmittedErrorIds.has(curId)) return;
        const latestCache = GM_getValue(STORAGE_KEYS.ERROR_CACHE, errorCache);
        if (Array.isArray(latestCache)) errorCache = latestCache;
        const hit = (Array.isArray(errorCache) ? errorCache : []).some(
            x => String(x?.source_id || '').toLowerCase() === curId
        );
        if (!hit) return;
        autoSubmittedErrorIds.add(curId);
        for (let attempt = 0; attempt < 16; attempt += 1) {
            if (noteId(location.pathname).toLowerCase() !== curId) return;
            if (extractCurrentDetailDirectItem(curId)) {
                try {
                    const {manifest, res} = await submitDetailDirectNote(curId, 'error_retry');
                    render();
                    if (typeof GM_notification === 'function') {
                        try {
                            GM_notification({
                                title: '小红书失败作品已自动补传 NAS',
                                text: `${manifest.title || manifest.note_id}（${manifest.media.length} 份媒体，新增 ${res.accepted}）`,
                                timeout: 4500,
                            });
                        } catch {}
                    }
                } catch (err) {
                    status(`自动直推作品 ${curId} 失败：${err?.message || err}`, true);
                }
                return;
            }
            await sleep(400);
        }
        status(`已进入失败作品页 ${curId}，但暂未捕获到媒体直链，可点「详情抓图直推 NAS」手动重试。`, true);
    };

    $('#sync-errors').onclick = () => execute(async () => {
        captureVisiblePassive();
        errorCache = await fetchNasErrors(config);
        lastErrorSyncAt = Date.now();
        const prog = computeFailedCaptureProgress(errorCache, freshCapturedById, mode === 'liked' || mode === 'saved' ? mode : '');
        status(
            `已同步 ${errorCache.length} 条失败记录，当前已从页面/滚动捕获 ${prog.capturedTotal}/${prog.total} 条新链接。`
        );
    });

    $('#push-captured-errors').onclick = () => execute(async () => {
        captureVisiblePassive();
        const prog = computeFailedCaptureProgress(errorCache, freshCapturedById, '');
        const capturedList = Object.values(prog.capturedMap);
        if (!capturedList.length) {
            throw new Error('暂未捕获到带新鲜 xsec_token 的失败作品，请在点赞/收藏页向下滚动或点击「自动加载」。');
        }
        const groups = new Map();
        for (const item of capturedList) {
            const cat = item.category === 'liked' || item.category === 'saved' ? item.category : '';
            if (!groups.has(cat)) groups.set(cat, []);
            groups.get(cat).push(item);
        }
        let totalSubmitted = 0;
        let totalAccepted = 0;
        let totalSkipped = 0;
        const acc = activeAccount();
        for (const [cat, list] of groups.entries()) {
            for (let offset = 0; offset < list.length; offset += 500) {
                const chunk = list.slice(offset, offset + 500);
                const body = await requestSubmit(config, {
                    urls: chunk.map(x => x.url),
                    destination: 'batch',
                    ...(cat ? {category: cat} : {}),
                    force_retry: true,
                    batch_id: makeBatchId(`xhs_err_${cat || 'mix'}`),
                    source_mode: 'error_retry',
                    account: acc ? `${acc}:${cat || 'error_retry'}` : (cat || 'error_retry'),
                    discovered: capturedList.length,
                });
                totalSubmitted += chunk.length;
                totalAccepted += Number(body.accepted || 0);
                totalSkipped += Number(body.skipped || 0);
                for (const x of chunk) {
                    const sid = noteId(x.id || x.url).toLowerCase();
                    if (sid) {
                        submittedErrIds.add(sid);
                        if (states[sid]) states[sid] = 'pending';
                    }
                }
            }
        }
        if (config.token) {
            errorCache = await fetchNasErrors(config).catch(() => errorCache);
            if (items.length) states = await reconcileNotes(items).catch(() => states);
        }
        status(`已一键重推 ${totalSubmitted} 条捕获到的失败作品 → NAS 新增入队 ${totalAccepted}，跳过 ${totalSkipped}。`);
    });

    $('#auto-retry-error').onchange = () => {
        saveConfig({autoRetryErrorDetail: Boolean($('#auto-retry-error').checked)}).catch(() => {});
    };
    $('#hunt-errors').onchange = () => {
        saveConfig({huntErrorsBeforeStop: Boolean($('#hunt-errors').checked)}).catch(() => {});
    };
    $('#capture-detail-direct').onclick = () => execute(async () => {
        await submitDetailDirectNote('', 'workbench');
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
        const nextStopConsecutive = Number($('#stop-consecutive').value);
        if (!Number.isInteger(nextMax) || nextMax < 1 || nextMax > 1000) {
            throw new Error('手动滚动次数应为 1–1000 的整数。');
        }
        if (!Number.isInteger(nextBgMax) || nextBgMax < 1 || nextBgMax > 1000) {
            throw new Error('后台滚动次数应为 1–1000 的整数。');
        }
        if (!Number.isInteger(nextStopConsecutive) || nextStopConsecutive < 0 || nextStopConsecutive > 500) {
            throw new Error('早停连续数应为 0–500 的整数。');
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
            stopConsecutive: nextStopConsecutive,
            idSync: true,
            bgProfileUrl: $('#bg-url').value.trim().split('#')[0],
            dailyBgEnabled: $('#daily-enabled').checked,
            autoRetryErrorDetail: $('#auto-retry-error').checked,
            huntErrorsBeforeStop: $('#hunt-errors').checked,
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

    window.addEventListener('scroll', scheduleLiveRefresh, {passive: true, capture: true});
    document.addEventListener('scroll', scheduleLiveRefresh, {passive: true, capture: true});

    let lastUrl = location.href;
    let lastPath = location.pathname;
    setInterval(() => {
        ensureMounted();
        if (lastUrl === location.href) return;
        lastUrl = location.href;
        if (lastPath !== location.pathname) {
            lastPath = location.pathname;
            apiCaptured.liked.clear();
            apiCaptured.saved.clear();
            apiCaptured.published.clear();
        }
        stopped = true;
        items = [];
        states = {};
        context = '';
        captureVisiblePassive();
        syncFormFields();
        render();
        status('页面已切换，请重新读取作品。');
        checkAutoRetryDetailPage().catch(() => {});
    }, 700);
    GM_registerMenuCommand('打开小红书 NAS 下载工作台', () => {
        ensureMounted();
        captureVisiblePassive();
        $('.panel').classList.remove('hidden');
        render();
        refreshErrorsSilently(false).then(() => render()).catch(() => {});
    });
    captureVisiblePassive();
    render();
    if (config.token) {
        refreshErrorsSilently(true).then(() => render()).catch(() => {});
    }
    checkAutoRetryDetailPage().catch(() => {});
    });
})();
