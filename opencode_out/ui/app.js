// ── State ─────────────────────────────────────────────────────────────
// Per-chat sending state — replaces the old global `sending` bool.
// Each chat can be independently streaming without blocking others.
const sendingChats = new Set();   // set of chat IDs currently awaiting a response

// Per-chat partial stream state so we can re-render when switching back
// to a chat that is still responding.
// chatId -> { assistantText, hasContent }
const chatStreamState = {};

let selectedModel = 'minimax-m2.5-free';
let selectedModelCtx = 1000000;
let selectedAgent = 'build';  // build | plan | explore | ask

function formatCtx(n) {
    if (n >= 1000000) return (n / 1000000).toFixed(0) + 'M';
    if (n >= 1000) return Math.round(n / 1000) + 'k';
    return String(n);
}

// Each chat: { id, title, workingDirs: [], history: [], createdAt }
let chats = [];
let activeChatId = null;

// ── DOM refs ──────────────────────────────────────────────────────────
const chatEl       = document.getElementById('chat');
const input        = document.getElementById('input');
const sendBtn      = document.getElementById('send');
const modelBtn     = document.getElementById('model-btn');
const modelLabel   = document.getElementById('model-label');
const modelDropdown= document.getElementById('model-dropdown');
const sidebar      = document.getElementById('sidebar');
const menuBtn      = document.getElementById('menu-btn');
const folderBtn    = document.getElementById('folder-btn');
const folderBar    = document.getElementById('folder-bar');
const chatList     = document.getElementById('chat-list');
const chatTitle    = document.getElementById('chat-title');
const newChatBtn   = document.getElementById('new-chat-btn');
const chatMenuBtn  = document.getElementById('chat-menu-btn');
const chatMenu     = document.getElementById('chat-menu');
const renameChatBtn= document.getElementById('rename-chat-btn');
const deleteChatBtn= document.getElementById('delete-chat-btn');
const compressChatBtn = document.getElementById('compress-chat-btn');
const renameModal  = document.getElementById('rename-modal');
const renameInput  = document.getElementById('rename-input');
const renameCancel = document.getElementById('rename-cancel');
const renameConfirm= document.getElementById('rename-confirm');

// ── Android bridge ────────────────────────────────────────────────────
function androidBridge() { return window.Android; }

// ── Storage dir path ──────────────────────────────────────────────────
let storageDir = '';

async function getStorageDir() {
    try {
        const r = await fetch('/storage_dir');
        const d = await r.json();
        storageDir = d.path || '';
    } catch {}
}

// ── Chat persistence ──────────────────────────────────────────────────
async function saveChats() {
    try {
        await fetch('/save_chats', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ chats, activeChatId })
        });
    } catch {}
}

async function loadChats() {
    try {
        const r = await fetch('/load_chats');
        const d = await r.json();
        chats = d.chats || [];
        activeChatId = d.activeChatId || null;
    } catch {
        chats = [];
        activeChatId = null;
    }
}

// ── Chat helpers ──────────────────────────────────────────────────────
function activeChat() {
    return chats.find(c => c.id === activeChatId) || null;
}

function createChat() {
    const id = 'chat_' + Date.now();
    const chat = { id, title: 'new chat', workingDirs: [], history: [], createdAt: Date.now() };
    chats.unshift(chat);
    return chat;
}

function truncatePath(p) {
    const parts = p.replace(/\\/g, '/').split('/').filter(Boolean);
    if (parts.length <= 3) return p;
    return '.../' + parts.slice(-3).join('/');
}

// ── Send button state ─────────────────────────────────────────────────
// Only disable the send button if the *currently active* chat is sending.
// Other chats streaming in the background do not affect this chat's button.
function updateSendButton() {
    const busy = sendingChats.has(activeChatId);
    sendBtn.disabled = busy;
    sendBtn.innerHTML = busy
        ? '<span class="dots"><span></span><span></span><span></span></span>'
        : '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>';
}

