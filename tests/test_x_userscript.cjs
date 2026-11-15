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

test('x daily scheduler runs once per day and resets on next day', () => {
  const day1 = new Date(2026, 10, 15, 8, 30, 0).getTime();
  const day2 = new Date(2026, 10, 16, 8, 30, 0).getTime();
  const cfg = {dailyBgEnabled: true, bgTargetUrl: 'https://x.com/i/history/likes'};

  let d = xBg.shouldRunDailyBg({config: cfg, dailyState: {}, nowMs: day1});
  assert.equal(d.run, true);

  const succeeded = xBg.markDailySuccess({}, day1 + 5000, 'pushed (+3)');
  assert.equal(xBg.shouldRunDailyBg({config: cfg, dailyState: succeeded, nowMs: day1 + 3600000}).reason, 'already_succeeded_today');
  assert.equal(xBg.shouldRunDailyBg({config: cfg, dailyState: succeeded, nowMs: day2}).run, true);
  assert.equal(xBg.shouldRunDailyBg({config: cfg, dailyState: succeeded, nowMs: day1 + 3600000, ignoreLock: true}).run, true);
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
});
