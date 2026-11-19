const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');

const script = fs.readFileSync(path.join(__dirname, '../tools/userscripts/xhs-docker-queue.user.js'), 'utf8');
const ids = ['aaaaaaaaaaaaaaaaaaaaaaaa', 'bbbbbbbbbbbbbbbbbbbbbbbb', 'cccccccccccccccccccccccc'];

(async () => {
  const browser = await chromium.launch({
    headless: true,
    ...(process.env.PLAYWRIGHT_EXECUTABLE_PATH ? {executablePath: process.env.PLAYWRIGHT_EXECUTABLE_PATH} : {}),
  });
  try {
    const page = await browser.newPage();
    await page.route('https://www.xiaohongshu.com/**', route => route.fulfill({
      contentType: 'text/html; charset=utf-8',
      body: '<div class="reds-tab-item">笔记</div><div class="reds-tab-item">收藏</div><div class="reds-tab-item">点赞</div>',
    }));
    await page.addInitScript(({script, ids}) => {
      window.GM_getValue = (key, fallback) => JSON.parse(localStorage.getItem(key) || 'null') ?? fallback;
      window.GM_setValue = (key, value) => localStorage.setItem(key, JSON.stringify(value));
      window.GM_registerMenuCommand = () => {};
      window.unsafeWindow = window;
      window.sent = [];
      window.checked = [];
      window.states = {[ids[0]]: 'archived', [ids[1]]: 'missing', [ids[2]]: 'pending'};
      window.GM_xmlhttpRequest = options => {
        const endpoint = new URL(options.url).pathname;
        const payload = JSON.parse(options.data || '{}');
        let response;
        if (endpoint === '/api/xhs/reconcile') {
          checked.push(payload.ids);
          response = {states: Object.fromEntries(payload.ids.map(id => [id, states[id]]))};
        } else if (endpoint === '/api/xhs/links') {
          sent.push(payload);
          const urls = payload.urls || [];
          for (const url of urls) states[url.split('/explore/')[1].split('?')[0]] = 'pending';
          response = {ok: true, valid: urls.length, accepted: urls.length, skipped: 0, task_id: 'test-task'};
        } else {
          response = {ok: true};
        }
        options.onload({status: 200, responseText: JSON.stringify(response)});
      };
      const notes = ids.map((id, index) => ({id, xsecToken: 'signature-' + index, noteCard: {displayTitle: '作品 ' + index}}));
      window.__INITIAL_STATE__ = {user: {notes: {_rawValue: [notes, notes, notes]}}};
      document.addEventListener('DOMContentLoaded', () => {
        const element = document.createElement('script');
        element.textContent = script;
        document.body.append(element);
      });
    }, {script, ids});
    await page.goto('https://www.xiaohongshu.com/user/profile/alice');
    const ui = page.locator('#nas-xhs-workbench');
    await ui.locator('.launch').click();
    await ui.locator('#scan').click();
    await ui.locator('#count').getByText('已加载 3 · 待下载 1').waitFor();
    assert.equal(await ui.getByText('设为截止').count(), 0);
    await ui.locator('#send-batch').click();
    await ui.locator('#status').getByText(/新入队 1 个/).waitFor();
    const [sent] = await page.evaluate(() => window.sent);
    assert.equal(sent.category, 'liked');
    assert.deepEqual(sent.urls, [`https://www.xiaohongshu.com/explore/${ids[1]}?xsec_token=signature-1&xsec_source=pc_user`]);
    await ui.locator('#send-batch').click();
    await ui.locator('#status').getByText(/新入队 0 个/).waitFor();
    assert.equal(await page.evaluate(() => sent.length), 1);
    await page.goto('https://www.xiaohongshu.com/user/profile/alice?tab=saved#nas_bg_run=test-sync&nas_bg_mode=saved');
    await page.waitForFunction(() => localStorage.getItem('nas_prod_xhs_bg_result:test-sync') !== null ||
      localStorage.getItem('nas_prod_xhs_bg_result')?.includes('test-sync'), null, {timeout: 32000});
    const background = await page.evaluate(() => window.sent);
    assert.equal(background[0].category, 'saved');
    assert.equal(background[0].run_id, 'test-sync');
    assert.ok(background[0].urls[0].includes('signature-1'));
    console.log('PASS: manual and daily background sync use NAS work IDs and keep the signed links');
  } finally {
    await browser.close();
  }
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
