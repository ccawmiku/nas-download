const {test} = require('node:test');
const assert = require('node:assert/strict');
const xQueue = require('../tools/userscripts/x-nas-queue.user.js');
const xBg = require('../tools/userscripts/x-nas-bg.user.js');

function buildMockLikesResponse() {
  return {
    data: {
      user: {
        result: {
          timeline_v2: {
            timeline: {
              instructions: [
                {
                  type: 'TimelineAddEntries',
                  entries: [
                    {
                      entryId: 'tweet-1001',
                      sortIndex: '9001',
                      content: {
                        entryType: 'TimelineTimelineItem',
                        itemContent: {
                          tweet_results: {
                            result: {
                              __typename: 'Tweet',
                              rest_id: '1001',
                              core: {
                                user_results: {
                                  result: {core: {screen_name: 'alice'}},
                                },
                              },
                              legacy: {
                                id_str: '1001',
                                full_text: 'outer post with quote',
                                extended_entities: {
                                  media: [
                                    {
                                      media_key: '3_1001_1',
                                      type: 'photo',
                                      media_url_https: 'https://pbs.twimg.com/media/abc123?format=jpg&name=large',
                                    },
                                  ],
                                },
                              },
                              quoted_status_result: {
                                result: {
                                  __typename: 'TweetWithVisibilityResults',
                                  tweet: {
                                    rest_id: '2002',
                                    core: {
                                      user_results: {
                                        result: {legacy: {screen_name: 'bob'}},
                                      },
                                    },
                                    legacy: {
                                      id_str: '2002',
                                      full_text: 'quoted original video',
                                      extended_entities: {
                                        media: [
                                          {
                                            media_key: '7_2002_1',
                                            type: 'video',
                                            video_info: {
                                              variants: [
                                                {content_type: 'application/x-mpegURL', url: 'https://video.twimg.com/playlist.m3u8'},
                                                {content_type: 'video/mp4', bitrate: 832000, url: 'https://video.twimg.com/ext_tw_video/2002/pu/vid/low.mp4'},
                                                {content_type: 'video/mp4', bitrate: 2176000, url: 'https://video.twimg.com/ext_tw_video/2002/pu/vid/high.mp4'},
                                              ],
                                            },
                                          },
                                        ],
                                      },
                                    },
                                  },
                                },
                              },
                            },
                          },
                        },
                      },
                    },
                    {
                      entryId: 'tweet-1002',
                      sortIndex: '9000',
                      content: {
                        entryType: 'TimelineTimelineItem',
                        itemContent: {
                          tweet_results: {
                            result: {
                              rest_id: '1002',
                              core: {user_results: {result: {legacy: {screen_name: 'carol'}}}},
                              legacy: {full_text: 'text only tweet'},
                            },
                          },
                        },
                      },
                    },
                    {
                      entryId: 'cursor-bottom-1',
                      content: {entryType: 'TimelineTimelineCursor', cursorType: 'Bottom', value: 'CURSOR_END'},
                    },
                  ],
                },
              ],
            },
          },
        },
      },
    },
  };
}

test('parses GraphQL Likes, resolves highest bitrate MP4 and :orig image, and flattens Quote Option A independently', () => {
  assert.equal(xQueue.isLikesGraphqlUrl('https://x.com/i/api/graphql/xyz123/Likes?variables={}'), true);
  assert.equal(xQueue.isLikesPagePath('/i/history/likes'), true);
  assert.equal(xQueue.resolveLikesAccount('/alice/likes'), 'alice');
  assert.equal(xQueue.resolveLikesAccount('/i/history/likes'), 'i_history_likes');
  assert.equal(xQueue.resolveLikesAccount('/i/history/likes', 'myhandle'), 'myhandle');

  const {entries, bottomCursor} = xQueue.parseLikesPayload(buildMockLikesResponse());
  assert.equal(bottomCursor, 'CURSOR_END');
  assert.equal(entries.length, 2);

  const flat = xQueue.flattenDirectManifestItems(entries);
  assert.equal(flat.textOnlySkipped, 1);
  assert.equal(flat.manifests.length, 2);

  const outer = flat.manifests.find((m) => m.tweet_id === '1001');
  const quoted = flat.manifests.find((m) => m.tweet_id === '2002');
  assert.equal(outer.author, 'alice');
  assert.equal(outer.owner_role, 'outer');
  assert.equal(outer.media[0].url, 'https://pbs.twimg.com/media/abc123?format=jpg&name=orig');

  assert.equal(quoted.author, 'bob');
  assert.equal(quoted.owner_role, 'quoted');
  assert.equal(quoted.quoted_from, '1001');
  assert.equal(quoted.media[0].type, 'video');
  assert.equal(quoted.media[0].url, 'https://video.twimg.com/ext_tw_video/2002/pu/vid/high.mp4');
});

