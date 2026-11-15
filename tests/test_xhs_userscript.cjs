const {test} = require('node:test');
const assert = require('node:assert/strict');
const {range, noteId, accountId, endpoint} = require('../tools/userscripts/xhs-docker-queue.user.js');
const id = '1234567890abcdef12345678';
test('cutoff is excluded, including when first, and missing cutoff fails closed', () => {
  const notes = [{id: 'a'}, {id: 'b'}, {id: 'c'}];
  assert.deepEqual(range(notes, 'b'), [{id: 'a'}]);
  assert.deepEqual(range(notes, 'a'), []);
  assert.deepEqual(range(notes, ''), notes);
  assert.throws(() => range(notes, 'missing'), /未找到截止作品/);
});
test('extract ID without using title or signature as cutoff', () => {
  assert.equal(noteId(`https://www.xiaohongshu.com/explore/${id}?xsec_token=hello`), id);
  assert.equal(noteId(id), id);
  assert.equal(noteId('数学界最美味的一天'), '');
  assert.equal(accountId('/user/profile/alice?tab=liked'), 'alice');
});
test('legacy Docker host/port/path continue working', () => {
  assert.equal(endpoint('192.168.1.20', '14001', '/api/xhs/links'), 'http://192.168.1.20:14001/api/xhs/links');
  assert.equal(endpoint('https://nas.example:4430', '14001', '/custom'), 'https://nas.example:4430/custom');
});

const {checkpointMatch, batchRange, nextCheckpoint} = require('../tools/userscripts/xhs-docker-queue.user.js');
const notes = (...ids) => ids.map(id => ({id, title: id}));
test('five-note checkpoint tolerates deletions and stops before first ordered survivor', () => {
  const previous = notes('a', 'b', 'c', 'd', 'e');
  assert.deepEqual(batchRange(notes('new1', 'new2', 'a', 'b', 'c', 'd', 'e'), '', previous), notes('new1', 'new2'));
  assert.deepEqual(batchRange(notes('new', 'b', 'e', 'older'), '', previous), notes('new'));
  assert.deepEqual(batchRange(notes('b', 'e'), '', previous), []);
  assert.equal(checkpointMatch(notes('new', 'b', 'e'), previous).count, 2);
});
test('one survivor or reversed order cannot trigger automatic cutoff', () => {
  const previous = notes('a', 'b', 'c', 'd', 'e');
  for (const loaded of [notes('new', 'e'), notes('e', 'b'), notes('b', 'a', 'e'), notes('new')]) {
    assert.equal(checkpointMatch(loaded, previous).matched, false);
    assert.throws(() => batchRange(loaded, '', previous), /自动截止尚未匹配/);
  }
});
test('manual and automatic cutoffs use whichever is earlier', () => {
  const previous = notes('a', 'b', 'c', 'd', 'e');
  assert.deepEqual(batchRange(notes('new', 'manual', 'b', 'e'), 'manual', previous), notes('new'));
  assert.deepEqual(batchRange(notes('new', 'b', 'e', 'manual'), 'manual', previous), notes('new'));
  assert.deepEqual(batchRange(notes('new', 'b', 'e'), 'deleted-manual', previous), notes('new'));
  assert.deepEqual(batchRange(notes('new', 'manual'), 'manual', previous), notes('new'));
});
test('successful small batches retain older confirmed anchors and use unique IDs', () => {
  const previous = notes('a', 'b', 'c', 'd', 'e');
  assert.deepEqual(nextCheckpoint(notes('new'), previous), notes('new', 'a', 'b', 'c', 'd'));
  assert.deepEqual(nextCheckpoint(notes('a', 'a', 'b'), previous), previous);
  assert.deepEqual(nextCheckpoint(notes('1', '2', '3', '4', '5', '6'), previous), notes('1', '2', '3', '4', '5'));
  assert.deepEqual(nextCheckpoint(notes('first')), notes('first'));
  assert.deepEqual(batchRange(notes('next', 'first'), '', notes('first')), notes('next'));
});

