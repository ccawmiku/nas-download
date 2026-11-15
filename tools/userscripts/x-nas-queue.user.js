// ==UserScript==
// @name         X · NAS 下载工作台
// @namespace    https://yaemiko.lol/nas-download
// @version      1.0.0
// @description  拦截 X GraphQL 响应直接提取无水印原图与最高码率 MP4 直链，按推文/引用双重归档（方案 A），支持 5 锚点增量同步、NAS Token 直推与每日后台配合
// @match        https://x.com/*
// @match        https://twitter.com/*
// @run-at       document-start
// @early-start
// @storageName  nas_prod_x_v1
// @grant        unsafeWindow
// @grant        GM.getValue
// @grant        GM.setValues
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_addValueChangeListener
// @grant        GM_removeValueChangeListener
// @grant        GM_openInTab
// @grant        GM_xmlhttpRequest
// @grant        GM_registerMenuCommand
// @connect      *
// @noframes
// ==/UserScript==

(() => {
  'use strict';

  const STORAGE_KEYS = {
    CONFIG: 'nas_prod_x_config_v1',
    ANCHORS_PREFIX: 'nas:prod:x:anchors:',
    CUTOFF_PREFIX: 'nas:prod:x:cutoff:',
    DAILY_STATE: 'nas_prod_x_daily_state',
    BG_RESULT: 'nas_prod_x_bg_result',
  };

  const MAX_ANCHORS = 5;
  const CHUNK_SIZE = 200;

  const isLikesGraphqlUrl = (url) =>
    /\/(?:i\/api\/)?graphql\/[^/]+\/Likes(?:[/?#]|$)/.test(String(url || ''));

  const isTweetDetailGraphqlUrl = (url) =>
    /\/(?:i\/api\/)?graphql\/[^/]+\/Tweet(?:ResultByRestId|Detail)(?:[/?#]|$)/.test(
      String(url || '')
    );

  const isLikesPagePath = (pathname) =>
    /^\/(?:i\/history\/likes|[^/]+\/likes)\/?$/.test(String(pathname || ''));

  const resolveLikesAccount = (pathname, fallbackHandle = '') => {
    const p = String(pathname || '');
    if (/^\/i\/history\/likes\/?$/.test(p)) {
      return String(fallbackHandle || 'i_history_likes');
    }
    const m = p.match(/^\/([^/]+)\/likes\/?$/);
    if (m && m[1] !== 'i') return m[1];
    return String(fallbackHandle || 'default');
  };

  const tweetIdFromUrl = (raw) =>
    String(raw || '').match(/\/status\/(\d{1,30})/)?.[1] ||
    String(raw || '').match(/^(\d{5,30})$/)?.[1] ||
    '';

  const unwrapTweetResult = (node) => {
    let cur = node;
    for (let i = 0; i < 4 && cur; i += 1) {
      if (cur.__typename === 'TweetWithVisibilityResults' && cur.tweet) {
        cur = cur.tweet;
        continue;
      }
      if (cur.result) {
        cur = cur.result;
        continue;
      }
      break;
    }
    if (!cur || cur.__typename === 'TweetTombstone' || cur.__typename === 'TweetUnavailable') {
      return null;
    }
    return cur;
  };

  const toOrigImageUrl = (mediaUrlHttps) => {
    const raw = String(mediaUrlHttps || '').trim();
    if (!raw) return '';
    try {
      const u = new URL(raw);
      const extMatch = u.pathname.match(/\.([a-zA-Z0-9]+)$/);
      const format = (u.searchParams.get('format') || extMatch?.[1] || 'jpg').toLowerCase();
      if (extMatch) {
        u.pathname = u.pathname.slice(0, -extMatch[0].length);
      }
      u.searchParams.set('format', format === 'jpeg' ? 'jpg' : format);
      u.searchParams.set('name', 'orig');
      return u.toString();
    } catch {
      return raw;
    }
  };

  const extractMediaList = (tweetNode) => {
    if (!tweetNode) return [];
    const legacy = tweetNode.legacy || {};
    const rawMedia = legacy.extended_entities?.media || legacy.entities?.media || [];
    if (!Array.isArray(rawMedia)) return [];
    return rawMedia.map((m) => {
      const type = String(m?.type || 'unknown');
      const coverUrl = String(m?.media_url_https || m?.media_url || '');
      const mediaKey = String(m?.media_key || m?.id_str || '');
      if (type === 'photo') {
        const directUrl = toOrigImageUrl(coverUrl);
        return {
          mediaKey,
          type,
          coverUrl,
          directUrl,
          hasMp4Variant: false,
          missingVideoVariant: false,
          coverOnly: !directUrl && Boolean(coverUrl),
        };
      }
      if (type === 'video' || type === 'animated_gif') {
        const variants = Array.isArray(m?.video_info?.variants) ? m.video_info.variants : [];
        const mp4Variants = variants
          .filter((v) => v && v.content_type === 'video/mp4' && v.url)
          .sort((a, b) => Number(b.bitrate || 0) - Number(a.bitrate || 0));
        const bestMp4 = mp4Variants[0]?.url ? String(mp4Variants[0].url) : '';
        return {
          mediaKey,
          type,
          coverUrl,
          directUrl: bestMp4,
          hasMp4Variant: Boolean(bestMp4),
          missingVideoVariant: !bestMp4,
          coverOnly: !bestMp4 && Boolean(coverUrl),
        };
      }
      return {
        mediaKey,
        type,
        coverUrl,
        directUrl: '',
        hasMp4Variant: false,
        missingVideoVariant: false,
        coverOnly: Boolean(coverUrl),
      };
    });
  };

  const getTweetIdAndAuthor = (tweetNode) => {
    if (!tweetNode) return { tweetId: '', author: '' };
    const legacy = tweetNode.legacy || {};
    const tweetId = String(tweetNode.rest_id || legacy.id_str || '');
    const userCore =
      tweetNode.core?.user_results?.result?.core ||
      tweetNode.core?.user_results?.result?.legacy ||
      {};
    const author = String(
      userCore.screen_name ||
        tweetNode.core?.user_results?.result?.legacy?.screen_name ||
        ''
    );
    return { tweetId, author };
  };

  const buildTweetStatusUrl = (tweetId, author = '') =>
    author
      ? `https://x.com/${author}/status/${tweetId}`
      : `https://x.com/i/web/status/${tweetId}`;

  const normalizeTweetResult = (rawResult, sortIndex = '') => {
    const outer = unwrapTweetResult(rawResult);
    if (!outer) return null;
    const { tweetId: outerTweetId, author: outerAuthor } = getTweetIdAndAuthor(outer);
    if (!outerTweetId) return null;

    const retweetedRaw =
      outer.legacy?.retweeted_status_result?.result ||
      outer.retweeted_status_result?.result ||
      null;
    const retweeted = unwrapTweetResult(retweetedRaw);
    const { tweetId: retweetedTweetId, author: retweetedAuthor } =
      getTweetIdAndAuthor(retweeted);

    const quotedRaw =
      outer.quoted_status_result?.result || retweeted?.quoted_status_result?.result || null;
    const quoted = unwrapTweetResult(quotedRaw);
    const { tweetId: resolvedQuotedId, author: quotedAuthor } = getTweetIdAndAuthor(quoted);
    const quotedTweetId =
      resolvedQuotedId ||
      String(
        outer.legacy?.quoted_status_id_str ||
          outer.quoted_status_permalink?.expanded?.match(/\/status\/(\d+)/)?.[1] ||
          ''
      );

    const outerMedia = extractMediaList(outer).map((m) => ({
      ...m,
      owner: 'outer',
      ownerTweetId: outerTweetId,
      ownerAuthor: outerAuthor,
    }));
    const retweetedMedia = extractMediaList(retweeted).map((m) => ({
      ...m,
      owner: 'retweeted',
      ownerTweetId: retweetedTweetId,
      ownerAuthor: retweetedAuthor || outerAuthor,
    }));
    const quotedMedia = extractMediaList(quoted).map((m) => ({
      ...m,
      owner: 'quoted',
      ownerTweetId: quotedTweetId,
      ownerAuthor: quotedAuthor || outerAuthor,
    }));

    const allMedia = [...outerMedia, ...retweetedMedia, ...quotedMedia];
    const directUrls = allMedia
      .filter((m) => Boolean(m.directUrl))
      .map((m) => ({
        owner: m.owner,
        ownerTweetId: m.ownerTweetId,
        ownerAuthor: m.ownerAuthor || '',
        mediaKey: m.mediaKey || '',
        type: m.type,
        url: m.directUrl,
      }));

    const fullText = String(
      outer.note_tweet?.note_tweet_results?.result?.text || outer.legacy?.full_text || ''
    );
    const anomalies = [];
    if (!allMedia.length) anomalies.push('text_only');
    if (allMedia.some((m) => m.missingVideoVariant)) anomalies.push('missing_video_variant');
    if (allMedia.some((m) => m.coverOnly)) anomalies.push('cover_only');

    return {
      id: outerTweetId,
      outerTweetId,
      outerAuthor,
      retweetedTweetId,
      retweetedAuthor,
      quotedTweetId,
      quotedAuthor,
      url: buildTweetStatusUrl(outerTweetId, outerAuthor),
      sortIndex: String(sortIndex || ''),
      attachmentCount: allMedia.length,
      usableDirectCount: directUrls.length,
      directUrls,
      mediaDetails: allMedia,
      anomalies,
      textPreview: fullText.replace(/\s+/g, ' ').slice(0, 120),
    };
  };

  /**
   * 方案 A：将外层推文与被引用推文的媒体按各自真实 tweet_id 分组输出，互不覆盖。
   */
  const flattenDirectManifestItems = (tweets) => {
    const byTweetId = new Map();
    let textOnlySkipped = 0;

    const ensureBucket = (tweetId, meta) => {
      if (!tweetId) return null;
      if (!byTweetId.has(tweetId)) {
        byTweetId.set(tweetId, {
          tweet_id: tweetId,
          author: meta.author || '',
          url: buildTweetStatusUrl(tweetId, meta.author || ''),
          title: meta.title || '',
          owner_role: meta.ownerRole || 'outer',
          quoted_from: meta.quotedFrom || '',
          media: [],
          unresolvable_reason: '',
          _seenUrls: new Set(),
        });
      }
      const bucket = byTweetId.get(tweetId);
      if (!bucket.author && meta.author) {
        bucket.author = meta.author;
        bucket.url = buildTweetStatusUrl(tweetId, meta.author);
      }
      if (!bucket.title && meta.title) bucket.title = meta.title;
      return bucket;
    };

    for (const tweet of Array.isArray(tweets) ? tweets : []) {
      const details = Array.isArray(tweet?.mediaDetails) ? tweet.mediaDetails : [];
      if (!details.length) {
        textOnlySkipped += 1;
        continue;
      }
      for (const media of details) {
        const ownerTweetId = String(media.ownerTweetId || tweet.outerTweetId || tweet.id || '');
        if (!ownerTweetId) continue;
        const ownerRole =
          media.owner === 'quoted'
            ? 'quoted'
            : media.owner === 'retweeted'
              ? 'retweeted'
              : 'outer';
        const ownerAuthor =
          media.ownerAuthor ||
          (ownerRole === 'quoted'
            ? tweet.quotedAuthor
            : ownerRole === 'retweeted'
              ? tweet.retweetedAuthor
              : tweet.outerAuthor) ||
          '';
        const quotedFrom = ownerRole === 'quoted' ? String(tweet.outerTweetId || tweet.id || '') : '';
        const bucket = ensureBucket(ownerTweetId, {
          author: ownerAuthor,
          title: ownerRole === 'outer' ? tweet.textPreview || '' : '',
          ownerRole,
          quotedFrom,
        });
        if (!bucket) continue;
        if (media.directUrl && !bucket._seenUrls.has(media.directUrl)) {
          bucket._seenUrls.add(media.directUrl);
          bucket.media.push({
            media_key: media.mediaKey || '',
            type: media.type,
            url: media.directUrl,
          });
          bucket.unresolvable_reason = '';
        } else if (!media.directUrl && !bucket.media.length && !bucket.unresolvable_reason) {
          bucket.unresolvable_reason = media.missingVideoVariant
            ? 'missing_video_variant'
            : media.coverOnly
              ? 'cover_only'
              : 'missing_direct_url';
        }
      }
    }

    const manifests = [...byTweetId.values()].map(({ _seenUrls, ...clean }) => clean);
    return { manifests, textOnlySkipped };
  };

  const extractInstructionsFromPayload = (payload) => {
    const userResult = payload?.data?.user?.result || payload?.data?.user_result?.result;
    const timeline =
      userResult?.timeline?.timeline ||
      userResult?.timeline_v2?.timeline ||
      payload?.data?.timeline?.timeline ||
      null;
    if (Array.isArray(timeline?.instructions)) return timeline.instructions;
    const conv = payload?.data?.threaded_conversation_with_injections_v2?.instructions;
    if (Array.isArray(conv)) return conv;
    return [];
  };

  const parseLikesPayload = (payload) => {
    const instructions = extractInstructionsFromPayload(payload);
    const entries = [];
    let bottomCursor = '';

    const visitEntry = (entry) => {
      if (!entry || typeof entry !== 'object') return;
      const entryId = String(entry.entryId || '');
      const content = entry.content || {};
      const sortIndex = String(entry.sortIndex || '');
      if (content.cursorType === 'Bottom' || entryId.startsWith('cursor-bottom-')) {
        if (content.value) bottomCursor = String(content.value);
        return;
      }
      const rawTweet = content.itemContent?.tweet_results?.result || null;
      if (rawTweet && (!entryId || entryId.startsWith('tweet-'))) {
        const parsed = normalizeTweetResult(rawTweet, sortIndex);
        if (parsed) entries.push(parsed);
      }
      if (Array.isArray(content.items)) {
        for (const sub of content.items) {
          const subRaw = sub?.item?.itemContent?.tweet_results?.result;
          if (subRaw) {
            const parsed = normalizeTweetResult(subRaw, sortIndex);
            if (parsed) entries.push(parsed);
          }
        }
      }
    };

    for (const instr of instructions) {
      if (Array.isArray(instr?.entries)) {
        for (const e of instr.entries) visitEntry(e);
      }
      if (instr?.entry) visitEntry(instr.entry);
      if (Array.isArray(instr?.moduleItems)) {
        for (const sub of instr.moduleItems) visitEntry({ content: sub.item || sub });
      }
    }
    return { entries, bottomCursor };
  };

  const parseTweetDetailPayload = (payload) => {
    const out = [];
    const direct = payload?.data?.tweetResult?.result;
    if (direct) {
      const parsed = normalizeTweetResult(direct);
      if (parsed) out.push(parsed);
    }
    const conv = parseLikesPayload(payload);
    out.push(...conv.entries);
    return out;
  };

  const computeIncrementalSlice = (items, anchors = [], cutoff = '') => {
    const anchorList = Array.isArray(anchors) ? anchors.filter(Boolean) : [];
    const anchorSet = new Set(anchorList);
    let anchorHitIndex = -1;
    let matchedAnchor = '';
    for (let i = 0; i < items.length; i += 1) {
      if (anchorSet.has(items[i].id)) {
        anchorHitIndex = i;
        matchedAnchor = items[i].id;
        break;
      }
    }
    const cutoffIdx = cutoff ? items.findIndex((it) => it.id === cutoff) : -1;
    const stopCandidates = [
      anchorHitIndex >= 0 ? { idx: anchorHitIndex, by: 'anchor', id: matchedAnchor } : null,
      cutoffIdx >= 0 ? { idx: cutoffIdx, by: 'cutoff', id: cutoff } : null,
    ].filter(Boolean);

    if (stopCandidates.length > 0) {
      stopCandidates.sort((a, b) => a.idx - b.idx);
      const winner = stopCandidates[0];
      return { items: items.slice(0, winner.idx), matchedBy: winner.by, stopId: winner.id };
    }
    if (anchorList.length > 0 || cutoff) {
      throw new Error(
        '未匹配到 5 锚点或手动截止点，为防止全量误提交已中止。请继续向下滚动加载后重试。'
      );
    }
    return { items, matchedBy: '', stopId: '' };
  };

  const nextAnchors = (sentItems, previousAnchors = []) => {
    const seen = new Set();
    const out = [];
    for (const id of [
      ...(Array.isArray(sentItems) ? sentItems.map((it) => it.id) : []),
      ...(Array.isArray(previousAnchors) ? previousAnchors : []),
    ]) {
      if (id && !seen.has(id)) {
        seen.add(id);
        out.push(id);
        if (out.length >= MAX_ANCHORS) break;
      }
    }
    return out;
  };

  const endpoint = (host, port, path) => {
    const url = new URL(/^https?:\/\//i.test(host) ? host : `http://${host}`);
    if (port && !url.port) url.port = String(port);
    url.pathname = String(path || '').startsWith('/') ? path : `/${path || ''}`;
    url.search = '';
    url.hash = '';
    return url.href;
  };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
      STORAGE_KEYS,
      isLikesGraphqlUrl,
      isTweetDetailGraphqlUrl,
      isLikesPagePath,
      resolveLikesAccount,
      tweetIdFromUrl,
      toOrigImageUrl,
      extractMediaList,
      normalizeTweetResult,
      flattenDirectManifestItems,
      parseLikesPayload,
      parseTweetDetailPayload,
      computeIncrementalSlice,
      nextAnchors,
      endpoint,
    };
    return;
  }

  const pageWin = typeof unsafeWindow !== 'undefined' ? unsafeWindow : window;
  const capturedMap = new Map();
  const capturedOrder = [];
  const cursors = [];
  let likesGraphqlCount = 0;

  const ingestTweets = (entries) => {
    for (const item of entries) {
      if (!item || !item.id) continue;
      if (!capturedMap.has(item.id)) capturedOrder.push(item.id);
      capturedMap.set(item.id, item);
    }
  };

  // document-start Hook: 同步/异步捕获所有 /Likes 与 TweetDetail GraphQL 响应
  const rawFetch = pageWin.fetch;
  if (typeof rawFetch === 'function') {
    pageWin.fetch = async function (...args) {
      const response = await rawFetch.apply(this, args);
      try {
        const reqUrl = typeof args[0] === 'string' ? args[0] : args[0]?.url || response?.url || '';
        if (isLikesGraphqlUrl(reqUrl)) {
          response.clone().json().then((json) => {
            const { entries, bottomCursor } = parseLikesPayload(json);
            if (bottomCursor) cursors.push(bottomCursor);
            likesGraphqlCount += 1;
            ingestTweets(entries);
          }).catch(() => {});
        } else if (isTweetDetailGraphqlUrl(reqUrl)) {
          response.clone().json().then((json) => {
            ingestTweets(parseTweetDetailPayload(json));
          }).catch(() => {});
        }
      } catch {}
      return response;
    };
  }

  const RawXhrOpen = pageWin.XMLHttpRequest?.prototype?.open;
  const RawXhrSend = pageWin.XMLHttpRequest?.prototype?.send;
  if (RawXhrOpen && RawXhrSend) {
    pageWin.XMLHttpRequest.prototype.open = function (method, url, ...rest) {
      this.__nasReqUrl = String(url || '');
      return RawXhrOpen.call(this, method, url, ...rest);
    };
    pageWin.XMLHttpRequest.prototype.send = function (...args) {
      this.addEventListener('load', () => {
        try {
          const reqUrl = this.responseURL || this.__nasReqUrl || '';
          if (this.status >= 200 && this.status < 300 && this.responseText) {
            if (isLikesGraphqlUrl(reqUrl)) {
              const json = JSON.parse(this.responseText);
              const { entries, bottomCursor } = parseLikesPayload(json);
              if (bottomCursor) cursors.push(bottomCursor);
              likesGraphqlCount += 1;
              ingestTweets(entries);
            } else if (isTweetDetailGraphqlUrl(reqUrl)) {
              const json = JSON.parse(this.responseText);
              ingestTweets(parseTweetDetailPayload(json));
            }
          }
        } catch {}
      });
      return RawXhrSend.apply(this, args);
    };
  }

  const defaultConfig = () => ({
    host: '192.168.1.20',
    port: '14001',
    path: '/api/x/direct',
    token: '',
    maxScrolls: 60,
    maxBgScrolls: 25,
    dailyBgEnabled: true,
    bgTargetUrl: 'https://x.com/i/history/likes',
  });

  const loadConfig = () => {
    const saved = typeof GM_getValue === 'function' ? GM_getValue(STORAGE_KEYS.CONFIG, {}) : {};
    return { ...defaultConfig(), ...(saved && typeof saved === 'object' ? saved : {}) };
  };

  const saveConfig = async (next) => {
    if (typeof GM.setValues === 'function') {
      await GM.setValues({ [STORAGE_KEYS.CONFIG]: next });
    } else if (typeof GM_setValue === 'function') {
      GM_setValue(STORAGE_KEYS.CONFIG, next);
    }
  };

  const parseBgHash = () => {
    const h = String(location.hash || '').replace(/^#/, '');
    if (!h) return null;
    const params = new URLSearchParams(h);
    const runId = params.get('nas_bg_run');
    if (!runId) return null;
    return {
      runId,
      ignoreLock: params.get('nas_bg_ignore_lock') === '1',
    };
  };

  const gmSetAll = async (obj) => {
    if (typeof GM.setValues === 'function') {
      await GM.setValues(obj);
      return;
    }
    for (const [k, v] of Object.entries(obj)) {
      GM_setValue(k, v);
    }
  };

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  const postToNas = (url, token, body) =>
    new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method: 'POST',
        url,
        headers: {
          'Content-Type': 'application/json',
          'X-NAS-Submit-Token': token || '',
        },
        data: JSON.stringify(body),
        timeout: 30000,
        onload(resp) {
          try {
            if (resp.status === 401) {
              reject(new Error('NAS 拒绝推送 (401)：推送 Token 未填写或与控制台不一致'));
              return;
            }
            if (resp.status < 200 || resp.status >= 300) {
              reject(new Error(`NAS HTTP ${resp.status}：${resp.responseText?.slice(0, 120) || '请求失败'}`));
              return;
            }
            resolve(JSON.parse(resp.responseText || '{}'));
          } catch (err) {
            reject(err);
          }
        },
        onerror: () => reject(new Error('无法连接 NAS，请检查主机地址、端口或内网连通性')),
        ontimeout: () => reject(new Error('NAS 响应超时，请稍后在管理页确认是否已入队')),
      });
    });

  const reportToNas = async (cfg, payload) => {
    if (!cfg.token) return;
    try {
      await postToNas(endpoint(cfg.host, cfg.port, '/api/browser-push/report'), cfg.token, {
        platform: 'x',
        ...payload,
      });
    } catch {}
  };

  const getOrderedTweets = () =>
    capturedOrder.map((id) => capturedMap.get(id)).filter(Boolean);

  const accountScopeKey = () => {
    const fallback =
      document.querySelector('[data-testid="SideNav_AccountSwitcher_Button"] [dir="ltr"]')?.textContent?.replace(/^@/, '').trim() ||
      '';
    return resolveLikesAccount(location.pathname, fallback);
  };

  const getAnchors = (acct) => {
    const val = GM_getValue(STORAGE_KEYS.ANCHORS_PREFIX + acct, []);
    return Array.isArray(val) ? val : [];
  };

  const getCutoff = (acct) => GM_getValue(STORAGE_KEYS.CUTOFF_PREFIX + acct, '');

  const pushTweetsBatch = async (tweets, { sourceMode, updateAnchors, account, cfg, onProgress }) => {
    const { manifests, textOnlySkipped } = flattenDirectManifestItems(tweets);
    if (!manifests.length) {
      await reportToNas(cfg, {
        source_mode: sourceMode,
        account,
        status: 'empty',
        discovered: tweets.length,
        message: `检查 ${tweets.length} 条点赞推文（其中纯文字 ${textOnlySkipped} 条），无新增媒体直链`,
      });
      if (updateAnchors && tweets.length) {
        await gmSetAll({
          [STORAGE_KEYS.ANCHORS_PREFIX + account]: nextAnchors(tweets, getAnchors(account)),
        });
      }
      return { submitted: 0, accepted: 0, skipped: 0, unresolvable: 0, textOnlySkipped, taskIds: [] };
    }

    if (!cfg.token) {
      throw new Error('请先在设置中填写从 NAS 控制台复制的「推送授权 Token」。');
    }

    const batchId = `x_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
    const url = endpoint(cfg.host, cfg.port, cfg.path || '/api/x/direct');
    let accepted = 0;
    let skipped = 0;
    let unresolvable = 0;
    const taskIds = [];

    for (let offset = 0; offset < manifests.length; offset += CHUNK_SIZE) {
      const chunk = manifests.slice(offset, offset + CHUNK_SIZE);
      const chunkBatchId = manifests.length <= CHUNK_SIZE ? batchId : `${batchId}_${offset}`;
      if (onProgress) onProgress(`正在推送 ${offset}/${manifests.length} 条推文清单…`);
      const reply = await postToNas(url, cfg.token, {
        batch_id: chunkBatchId,
        source_mode: sourceMode,
        account,
        discovered: tweets.length,
        items: chunk,
      });
      const handledCount =
        Number(reply.accepted || 0) +
        Number(reply.skipped || 0) +
        Number(reply.unresolvable || 0);
      if (!reply.ok || handledCount !== chunk.length) {
        throw new Error('NAS 返回回执数量不完整，已保留断点，请勿重置锚点。');
      }
      accepted += Number(reply.accepted || 0);
      skipped += Number(reply.skipped || 0);
      unresolvable += Number(reply.unresolvable || 0);
      if (reply.task_id) taskIds.push(reply.task_id);
    }

    if (updateAnchors && tweets.length) {
      await gmSetAll({
        [STORAGE_KEYS.ANCHORS_PREFIX + account]: nextAnchors(tweets, getAnchors(account)),
      });
    }
    return {
      submitted: manifests.length,
      accepted,
      skipped,
      unresolvable,
      textOnlySkipped,
      taskIds,
    };
  };

  // 等待 DOM Ready
  const onReady = (fn) => {
    if (document.readyState !== 'loading') fn();
    else document.addEventListener('DOMContentLoaded', fn, { once: true });
  };

  onReady(async () => {
    const bgRun = parseBgHash();
    if (bgRun) {
      const cfg = loadConfig();
      let anchorUpdated = false;
      try {
        await sleep(3500);
        if (/\/i\/flow\/login|\/login/.test(location.pathname)) {
          throw new Error('X 账号未登录或会话已失效，请在 Chrome 中重新登录 x.com');
        }
        const acct = accountScopeKey();
        const anchors = getAnchors(acct);
        const cutoff = getCutoff(acct);
        const maxScrolls = Math.max(1, Math.min(200, Number(cfg.maxBgScrolls || 25)));
        let stableTicks = 0;

        for (let step = 0; step < maxScrolls; step += 1) {
          if (/\/i\/flow\/login|\/login/.test(location.pathname)) {
            throw new Error('X 跳转至登录页，账号会话已失效');
          }
          const ordered = getOrderedTweets();
          const anchorSet = new Set(anchors);
          const hitStop = ordered.some((t) => anchorSet.has(t.id) || (cutoff && t.id === cutoff));
          if (hitStop && ordered.length > 0) break;
          const prevCount = ordered.length;
          window.scrollBy(0, Math.round(window.innerHeight * 0.9));
          await sleep(1600);
          stableTicks = getOrderedTweets().length === prevCount ? stableTicks + 1 : 0;
          if (stableTicks >= 4) break;
        }

        const ordered = getOrderedTweets();
        if (!ordered.length) {
          throw new Error('未拦截到 X /Likes 推文数据，请确认页面可访问且已登录');
        }
        const sliced = computeIncrementalSlice(ordered, anchors, cutoff);
        if (!sliced.items.length) {
          await reportToNas(cfg, {
            source_mode: 'bg',
            account: acct,
            status: 'empty',
            discovered: ordered.length,
            message: `后台定时检查 ${ordered.length} 条点赞推文，命中断点无新增`,
          });
          await gmSetAll({
            [STORAGE_KEYS.BG_RESULT]: {
              runId: bgRun.runId,
              platform: 'x',
              status: 'ok',
              reason: 'no_new_items',
              itemCount: 0,
              anchorUpdated: false,
              finishedAt: new Date().toISOString(),
            },
          });
          return;
        }

        const summary = await pushTweetsBatch(sliced.items, {
          sourceMode: 'bg',
          updateAnchors: true,
          account: acct,
          cfg,
        });
        anchorUpdated = true;
        await gmSetAll({
          [STORAGE_KEYS.BG_RESULT]: {
            runId: bgRun.runId,
            platform: 'x',
            status: 'ok',
            reason: 'pushed',
            itemCount: summary.accepted,
            submitted: summary.submitted,
            skipped: summary.skipped,
            anchorUpdated: true,
            finishedAt: new Date().toISOString(),
          },
        });
      } catch (err) {
        const msg = err?.message || String(err);
        await reportToNas(cfg, {
          source_mode: 'bg',
          account: accountScopeKey(),
          status: 'error',
          discovered: getOrderedTweets().length,
          message: `后台同步失败：${msg}`,
        });
        await gmSetAll({
          [STORAGE_KEYS.BG_RESULT]: {
            runId: bgRun.runId,
            platform: 'x',
            status: 'failed',
            reason: msg,
            anchorUpdated,
            finishedAt: new Date().toISOString(),
          },
        });
      }
      return;
    }

    // 前台工作台 UI
    const host = document.createElement('div');
    host.id = 'nas-x-workbench';
    const shadow = host.attachShadow({ mode: 'open' });
    shadow.innerHTML = `<style>
:host{all:initial;font:13.5px/1.5 system-ui,-apple-system,"Segoe UI","PingFang SC",sans-serif;color:#111827}
*{box-sizing:border-box}button,input{font:inherit}
button{cursor:pointer;border:1px solid #d8dee9;background:#fff;color:#334155;border-radius:10px;padding:8px 12px;transition:all .15s;font-weight:500}
button:hover:not(:disabled){background:#f8fafc;border-color:#cbd5e1}button:disabled{opacity:.45;cursor:not-allowed}
button.primary{background:#111827;color:#fff;border-color:#111827}button.primary:hover:not(:disabled){background:#1f2937}
button.accent{background:#4f46e5;color:#fff;border-color:#4f46e5}button.accent:hover:not(:disabled){background:#4338ca}
button.quiet{border:0;background:transparent;color:#64748b;padding:4px 8px}
input{width:100%;border:1px solid #d8dee9;border-radius:9px;padding:8px 10px;background:#fff;color:#0f172a;min-width:0}
input:focus,button:focus-visible{outline:2px solid #6366f1;outline-offset:2px}
.launch{position:fixed;right:20px;bottom:22px;z-index:2147483646;background:#111827;color:#fff;border:1px solid #334155;box-shadow:0 8px 28px rgba(15,23,42,.28);border-radius:16px;padding:11px 16px;font-weight:600;display:flex;align-items:center;gap:8px}
.panel{position:fixed;right:20px;bottom:76px;width:440px;max-width:calc(100vw - 24px);max-height:calc(100vh - 96px);z-index:2147483646;border:1px solid #e2e8f0;border-radius:20px;background:#f8fafc;box-shadow:0 24px 72px rgba(15,23,42,.22);overflow:auto}
.hidden{display:none!important}
header{padding:18px 20px;display:flex;align-items:center;justify-content:space-between;background:#fff;border-bottom:1px solid #e2e8f0;position:sticky;top:0;z-index:2}
.brand{font-size:10px;letter-spacing:1.6px;color:#4f46e5;font-weight:750;text-transform:uppercase}
h2{margin:2px 0 0;font-size:17px;font-weight:700}
small,.muted{color:#64748b;font-size:12px}
.body{padding:14px;display:grid;gap:12px}
.box{background:#fff;border:1px solid #e2e8f0;border-radius:14px;padding:14px;display:grid;gap:10px}
.row{display:flex;gap:8px;align-items:center}.spread{justify-content:space-between}.grow{flex:1;min-width:0}
.cut{background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:10px;font-size:12px}
.list{max-height:280px;overflow:auto;display:grid;gap:8px}
.item{border:1px solid #eef2f6;border-radius:10px;padding:10px;display:grid;gap:5px;background:#fff}
.badge{display:inline-flex;align-items:center;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:600;background:#eef2ff;color:#4338ca}
.badge.ok{background:#ecfdf5;color:#047857}.badge.warn{background:#fef3c7;color:#b45309}
#status{white-space:pre-wrap;overflow-wrap:anywhere;padding:10px 12px;border-radius:10px;background:#eef2ff;color:#312e81;font-size:12px;line-height:1.5}
#status.error{background:#fef2f2;color:#b91c1c}
label.field{display:grid;gap:4px;font-size:12px;color:#475569;font-weight:500}
details summary{cursor:pointer;font-weight:600;color:#334155;font-size:13px}
</style>
<button class="launch">𝕏 NAS 工作台 <span id="pill" class="badge">0</span></button>
<section class="panel hidden" aria-label="X NAS 下载工作台">
<header><div><div class="brand">X / DIRECT CDN · QUOTE 方案 A</div><h2>X 浏览器直推工作台</h2><small id="page-hint">无需 Cookie · 外贴与引用双重归档</small></div><button class="quiet" id="close">✕</button></header>
<div class="body">
  <div class="box">
    <div class="row spread"><strong>增量采集 · 点赞与详情直链</strong><small id="acct-label"></small></div>
    <div class="cut" id="anchor-box"></div>
    <div class="cut">
      <div class="row spread"><span>手动截止推文（不包含）</span><button class="quiet" id="clear-cutoff">清除</button></div>
      <div id="cutoff-label" class="muted" style="margin:4px 0 6px"></div>
      <div class="row"><input id="cutoff-input" class="grow" placeholder="粘贴 /status/推文ID 作为截止边界"><button id="save-cutoff">保存截止</button></div>
    </div>
    <div class="row">
      <button id="scroll-load" class="grow">自动向下加载</button>
      <button id="stop-scroll" class="hidden">停止滚动</button>
      <button id="push-likes" class="accent grow">发送增量给 NAS</button>
    </div>
    <div class="row">
      <button id="bg-run-now" class="grow">测试后台静默运行（忽略今日锁）</button>
    </div>
  </div>
  <div class="box">
    <div class="row spread"><strong>当前页面已捕获推文</strong><small id="list-summary"></small></div>
    <div id="tweet-list" class="list"></div>
  </div>
  <div id="status" role="status">就绪：打开点赞页 (/i/history/likes) 或推文详情页即可自动捕获高清直链。</div>
  <details class="box">
    <summary>NAS 连接与每日后台设置</summary>
    <label class="field">NAS 主机<input id="cfg-host" placeholder="192.168.1.20 或 https://..."></label>
    <div class="row">
      <label class="field grow">端口<input id="cfg-port"></label>
      <label class="field grow">最大手动滚动<input id="cfg-max-scroll" type="number" min="1" max="300"></label>
      <label class="field grow">后台最大滚动<input id="cfg-bg-scroll" type="number" min="1" max="200"></label>
    </div>
    <label class="field">直连推送路径<input id="cfg-path"></label>
    <label class="field">推送授权 Token（从 NAS 控制台复制）<input id="cfg-token" type="password" autocomplete="off"></label>
    <label class="field">后台赞过页目标网址<input id="cfg-bg-url" placeholder="https://x.com/i/history/likes"></label>
    <div class="row spread"><label class="row"><input id="cfg-bg-enabled" type="checkbox" style="width:auto">启用每日一次后台自动检查</label><button id="clear-daily-lock" class="quiet">重置今日锁</button></div>
    <div class="row">
      <button id="save-cfg" class="primary grow">保存设置</button>
      <button id="test-conn" class="grow">测试推送连通性</button>
    </div>
  </details>
</div></section>`;
    document.body.append(host);

    const $ = (sel) => shadow.querySelector(sel);
    let cfg = loadConfig();
    let scrolling = false;
    let busy = false;

    const setStatus = (msg, isError = false) => {
      $('#status').textContent = msg;
      $('#status').classList.toggle('error', isError);
    };

    const refreshView = () => {
      const acct = accountScopeKey();
      const ordered = getOrderedTweets();
      const anchors = getAnchors(acct);
      const cutoff = getCutoff(acct);
      const daily = GM_getValue(STORAGE_KEYS.DAILY_STATE, {});
      $('#pill').textContent = String(ordered.length);
      $('#acct-label').textContent = `账号区：${acct}`;
      $('#page-hint').textContent = isLikesPagePath(location.pathname)
        ? '当前位于点赞列表 · 自动捕获原图与 MP4'
        : `当前页面 ${ordered.length} 条已缓存 · 无需 Cookie`;
      $('#cutoff-label').textContent = cutoff ? `已设置截止 ID：${cutoff}` : '未设置手动截止';
      $('#anchor-box').innerHTML = `
        <div class="row spread"><strong>5 锚点断点（发送确认后推进）</strong><button class="quiet" id="clear-anchors">清空锚点</button></div>
        <div class="muted" style="margin-top:4px">${
          anchors.length ? `已保存 ${anchors.length} 个：${anchors.join(', ')}` : '尚未建立锚点（首次发送后自动保存最近 5 条）'
        }</div>
        <div class="muted" style="margin-top:4px">今日自动定时锁：${daily.lastSuccessDate || '今日尚未完成'}</div>
      `;
      $('#clear-anchors').onclick = async () => {
        await gmSetAll({ [STORAGE_KEYS.ANCHORS_PREFIX + acct]: [] });
        refreshView();
        setStatus('已清空当前账号区 5 锚点记录。');
      };

      const manifests = flattenDirectManifestItems(ordered);
      $('#list-summary').textContent = `推文 ${ordered.length} · 可下直链组 ${manifests.manifests.length} · 纯文 ${manifests.textOnlySkipped}`;
      const listEl = $('#tweet-list');
      listEl.replaceChildren();
      for (const item of ordered.slice(0, 60)) {
        const div = document.createElement('div');
        div.className = 'item';
        const flat = flattenDirectManifestItems([item]);
        const quoteBadge = item.quotedTweetId
          ? `<span class="badge warn">含引用 #${item.quotedTweetId}</span>`
          : '';
        div.innerHTML = `
          <div class="row spread"><strong>@${item.outerAuthor || 'unknown'} · ${item.id}</strong>${quoteBadge}</div>
          <div class="muted">${item.textPreview || '（无文字）'}</div>
          <div class="row spread">
            <span class="muted">归档组 ${flat.manifests.length} · 直链 ${item.usableDirectCount}/${item.attachmentCount}</span>
            <div class="row">
              <button data-act="cutoff">设为截止</button>
              <button data-act="push" class="primary">单条直推</button>
            </div>
          </div>
        `;
        div.querySelector('[data-act="cutoff"]').onclick = async () => {
          await gmSetAll({ [STORAGE_KEYS.CUTOFF_PREFIX + acct]: item.id });
          refreshView();
          setStatus(`已保存手动截止推文：${item.id}（不包含它）。`);
        };
        div.querySelector('[data-act="push"]').onclick = async () => {
          if (busy) return;
          busy = true;
          try {
            const res = await pushTweetsBatch([item], {
              sourceMode: 'single',
              updateAnchors: false,
              account: acct,
              cfg,
              onProgress: (m) => setStatus(m),
            });
            setStatus(`单条推送完成：清单 ${res.submitted} · 新增 ${res.accepted} · 跳过 ${res.skipped}`);
          } catch (e) {
            setStatus(e.message || String(e), true);
          } finally {
            busy = false;
            refreshView();
          }
        };
        listEl.append(div);
      }
    };

    const hydrateSettingsForm = () => {
      cfg = loadConfig();
      $('#cfg-host').value = cfg.host;
      $('#cfg-port').value = cfg.port;
      $('#cfg-path').value = cfg.path;
      $('#cfg-token').value = cfg.token;
      $('#cfg-max-scroll').value = cfg.maxScrolls;
      $('#cfg-bg-scroll').value = cfg.maxBgScrolls;
      $('#cfg-bg-url').value = cfg.bgTargetUrl;
      $('#cfg-bg-enabled').checked = cfg.dailyBgEnabled !== false;
    };

    $('.launch').onclick = () => {
      $('.panel').classList.toggle('hidden');
      hydrateSettingsForm();
      refreshView();
    };
    $('#close').onclick = () => $('.panel').classList.add('hidden');
    $('#save-cutoff').onclick = async () => {
      const id = tweetIdFromUrl($('#cutoff-input').value.trim());
      if (!id) return setStatus('请输入有效的 X 推文链接或数字 ID。', true);
      await gmSetAll({ [STORAGE_KEYS.CUTOFF_PREFIX + accountScopeKey()]: id });
      refreshView();
      setStatus(`已保存手动截止：${id}`);
    };
    $('#clear-cutoff').onclick = async () => {
      await gmSetAll({ [STORAGE_KEYS.CUTOFF_PREFIX + accountScopeKey()]: '' });
      refreshView();
      setStatus('已清除手动截止。');
    };

    $('#scroll-load').onclick = async () => {
      if (scrolling || busy) return;
      scrolling = true;
      $('#stop-scroll').classList.remove('hidden');
      const acct = accountScopeKey();
      const anchors = new Set(getAnchors(acct));
      const cutoff = getCutoff(acct);
      let idle = 0;
      try {
        for (let step = 0; step < Number(cfg.maxScrolls || 60) && scrolling; step += 1) {
          const ordered = getOrderedTweets();
          if (ordered.some((t) => anchors.has(t.id) || (cutoff && t.id === cutoff))) {
            setStatus(`已滚动命中截止位置，共缓存 ${ordered.length} 条。`);
            break;
          }
          const before = ordered.length;
          window.scrollBy(0, Math.round(window.innerHeight * 0.9));
          await sleep(1400);
          const after = getOrderedTweets().length;
          idle = after === before ? idle + 1 : 0;
          setStatus(`正在加载点赞列表…第 ${step + 1} 次滚动，已捕获 ${after} 条推文`);
          refreshView();
          if (idle >= 5) {
            setStatus(`连续多次无新增推文，停止自动加载（共 ${after} 条）。`);
            break;
          }
        }
      } finally {
        scrolling = false;
        $('#stop-scroll').classList.add('hidden');
        refreshView();
      }
    };
    $('#stop-scroll').onclick = () => { scrolling = false; };

    $('#push-likes').onclick = async () => {
      if (busy) return;
      busy = true;
      try {
        const acct = accountScopeKey();
        const ordered = getOrderedTweets();
        if (!ordered.length) throw new Error('暂未捕获到推文，请先在点赞页滚动加载。');
        const sliced = computeIncrementalSlice(ordered, getAnchors(acct), getCutoff(acct));
        if (!sliced.items.length) {
          await reportToNas(cfg, {
            source_mode: 'workbench',
            account: acct,
            status: 'empty',
            discovered: ordered.length,
            message: '工作台手动检查命中锚点，无新增内容',
          });
          return setStatus('范围内没有新推文（已发送「无新增」状态到 NAS）。');
        }
        const res = await pushTweetsBatch(sliced.items, {
          sourceMode: 'workbench',
          updateAnchors: true,
          account: acct,
          cfg,
          onProgress: (m) => setStatus(m),
        });
        setStatus(
          `发送成功！范围 ${sliced.items.length} 条 → 清单 ${res.submitted} 组 · NAS 接收 ${res.accepted} · 重复跳过 ${res.skipped} · 异常直链 ${res.unresolvable}（已更新 5 锚点）`
        );
      } catch (e) {
        setStatus(e.message || String(e), true);
      } finally {
        busy = false;
        refreshView();
      }
    };

    $('#bg-run-now').onclick = async () => {
      if (busy) return;
      cfg = loadConfig();
      const targetUrl = String(cfg.bgTargetUrl || 'https://x.com/i/history/likes').split('#')[0];
      if (!targetUrl) return setStatus('请先在设置中填写后台赞过页目标网址。', true);
      busy = true;
      const runId = `x_manual_${Date.now()}`;
      const tabUrl = `${targetUrl}#nas_bg_run=${encodeURIComponent(runId)}&nas_bg_ignore_lock=1`;
      setStatus(`正在打开后台静默标签页执行一次采集 (${runId})…`);
      let tab = null;
      let listenerId = null;
      try {
        const result = await new Promise((resolve, reject) => {
          const timer = setTimeout(() => {
            reject(new Error('后台标签页超时（120 秒）未返回回执'));
          }, 120000);
          listenerId = GM_addValueChangeListener(STORAGE_KEYS.BG_RESULT, (_name, _oldVal, val) => {
            if (val && val.runId === runId) {
              clearTimeout(timer);
              resolve(val);
            }
          });
          tab = GM_openInTab(tabUrl, { active: false, insert: true, setParent: true });
        });
        setStatus(
          `后台运行结束：${result.status} (${result.reason || ''}) · 新增 ${result.itemCount || 0} · 锚点更新=${Boolean(result.anchorUpdated)}`,
          result.status !== 'ok'
        );
      } catch (e) {
        setStatus(e.message || String(e), true);
      } finally {
        if (listenerId !== null && typeof GM_removeValueChangeListener === 'function') {
          try { GM_removeValueChangeListener(listenerId); } catch {}
        }
        try { tab?.close?.(); } catch {}
        busy = false;
        refreshView();
      }
    };

    $('#save-cfg').onclick = async () => {
      const next = {
        host: $('#cfg-host').value.trim() || '192.168.1.20',
        port: $('#cfg-port').value.trim() || '14001',
        path: $('#cfg-path').value.trim() || '/api/x/direct',
        token: $('#cfg-token').value.trim(),
        maxScrolls: Math.max(1, Math.min(300, Number($('#cfg-max-scroll').value || 60))),
        maxBgScrolls: Math.max(1, Math.min(200, Number($('#cfg-bg-scroll').value || 25))),
        bgTargetUrl: $('#cfg-bg-url').value.trim() || 'https://x.com/i/history/likes',
        dailyBgEnabled: $('#cfg-bg-enabled').checked,
      };
      endpoint(next.host, next.port, next.path);
      cfg = next;
      await saveConfig(next);
      refreshView();
      setStatus('连接与每日后台设置已保存。');
    };

    $('#clear-daily-lock').onclick = async () => {
      await gmSetAll({ [STORAGE_KEYS.DAILY_STATE]: {} });
      refreshView();
      setStatus('已重置今日定时运行锁。');
    };

    $('#test-conn').onclick = async () => {
      cfg = loadConfig();
      if (!cfg.token) return setStatus('请先填写并保存推送授权 Token。', true);
      try {
        await postToNas(endpoint(cfg.host, cfg.port, '/api/browser-push/report'), cfg.token, {
          platform: 'x',
          source_mode: 'workbench',
          account: accountScopeKey(),
          status: 'ok',
          discovered: 0,
          message: 'X 工作台手动连通性测试通过',
        });
        setStatus('连通性测试通过！NAS 控制台的「浏览器推送」面板已收到记录。');
      } catch (e) {
        setStatus(e.message || String(e), true);
      }
    };

    GM_registerMenuCommand('打开 X NAS 下载工作台', () => {
      $('.panel').classList.remove('hidden');
      hydrateSettingsForm();
      refreshView();
    });
    hydrateSettingsForm();
    refreshView();
    setInterval(() => {
      if (!$('.panel').classList.contains('hidden')) refreshView();
      else $('#pill').textContent = String(getOrderedTweets().length);
    }, 2000);
  });
})();