test('incremental slicing stops at anchor or manual cutoff and fails closed when lost', () => {
  const list = [{id: '5'}, {id: '4'}, {id: '3'}, {id: '2'}, {id: '1'}];
  const s1 = xQueue.computeIncrementalSlice(list, ['3', '2'], '');
  assert.deepEqual(s1.items.map((x) => x.id), ['5', '4']);
  assert.equal(s1.matchedBy, 'anchor');

  const s2 = xQueue.computeIncrementalSlice(list, ['2'], '4');
  assert.deepEqual(s2.items.map((x) => x.id), ['5']);
  assert.equal(s2.matchedBy, 'cutoff');

  assert.throws(() => xQueue.computeIncrementalSlice(list, ['missing'], ''), /未匹配到/);
  assert.deepEqual(xQueue.nextAnchors([{id: '7'}, {id: '6'}], ['5', '4', '3', '2']), ['7', '6', '5', '4', '3']);
});

test('x daily scheduler delegates daily lock to native @crontab once and allows manual retries', () => {
  const day1 = new Date(2026, 10, 15, 8, 30, 0).getTime();
  const cfg = {dailyBgEnabled: true, bgTargetUrl: 'https://x.com/i/history/likes'};

  const d = xBg.shouldRunDailyBg({config: cfg, nowMs: day1});
  assert.equal(d.run, true);
  assert.equal(d.targetUrl, 'https://x.com/i/history/likes');

  assert.equal(xBg.canScheduleRetry({}, day1), true);
  let failState = {};
  failState = xBg.markDailyFailure(failState, day1, 'e1');
  failState = xBg.markDailyFailure(failState, day1 + 1000, 'e2');
  assert.equal(xBg.canScheduleRetry(failState, day1 + 2000), true);
  failState = xBg.markDailyFailure(failState, day1 + 3000, 'e3');
  assert.equal(xBg.canScheduleRetry(failState, day1 + 4000), false);
  // Empty bgTargetUrl falls back safely to default /i/history/likes URL
  const fallback = xBg.shouldRunDailyBg({config: {bgTargetUrl: ''}, nowMs: day1});
  assert.equal(fallback.run, true);
  assert.equal(fallback.targetUrl, 'https://x.com/i/history/likes');
});

test('resolves cached handle, parses bg hash/state, and installs background visibility/rAF bypass', async () => {
  assert.equal(xQueue.cleanAccountHandle('@CCCW'), 'CCCW');
  assert.equal(xQueue.cleanAccountHandle('i_history_likes'), '');
  assert.equal(xQueue.resolveLikesAccount('/i/history/likes', '', 'CCCW'), 'CCCW');
  assert.equal(xQueue.resolveLikesAccount('/i/history/likes', 'i_history_likes', 'CCCW'), 'CCCW');
  assert.equal(xQueue.resolveLikesAccount('/i/history/likes', '@LiveUser', 'CCCW'), 'LiveUser');

  assert.deepEqual(xQueue.parseBgHash('#nas_bg_run=x_daily_1&nas_bg_ignore_lock=1'), {
    runId: 'x_daily_1',
    ignoreLock: true,
  });
  assert.equal(xQueue.parseBgHash('#other=1'), null);

  assert.equal(
    xQueue.extractInitialStateHandle({
      session: {user_id: 'u1'},
      entities: {users: {entities: {u1: {screen_name: 'CCCW'}}}},
    }),
    'CCCW'
  );

  const registeredEvents = [];
  let frozenRafCalled = false;
  const mockDoc = {
    hidden: true,
    webkitHidden: true,
    visibilityState: 'hidden',
    webkitVisibilityState: 'hidden',
    hasFocus: () => false,
    addEventListener: (type) => registeredEvents.push(type),
  };
  const mockWin = {
    document: mockDoc,
    addEventListener: (type) => registeredEvents.push(`win:${type}`),
    requestAnimationFrame: () => {
      frozenRafCalled = true;
      return 99;
    },
    cancelAnimationFrame: () => {},
  };

  assert.equal(xQueue.installBgVisibilityBypass(mockWin), true);
  assert.equal(mockDoc.hidden, false);
  assert.equal(mockDoc.visibilityState, 'visible');
  assert.equal(mockDoc.hasFocus(), true);

  registeredEvents.length = 0;
  mockDoc.addEventListener('visibilitychange', () => {});
  mockWin.addEventListener('webkitvisibilitychange', () => {});
  mockDoc.addEventListener('DOMContentLoaded', () => {});
  assert.deepEqual(registeredEvents, ['DOMContentLoaded']);

  let rafFired = false;
  await new Promise((resolve) => {
    const cancelled = mockWin.requestAnimationFrame(() => {
      throw new Error('cancelled rAF should not fire');
    });
    mockWin.cancelAnimationFrame(cancelled);
    mockWin.requestAnimationFrame((ts) => {
      rafFired = Number.isFinite(ts);
      resolve();
    });
  });
  assert.equal(frozenRafCalled, true);
  assert.equal(rafFired, true);

  const mid = xQueue.computeScrollDelta({scrollTop: 0, clientHeight: 900, scrollHeight: 9500});
  assert.equal(mid.nearBottom, false);
  assert.equal(mid.step, 2600);

  const bottom = xQueue.computeScrollDelta({scrollTop: 8500, clientHeight: 900, scrollHeight: 9520});
  assert.equal(bottom.nearBottom, true);
  assert.equal(bottom.step, 120);
});

