const {test} = require('node:test');
const assert = require('node:assert/strict');
const {noteId, accountId, endpoint, selectMissing} = require('../tools/userscripts/xhs-docker-queue.user.js');
const id = '1234567890abcdef12345678';
test('only missing works are selected regardless of order and existing catalog notes', () => {
  const other = 'aaaaaaaaaaaaaaaaaaaaaaaa';
  const third = 'bbbbbbbbbbbbbbbbbbbbbbbb';
  const items = [{id: other}, {id}, {id: third}];
  assert.deepEqual(selectMissing(items, {[id]: 'missing', [other]: 'archived', [third]: 'pending'}), [{id}]);
  assert.deepEqual(selectMissing(items.reverse(), {[id]: 'deferred', [other]: 'missing', [third]: 'archived'}), [{id: other}]);
});
test('extract stable work ID from signed URL', () => {
  assert.equal(noteId(`https://www.xiaohongshu.com/explore/${id}?xsec_token=hello`), id);
  assert.equal(noteId(id), id);
  assert.equal(noteId('数学界最美味的一天'), '');
  assert.equal(accountId('/user/profile/alice?tab=liked'), 'alice');
});
test('legacy Docker host, port and path continue working', () => {
  assert.equal(endpoint('192.168.1.20', '14001', '/api/xhs/links'), 'http://192.168.1.20:14001/api/xhs/links');
  assert.equal(endpoint('https://nas.example:4430', '14001', '/custom'), 'https://nas.example:4430/custom');
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

test('xhs daily background scheduler finishes partial modes and reruns all modes on manual click', () => {
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
  // Manual "run once" when today's modes are already complete executes all configured modes again
  d = xhsBg.shouldRunDailyBg({config: cfg, dailyState: state, nowMs: day1 + 4000});
  assert.equal(d.run, true);
  assert.deepEqual(d.pendingModes, ['liked', 'saved']);

  const nextDay = xhsBg.shouldRunDailyBg({config: cfg, dailyState: state, nowMs: day2});
  assert.equal(nextDay.run, true);
  assert.deepEqual(nextDay.pendingModes, ['liked', 'saved']);

  // 3 failures stop scheduling automatic CATRetryError retries for today
  let failState = {};
  assert.equal(xhsBg.canScheduleRetry(failState, day1), true);
  failState = xhsBg.markDailyFailure(failState, day1, 'err1');
  failState = xhsBg.markDailyFailure(failState, day1 + 30 * 60000, 'err2');
  assert.equal(xhsBg.canScheduleRetry(failState, day1 + 45 * 60000), true);
  failState = xhsBg.markDailyFailure(failState, day1 + 60 * 60000, 'err3');
  assert.equal(xhsBg.canScheduleRetry(failState, day1 + 120 * 60000), false);
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

const {extractXhsApiNotes, evaluateXhsEarlyStop, extractXhsNoteMedia, pickFailedRefreshIds} = require('../tools/userscripts/xhs-docker-queue.user.js');

test('extracts XHS API page notes and evaluates consecutive archived early-stop', () => {
  const apiNotes = extractXhsApiNotes({
    data: {
      notes: [
        {
          note_id: id,
          xsec_token: 'tokA',
          display_title: '笔记标题',
          user: {nickname: '作者'},
          cover: {url_default: 'https://sns-img.example/1.jpg'},
        },
        {note_id: 'invalid'},
      ],
    },
  });
  assert.equal(apiNotes.length, 1);
  assert.equal(apiNotes[0].id, id);
  assert.equal(apiNotes[0].url, `https://www.xiaohongshu.com/explore/${id}?xsec_token=tokA&xsec_source=pc_user`);

  const ids = ['a1', 'a2', 'a3', 'a4', 'a5'];
  const states = {a1: 'archived', a2: 'missing', a3: 'archived', a4: 'pending', a5: 'deferred'};
  const hit = evaluateXhsEarlyStop(ids, states, 3);
  assert.equal(hit.shouldStop, true);
  assert.equal(hit.consecutiveSettled, 3);
  assert.equal(hit.stopAtId, 'a5');

  const noHit = evaluateXhsEarlyStop(ids, states, 4);
  assert.equal(noHit.shouldStop, false);
  assert.equal(noHit.consecutiveSettled, 3);

  const full = evaluateXhsEarlyStop(ids, states, 0);
  assert.equal(full.shouldStop, false);

  // 50 missing items reconciled as missing must not trigger early-stop at threshold 30
  const fiftyIds = Array.from({length: 50}, (_, i) => `${i + 1}`.padStart(24, '0'));
  const missingStates = Object.fromEntries(fiftyIds.map(nid => [nid, 'missing']));
  const batchCheck = evaluateXhsEarlyStop(fiftyIds, missingStates, 30);
  assert.equal(batchCheck.shouldStop, false);
  assert.equal(batchCheck.consecutiveSettled, 0);
});

test('xhs userscript metadata uses document-start and matches both www and apex xiaohongshu domains', () => {
  const fs = require('node:fs');
  const path = require('node:path');
  const src = fs.readFileSync(
    path.join(__dirname, '../tools/userscripts/xhs-docker-queue.user.js'),
    'utf8'
  );
  assert.match(src, /@run-at\s+document-start/);
  assert.match(src, /@match\s+https:\/\/www\.xiaohongshu\.com\/\*/);
  assert.match(src, /@match\s+https:\/\/xiaohongshu\.com\/\*/);
  assert.match(src, /ensureMounted/);
});

test('extractXhsNoteMedia parses images/videos and pickFailedRefreshIds detects refreshed xsec_token URLs', () => {
  const imgNote = extractXhsNoteMedia(
    {
      note_id: id,
      title: '多图详情',
      user: {nickname: '小红薯'},
      image_list: [
        {
          info_list: [
            {image_scene: 'WB_PRV', url: 'http://sns-webpic-qc.xhscdn.com/prv.jpg'},
            {image_scene: 'WB_DFT', url: 'http://sns-webpic-qc.xhscdn.com/dft.jpg'},
          ],
        },
        {url_default: 'https://sns-webpic-qc.xhscdn.com/second.jpg'},
      ],
    },
    `https://www.xiaohongshu.com/explore/${id}?xsec_token=tok123`
  );
  assert.equal(imgNote.note_id, id);
  assert.equal(imgNote.title, '多图详情');
  assert.equal(imgNote.author, '小红薯');
  assert.deepEqual(imgNote.media, [
    {type: 'image', url: 'https://sns-webpic-qc.xhscdn.com/dft.jpg'},
    {type: 'image', url: 'https://sns-webpic-qc.xhscdn.com/second.jpg'},
  ]);

  const vidId = 'abcdefabcdefabcdefabcdef';
  const vidNote = extractXhsNoteMedia({
    noteId: vidId,
    type: 'video',
    displayTitle: '高清视频',
    video: {
      media: {
        stream: {
          h264: [
            {masterUrl: 'http://sns-video-bd.xhscdn.com/low.mp4', videoBitrate: 500},
            {master_url: 'http://sns-video-bd.xhscdn.com/high.mp4', video_bitrate: 1800},
          ],
        },
      },
    },
  });
  assert.equal(vidNote.note_id, vidId);
  assert.deepEqual(vidNote.media, [{type: 'video', url: 'https://sns-video-bd.xhscdn.com/high.mp4'}]);

  const staleUrl = `https://www.xiaohongshu.com/explore/${id}?xsec_token=old`;
  const refreshedUrl = `https://www.xiaohongshu.com/explore/${id}?xsec_token=new`;
  const failedMap = {
    [id]: {source_id: id, url: staleUrl},
    [vidId]: {source_id: vidId, url: `https://www.xiaohongshu.com/explore/${vidId}?xsec_token=same`},
  };
  const picked = pickFailedRefreshIds(
    [
      {id, url: refreshedUrl},
      {id: vidId, url: `https://www.xiaohongshu.com/explore/${vidId}?xsec_token=same`},
      {id: 'cccccccccccccccccccccccc', url: 'https://www.xiaohongshu.com/explore/cccccccccccccccccccccccc?xsec_token=x'},
    ],
    failedMap
  );
  assert.deepEqual(picked, [id]);
});

