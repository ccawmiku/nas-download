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