// ── Folder bar ────────────────────────────────────────────────────────
function renderFolderBar() {
    const chat = activeChat();
    const dirs = chat ? chat.workingDirs : [];
    if (!dirs.length) {
        folderBar.classList.add('hidden');
        return;
    }
    folderBar.classList.remove('hidden');
    folderBar.innerHTML = dirs.map((d) =>
        '<span class="folder-chip-tag" title="' + escHtml(d) + '">' +
            '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>' +
            escHtml(truncatePath(d)) +
            '<button class="folder-remove" data-path="' + escHtml(d) + '" title="Remove folder">x</button>' +
        '</span>'
    ).join('');
    folderBar.querySelectorAll('.folder-remove').forEach(btn => {
        btn.onclick = async (e) => {
            e.stopPropagation();
            const chat = activeChat();
            if (!chat) return;
            const pathToRemove = btn.dataset.path;
            chat.workingDirs = chat.workingDirs.filter(d => d !== pathToRemove);
            await syncWorkingDirs();
            renderFolderBar();
            saveChats();
        };
    });
}

async function syncWorkingDirs() {
    const chat = activeChat();
    const dirs = chat ? chat.workingDirs : [];
    try {
        const resp = await fetch('/working_dirs', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ working_dirs: dirs })
        });
        const data = await resp.json();
        if (data.invalid_dirs && data.invalid_dirs.length && chat) {
            const removed = new Set(data.invalid_dirs);
            const before = chat.workingDirs.length;
            chat.workingDirs = chat.workingDirs.filter(d => !removed.has(d));
            if (chat.workingDirs.length !== before) {
                renderFolderBar();
                renderChatList();
                saveChats();
                const notice = document.createElement('div');
                notice.className = 'folder-removed-notice';
                notice.textContent = removed.size + ' folder(s) no longer accessible and were removed.';
                document.getElementById('chat').appendChild(notice);
                setTimeout(() => notice.remove(), 4000);
            }
        }
    } catch {}
}

// ── Chat list sidebar ─────────────────────────────────────────────────
function renderChatList() {
    if (!chats.length) {
        chatList.innerHTML = '<div class="chat-list-empty">no chats yet</div>';
        return;
    }
    chatList.innerHTML = chats.map(c => {
        const isSending = sendingChats.has(c.id);
        return '<div class="chat-item ' + (c.id === activeChatId ? 'active' : '') + '" data-id="' + c.id + '">' +
            '<div class="chat-item-inner">' +
                '<span class="chat-item-title">' + escHtml(c.title) + '</span>' +
                (isSending ? '<span class="chat-item-responding">responding\u2026</span>' : '') +
                (!isSending && c.workingDirs.length ? '<span class="chat-item-dir" title="' + escHtml(c.workingDirs[0]) + '">' + escHtml(truncatePath(c.workingDirs[0])) + '</span>' : '') +
            '</div>' +
        '</div>';
    }).join('');
    chatList.querySelectorAll('.chat-item').forEach(el => {
        el.onclick = () => switchChat(el.dataset.id);
    });
}

async function switchChat(id) {
    if (id === activeChatId) {
        sidebar.classList.add('collapsed');
        return;
    }
    activeChatId = id;
    const chat = activeChat();
    chatTitle.textContent = chat ? chat.title : 'new chat';

    // Tell backend which chat is now active
    await fetch('/switch_chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ chat_id: id, history: chat ? chat.history : [] })
    });
    await syncWorkingDirs();

    renderChatList();
    renderFolderBar();

    // Render saved history first
    renderHistory();

    // If this chat is still streaming in the background, show the partial response
    if (sendingChats.has(id)) {
        const state = chatStreamState[id];
        const div = createAssistantShell();
        div.dataset.live = id;
        if (state && state.hasContent) {
            div.innerHTML = '<span class="msg-prefix">assistant</span>' + parseMarkdown(state.assistantText) + '<span class="cursor"></span>';
        }
        scrollBottom();
    }

    updateContextBadge();
    updateSendButton();
    saveChats();
    sidebar.classList.add('collapsed');
}