test('evaluates server reconcile early-stop with text-only skip, Quote Option A multi-IDs, and cache trimming', () => {
  const {entries} = xQueue.parseLikesPayload(buildMockLikesResponse());
  assert.deepEqual(xQueue.extractTweetManifestIds(entries[0]), ['1001', '2002']);
  assert.deepEqual(xQueue.extractTweetManifestIds(entries[1]), []);

  const t1 = entries[0]; // media IDs: 1001, 2002
  const tText = entries[1]; // text only
  const t2 = {
    id: '1003',
    outerTweetId: '1003',
    outerAuthor: 'd',
    mediaDetails: [{owner: 'outer', ownerTweetId: '1003', ownerAuthor: 'd', type: 'photo', directUrl: 'https://pbs.twimg.com/media/x?format=jpg&name=orig'}],
  };
  const t3 = {
    id: '1004',
    outerTweetId: '1004',
    outerAuthor: 'e',
    mediaDetails: [{owner: 'outer', ownerTweetId: '1004', ownerAuthor: 'e', type: 'photo', directUrl: 'https://pbs.twimg.com/media/y?format=jpg&name=orig'}],
  };

  // Quote missing -> t1 not settled even if outer 1001 is archived; tText does not reset consecutiveSettled
  const res1 = xQueue.evaluateReconcileStop(
    [t1, t2, tText, t3],
    {'1001': 'archived', '2002': 'missing', '1003': 'archived', '1004': 'pending'},
    2
  );
  assert.equal(res1.toPush.map((t) => t.id).join(','), '1001');
  assert.equal(res1.shouldStop, true);
  assert.equal(res1.consecutiveSettled, 2);
  assert.equal(res1.stopAtTweetId, '1004');
  assert.equal(res1.textOnly, 1);

  // stopThreshold=0 disables early stop
  const resFull = xQueue.evaluateReconcileStop(
    [t2, t3],
    {'1003': 'archived', '1004': 'archived'},
    0
  );
  assert.equal(resFull.shouldStop, false);
  assert.equal(resFull.toPush.length, 0);

  // Cache trimming keeps recent window, protected IDs, and never evicts unevaluated arrivals
  const order = ['1', '2', '3', '4', '5'];
  const map = new Map(order.map((id) => [id, {id}]));
  const trimmed = xQueue.trimProcessedCache(order, map, 2, new Set(['2']));
  assert.equal(trimmed.removedCount, 2);
  assert.deepEqual(order, ['2', '4', '5']);
  assert.equal(map.has('1'), false);
  assert.equal(map.has('2'), true);
  assert.equal(map.has('3'), false);
  assert.equal(map.has('5'), true);

  const streamOrder = Array.from({length: 151}, (_, i) => String(i + 1));
  const streamMap = new Map(streamOrder.map((id) => [id, {id}]));
  const evaluated = new Set(streamOrder.slice(0, 150));
  // Move #151 earlier in order or test full 151 where only 1..150 are eligible (simulate #151 arriving anywhere in window)
  const mixedOrder = ['151', ...streamOrder.slice(0, 150)];
  const streamRes = xQueue.trimProcessedCache(mixedOrder, streamMap, 120, new Set(), evaluated);
  assert.equal(streamRes.removedCount, 30);
  assert.equal(streamMap.has('151'), true);
  assert.equal(mixedOrder.includes('151'), true);
});
