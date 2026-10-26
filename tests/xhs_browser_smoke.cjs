const {chromium}=require('playwright');
const fs=require('fs');const assert=require('node:assert/strict');
const script=fs.readFileSync(require('path').join(__dirname,'../tools/userscripts/xhs-docker-queue.user.js'),'utf8');
(async()=>{
const browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_EXECUTABLE_PATH ? {executablePath:process.env.PLAYWRIGHT_EXECUTABLE_PATH} : {})});
const page=await browser.newPage({viewport:{width:1280,height:1000}});
await page.route('https://www.xiaohongshu.com/**',route=>route.fulfill({contentType:'text/html; charset=utf-8',body:'<html><body><div class="reds-tab-item">笔记</div><div class="reds-tab-item">收藏</div><div class="reds-tab-item">点赞</div></body></html>'}));
const ids=['aaaaaaaaaaaaaaaaaaaaaaaa','bbbbbbbbbbbbbbbbbbbbbbbb','cccccccccccccccccccccccc'];
await page.addInitScript(({script,ids})=>{
window.GM_getValue=(key, fallback)=>JSON.parse(localStorage.getItem(key)||'null')??fallback;
window.GM_setValue=(key,value)=>localStorage.setItem(key,JSON.stringify(value));
window.GM_registerMenuCommand=()=>{};window.unsafeWindow=window;
window.sent=[];window.GM_xmlhttpRequest=options=>{
  const payload=JSON.parse(options.data);sent.push(payload);
  const fail=window.failNext;window.failNext=false;
  const respond=()=>{
    if(window.stopAfterRequest){window.stopAfterRequest=false;document.querySelector('#nas-xhs-workbench').shadowRoot.querySelector('#stop').click();}
    options.onload({status:fail?500:200,responseText:JSON.stringify({ok:!fail,valid:payload.urls.length,accepted:payload.urls.length,skipped:0})});
  };
  if(window.holdNext){window.holdNext=false;window.release=respond;}else respond();
};
const notes=ids.map((id,i)=>({id,xsecToken:'signature-'+i,noteCard:{displayTitle:['今天的收藏','想留下的一刻','周末小记'][i],user:{nickName:'旅行日记'}}}));
window.__INITIAL_STATE__={user:{notes:{_rawValue:[notes,notes,notes]}}};
document.addEventListener('DOMContentLoaded',()=>{const el=document.createElement('script');el.textContent=script;document.body.append(el);});
},{script,ids});
await page.goto('https://www.xiaohongshu.com/user/profile/alice');
const ui=page.locator('#nas-xhs-workbench');
await ui.locator('.launch').click();
await ui.locator('#scan').click();
await ui.locator('.note').nth(1).getByRole('button',{name:'设为截止'}).click();
assert.match(await ui.locator('#count').textContent(),/范围内 1/);
await ui.locator('#send-batch').click();
let sent=await page.evaluate(()=>sent);assert.equal(sent[0].destination,'batch');assert.equal(sent[0].urls.length,1);assert.ok(sent[0].urls[0].includes('signature-0'));
await page.reload();await ui.locator('.launch').click();assert.ok((await ui.locator('#cutoff').textContent()).includes(ids[1]));
await ui.locator('#scan').click();await ui.locator('.note').nth(2).getByRole('button',{name:'单独发送'}).click();
sent=await page.evaluate(()=>sent);assert.equal(sent[0].destination,'single');assert.ok(sent[0].urls[0].includes(ids[2]));
assert.deepEqual(await page.evaluate(()=>JSON.parse(localStorage.getItem('nas:checkpoint:v1:alice:liked'))).then(rows=>rows.map(row=>row.id)),[ids[0]]);
await ui.locator('[data-mode="saved"]').click();assert.equal(await ui.locator('#auto-notes li').count(),0);assert.match(await ui.locator('#cutoff').textContent(),/未设置/);
await ui.locator('#scan').click();await ui.locator('#cutoff-input').fill('dddddddddddddddddddddddd');await ui.locator('#save-cutoff').click();
await ui.locator('#send-batch').click();assert.match(await ui.locator('#status').textContent(),/未找到截止作品/);assert.equal(await page.evaluate(()=>sent.length),1);
await ui.locator('[data-mode="liked"]').click();assert.ok((await ui.locator('#cutoff').textContent()).includes(ids[1]));
await ui.locator('#scan').click();
if(process.env.XHS_SCREENSHOT) await page.screenshot({path:process.env.XHS_SCREENSHOT});
await page.goto('https://www.xiaohongshu.com/user/profile/bob');await ui.locator('.launch').click();assert.match(await ui.locator('#cutoff').textContent(),/未设置/);
// Fresh account: save five anchors, survive deletions, reject reordered anchors,
// preserve markers on failed submission and isolate an in-flight account switch.
await page.goto('https://www.xiaohongshu.com/user/profile/checkpoints');
await ui.locator('.launch').click();
assert.ok((await ui.locator('.launch').boundingBox()).x < 40);
assert.ok((await ui.locator('.panel').boundingBox()).x < 40);
const autoKey='nas:checkpoint:v1:checkpoints:liked';
const makeId=n=>n.toString(16).padStart(24,'0');
async function setNotes(numbers) {
  await page.evaluate(ids=>{
    window.__INITIAL_STATE__.user.notes._rawValue[2]=ids.map(id=>({id,xsecToken:'token',noteCard:{displayTitle:id,user:{nickName:'test'}}}));
    window.scrolls=0;document.scrollingElement.scrollTo=()=>window.scrolls++;
  },numbers.map(makeId));
}
async function scan(button='#scan') {
  await ui.locator(button).click();
  await page.waitForFunction(()=>!document.querySelector('#nas-xhs-workbench').shadowRoot.querySelector('#scan').disabled);
}
async function stored(key=autoKey) { return page.evaluate(key=>JSON.parse(localStorage.getItem(key)||'[]'),key); }
await setNotes([1,2,3,4,5,6,7]);await scan();await ui.locator('#send-batch').click();
assert.deepEqual((await stored()).map(n=>n.id),[1,2,3,4,5].map(makeId));
await page.reload();await ui.locator('.launch').click();
assert.equal(await ui.locator('#auto-notes li').count(),5);
await setNotes([8,9,2,5,7]);await scan('#load');
assert.equal(await page.evaluate(()=>window.scrolls),0);
assert.match(await ui.locator('#count').textContent(),/范围内 2/);
await ui.locator('#send-batch').click();
assert.deepEqual((await stored()).map(n=>n.id),[8,9,1,2,3].map(makeId));
const before=await stored();
await setNotes([10,9,8,1,2]);await scan();
assert.match(await ui.locator('#auto-summary').textContent(),/顺序不一致/);
const sentBefore=await page.evaluate(()=>sent.length);
await ui.locator('#send-batch').click();
assert.equal(await page.evaluate(()=>sent.length),sentBefore);
await setNotes([10,8,9]);await scan();await page.evaluate(()=>window.failNext=true);
await ui.locator('#send-batch').click();assert.deepEqual(await stored(),before);
await setNotes([...Array.from({length:501},(_,i)=>1000+i),8,9]);await scan();
const partialBefore=await page.evaluate(()=>sent.length);
await page.evaluate(()=>window.stopAfterRequest=true);await ui.locator('#send-batch').click();
assert.equal(await page.evaluate(()=>sent.length),partialBefore+1);
assert.deepEqual(await stored(),before);
assert.match(await ui.locator('#status').textContent(),/500\/501/);
await setNotes([10,8,9]);await scan();
await page.evaluate(()=>window.holdNext=true);await ui.locator('#send-batch').click();
await page.evaluate(()=>{history.pushState({},'','/user/profile/other-account');window.release();});
await page.waitForFunction(()=>!document.querySelector('#nas-xhs-workbench').shadowRoot.querySelector('#scan').disabled);
assert.deepEqual((await stored()).map(n=>n.id),[10,8,9,1,2].map(makeId));
assert.deepEqual(await stored('nas:checkpoint:v1:other-account:liked'),[]);
await browser.close();console.log('PASS: five-note persistence, two ordered survivors, automatic loading stop, reversed order rejected, failed send retention, in-flight account isolation, left placement; exclusive cutoff, signature preservation, reload persistence, account/category isolation, fail-closed cutoff, single routing');
})().catch(e=>{console.error(e);process.exit(1)});