function renderHistory() {
    const chat = activeChat();
    chatEl.innerHTML = '';
    if (!chat || !chat.history.length) {
        updateContextBadge();
        return;
    }
    for (const msg of chat.history) {
        if (msg.role === 'user') {
            addUserMsgStatic(msg.content);
        } else if (msg.role === 'assistant' && (msg.content || msg.reasoning_content)) {
            addAssistantMsgStatic(msg.content, msg.reasoning_content);
        }
    }
    updateContextBadge();
    scrollBottom();
}

// ── Folder picker ─────────────────────────────────────────────────────
folderBtn.onclick = () => {
    const android = androidBridge();
    if (android && android.openFolderPicker) {
        android.openFolderPicker();
    } else {
        const path = prompt('Enter absolute folder path:');
        if (path && path.trim()) addFolder(path.trim());
    }
};

async function addFolder(path) {
    let chat = activeChat();
    if (!chat) {
        chat = createChat();
        activeChatId = chat.id;
    }
    if (!chat.workingDirs.includes(path)) {
        chat.workingDirs.push(path);
    }
    await syncWorkingDirs();
    renderFolderBar();
    renderChatList();
    saveChats();
}

setInterval(async () => {
    const android = androidBridge();
    if (!android || !android.getWorkingDir) return;
    const newPath = android.getWorkingDir();
    if (!newPath) return;
    const chat = activeChat();
    if (chat && !chat.workingDirs.includes(newPath)) {
        await addFolder(newPath);
        if (android.clearWorkingDir) android.clearWorkingDir();
    }
}, 1000);

// ── New chat ──────────────────────────────────────────────────────────
newChatBtn.onclick = async () => {
    const chat = createChat();
    activeChatId = chat.id;
    chatTitle.textContent = chat.title;
    chatEl.innerHTML = '';
    await fetch('/switch_chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ chat_id: chat.id, history: [] })
    });
    await syncWorkingDirs();
    renderChatList();
    renderFolderBar();
    updateSendButton();
    saveChats();
    input.focus();
    sidebar.classList.add('collapsed');
};

// ── Context badge (in header) ─────────────────────────────────────────
const contextBadge = document.getElementById('context-badge');

function updateContextBadge() {
    if (!contextBadge) return;
    const chat = activeChat();
    const charCount = chat ? chat.history.reduce((n, m) => n + String(m.content || '').length, 0) : 0;
    const tokEst = Math.round(charCount / 4);
    const total = selectedModelCtx;
    contextBadge.textContent = formatCtx(tokEst) + '/' + formatCtx(total);
    const pct = tokEst / total;
    contextBadge.style.color = pct > 0.9 ? 'var(--err, #e05)' : pct > 0.7 ? 'var(--warn, #f90)' : '';
}

// ── Chat menu (three dots) ─────────────────────────────────────────────────────
chatMenuBtn.onclick = (e) => {
    e.stopPropagation();
    chatMenu.classList.toggle('hidden');
};
document.addEventListener('click', () => {
    chatMenu.classList.add('hidden');
    modelDropdown.classList.add('hidden');
    modelBtn.classList.remove('open');
    if (agentDropdown) { agentDropdown.classList.add('hidden'); agentBtn.classList.remove('open'); }
});

renameChatBtn.onclick = (e) => {
    e.stopPropagation();
    chatMenu.classList.add('hidden');
    const chat = activeChat();
    if (!chat) return;
    renameInput.value = chat.title === 'new chat' ? '' : chat.title;
    renameModal.classList.remove('hidden');
    renameInput.focus();
};

renameCancel.onclick = () => renameModal.classList.add('hidden');
renameConfirm.onclick = doRename;
renameInput.onkeydown = (e) => { if (e.key === 'Enter') doRename(); if (e.key === 'Escape') renameModal.classList.add('hidden'); };
renameModal.onclick = (e) => { if (e.target === renameModal) renameModal.classList.add('hidden'); };

function doRename() {
    const val = renameInput.value.trim();
    if (!val) return;
    const chat = activeChat();
    if (!chat) return;
    chat.title = val;
    chatTitle.textContent = val;
    renderChatList();
    saveChats();
    renameModal.classList.add('hidden');
}

