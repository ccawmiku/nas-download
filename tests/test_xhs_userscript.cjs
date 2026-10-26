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