const {redactXhsUrl, parseBgRunHash} = require('../tools/userscripts/xhs-docker-queue.user.js');
const xhsBg = require('../tools/userscripts/xhs-nas-bg.user.js');

test('noteId supports /user/profile/<uid>/<id> and redaction hides xsec_token', () => {
  assert.equal(noteId(`https://www.xiaohongshu.com/user/profile/617973440000000002027e19/${id}?xsec_token=sec`), id);
  const redacted = redactXhsUrl(`https://www.xiaohongshu.com/explore/${id}?xsec_token=secret_value_12345&xsec_source=pc_user`);
  assert.match(redacted, /xsec_token=<已隐藏:长度18>/);
  assert.equal(redacted.includes('secret_value_12345'), false);
  assert.deepEqual(parseBgRunHash('#nas_bg_run=r_123&nas_bg_mode=saved'), {runId: 'r_123', mode: 'saved'});
});

test('xhs daily background lock tracks liked and saved sequentially per calendar day', () => {
  const day1 = new Date(2026, 10, 15, 9, 0, 0).getTime();
  const day2 = new Date(2026, 10, 16, 9, 0, 0).getTime();
  const cfg = {dailyBgEnabled: true, bgProfileUrl: 'https://www.xiaohongshu.com/user/profile/u1', bgModes: ['liked', 'saved']};

  let state = {};
  let d = xhsBg.shouldRunDailyBg({config: cfg, dailyState: state, nowMs: day1});
  assert.equal(d.run, true);
  assert.deepEqual(d.pendingModes, ['liked', 'saved']);

  state = xhsBg.markModeSuccess(state, 'liked', d.configuredModes, day1 + 1000, 'liked:ok');
  assert.equal(state.lastSuccessDate, '');
  d = xhsBg.shouldRunDailyBg({config: cfg, dailyState: state, nowMs: day1 + 2000});
  assert.equal(d.run, true);
  assert.deepEqual(d.pendingModes, ['saved']);

  state = xhsBg.markModeSuccess(state, 'saved', d.configuredModes, day1 + 3000, 'saved:ok');
  assert.equal(state.lastSuccessDate, xhsBg.localDateKey(day1));
  d = xhsBg.shouldRunDailyBg({config: cfg, dailyState: state, nowMs: day1 + 4000});
  assert.equal(d.run, false);
  assert.equal(d.reason, 'already_succeeded_today');

  // Next calendar day unlocks automatically!
  const nextDay = xhsBg.shouldRunDailyBg({config: cfg, dailyState: state, nowMs: day2});
  assert.equal(nextDay.run, true);
  assert.deepEqual(nextDay.pendingModes, ['liked', 'saved']);

  // 3 failures pause for today with cooldown
  let failState = {};
  failState = xhsBg.markDailyFailure(failState, day1, 'err1');
  assert.equal(xhsBg.shouldRunDailyBg({config: cfg, dailyState: failState, nowMs: day1 + 1000}).reason, 'cooldown_active');
  failState = xhsBg.markDailyFailure(failState, day1 + 30 * 60000, 'err2');
  failState = xhsBg.markDailyFailure(failState, day1 + 60 * 60000, 'err3');
  assert.equal(xhsBg.shouldRunDailyBg({config: cfg, dailyState: failState, nowMs: day1 + 120 * 60000}).reason, 'daily_fail_limit_reached');
});

const {REPORT_PLATFORM, extractErrorMessage} = require('../tools/userscripts/xhs-docker-queue.user.js');

test('xhs report uses canonical xhs platform and surfaces HTTP 422 detail', () => {
  assert.equal(REPORT_PLATFORM, 'xhs');
  assert.equal(
    extractErrorMessage(422, '{"detail":"平台上报参数无效"}'),
    'Docker HTTP 422：平台上报参数无效'
  );
  assert.equal(
    extractErrorMessage(422, ''),
    'Docker HTTP 422，请检查地址、授权和服务版本。'
  );
});