deleteChatBtn.onclick = async (e) => {
    e.stopPropagation();
    chatMenu.classList.add('hidden');
    const chat = activeChat();
    if (!chat) return;

    await fetch('/delete_chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ chat_id: chat.id })
    });

    chats = chats.filter(c => c.id !== chat.id);
    activeChatId = chats.length ? chats[0].id : null;
    await saveChats();
    location.reload();
};

compressChatBtn.onclick = async (e) => {
    e.stopPropagation();
    chatMenu.classList.add('hidden');
    const chat = activeChat();
    if (!chat) return;

    showStatusBanner('✦ Compressing chat…', 'info');

    try {
        const resp = await fetch('/compact', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ chat_id: chat.id, model: selectedModel })
        });
        const data = await resp.json();

        if (data.history) {
            chat.history = data.history;
            await saveChats();
            renderHistory();
            updateContextBadge();
        }

        const parts = [];
        if (data.compacted)
            parts.push('history summarised');

        if (parts.length) {
            showStatusBanner('✓ ' + parts.join(' · '), 'ok');
        } else {
            showStatusBanner('✓ Not enough history to summarise yet', 'info');
        }
    } catch (err) {
        showStatusBanner('⚠ Compression failed: ' + err.message, 'error');
    }
};

// ── Status banner (pencil / compact notifications) ─────────────────────
function showStatusBanner(text, kind = 'info') {
    // Remove any existing banner
    const old = document.getElementById('status-banner');
    if (old) old.remove();

    const el = document.createElement('div');
    el.id = 'status-banner';
    el.className = 'status-banner status-' + kind;
    el.textContent = text;
    chatEl.appendChild(el);
    scrollBottom();
    setTimeout(() => el.remove(), 4000);
}

// ── Sidebar toggle ────────────────────────────────────────────────────
menuBtn.onclick = () => sidebar.classList.toggle('collapsed');

// ── Model selector ────────────────────────────────────────────────────
modelBtn.onclick = (e) => {
    e.stopPropagation();
    const isHidden = modelDropdown.classList.toggle('hidden');
    modelBtn.classList.toggle('open', !isHidden);
};
modelDropdown.querySelectorAll('.model-option').forEach(btn => {
    btn.onclick = (e) => {
        e.stopPropagation();
        selectedModel = btn.dataset.model;
        selectedModelCtx = parseInt(btn.dataset.ctx || '128000', 10);
        modelLabel.textContent = btn.dataset.label;
        modelDropdown.querySelectorAll('.model-option').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        modelDropdown.classList.add('hidden');
        modelBtn.classList.remove('open');
        updateContextBadge();
    };
});

// ── Agent selector ────────────────────────────────────────────────────
const agentBtn      = document.getElementById('agent-btn');
const agentLabel    = document.getElementById('agent-label');
const agentDropdown = document.getElementById('agent-dropdown');

agentBtn.onclick = (e) => {
    e.stopPropagation();
    const isHidden = agentDropdown.classList.toggle('hidden');
    agentBtn.classList.toggle('open', !isHidden);
    modelDropdown.classList.add('hidden');
    modelBtn.classList.remove('open');
};
agentDropdown.querySelectorAll('.agent-option').forEach(btn => {
    btn.onclick = (e) => {
        e.stopPropagation();
        selectedAgent = btn.dataset.agent;
        agentLabel.textContent = selectedAgent;
        agentDropdown.querySelectorAll('.agent-option').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        agentDropdown.classList.add('hidden');
        agentBtn.classList.remove('open');
        showStatusBanner('Agent: ' + selectedAgent, 'info');
    };
});

