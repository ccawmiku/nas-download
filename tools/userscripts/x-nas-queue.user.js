// ==UserScript==
// @name         X · NAS 下载工作台
// @namespace    https://yaemiko.lol/nas-download
// @version      1.1.0
// @description  拦截 X GraphQL 响应直接提取无水印原图与最高码率 MP4 直链，按推文/引用双重归档（方案 A），支持服务端 tweet_id 状态对账、连续已归档早停、流式推送、仅导入基线与每日后台回执
// @match        https://x.com/*
// @match        https://twitter.com/*
// @run-at       document-start
// @storageName  nas_prod_x_v1
// @grant        unsafeWindow
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
// @grant        GM_registerMenuCommand
// @grant        window.close
// @connect      *
// @noframes
// ==/UserScript==

(() => {
  'use strict';

  const STORAGE_KEYS = {
    CONFIG: 'nas_prod_x_config_v1',
    ANCHORS_PREFIX: 'nas:prod:x:anchors:',
    CUTOFF_PREFIX: 'nas:prod:x:cutoff:',
    LAST_ACCOUNT: 'nas_prod_x_last_account_v1',
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

  const cleanAccountHandle = (value) => {
    const normalized = String(value || '').replace(/^@/, '').trim();
    if (!normalized || normalized === 'i_history_likes' || normalized === 'default') return '';
    return normalized;
  };

  const resolveLikesAccount = (pathname, fallbackHandle = '', cachedHandle = '') => {
    const p = String(pathname || '');
    const resolved = cleanAccountHandle(fallbackHandle) || cleanAccountHandle(cachedHandle);
    if (/^\/i\/history\/likes\/?$/.test(p)) {
      return resolved || 'i_history_likes';
    }
    const m = p.match(/^\/([^/]+)\/likes\/?$/);
    if (m && m[1] !== 'i') return m[1];
    return resolved || 'default';
  };

  const extractInitialStateHandle = (state) => {
    try {
      const uid = state?.session?.user_id;
      const userObj = uid ? state?.entities?.users?.entities?.[uid] : null;
      return cleanAccountHandle(userObj?.screen_name || '');
    } catch {
      return '';
    }
  };

  const parseBgHash = (rawHash) => {
    const src =
      rawHash !== undefined
        ? rawHash
        : typeof location !== 'undefined'
          ? location.hash
          : '';
    const h = String(src || '').replace(/^#/, '');
    if (!h) return null;
    const params = new URLSearchParams(h);
    const runId = params.get('nas_bg_run');
    if (!runId) return null;
    return {
      runId,
      ignoreLock: params.get('nas_bg_ignore_lock') === '1',
    };
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

  const extractTweetManifestIds = (tweet) => {
    if (!tweet) return [];
    const { manifests } = flattenDirectManifestItems([tweet]);
    return manifests.map((m) => String(m.tweet_id || '')).filter(Boolean);
  };

  const evaluateReconcileStop = (tweets, statesMap = {}, stopThreshold = 15) => {
    const threshold = Math.max(0, Number(stopThreshold || 0));
    const settledSet = new Set(['archived', 'pending', 'deferred']);
    const toPush = [];
    let consecutiveSettled = 0;
    let settledTweets = 0;
    let textOnly = 0;
    let shouldStop = false;
    let stopAtTweetId = '';

    for (const tweet of Array.isArray(tweets) ? tweets : []) {
      if (!tweet || !tweet.id) continue;
      const manifestIds = extractTweetManifestIds(tweet);
      if (!manifestIds.length) {
        textOnly += 1;
        continue;
      }
      const allSettled = manifestIds.every((id) => settledSet.has(statesMap[id]));
      if (allSettled) {
        settledTweets += 1;
        consecutiveSettled += 1;
      } else {
        consecutiveSettled = 0;
        toPush.push(tweet);
      }
      if (threshold > 0 && consecutiveSettled >= threshold) {
        shouldStop = true;
        stopAtTweetId = String(tweet.id);
        break;
      }
    }

    return {
      toPush,
      consecutiveSettled,
      settledTweets,
      textOnly,
      shouldStop,
      stopAtTweetId,
    };
  };

  const trimProcessedCache = (orderArr, mapObj, keepRecent = 120, protectedIds = null, eligibleIds = null) => {
    if (!Array.isArray(orderArr)) return { removedCount: 0, keptCount: 0 };
    const keepCount = Math.max(0, Number(keepRecent || 0));
    if (orderArr.length <= keepCount) {
      return { removedCount: 0, keptCount: orderArr.length };
    }
    const protectedSet = protectedIds instanceof Set
      ? protectedIds
      : Array.isArray(protectedIds)
        ? new Set(protectedIds)
        : new Set();
    const eligibleSet = eligibleIds === null || eligibleIds === undefined
      ? null
      : eligibleIds instanceof Set
        ? eligibleIds
        : new Set(eligibleIds);
    const cut = orderArr.length - keepCount;
    const retained = [];
    let removedCount = 0;

    for (let i = 0; i < orderArr.length; i += 1) {
      const id = orderArr[i];
      if (i < cut && !protectedSet.has(id) && (!eligibleSet || eligibleSet.has(id))) {
        if (mapObj instanceof Map) mapObj.delete(id);
        else if (mapObj && typeof mapObj === 'object') delete mapObj[id];
        removedCount += 1;
      } else {
        retained.push(id);
      }
    }
    orderArr.splice(0, orderArr.length, ...retained);
    return { removedCount, keptCount: orderArr.length };
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

  const installBgVisibilityBypass = (win) => {
    if (!win || !win.document) return false;
    const doc = win.document;
    const defineVisibleProp = (target, prop, getter) => {
      try {
        Object.defineProperty(target, prop, {
          configurable: true,
          get: getter,
        });
      } catch {}
    };
    const docProto = win.Document?.prototype;
    for (const target of [docProto, doc].filter(Boolean)) {
      defineVisibleProp(target, 'hidden', () => false);
      defineVisibleProp(target, 'webkitHidden', () => false);
      defineVisibleProp(target, 'visibilityState', () => 'visible');
      defineVisibleProp(target, 'webkitVisibilityState', () => 'visible');
    }
    try {
      if (docProto) {
        Object.defineProperty(docProto, 'hasFocus', {
          configurable: true,
          writable: true,
          value: () => true,
        });
      }
      doc.hasFocus = () => true;
    } catch {}

    const BLOCKED_VISIBILITY_EVENTS = new Set(['visibilitychange', 'webkitvisibilitychange']);
    for (const target of [doc, win]) {
      if (!target?.addEventListener) continue;
      const rawAdd = target.addEventListener.bind(target);
      try {
        target.addEventListener = function (type, listener, options) {
          if (BLOCKED_VISIBILITY_EVENTS.has(String(type))) return undefined;
          return rawAdd(type, listener, options);
        };
      } catch {}
      try {
        rawAdd('visibilitychange', (ev) => ev.stopImmediatePropagation(), true);
        rawAdd('webkitvisibilitychange', (ev) => ev.stopImmediatePropagation(), true);
      } catch {}
    }

    const rawRaf = typeof win.requestAnimationFrame === 'function' ? win.requestAnimationFrame.bind(win) : null;
    const rawCancelRaf =
      typeof win.cancelAnimationFrame === 'function' ? win.cancelAnimationFrame.bind(win) : null;
    const activeFrames = new Map();
    let seq = 1;
    try {
      win.requestAnimationFrame = (cb) => {
        if (typeof cb !== 'function') return 0;
        const id = seq++;
        let done = false;
        const state = { rawId: 0, timerId: 0 };
        activeFrames.set(id, state);
        const fire = (ts) => {
          if (done) return;
          done = true;
          activeFrames.delete(id);
          if (state.timerId) clearTimeout(state.timerId);
          if (state.rawId && rawCancelRaf) {
            try { rawCancelRaf(state.rawId); } catch {}
          }
          const stamp =
            typeof ts === 'number' && Number.isFinite(ts)
              ? ts
              : typeof performance !== 'undefined'
                ? performance.now()
                : Date.now();
          cb(stamp);
        };
        if (rawRaf) {
          try {
            state.rawId = rawRaf(fire);
          } catch {}
        }
        state.timerId = setTimeout(() => fire(), 40);
        return id;
      };
      win.cancelAnimationFrame = (id) => {
        const state = activeFrames.get(id);
        if (!state) {
          if (rawCancelRaf) {
            try { rawCancelRaf(id); } catch {}
          }
          return;
        }
        activeFrames.delete(id);
        if (state.timerId) clearTimeout(state.timerId);
        if (state.rawId && rawCancelRaf) {
          try { rawCancelRaf(state.rawId); } catch {}
        }
      };
    } catch {}
    return true;
  };

  const computeScrollDelta = ({ scrollTop = 0, clientHeight = 900, scrollHeight = 0 }) => {
    const view = Math.max(400, Number(clientHeight) || 900);
    const top = Math.max(0, Number(scrollTop) || 0);
    const total = Math.max(top + view, Number(scrollHeight) || 0);
    const remaining = Math.max(0, total - (top + view));
    const step =
      remaining <= 0
        ? 0
        : remaining <= 220
          ? remaining
          : Math.max(Math.round(view * 0.9), Math.min(2600, remaining));
    return {
      step,
      remaining,
      nearBottom: remaining <= 220,
    };
  };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
      STORAGE_KEYS,
      isLikesGraphqlUrl,
      isTweetDetailGraphqlUrl,
      isLikesPagePath,
      cleanAccountHandle,
      resolveLikesAccount,
      extractInitialStateHandle,
      parseBgHash,
      installBgVisibilityBypass,
      computeScrollDelta,
      tweetIdFromUrl,
      toOrigImageUrl,
      extractMediaList,
      normalizeTweetResult,
      flattenDirectManifestItems,
      parseLikesPayload,
      parseTweetDetailPayload,
      extractTweetManifestIds,
      evaluateReconcileStop,
      trimProcessedCache,
      computeIncrementalSlice,
      nextAnchors,
      endpoint,
    };
    return;
  }

  const pageWin = typeof unsafeWindow !== 'undefined' ? unsafeWindow : window;
  // document-start 阶段立刻锁定后台运行标志并注入可见性/rAF 补偿，防止 X SPA 改写 hash 或后台休眠暂停渲染
  const initialBgRun = parseBgHash();
  if (initialBgRun) {
    installBgVisibilityBypass(pageWin);
  }
  const likesMap = new Map();
  const likesOrder = [];
  const detailMap = new Map();
  const detailOrder = [];
  const cursors = [];
  let likesGraphqlCount = 0;
  let activeLikesPath = isLikesPagePath(location.pathname) ? location.pathname : '';

  const resetLikesCache = (nextPath = '') => {
    likesMap.clear();
    likesOrder.length = 0;
    cursors.length = 0;
    likesGraphqlCount = 0;
    activeLikesPath = nextPath;
  };

  const ingestLikesTweets = (entries, reqLikesPath = '') => {
    if (reqLikesPath) {
      const curLikesPath = isLikesPagePath(location.pathname) ? location.pathname : '';
      if (curLikesPath && curLikesPath !== reqLikesPath) return;
      if (reqLikesPath !== activeLikesPath) resetLikesCache(reqLikesPath);
    }
    for (const item of entries) {
      if (!item || !item.id) continue;
      if (!likesMap.has(item.id)) likesOrder.push(item.id);
      likesMap.set(item.id, item);
    }
  };

  const ingestDetailTweets = (entries) => {
    for (const item of entries) {
      if (!item || !item.id) continue;
      if (!detailMap.has(item.id)) detailOrder.push(item.id);
      detailMap.set(item.id, item);
    }
  };

  // document-start Hook: 隔离捕获 /Likes 与 TweetDetail GraphQL 响应，避免 SPA 跳转串台
  const rawFetch = pageWin.fetch;
  if (typeof rawFetch === 'function') {
    pageWin.fetch = async function (...args) {
      const reqLikesPath = isLikesPagePath(location.pathname) ? location.pathname : '';
      const response = await rawFetch.apply(this, args);
      try {
        const reqUrl = typeof args[0] === 'string' ? args[0] : args[0]?.url || response?.url || '';
        if (isLikesGraphqlUrl(reqUrl)) {
          response.clone().json().then((json) => {
            const { entries, bottomCursor } = parseLikesPayload(json);
            const curLikesPath = isLikesPagePath(location.pathname) ? location.pathname : '';
            if (reqLikesPath && curLikesPath && curLikesPath !== reqLikesPath) return;
            if (reqLikesPath && reqLikesPath !== activeLikesPath) resetLikesCache(reqLikesPath);
            if (bottomCursor) cursors.push(bottomCursor);
            likesGraphqlCount += 1;
            ingestLikesTweets(entries, reqLikesPath);
          }).catch(() => {});
        } else if (isTweetDetailGraphqlUrl(reqUrl)) {
          response.clone().json().then((json) => {
            ingestDetailTweets(parseTweetDetailPayload(json));
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
      this.__nasReqLikesPath = isLikesPagePath(location.pathname) ? location.pathname : '';
      return RawXhrOpen.call(this, method, url, ...rest);
    };
    pageWin.XMLHttpRequest.prototype.send = function (...args) {
      const reqLikesPath = this.__nasReqLikesPath || '';
      this.addEventListener('load', () => {
        try {
          const reqUrl = this.responseURL || this.__nasReqUrl || '';
          if (this.status >= 200 && this.status < 300 && this.responseText) {
            if (isLikesGraphqlUrl(reqUrl)) {
              const json = JSON.parse(this.responseText);
              const { entries, bottomCursor } = parseLikesPayload(json);
              const curLikesPath = isLikesPagePath(location.pathname) ? location.pathname : '';
              if (reqLikesPath && curLikesPath && curLikesPath !== reqLikesPath) return;
              if (reqLikesPath && reqLikesPath !== activeLikesPath) resetLikesCache(reqLikesPath);
              if (bottomCursor) cursors.push(bottomCursor);
              likesGraphqlCount += 1;
              ingestLikesTweets(entries, reqLikesPath);
            } else if (isTweetDetailGraphqlUrl(reqUrl)) {
              const json = JSON.parse(this.responseText);
              ingestDetailTweets(parseTweetDetailPayload(json));
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
    maxBgScrolls: 60,
    stopConsecutive: 15,
    flushBatchSize: 30,
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

  const gmSetAll = async (obj) => {
    if (typeof GM.setValues === 'function') {
      await GM.setValues(obj);
      return;
    }
    for (const [k, v] of Object.entries(obj)) {
      GM_setValue(k, v);
    }
  };

  const writeBgResult = async (payload) => {
    try {
      if (typeof GM_setValue === 'function') GM_setValue(STORAGE_KEYS.BG_RESULT, payload);
    } catch {}
    await gmSetAll({ [STORAGE_KEYS.BG_RESULT]: payload });
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

  const gmGetJson = (url, token) =>
    new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method: 'GET',
        url,
        headers: {
          'X-NAS-Submit-Token': token || '',
        },
        timeout: 15000,
        onload(resp) {
          if (resp.status === 401) {
            reject(new Error('NAS 授权失败 (401)'));
            return;
          }
          if (resp.status < 200 || resp.status >= 300) {
            reject(new Error(`NAS HTTP ${resp.status}`));
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

  const fetchNasBgRun = async (cfg, runId) => {
    if (!cfg?.token || !runId) return null;
    try {
      const url = endpoint(cfg.host, cfg.port, `/api/browser-push/runs/${encodeURIComponent(runId)}`);
      const data = await gmGetJson(url, cfg.token);
      if (data?.found && data.run) return { ...data.run, runId };
    } catch {}
    return null;
  };

  const reportToNas = async (cfg, payload, { strict = false } = {}) => {
    if (!cfg?.token) {
      if (strict) throw new Error('请先在设置中填写从 NAS 控制台复制的「推送授权 Token」。');
      return null;
    }
    try {
      return await postToNas(endpoint(cfg.host, cfg.port, '/api/browser-push/report'), cfg.token, {
        platform: 'x',
        ...payload,
      });
    } catch (err) {
      if (strict) throw err;
      return null;
    }
  };

  const getLikesTweets = () =>
    likesOrder.map((id) => likesMap.get(id)).filter(Boolean);

  const getDetailTweets = () =>
    detailOrder.map((id) => detailMap.get(id)).filter(Boolean);

  const getOrderedTweets = () => {
    if (isLikesPagePath(location.pathname)) return getLikesTweets();
    const details = getDetailTweets();
    return details.length ? details : getLikesTweets();
  };

  const reconcileTweets = async (ids, cfg) => {
    const cleanIds = [...new Set((Array.isArray(ids) ? ids : []).map((x) => String(x || '').trim()).filter((x) => /^\d{1,30}$/.test(x)))];
    const states = {};
    if (!cleanIds.length) return states;
    if (!cfg?.token) {
      throw new Error('请先在设置中填写从 NAS 控制台复制的「推送授权 Token」。');
    }
    const url = endpoint(cfg.host, cfg.port, '/api/x/reconcile');
    for (let offset = 0; offset < cleanIds.length; offset += 500) {
      const part = cleanIds.slice(offset, offset + 500);
      const reply = await postToNas(url, cfg.token, { ids: part });
      for (const id of part) {
        const st = reply?.states?.[id];
        if (!['archived', 'pending', 'deferred', 'missing'].includes(st)) {
          throw new Error('NAS 未完整返回推文 ID 对账状态。');
        }
        states[id] = st;
      }
    }
    return states;
  };

  const pushBaselineIds = async (ids, { account, sourceMode = 'workbench', cfg, onProgress }) => {
    const cleanIds = [...new Set((Array.isArray(ids) ? ids : []).map((x) => String(x || '').trim()).filter((x) => /^\d{1,30}$/.test(x)))];
    if (!cleanIds.length) return { submitted: 0, added: 0, existing: 0, baselineTotal: 0 };
    if (!cfg?.token) {
      throw new Error('请先在设置中填写从 NAS 控制台复制的「推送授权 Token」。');
    }
    const url = endpoint(cfg.host, cfg.port, '/api/x/baseline');
    let submitted = 0;
    let added = 0;
    let existing = 0;
    let baselineTotal = 0;
    for (let offset = 0; offset < cleanIds.length; offset += 500) {
      const part = cleanIds.slice(offset, offset + 500);
      if (onProgress) onProgress(`正在导入基线 ${offset}/${cleanIds.length} 条推文 ID…`);
      const reply = await postToNas(url, cfg.token, {
        ids: part,
        account,
        source_mode: sourceMode,
      });
      if (!reply?.ok) {
        throw new Error('NAS 基线导入未返回确认回执。');
      }
      submitted += part.length;
      added += Number(reply.added || 0);
      existing += Number(reply.existing || 0);
      baselineTotal = Number(reply.baseline_total || baselineTotal);
    }
    return { submitted, added, existing, baselineTotal };
  };

  const RESERVED_PATH_ROOTS = new Set(['i', 'home', 'explore', 'notifications', 'messages', 'settings', 'search']);

  const readLiveAccountHandle = () => {
    const switcher = cleanAccountHandle(
      document.querySelector('[data-testid="SideNav_AccountSwitcher_Button"] [dir="ltr"]')?.textContent || ''
    );
    if (switcher) return switcher;

    const profileHref =
      document.querySelector('[data-testid="AppTabBar_Profile_Link"]')?.getAttribute('href') || '';
    const profileMatch = profileHref.match(/^\/([A-Za-z0-9_]{1,30})\/?$/);
    if (profileMatch && !RESERVED_PATH_ROOTS.has(profileMatch[1].toLowerCase())) {
      return profileMatch[1];
    }

    return extractInitialStateHandle(pageWin.__INITIAL_STATE__);
  };

  let lastCachedAccount = '';
  const accountScopeKey = () => {
    const cached =
      typeof GM_getValue === 'function' ? cleanAccountHandle(GM_getValue(STORAGE_KEYS.LAST_ACCOUNT, '')) : '';
    const live = readLiveAccountHandle();
    const resolved = resolveLikesAccount(location.pathname, live, cached);
    const valid = cleanAccountHandle(resolved);
    if (valid && valid !== cached && valid !== lastCachedAccount && typeof GM_setValue === 'function') {
      lastCachedAccount = valid;
      try {
        GM_setValue(STORAGE_KEYS.LAST_ACCOUNT, valid);
      } catch {}
    }
    return resolved;
  };

  const getAnchors = (acct) => {
    const val = GM_getValue(STORAGE_KEYS.ANCHORS_PREFIX + acct, []);
    return Array.isArray(val) ? val : [];
  };

  const getCutoff = (acct) => GM_getValue(STORAGE_KEYS.CUTOFF_PREFIX + acct, '');

  const pushTweetsBatch = async (
    tweets,
    { sourceMode, updateAnchors, account, cfg, discovered, runId = '', onProgress }
  ) => {
    const discoveredTotal = Number(discovered || tweets.length);
    const { manifests, textOnlySkipped } = flattenDirectManifestItems(tweets);
    if (!manifests.length) {
      await reportToNas(cfg, {
        source_mode: sourceMode,
        ...(runId ? { run_id: runId } : {}),
        account,
        status: 'empty',
        discovered: discoveredTotal,
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
      const isLastChunk = offset + CHUNK_SIZE >= manifests.length;
      const chunkBatchId = manifests.length <= CHUNK_SIZE ? batchId : `${batchId}_${offset}`;
      if (onProgress) onProgress(`正在推送 ${offset}/${manifests.length} 条推文清单…`);
      const reply = await postToNas(url, cfg.token, {
        batch_id: chunkBatchId,
        source_mode: sourceMode,
        ...(runId && isLastChunk ? { run_id: runId } : {}),
        account,
        discovered: discoveredTotal,
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

  const scrollPageStep = async () => {
    const scroller = document.scrollingElement || document.documentElement || document.body;
    const beforeTop = Number(window.scrollY ?? scroller?.scrollTop ?? 0);
    const view = Math.max(400, Number(window.innerHeight || scroller?.clientHeight || 900));
    const scrollHeight = Number(scroller?.scrollHeight || document.body?.scrollHeight || 0);
    const calc = computeScrollDelta({ scrollTop: beforeTop, clientHeight: view, scrollHeight });

    if (calc.nearBottom) {
      // 贴近底部哨兵区时先轻微上提再回底，重新触发 X 的下一页 /Likes IntersectionObserver
      const bounceTop = Math.max(0, beforeTop - 240);
      try { window.scrollTo(0, bounceTop); } catch {}
      if (scroller && Number(scroller.scrollTop || 0) === beforeTop && bounceTop < beforeTop) {
        try { scroller.scrollTop = bounceTop; } catch {}
      }
      await sleep(80);
      const targetBottom = Number(scroller?.scrollHeight || scrollHeight);
      try { window.scrollTo(0, targetBottom); } catch {}
      if (scroller) {
        try { scroller.scrollTop = targetBottom; } catch {}
      }
    } else {
      try { window.scrollBy(0, calc.step); } catch {}
      const checkTop = Number(window.scrollY ?? scroller?.scrollTop ?? 0);
      if (scroller && checkTop <= beforeTop + 2) {
        try { scroller.scrollTop = beforeTop + calc.step; } catch {}
      }
    }

    try {
      window.dispatchEvent(new Event('scroll'));
      document.dispatchEvent(new Event('scroll'));
    } catch {}

    const afterTop = Number(window.scrollY ?? scroller?.scrollTop ?? 0);
    const afterHeight = Number(scroller?.scrollHeight || scrollHeight);
    return {
      moved: afterTop > beforeTop + 2,
      atBottom: afterTop + view >= afterHeight - 220,
      calc,
    };
  };

  const waitForBgInitialState = async (maxWaitMs = 8000) => {
    const startAt = Date.now();
    const deadline = startAt + maxWaitMs;
    while (Date.now() < deadline) {
      if (/\/i\/flow\/login|\/login/.test(location.pathname)) {
        throw new Error('X 账号未登录或会话已失效，请在 Chrome 中重新登录 x.com');
      }
      const tweetsReady = getOrderedTweets().length > 0;
      const handleReady = Boolean(readLiveAccountHandle());
      if (tweetsReady && (handleReady || Date.now() - startAt >= 5000)) {
        return;
      }
      await sleep(350);
    }
  };

  const awaitBgResult = ({ cfg, storageKey, runId, openTab, timeoutMs, timeoutMessage }) =>
    new Promise((resolve, reject) => {
      let settled = false;
      let timer = null;
      let pollTimer = null;
      let nasPollTimer = null;
      let closeTimer = null;
      let listenerId = null;

      const cleanup = () => {
        if (timer) clearTimeout(timer);
        if (pollTimer) clearInterval(pollTimer);
        if (nasPollTimer) clearInterval(nasPollTimer);
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
            typeof GM.getValue === 'function'
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
      listenerId = GM_addValueChangeListener(storageKey, (_name, _oldVal, val) => {
        if (val && val.runId === runId) finish(null, val);
      });
      pollTimer = setInterval(async () => {
        try {
          const cur =
            typeof GM.getValue === 'function'
              ? await GM.getValue(storageKey, null)
              : GM_getValue(storageKey, null);
          if (cur && cur.runId === runId) finish(null, cur);
        } catch {}
      }, 800);
      nasPollTimer = setInterval(async () => {
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

  // 等待 DOM Ready
  const onReady = (fn) => {
    if (document.readyState !== 'loading') fn();
    else document.addEventListener('DOMContentLoaded', fn, { once: true });
  };

  onReady(async () => {
    const bgRun = initialBgRun || parseBgHash();
    if (bgRun) {
      const cfg = loadConfig();
      let anchorUpdated = false;
      let acct = '';
      let totalDiscovered = 0;
      try {
        if (!cfg.token) {
          throw new Error('请先在设置中填写从 NAS 控制台复制的「推送授权 Token」。');
        }
        await waitForBgInitialState(8000);
        if (/\/i\/flow\/login|\/login/.test(location.pathname)) {
          throw new Error('X 账号未登录或会话已失效，请在 Chrome 中重新登录 x.com');
        }
        acct = accountScopeKey();
        const cutoff = getCutoff(acct);
        const maxScrolls = Math.max(1, Math.min(200, Number(cfg.maxBgScrolls || 60)));
        const stopThreshold = Math.max(0, Number(cfg.stopConsecutive ?? 15));
        const flushBatchSize = Math.max(5, Math.min(200, Number(cfg.flushBatchSize || 30)));
        const settledSet = new Set(['archived', 'pending', 'deferred']);

        const evaluatedIds = new Set();
        const queuedOrPushedIds = new Set();
        const reconciledStates = {};
        const pendingBuffer = [];
        const headAnchorCandidates = [];
        let consecutiveSettled = 0;
        let stoppedByReconcile = false;
        let stoppedByCutoff = false;
        let totalSubmitted = 0;
        let totalAccepted = 0;
        let totalSkipped = 0;
        let totalUnresolvable = 0;
        let lastTaskId = '';

        const syncNewTweetsAndFlush = async (forceFinal = false) => {
          do {
            const freshTweets =
              !stoppedByReconcile && !stoppedByCutoff
                ? getLikesTweets().filter((t) => t && t.id && !evaluatedIds.has(t.id))
                : [];
            if (freshTweets.length > 0) {
              const needCheckIds = [];
              for (const tweet of freshTweets) {
                for (const mid of extractTweetManifestIds(tweet)) {
                  if (!(mid in reconciledStates)) needCheckIds.push(mid);
                }
              }
              if (needCheckIds.length > 0) {
                Object.assign(reconciledStates, await reconcileTweets(needCheckIds, cfg));
              }
              for (const tweet of freshTweets) {
                totalDiscovered += 1;
                if (headAnchorCandidates.length < MAX_ANCHORS) headAnchorCandidates.push(tweet);
                if (cutoff && tweet.id === cutoff) {
                  stoppedByCutoff = true;
                  evaluatedIds.add(tweet.id);
                  break;
                }
                const mids = extractTweetManifestIds(tweet);
                if (!mids.length) {
                  evaluatedIds.add(tweet.id);
                  continue;
                }
                const allSettled = mids.every((id) => settledSet.has(reconciledStates[id]));
                if (allSettled) {
                  consecutiveSettled += 1;
                } else {
                  consecutiveSettled = 0;
                  if (!queuedOrPushedIds.has(tweet.id)) {
                    queuedOrPushedIds.add(tweet.id);
                    pendingBuffer.push(tweet);
                  }
                }
                evaluatedIds.add(tweet.id);
                if (stopThreshold > 0 && consecutiveSettled >= stopThreshold) {
                  stoppedByReconcile = true;
                  break;
                }
              }
            }

            while (
              (!forceFinal && pendingBuffer.length >= flushBatchSize) ||
              (forceFinal && pendingBuffer.length > 0)
            ) {
              const take = forceFinal
                ? pendingBuffer.splice(0, CHUNK_SIZE)
                : pendingBuffer.splice(0, flushBatchSize);
              const summary = await pushTweetsBatch(take, {
                sourceMode: 'bg',
                updateAnchors: false,
                account: acct,
                cfg,
                discovered: totalDiscovered,
                runId: '',
              });
              totalSubmitted += summary.submitted;
              totalAccepted += summary.accepted;
              totalSkipped += summary.skipped;
              totalUnresolvable += summary.unresolvable;
              if (summary.taskIds?.length) {
                lastTaskId = summary.taskIds[summary.taskIds.length - 1];
              }
              trimProcessedCache(
                likesOrder,
                likesMap,
                120,
                new Set(pendingBuffer.map((t) => t.id)),
                evaluatedIds
              );
            }
          } while (
            forceFinal &&
            (pendingBuffer.length > 0 ||
              (!stoppedByReconcile &&
                !stoppedByCutoff &&
                getLikesTweets().some((t) => t && t.id && !evaluatedIds.has(t.id))))
          );
        };

        let stableTicks = 0;
        for (let step = 0; step < maxScrolls; step += 1) {
          if (/\/i\/flow\/login|\/login/.test(location.pathname)) {
            throw new Error('X 跳转至登录页，账号会话已失效');
          }
          await syncNewTweetsAndFlush(false);
          if (stoppedByReconcile || stoppedByCutoff) break;

          const prevCount = getLikesTweets().length;
          const prevReqCount = likesGraphqlCount;
          const { moved, atBottom } = await scrollPageStep();
          await sleep(atBottom ? 1800 : 700);
          const curCount = getLikesTweets().length;
          const curReqCount = likesGraphqlCount;
          // 未滑到底部时处于单页内容区内部，不得累加静止次数以免提前中断翻页
          stableTicks =
            curCount === prevCount && curReqCount === prevReqCount && (atBottom || !moved)
              ? stableTicks + 1
              : 0;
          if (stableTicks >= 4) break;
        }

        await syncNewTweetsAndFlush(true);
        if (totalDiscovered === 0) {
          throw new Error('未拦截到 X /Likes 推文数据，请确认页面可访问且已登录');
        }

        if (totalSubmitted > 0 && headAnchorCandidates.length) {
          await gmSetAll({
            [STORAGE_KEYS.ANCHORS_PREFIX + acct]: nextAnchors(
              headAnchorCandidates,
              getAnchors(acct)
            ),
          });
          anchorUpdated = true;
        }

        if (totalSubmitted === 0) {
          await reportToNas(
            cfg,
            {
              source_mode: 'bg',
              run_id: bgRun.runId,
              account: acct,
              status: 'empty',
              discovered: totalDiscovered,
              message: `后台对账 ${totalDiscovered} 条点赞推文（连续已归档 ${consecutiveSettled}），无新增媒体`,
            },
            { strict: true }
          );
          await writeBgResult({
            runId: bgRun.runId,
            platform: 'x',
            status: 'ok',
            reason: 'no_new_items',
            itemCount: 0,
            anchorUpdated: false,
            finishedAt: new Date().toISOString(),
          });
          return;
        }

        await reportToNas(
          cfg,
          {
            source_mode: 'bg',
            run_id: bgRun.runId,
            account: acct,
            status: 'ok',
            discovered: totalDiscovered,
            item_count: totalAccepted,
            accepted: totalAccepted,
            submitted: totalSubmitted,
            skipped: totalSkipped,
            ...(lastTaskId ? { task_id: lastTaskId } : {}),
            message: `流式同步完成：共检查 ${totalDiscovered} 条，提交 ${totalSubmitted} 组（新增 ${totalAccepted}）`,
          },
          { strict: true }
        );
        await writeBgResult({
          runId: bgRun.runId,
          platform: 'x',
          status: 'ok',
          reason: 'pushed',
          itemCount: totalAccepted,
          submitted: totalSubmitted,
          skipped: totalSkipped,
          unresolvable: totalUnresolvable,
          anchorUpdated,
          finishedAt: new Date().toISOString(),
        });
      } catch (err) {
        const msg = err?.message || String(err);
        await reportToNas(cfg, {
          source_mode: 'bg',
          run_id: bgRun.runId,
          account: acct || accountScopeKey(),
          status: 'error',
          discovered: totalDiscovered || getLikesTweets().length,
          message: `后台同步失败：${msg}`,
        });
        await writeBgResult({
          runId: bgRun.runId,
          platform: 'x',
          status: 'failed',
          reason: msg,
          anchorUpdated,
          finishedAt: new Date().toISOString(),
        });
      } finally {
        // 先留足扩展存储同步时间，再在标签页内自关兜底，避免后台标签长期残留
        await sleep(1200);
        try { window.onbeforeunload = null; } catch {}
        try { pageWin.onbeforeunload = null; } catch {}
        try {
          if (typeof window.close === 'function') window.close();
        } catch {}
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
      <button id="push-baseline-loaded" class="grow">已捕获导入基线</button>
      <button id="scroll-baseline" class="grow">边滚边导基线（不下载）</button>
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
    <div class="row">
      <label class="field grow">连续已归档早停数（0=全量）<input id="cfg-stop-consecutive" type="number" min="0" max="500"></label>
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
      $('#cfg-stop-consecutive').value = cfg.stopConsecutive ?? 15;
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
      const stopThreshold = Math.max(0, Number(cfg.stopConsecutive ?? 15));
      const statesCache = {};
      let idle = 0;
      try {
        for (let step = 0; step < Number(cfg.maxScrolls || 60) && scrolling; step += 1) {
          const ordered = getLikesTweets();
          if (cutoff && ordered.some((t) => t.id === cutoff)) {
            setStatus(`已滚动命中手动截止位置，共缓存 ${ordered.length} 条。`);
            break;
          }
          let statusExtra = '';
          if (cfg.token && ordered.length > 0 && stopThreshold > 0) {
            const needIds = [];
            for (const t of ordered) {
              for (const mid of extractTweetManifestIds(t)) {
                if (!(mid in statesCache)) needIds.push(mid);
              }
            }
            if (needIds.length > 0) {
              Object.assign(statesCache, await reconcileTweets(needIds, cfg));
            }
            const evalRes = evaluateReconcileStop(ordered, statesCache, stopThreshold);
            statusExtra = ` · 待推 ${evalRes.toPush.length} · 连续已归档 ${evalRes.consecutiveSettled}/${stopThreshold}`;
            if (evalRes.shouldStop) {
              setStatus(`已命中连续 ${stopThreshold} 条 NAS 已归档记录，停止加载（共 ${ordered.length} 条）。`);
              break;
            }
          } else if (ordered.some((t) => anchors.has(t.id))) {
            setStatus(`已滚动命中本地锚点，共缓存 ${ordered.length} 条。`);
            break;
          }
          const before = ordered.length;
          const prevReq = likesGraphqlCount;
          const { moved, atBottom } = await scrollPageStep();
          await sleep(atBottom ? 1600 : 700);
          const after = getLikesTweets().length;
          idle = after === before && likesGraphqlCount === prevReq && (atBottom || !moved) ? idle + 1 : 0;
          setStatus(`正在加载点赞列表…第 ${step + 1} 次滚动，已捕获 ${after} 条推文${statusExtra}`);
          refreshView();
          if (idle >= 5) {
            setStatus(`连续多次无新增推文，停止自动加载（共 ${after} 条${statusExtra}）。`);
            break;
          }
        }
      } catch (e) {
        setStatus(e.message || String(e), true);
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
        cfg = loadConfig();
        const acct = accountScopeKey();
        const ordered = getLikesTweets();
        if (!ordered.length) throw new Error('暂未捕获到点赞推文，请先在点赞页滚动加载。');
        const cutoff = getCutoff(acct);
        const cutoffIdx = cutoff ? ordered.findIndex((it) => it.id === cutoff) : -1;
        const scoped = cutoffIdx >= 0 ? ordered.slice(0, cutoffIdx) : ordered;
        const checkIds = [];
        for (const t of scoped) {
          for (const mid of extractTweetManifestIds(t)) checkIds.push(mid);
        }
        const states = await reconcileTweets(checkIds, cfg);
        const evalRes = evaluateReconcileStop(scoped, states, 0);
        const toPush = evalRes.toPush;
        if (!toPush.length) {
          await reportToNas(cfg, {
            source_mode: 'workbench',
            account: acct,
            status: 'empty',
            discovered: scoped.length,
            message: `工作台与 NAS 对账 ${scoped.length} 条点赞推文，无新增待下载内容`,
          });
          return setStatus(`对账完成（共 ${scoped.length} 条），没有需要下载的新推文。`);
        }
        const res = await pushTweetsBatch(toPush, {
          sourceMode: 'workbench',
          updateAnchors: true,
          account: acct,
          cfg,
          discovered: scoped.length,
          onProgress: (m) => setStatus(m),
        });
        if (scoped.length) {
          await gmSetAll({
            [STORAGE_KEYS.ANCHORS_PREFIX + acct]: nextAnchors(scoped, getAnchors(acct)),
          });
        }
        setStatus(
          `发送成功！对账 ${scoped.length} 条 → 待推 ${toPush.length} 条 · 清单 ${res.submitted} 组 · NAS 接收 ${res.accepted} · 重复跳过 ${res.skipped} · 异常直链 ${res.unresolvable}`
        );
      } catch (e) {
        setStatus(e.message || String(e), true);
      } finally {
        busy = false;
        refreshView();
      }
    };

    const collectAllTweetIdsFromItems = (list) => {
      const ids = [];
      for (const t of list) {
        if (t?.id) ids.push(String(t.id));
        if (t?.outerTweetId) ids.push(String(t.outerTweetId));
        if (t?.quotedTweetId) ids.push(String(t.quotedTweetId));
        if (t?.retweetedTweetId) ids.push(String(t.retweetedTweetId));
        for (const mid of extractTweetManifestIds(t)) ids.push(mid);
      }
      return [...new Set(ids.filter((x) => /^\d{1,30}$/.test(x)))];
    };

    $('#push-baseline-loaded').onclick = async () => {
      if (busy || scrolling) return;
      busy = true;
      try {
        cfg = loadConfig();
        const ordered = getOrderedTweets();
        const ids = collectAllTweetIdsFromItems(ordered);
        if (!ids.length) throw new Error('暂未捕获到推文 ID，请先在点赞页滚动加载。');
        const res = await pushBaselineIds(ids, {
          account: accountScopeKey(),
          sourceMode: 'workbench',
          cfg,
          onProgress: (m) => setStatus(m),
        });
        setStatus(
          `基线导入完成！提交 ${res.submitted} 条 ID · 新增 ${res.added} · 已存在 ${res.existing} · 累计基线 ${res.baselineTotal} 条`
        );
      } catch (e) {
        setStatus(e.message || String(e), true);
      } finally {
        busy = false;
        refreshView();
      }
    };

    $('#scroll-baseline').onclick = async () => {
      if (scrolling || busy) return;
      cfg = loadConfig();
      if (!cfg.token) return setStatus('请先填写并保存推送授权 Token。', true);
      scrolling = true;
      busy = true;
      $('#stop-scroll').classList.remove('hidden');
      const acct = accountScopeKey();
      const sentBaselineIds = new Set();
      const processedTweetIds = new Set();
      let totalAdded = 0;
      let totalSubmitted = 0;
      let latestTotal = 0;
      let idle = 0;
      try {
        for (let step = 0; step < Number(cfg.maxScrolls || 60) && scrolling; step += 1) {
          const current = getLikesTweets();
          const freshIds = collectAllTweetIdsFromItems(current).filter((id) => !sentBaselineIds.has(id));
          if (freshIds.length > 0) {
            const res = await pushBaselineIds(freshIds, {
              account: acct,
              sourceMode: 'workbench',
              cfg,
            });
            for (const id of freshIds) sentBaselineIds.add(id);
            for (const t of current) {
              if (t?.id) processedTweetIds.add(t.id);
            }
            totalSubmitted += res.submitted;
            totalAdded += res.added;
            latestTotal = res.baselineTotal;
            trimProcessedCache(likesOrder, likesMap, 120, null, processedTweetIds);
          }
          const before = getLikesTweets().length;
          const prevReq = likesGraphqlCount;
          const { moved, atBottom } = await scrollPageStep();
          await sleep(atBottom ? 1600 : 700);
          const after = getLikesTweets().length;
          idle = after === before && likesGraphqlCount === prevReq && (atBottom || !moved) ? idle + 1 : 0;
          setStatus(
            `正在导入基线…第 ${step + 1} 次滚动 · 累计提交 ${totalSubmitted} · 新增 ${totalAdded} · 总基线 ${latestTotal}`
          );
          refreshView();
          if (idle >= 5) break;
        }
        while (true) {
          const tailIds = collectAllTweetIdsFromItems(getLikesTweets()).filter((id) => !sentBaselineIds.has(id));
          if (!tailIds.length) break;
          const res = await pushBaselineIds(tailIds, {
            account: acct,
            sourceMode: 'workbench',
            cfg,
          });
          for (const id of tailIds) sentBaselineIds.add(id);
          totalSubmitted += res.submitted;
          totalAdded += res.added;
          latestTotal = res.baselineTotal;
        }
        setStatus(
          `边滚边导基线结束！共提交 ${totalSubmitted} 条唯一 ID · 新增 ${totalAdded} · 当前总基线 ${latestTotal} 条`
        );
      } catch (e) {
        setStatus(e.message || String(e), true);
      } finally {
        scrolling = false;
        busy = false;
        $('#stop-scroll').classList.add('hidden');
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
      try {
        const result = await awaitBgResult({
          cfg,
          storageKey: STORAGE_KEYS.BG_RESULT,
          runId,
          timeoutMs: 120000,
          timeoutMessage: '后台标签页超时（120 秒）未返回回执',
          openTab: () => {
            tab = GM_openInTab(tabUrl, { active: false, insert: true, setParent: true });
            return tab;
          },
        });
        setStatus(
          `后台运行结束：${result.status} (${result.reason || ''}) · 新增 ${result.itemCount || 0} · 锚点更新=${Boolean(result.anchorUpdated)}`,
          result.status !== 'ok'
        );
      } catch (e) {
        setStatus(e.message || String(e), true);
      } finally {
        const closeTabSafe = () => {
          try { tab?.close?.(); } catch {}
        };
        closeTabSafe();
        setTimeout(closeTabSafe, 500);
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
        maxBgScrolls: Math.max(1, Math.min(200, Number($('#cfg-bg-scroll').value || 60))),
        stopConsecutive: Math.max(0, Math.min(500, Number($('#cfg-stop-consecutive').value ?? 15))),
        flushBatchSize: cfg.flushBatchSize || 30,
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
      if (isLikesPagePath(location.pathname) && location.pathname !== activeLikesPath) {
        resetLikesCache(location.pathname);
      }
      if (!$('.panel').classList.contains('hidden')) refreshView();
      else $('#pill').textContent = String(getOrderedTweets().length);
    }, 2000);
  });
})();
