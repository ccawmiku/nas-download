// ==UserScript==
// @name         小红书 · NAS 下载工作台
// @namespace    xhs_downloader
// @version      3.0.0
// @description  点赞/收藏截止作品持久保存，批量入队与单个作品独立归档
// @match        https://www.xiaohongshu.com/*
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_xmlhttpRequest
// @grant        GM_registerMenuCommand
// @grant        unsafeWindow
// @connect      *
// @license      GPL-3.0
// @run-at       document-end
// ==/UserScript==

// Rebuilt for ccawmiku/nas-download; source project: JoeanAmier/XHS-Downloader (GPL-3.0).
(() => {
    'use strict';
    const unwrap = value => value?._rawValue ?? value?.value ?? value;
    const noteId = value => String(value || '').match(/(?:\/explore\/|\/discovery\/item\/|^)([a-f\d]{24})(?:[/?#]|$)/i)?.[1] || '';
    const accountId = path => path.match(/\/user\/profile\/([^/?#]+)/)?.[1] || '';
    const range = (items, cutoff) => {
        if (!cutoff) return items;
        const index = items.findIndex(item => item.id === cutoff);
        if (index < 0) throw new Error('未找到截止作品。请继续加载、重新选择或清除截止点，本次没有发送。');
        return items.slice(0, index);
    };
    const endpoint = (host, port, path) => {
        const url = new URL(/^https?:\/\//i.test(host) ? host : `http://${host}`);
        if (port && !url.port) url.port = port;
        url.pathname = path.startsWith('/') ? path : `/${path}`;
        url.search = ''; url.hash = '';
        return url.href;
    };
    // Pure helpers are also exercised by the Node regression tests.
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = {noteId, accountId, range, endpoint}; return;
    }

    const modes = {liked: '点赞', saved: '收藏', published: '发布', page: '当前页面'};
    const indices = {published: 0, saved: 1, liked: 2};
    let mode = 'liked', items = [], busy = false, stopped = false, context = '';
    const config = {
        host: GM_getValue('dockerHost', '192.168.1.20'),
        port: GM_getValue('dockerPort', '14001'),
        path: GM_getValue('dockerPath', '/api/xhs/links'),
        token: GM_getValue('nasSubmitToken', ''),
        max: GM_getValue('nasMaxScrolls', 100),
    };
    const key = () => `nas:cutoff:v1:${accountId(location.pathname)}:${mode}`;
    const cutoff = () => GM_getValue(key(), null);
    const remember = item => { GM_setValue(key(), item ? {id: item.id, title: item.title} : null); render(); };
    const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
    const host = document.createElement('div');
    host.id = 'nas-xhs-workbench';
    const shadow = host.attachShadow({mode: 'open'});
    shadow.innerHTML = `<style>
:host{all:initial;font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;color:#202736}
*{box-sizing:border-box}button,input,select{font:inherit}button{cursor:pointer;border:1px solid #e2e6ee;background:#fff;color:#384153;border-radius:10px;padding:9px 12px;transition:background .15s}button:hover{background:#f1f4f9}button:disabled{opacity:.45;cursor:default}button.primary{background:#e94465;color:white;border-color:#e94465}button.primary:hover{background:#d63757}button.quiet{border:0;background:transparent}input,select{border:1px solid #dce1e9;border-radius:9px;padding:9px;background:white;color:#273045;min-width:0}input:focus,button:focus-visible,select:focus{outline:2px solid #eb718b;outline-offset:2px}
.launch{position:fixed;right:24px;bottom:28px;z-index:2147483646;background:#e94465;color:white;border:0;box-shadow:0 6px 24px #63263730;border-radius:18px;padding:13px 18px;font-weight:650}
.panel{position:fixed;right:24px;bottom:88px;width:440px;max-width:calc(100vw - 24px);max-height:calc(100vh - 112px);z-index:2147483646;border:1px solid #e3e7ee;border-radius:22px;background:#f8fafc;box-shadow:0 20px 80px #1c294630;overflow:auto}.hidden{display:none!important}header{padding:21px 22px 16px;display:flex;align-items:center;justify-content:space-between;background:#fff}h2{font-size:19px;letter-spacing:-.5px;margin:2px 0}small,.muted{color:#7b8494;font-size:12px}.brand{font-size:10px;letter-spacing:2px;color:#e94465;font-weight:750}.body{padding:16px;display:grid;gap:14px}.box{background:white;border:1px solid #e7ebf1;border-radius:15px;padding:14px}.row{display:flex;gap:8px;align-items:center}.spread{justify-content:space-between}.grow{flex:1;min-width:0}.tabs{display:flex;gap:5px}.tabs button{flex:1;padding:8px 4px}.active{background:#fdebf0!important;color:#cf3655!important;border-color:#f8ccd6!important}h3{font-size:14px;margin:0 0 9px}p{margin:6px 0 12px}label{display:grid;gap:5px;margin:8px 0;font-size:12px;color:#636d7c}.cut{background:#fff4f6;border:1px solid #f9dde4;border-radius:10px;padding:10px;margin:10px 0;overflow-wrap:anywhere}.list{max-height:310px;overflow:auto;margin-top:10px}.note{display:flex;gap:10px;padding:10px 0;border-top:1px solid #edf0f5;align-items:center}.cover{width:48px;height:60px;border-radius:8px;object-fit:cover;background:#eff2f6}.title{font-size:13px;overflow-wrap:anywhere}.note button{font-size:11px;padding:5px 7px}.actions{display:flex;gap:5px;margin-top:6px}.tag{font-size:10px;color:#df4564}#status{white-space:pre-wrap;overflow-wrap:anywhere;padding:11px 13px;border-radius:10px;background:#edf2f8;font-size:12px;color:#546275}#status.error{color:#b72b43;background:#fff0f2}details summary{cursor:pointer;color:#677284;font-size:12px}progress{width:100%;height:5px;accent-color:#e94465}a{color:#ce3859;text-decoration:none}@media(max-width:500px){.panel{right:12px;bottom:80px}.launch{right:12px;bottom:18px}}
</style>
<button class="launch">↓ NAS 工作台</button>
<section class="panel hidden" aria-label="NAS 下载工作台">
<header><div><div class="brand">REDNOTE / NAS</div><h2>把喜欢的，收进 NAS</h2><small>浏览器选作品 · Docker 继续下载</small></div><button class="quiet" id="close" aria-label="关闭">✕</button></header>
<div class="body">
<div class="box"><h3>单个作品</h3><p class="muted">发送正在浏览的作品，或粘贴完整作品链接。独立保存到 single 目录。</p><div class="row"><input id="single" class="grow" placeholder="作品链接（保留 xsec_token）" aria-label="单个作品链接"><button id="send-single" class="primary">发送</button></div><button id="current" class="quiet">↗ 使用当前作品</button></div>
<div class="box"><div class="row spread"><h3>批量收录</h3><small id="account"></small></div><div class="tabs">${Object.entries(modes).map(([value, label]) => `<button data-mode="${value}">${label}</button>`).join('')}</div>
<div class="cut"><div class="row spread"><strong style="font-size:12px">截止作品 · 不包含</strong><button id="clear" class="quiet">清除</button></div><div id="cutoff" class="muted"></div><div class="row"><input id="cutoff-input" class="grow" placeholder="也可粘贴作品链接或 ID" aria-label="截止作品"><button id="save-cutoff">保存</button></div></div>
<div class="row"><button id="scan" class="grow">读取已加载</button><button id="load" class="grow">自动加载</button><button id="stop" class="hidden">停止</button></div>
<p class="muted">在对应列表中加载作品，点击「设为截止」。按账号分别长期保存点赞、收藏的截止点。</p>
<div class="row spread"><small id="count">尚未读取作品</small><button id="send-batch" class="primary">发送范围内作品</button></div><div id="list" class="list"></div></div>
<div id="status" role="status" aria-live="polite">准备就绪。先设置 Docker 地址，再选择作品。</div><progress class="hidden" id="progress"></progress>
<details class="box"><summary>连接与加载设置</summary><label>Docker 主机<input id="host" placeholder="192.168.1.20 或 https://nas.example.com"></label><div class="row"><label class="grow">端口<input id="port"></label><label class="grow">最多滚动次数<input id="max" type="number" min="1" max="1000"></label></div><label>接收路径<input id="path"></label><label>推送授权<input id="token" type="password" autocomplete="off"></label><button id="settings">保存设置</button><p class="muted">授权从 Docker 控制台复制。保存目录在 Docker 小红书设置中配置。</p></details>
</div></section>`;
    document.body.append(host);
    const $ = selector => shadow.querySelector(selector);
    const status = (message, error = false) => { $('#status').textContent = message; $('#status').classList.toggle('error', error); };
    const guard = () => {
        if (context !== `${accountId(location.pathname)}:${mode}:${location.pathname}`) throw new Error('页面或账号已切换，请重新读取作品。');
    };
    const ref = name => unwrap(unsafeWindow.__INITIAL_STATE__?.[name]);
    function normalize(raw) {
        const card = raw.noteCard || raw;
        const id = String(raw.id || raw.noteId || card.noteId || '');
        if (!noteId(id)) return null;
        const token = raw.xsecToken || raw.xsec_token || card.xsecToken || '';
        const url = new URL(`/explore/${id}`, location.origin);
        if (token) url.searchParams.set('xsec_token', token);
        url.searchParams.set('xsec_source', 'pc_user');
        return {id, url: url.href, title: card.displayTitle || card.title || id,
            author: card.user?.nickName || card.user?.nickname || '',
            cover: card.cover?.urlDefault || card.cover?.url || ''};
    }
    function read() {
        let raw;
        if (mode in indices) {
            if (!accountId(location.pathname)) throw new Error('请先打开要收录的小红书账号主页。');
            raw = unwrap(unwrap(ref('user')?.notes)?.[indices[mode]]);
        } else {
            if (/\/user\/profile\//.test(location.pathname)) throw new Error('账号主页请使用点赞、收藏或发布分类。');
            if (location.pathname.includes('board')) {
                const id = location.pathname.split('/board/')[1]?.split('/')[0];
                raw = unwrap(ref('board')?.boardFeedsMap)?.[id]?.notes;
            } else raw = unwrap((location.pathname.includes('search_result') ? ref('search') : ref('feed'))?.feeds);
        }
        if (!Array.isArray(raw)) throw new Error('暂未读取到列表。请登录并打开对应分类，等作品出现后重试。');
        const found = raw.map(normalize).filter(Boolean);
        const unique = [...new Map(found.map(item => [item.id, item])).values()];
        context = `${accountId(location.pathname)}:${mode}:${location.pathname}`;
        return unique;
    }
    async function activate() {
        if (!(mode in indices)) return;
        if (!accountId(location.pathname)) throw new Error('请先打开账号主页。');
        const names = {liked: ['点赞', '赞过', 'Liked', 'Likes'], saved: ['收藏', 'Collected', 'Saved'], published: ['笔记', '发布', 'Notes', 'Posts']};
        const tab = [...document.querySelectorAll('.reds-tab-item, [role="tab"], .tab-item')]
            .find(node => names[mode].includes(node.textContent.trim().replace(/\s*[\d.,万千wWkK+]+\s*$/, '').trim()));
        if (!tab) throw new Error(`未找到「${modes[mode]}」标签，请在账号主页打开该分类后重试。`);
        tab.click(); await sleep(1000);
    }
    function render() {
        $('#account').textContent = accountId(location.pathname) ? `账号 …${accountId(location.pathname).slice(-6)}` : '未在账号主页';
        shadow.querySelectorAll('[data-mode]').forEach(button => button.classList.toggle('active', button.dataset.mode === mode));
        const limited = ['liked', 'saved'].includes(mode);
        $('.cut').classList.toggle('hidden', !limited);
        const mark = cutoff();
        $('#cutoff').textContent = mark ? `${mark.title || mark.id} · ${mark.id}` : '未设置：发送当前加载的全部作品';
        $('#cutoff-input').placeholder = mark ? `已保存：${mark.id}` : '也可粘贴作品链接或 ID';
        let eligible = items;
        try { eligible = range(items, limited ? mark?.id : ''); } catch { eligible = []; }
        $('#count').textContent = `已加载 ${items.length} · 范围内 ${eligible.length}`;
        $('#list').replaceChildren();
        const selected = new Set(eligible.map(item => item.id));
        for (const item of items) {
            const row = document.createElement('div'); row.className = 'note';
            if (/^https?:\/\//.test(item.cover)) {
                const image = document.createElement('img'); image.className = 'cover'; image.src = item.cover;
                image.loading = 'lazy'; image.referrerPolicy = 'no-referrer'; image.alt = ''; row.append(image);
            }
            const body = document.createElement('div'); body.className = 'grow';
            const title = document.createElement('a'); title.className = 'title'; title.href = item.url;
            title.target = '_blank'; title.rel = 'noopener noreferrer'; title.textContent = item.title;
            const desc = document.createElement('div'); desc.className = 'muted'; desc.textContent = item.author;
            const actions = document.createElement('div'); actions.className = 'actions';
            if (limited) {
                const set = document.createElement('button'); set.textContent = mark?.id === item.id ? '✓ 截止作品' : '设为截止';
                set.disabled = busy; set.onclick = () => execute(async () => {guard(); remember(item); status('截止作品已保存，发送时不包含它。');}); actions.append(set);
            }
            const send = document.createElement('button'); send.textContent = '单独发送'; send.disabled = busy;
            send.onclick = () => execute(async () => {guard(); await push([item], 'single');}); actions.append(send);
            const tag = document.createElement('span'); tag.className = 'tag'; tag.textContent = selected.has(item.id) ? '待发送' : '截止范围外';
            body.append(title, desc, actions); row.append(body, tag); $('#list').append(row);
        }
        shadow.querySelectorAll('button').forEach(button => {
            if (!button.matches('.launch,#close,#stop')) button.disabled = busy;
        });
        $('#stop').classList.toggle('hidden', !busy);
        $('#progress').classList.toggle('hidden', !busy);
    }
    async function execute(action) {
        if (busy) return;
        busy = true; stopped = false; render();
        try { await action(); } catch (error) { status(error.message || String(error), true); }
        finally {busy = false; render();}
    }
    async function scan(auto) {
        await activate();
        items = read(); const snapshot = context;
        const collected = new Map(items.map(item => [item.id, item]));
        if (auto) {
            let idle = 0;
            for (let step = 0; step < config.max && !stopped; step++) {
                guard();
                const mark = ['liked', 'saved'].includes(mode) ? cutoff()?.id : '';
                if (mark && collected.has(mark)) break;
                const count = collected.size;
                const containers = [document.scrollingElement, ...document.querySelectorAll('.scroll-container, .feeds-container')].filter(Boolean);
                containers.forEach(node => node.scrollTo({top: node.scrollHeight, behavior: 'instant'}));
                await sleep(1200);
                guard();
                read().forEach(item => collected.set(item.id, item));
                if (context !== snapshot) throw new Error('页面已切换，请重新读取。');
                idle = collected.size === count ? idle + 1 : 0;
                items = [...collected.values()]; render(); status(`正在加载 ${modes[mode]} · ${items.length} 个作品 · 第 ${step + 1} 次滚动`);
                if (idle >= 5) break;
            }
        }
        items = [...collected.values()];
        status(`${stopped ? '已停止加载' : '读取完成'}：${items.length} 个作品。可选择截止作品或发送范围内作品。`);
    }
    function request(payload) {
        return new Promise((resolve, reject) => GM_xmlhttpRequest({
            method: 'POST', url: endpoint(config.host, config.port, config.path),
            headers: {'Content-Type': 'application/json', 'X-NAS-Submit-Token': config.token},
            data: JSON.stringify(payload), timeout: 30000,
            onload(response) {
                try {
                    if (response.status < 200 || response.status >= 300) throw new Error(`Docker HTTP ${response.status}，请检查地址、授权和服务版本。`);
                    const body = JSON.parse(response.responseText);
                    if (!body.ok || body.valid !== payload.urls.length || Number(body.accepted || 0) + Number(body.skipped || 0) !== payload.urls.length || body.invalid?.length) throw new Error('Docker 未完整确认接收，请检查任务队列后重试。');
                    resolve(body);
                } catch (error) {reject(error);}
            },
            onerror: () => reject(new Error('无法连接 Docker，请检查地址和网络。')),
            ontimeout: () => reject(new Error('Docker 请求超时，请先检查任务队列，避免重复发送。')),
        }));
    }
    async function push(notes, destination) {
        if (!notes.length) throw new Error('范围内没有作品，本次不发送。');
        let sent = 0;
        for (let offset = 0; offset < notes.length && !stopped; offset += 500) {
            const chunk = notes.slice(offset, offset + 500);
            status(`正在发送 ${sent}/${notes.length}…`);
            try {await request({urls: chunk.map(item => item.url), destination});}
            catch (error) {throw new Error(`已确认 ${sent}/${notes.length}。${error.message}`);}
            sent += chunk.length;
        }
        status(`Docker 已确认 ${sent}/${notes.length} 个作品${stopped ? '（已停止后续发送）' : ''}。${destination === 'single' ? '保存到单个作品目录。' : '截止点保持不变。'} 可在 Docker 控制台查看下载进度。`);
    }
    function currentNote() {
        const state = ref('note');
        const id = unwrap(state?.currentNoteId) || noteId(location.pathname);
        const detail = unwrap(state?.noteDetailMap)?.[id];
        const token = detail?.note?.xsecToken || detail?.xsecToken;
        const url = new URL(location.href);
        if (!id || !noteId(id)) throw new Error('请先打开一个作品，或粘贴作品完整链接。');
        const result = new URL(`/explore/${id}`, location.origin);
        if (url.searchParams.get('xsec_token') || token) result.searchParams.set('xsec_token', url.searchParams.get('xsec_token') || token);
        return result.href;
    }
    function parseLink(value) {
        const url = new URL(value.trim());
        if (!['www.xiaohongshu.com', 'xiaohongshu.com'].includes(url.hostname) || !['http:', 'https:'].includes(url.protocol) || !noteId(url.pathname)) throw new Error('请粘贴小红书完整作品链接，保留 xsec_token 参数。');
        return {id: noteId(url.pathname), url: url.href};
    }
    $('.launch').onclick = () => {$('.panel').classList.toggle('hidden'); render();};
    $('#close').onclick = () => $('.panel').classList.add('hidden');
    shadow.querySelectorAll('[data-mode]').forEach(button => button.onclick = () => {mode = button.dataset.mode; items = []; context = ''; render();});
    $('#scan').onclick = () => execute(() => scan(false));
    $('#load').onclick = () => execute(() => scan(true));
    $('#stop').onclick = () => {stopped = true; status('正在停止，等待当前操作完成…');};
    $('#clear').onclick = () => {remember(null); status('已清除当前账号此分类的截止作品。');};
    $('#save-cutoff').onclick = () => execute(async () => {
        if (!accountId(location.pathname)) throw new Error('请先打开账号主页。');
        const id = noteId($('#cutoff-input').value.trim());
        if (!id) throw new Error('请输入有效的作品链接或 24 位作品 ID。');
        remember(items.find(item => item.id === id) || {id, title: ''}); status('截止作品已长期保存。');
    });
    $('#send-batch').onclick = () => execute(async () => {guard(); await push(range(items, ['liked', 'saved'].includes(mode) ? cutoff()?.id : ''), 'batch');});
    $('#current').onclick = () => execute(async () => {$('#single').value = currentNote(); status('已填入当前作品，点击发送即可单独归档。');});
    $('#send-single').onclick = () => execute(() => push([parseLink($('#single').value || currentNote())], 'single'));
    for (const field of ['host', 'port', 'path', 'token', 'max']) $(`#${field}`).value = config[field];
    $('#settings').onclick = () => execute(async () => {
        const next = Object.fromEntries(['host', 'port', 'path', 'token', 'max'].map(field => [field, $(`#${field}`).value.trim()]));
        endpoint(next.host, next.port, next.path);
        next.max = Number(next.max);
        if (!Number.isInteger(next.max) || next.max < 1 || next.max > 1000) throw new Error('滚动次数应为 1–1000 的整数。');
        Object.assign(config, next);
        for (const [field, storage] of Object.entries({host: 'dockerHost', port: 'dockerPort', path: 'dockerPath', token: 'nasSubmitToken', max: 'nasMaxScrolls'})) GM_setValue(storage, config[field]);
        status('连接与加载设置已保存。');
    });
    let lastUrl = location.href;
    setInterval(() => {
        if (lastUrl === location.href) return;
        lastUrl = location.href; stopped = true; items = []; context = ''; render();
        status('页面已切换，请重新读取作品。');
    }, 700);
    GM_registerMenuCommand('打开 NAS 下载工作台', () => {$('.panel').classList.remove('hidden'); render();});
    render();
})();