// ── Markdown / HTML helpers ───────────────────────────────────────────
function escHtml(s) {
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function buildCodeBlock(lang, code) {
    return '<div class="code-block">' +
        '<div class="code-block-header">' +
        '<span class="code-lang">' + escHtml(lang||'code') + '</span>' +
        '<button class="copy-btn" onclick="copyCode(this)">copy</button>' +
        '</div>' +
        '<pre><code class="lang-' + escHtml(lang) + '">' + escHtml(code.trimEnd()) + '</code></pre>' +
        '</div>';
}

function parseMarkdown(text) {
    if (!text) return '';
    const segments = [];
    const fence = /```(\w*)\n?([\s\S]*?)```/g;
    let last = 0, m;
    while ((m = fence.exec(text)) !== null) {
        if (m.index > last) segments.push({ type:'text', content: text.slice(last, m.index) });
        segments.push({ type:'code', lang: m[1]||'', content: m[2] });
        last = m.index + m[0].length;
    }
    if (last < text.length) segments.push({ type:'text', content: text.slice(last) });
    return segments.map(seg => {
        if (seg.type === 'code') return buildCodeBlock(seg.lang, seg.content);
        let s = escHtml(seg.content);
        s = s.replace(/`([^`\n]+)`/g, '<code>$1</code>');
        s = s.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
        s = s.replace(/__([^_\n]+)__/g, '<strong>$1</strong>');
        s = s.replace(/\*([^*\n]+)\*/g, '<em>$1</em>');
        s = s.replace(/(^|[\s>])_([^_\n]+)_(?=[\s<,\.!?;:]|$)/gm, '$1<em>$2</em>');
        s = s.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
        s = s.replace(/^### (.+)$/gm, '<h3>$1</h3>');
        s = s.replace(/^## (.+)$/gm,  '<h2>$1</h2>');
        s = s.replace(/^# (.+)$/gm,   '<h1>$1</h1>');
        s = s.replace(/^[\*\-] (.+)$/gm, '<li>$1</li>');
        s = s.replace(/(<li>.*<\/li>\n?)+/g, function(mm) { return '<ul>' + mm + '</ul>'; });
        s = s.replace(/^---$/gm, '<hr>');
        return s.split(/\n\n+/).map(b => {
            b = b.trim();
            if (!b) return '';
            if (/^<(div|ul|ol|h[1-6]|hr|blockquote)/.test(b)) return b;
            return '<p>' + b.replace(/\n/g, '<br>') + '</p>';
        }).join('\n');
    }).join('');
}

window.copyCode = function(btn) {
    const code = btn.closest('.code-block').querySelector('code');
    navigator.clipboard.writeText(code.textContent).then(() => {
        btn.textContent = 'copied!';
        btn.classList.add('copied');
        setTimeout(() => { btn.textContent = 'copy'; btn.classList.remove('copied'); }, 1800);
    });
};

// ── DOM helpers ───────────────────────────────────────────────────────
function scrollBottom() { chatEl.scrollTop = chatEl.scrollHeight; }

function addUserMsgStatic(content) {
    const div = document.createElement('div');
    div.className = 'msg user';
    const inner = document.createElement('div');
    inner.className = 'user-inner';
    inner.textContent = content;
    div.appendChild(inner);
    chatEl.appendChild(div);
}

function addUserMsg(content) {
    addUserMsgStatic(content);
    scrollBottom();
}

function addAssistantMsgStatic(content, reasoning) {
    const div = document.createElement('div');
    div.className = 'msg assistant';
    const prefix = document.createElement('span');
    prefix.className = 'msg-prefix';
    prefix.textContent = 'assistant';
    div.appendChild(prefix);
    if (reasoning) {
        const wrapper = document.createElement('div');
        wrapper.className = 'thinking-wrapper';
        const header = document.createElement('button');
        header.className = 'thinking-header';
        header.innerHTML =
            '<span class="thinking-label">thought process</span>' +
            '<svg class="thinking-chevron" width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="6 9 12 15 18 9"/></svg>';
        const body = document.createElement('div');
        body.className = 'thinking-body open';
        body.textContent = reasoning;
        header.addEventListener('click', () => body.classList.toggle('open'));
        wrapper.appendChild(header);
        wrapper.appendChild(body);
        div.appendChild(wrapper);
    }
    if (content) {
        const contentDiv = document.createElement('div');
        contentDiv.innerHTML = parseMarkdown(content);
        div.appendChild(contentDiv);
    }
    chatEl.appendChild(div);
}

function createAssistantShell() {
    const div = document.createElement('div');
    div.className = 'msg assistant streaming';
    div.innerHTML = '<span class="msg-prefix">assistant</span><span class="cursor"></span>';
    chatEl.appendChild(div);
    scrollBottom();
    return div;
}

function sealAssistant(div, text) {
    div.classList.remove('streaming');
    div.removeAttribute('data-live');
    div.innerHTML = '<span class="msg-prefix">assistant</span>' + parseMarkdown(text);
}

function createThinkingBlock() {
    const wrapper = document.createElement('div');
    wrapper.className = 'thinking-wrapper';
    wrapper.innerHTML =
        '<button class="thinking-header">' +
            '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><circle cx="12" cy="12" r="10"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>' +
            '<span class="thinking-label">thinking\u2026</span>' +
            '<svg class="thinking-chevron" width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="6 9 12 15 18 9"/></svg>' +
        '</button>' +
        '<div class="thinking-body open"></div>';
    chatEl.appendChild(wrapper);
    scrollBottom();
    const header = wrapper.querySelector('.thinking-header');
    const body   = wrapper.querySelector('.thinking-body');
    let open = true;
    header.onclick = () => {
        open = !open;
        body.classList.toggle('open', open);
        wrapper.classList.toggle('collapsed', !open);
    };
    return { wrapper, body, header };
}

function sealThinking(block) {
    block.header.querySelector('.thinking-label').textContent = 'thought process';
}

function createToolGroup() {
    const group = document.createElement('div');
    group.className = 'tool-group';
    chatEl.appendChild(group);
    scrollBottom();
    return group;
}

function createToolPill(name, args, group) {
    const container = group || chatEl;
    const div = document.createElement('div');
    div.className = 'tool-pill';
    let icon, label;
    if (name === 'web_search') {
        icon = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" style="flex-shrink:0"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>';
        label = 'searching&nbsp;<em>' + escHtml(args.query||'') + '</em>';
    } else if (name === 'glob') {
        icon = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="flex-shrink:0"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>';
        label = 'finding&nbsp;<em>' + escHtml(args.pattern||'') + '</em>';
    } else if (name === 'grep') {
        icon = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="flex-shrink:0"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>';
        label = 'searching&nbsp;<em>' + escHtml(args.pattern||'') + '</em>';
    } else if (name === 'read') {
        icon = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="flex-shrink:0"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>';
        label = 'reading&nbsp;<em>' + escHtml(args.filePath||'') + '</em>';
    } else if (name === 'write') {
        icon = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="flex-shrink:0"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>';
        label = 'writing&nbsp;<em>' + escHtml(args.filePath||'') + '</em>';
    } else if (name === 'edit') {
        icon = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="flex-shrink:0"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>';
        label = 'editing&nbsp;<em>' + escHtml(args.filePath||'') + '</em>';
    } else {
        icon = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="flex-shrink:0"><circle cx="12" cy="12" r="10"/></svg>';
        label = 'running&nbsp;<em>' + escHtml(name) + '</em>';
    }
    div.innerHTML = '<span class="tool-spinner"></span>' + icon + '<span>' + label + '</span>';
    container.appendChild(div);
    scrollBottom();
    return div;
}

// ── Auto-title ────────────────────────────────────────────────────────
async function autoTitle(chatId, userMsg) {
    const chat = chats.find(c => c.id === chatId);
    if (!chat || chat.title !== 'new chat') return;
    const words = userMsg.trim().split(/\s+/).slice(0, 6).join(' ');
    chat.title = words.length > 40 ? words.slice(0, 40) + '\u2026' : words;
    if (chatId === activeChatId) chatTitle.textContent = chat.title;
    renderChatList();
    saveChats();
}

// ── Send ──────────────────────────────────────────────────────────────
// Each call to send() spawns an independent async stream for the current
// chat. Multiple chats can stream simultaneously without blocking each other.
async function send() {
    const userMsg = input.value.trim();
    if (!userMsg) return;

    // Prevent double-sending the SAME chat, but allow other chats to send freely
    if (sendingChats.has(activeChatId)) return;

    if (!activeChatId || !activeChat()) {
        const chat = createChat();
        activeChatId = chat.id;
        chatTitle.textContent = chat.title;
        await fetch('/switch_chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ chat_id: chat.id, history: [] })
        });
        await syncWorkingDirs();
        renderChatList();
    }

    // Snapshot the chat ID for this request's entire lifetime.
    // If the user switches chats, sendingChatId stays correct.
    const sendingChatId = activeChatId;
    const isActive = () => activeChatId === sendingChatId;

    input.value = '';
    input.style.height = 'auto';

    // Show user message only if we are still looking at this chat
    if (isActive()) addUserMsg(userMsg);

    // Immediately persist a snapshot so closing the app right after sending
    // doesn't lose the user's message. We write a _pending flag so that
    // history_update (which arrives at stream end) overwrites it cleanly.
    if (chat) {
        const snapshot = [...(chat.history || []), { id: 'u_pending_' + Date.now(), role: 'user', content: userMsg, _pending: true }];
        const prevHistory = chat.history;
        chat.history = snapshot;
        saveChats();
        chat.history = prevHistory;  // restore so the real history_update wins
    }

    autoTitle(sendingChatId, userMsg);

    // Mark this chat as busy
    sendingChats.add(sendingChatId);
    chatStreamState[sendingChatId] = { assistantText: '', hasContent: false };
    updateSendButton();
    renderChatList(); // show "responding..." badge in sidebar

    const chat = chats.find(c => c.id === sendingChatId);

    let thinkingBlock = null;
    let assistantDiv  = null;
    let toolPill      = null;
    let toolGroup     = null;
    let assistantText = '';

    let keepAliveTimer = setInterval(async () => {
        try { await fetch('/ping', { method: 'GET' }); } catch {}
    }, 20000);

    try {
        const resp = await fetch('/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                message: userMsg,
                model: selectedModel,
                agent: selectedAgent,
                chat_id: sendingChatId          // tell backend which chat this belongs to
            })
        });
        if (!resp.ok) throw new Error('HTTP ' + resp.status);

        const reader  = resp.body.getReader();
        const decoder = new TextDecoder();
        let buf = '';

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buf += decoder.decode(value, { stream: true });
            const lines = buf.split('\n');
            buf = lines.pop();

            for (const line of lines) {
                if (!line.startsWith('data: ')) continue;
                const raw = line.slice(6).trim();
                if (raw === '[DONE]') continue;
                let ev;
                try { ev = JSON.parse(raw); } catch { continue; }

                switch (ev.type) {
                    case 'thinking': {
                        if (!isActive()) break;
                        if (!thinkingBlock) thinkingBlock = createThinkingBlock();
                        if (!thinkingBlock._indicator) {
                            thinkingBlock._indicator = true;
                            thinkingBlock.header.querySelector('.thinking-label').innerHTML =
                                'thinking <span class="thinking-dots"><span></span><span></span><span></span></span>';
                        }
                        thinkingBlock.body.textContent += ev.text;
                        scrollBottom();
                        break;
                    }
                    case 'text': {
                        assistantText += ev.text;
                        // Always keep stream state up-to-date for background chats
                        chatStreamState[sendingChatId] = { assistantText, hasContent: true };

                        if (!isActive()) break; // don't touch DOM for background chat

                        if (thinkingBlock) { sealThinking(thinkingBlock); thinkingBlock = null; }
                        if (toolPill) { toolPill.classList.add('done'); toolPill = null; toolGroup = null; }

                        if (!assistantDiv) {
                            // Check if switchChat already planted a live shell for this chat
                            assistantDiv = chatEl.querySelector('[data-live="' + sendingChatId + '"]');
                            if (!assistantDiv) {
                                assistantDiv = createAssistantShell();
                                assistantDiv.dataset.live = sendingChatId;
                            }
                        }
                        assistantDiv.innerHTML = '<span class="msg-prefix">assistant</span>' + parseMarkdown(assistantText) + '<span class="cursor"></span>';
                        scrollBottom();
                        break;
                    }
                    case 'tool_use': {
                        if (!isActive()) break;
                        if (thinkingBlock) { sealThinking(thinkingBlock); thinkingBlock = null; }
                        if (assistantDiv) { sealAssistant(assistantDiv, assistantText); assistantDiv = null; }
                        if (!toolGroup) toolGroup = createToolGroup();
                        if (toolPill) toolPill.classList.add('done');
                        toolPill = createToolPill(ev.name, ev.args, toolGroup);
                        break;
                    }
                    case 'tool_done': {
                        if (!isActive()) break;
                        if (toolPill) {
                            const spinner = toolPill.querySelector('.tool-spinner');
                            if (spinner) spinner.outerHTML = '<span class="tool-check">\u2713</span>';
                        }
                        break;
                    }
                    case 'heartbeat': {
                        break;
                    }

                    case 'history_update': {
                        // Update the chat's stored history but DON'T re-render the DOM —
                        // the live stream is already painting the messages correctly.
                        // Re-rendering here would wipe the streamed content.
                        if (chat) {
                            chat.history = ev.history;
                            saveChats();
                            if (isActive()) updateContextBadge();
                        }
                        break;
                    }
                    case 'error': {
                        if (!isActive()) break;
                        if (thinkingBlock) { sealThinking(thinkingBlock); thinkingBlock = null; }
                        if (!assistantDiv) { assistantDiv = createAssistantShell(); }
                        assistantDiv.classList.remove('streaming');
                        assistantDiv.innerHTML = '<span class="msg-prefix">assistant</span><span class="error-msg">\u26a0 ' + escHtml(ev.text) + '</span>';
                        assistantDiv = null;
                        break;
                    }
                    case 'done': {
                        if (isActive()) {
                            if (thinkingBlock) { sealThinking(thinkingBlock); thinkingBlock = null; }
                            if (assistantDiv)  { sealAssistant(assistantDiv, assistantText); assistantDiv = null; }
                            if (toolPill)      { toolPill.classList.add('done'); toolPill = null; toolGroup = null; }
                        }
                        break;
                    }
                }
            }
        }

        // Final seal in case stream ended without a 'done' event
        if (isActive()) {
            if (thinkingBlock) sealThinking(thinkingBlock);
            if (assistantDiv)  sealAssistant(assistantDiv, assistantText);
            if (toolPill)      toolPill.classList.add('done');
        }

    } catch (e) {
        if (isActive()) {
            const d = assistantDiv || createAssistantShell();
            d.classList.remove('streaming');
            d.innerHTML = '<span class="msg-prefix">assistant</span><span class="error-msg">\u26a0 ' + escHtml(e.message) + '</span>';
        }
    }

    clearInterval(keepAliveTimer);

    // Unmark this chat as busy
    sendingChats.delete(sendingChatId);
    delete chatStreamState[sendingChatId];

    // Re-render sidebar badge and send button for whatever chat is active now
    renderChatList();
    updateSendButton();

    // If the user is still on this chat, re-focus input
    if (isActive()) input.focus();
}

sendBtn.onclick = send;
input.onkeydown = e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } };
input.oninput = () => {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 120) + 'px';
};

// ── Init ──────────────────────────────────────────────────────────────
async function init() {
    await getStorageDir();
    await loadChats();

    if (chats.length && activeChatId) {
        const chat = activeChat();
        if (chat) {
            chatTitle.textContent = chat.title;
            await fetch('/switch_chat', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ chat_id: chat.id, history: chat.history })
            });
            await syncWorkingDirs();
            renderHistory();
            updateContextBadge();
        }
    } else if (!chats.length) {
        const chat = createChat();
        activeChatId = chat.id;
        saveChats();
    }

    renderChatList();
    renderFolderBar();
    updateSendButton();
    input.focus();
}

init();
