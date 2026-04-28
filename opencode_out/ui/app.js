const API_BASE = '/chats';
let sending = false;
let activeChatId = null;
let chatsData = {};

const chatDiv      = document.getElementById('chat');
const input        = document.getElementById('input');
const sendBtn      = document.getElementById('send');
const modelBtn     = document.getElementById('model-btn');
const modelLabel   = document.getElementById('model-label');
const modelDropdown = document.getElementById('model-dropdown');
const sidebar      = document.getElementById('sidebar');
const menuBtn       = document.getElementById('menu-btn');
const newChatBtn    = document.getElementById('new-chat-btn');
const chatList      = document.getElementById('chat-list');

// ── Helpers ─────────────────────────────────────────────────────────
function escHtml(s) {
    return String(s)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
}

function truncatePathStart(path, maxLen = 30) {
    if (path.length <= maxLen) return path;
    return '...' + path.slice(path.length - (maxLen - 3));
}

// ── Markdown & code blocks ──────────────────────────────────────────
function buildCodeBlock(lang, code) {
    const displayLang = lang || 'code';
    const safeCode = escHtml(code.trimEnd());
    return `<div class="code-block">` +
        `<div class="code-block-header">` +
        `<span class="code-lang">${escHtml(displayLang)}</span>` +
        `<button class="copy-btn" onclick="copyCode(this)">copy</button>` +
        `</div>` +
        `<pre><code class="lang-${escHtml(lang)}">${safeCode}</code></pre>` +
        `</div>`;
}

function parseMarkdown(text) {
    if (!text) return '';
    const segments = [];
    const fence = /```(\w*)\n?([\s\S]*?)```/g;
    let last = 0, m;
    while ((m = fence.exec(text)) !== null) {
        if (m.index > last) segments.push({ type: 'text', content: text.slice(last, m.index) });
        segments.push({ type: 'code', lang: m[1] || '', content: m[2] });
        last = m.index + m[0].length;
    }
    if (last < text.length) segments.push({ type: 'text', content: text.slice(last) });

    return segments.map(seg => {
        if (seg.type === 'code') return buildCodeBlock(seg.lang, seg.content);
        let s = escHtml(seg.content);
        s = s.replace(/`([^`\n]+)`/g, '<code>$1</code>');
        s = s.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
        s = s.replace(/__([^_\n]+)__/g,     '<strong>$1</strong>');
        s = s.replace(/\*([^*\n]+)\*/g,     '<em>$1</em>');
        s = s.replace(/_([^_\n]+)_/g,       '<em>$1</em>');
        s = s.replace(/\[([^\]]+)\]\(([^)]+)\)/g,
            '<a href="$2" target="_blank" rel="noopener">$1</a>');
        s = s.replace(/^### (.+)$/gm, '<h3>$1</h3>');
        s = s.replace(/^## (.+)$/gm,  '<h2>$1</h2>');
        s = s.replace(/^# (.+)$/gm,   '<h1>$1</h1>');
        s = s.replace(/^[\*\-] (.+)$/gm, '<li>$1</li>');
        s = s.replace(/(<li>.*<\/li>\n?)+/g, mm => `<ul>${mm}</ul>`);
        s = s.replace(/^---$/gm, '<hr>');
        const blocks = s.split(/\n\n+/);
        return blocks.map(b => {
            b = b.trim();
            if (!b) return '';
            if (/^<(div|ul|ol|h[1-6]|hr|blockquote)/.test(b)) return b;
            return `<p>${b.replace(/\n/g, '<br>')}</p>`;
        }).join('\n');
    }).join('');
}

window.copyCode = function(btn) {
    const code = btn.closest('.code-block').querySelector('code');
    navigator.clipboard.writeText(code.textContent).then(() => {
        btn.textContent = 'copied!';
        btn.classList.add('copied');
        setTimeout(() => {
            btn.textContent = 'copy';
            btn.classList.remove('copied');
        }, 1800);
    });
};

// ── UI helpers ──────────────────────────────────────────────────────
function scrollBottom() { chatDiv.scrollTop = chatDiv.scrollHeight; }

function addUserMsg(content) {
    const div = document.createElement('div');
    div.className = 'msg user';
    const inner = document.createElement('div');
    inner.className = 'user-inner';
    inner.textContent = content;
    div.appendChild(inner);
    chatDiv.appendChild(div);
    scrollBottom();
}

function createAssistantShell() {
    const div = document.createElement('div');
    div.className = 'msg assistant streaming';
    div.innerHTML = `<span class="msg-prefix">assistant</span><span class="cursor"></span>`;
    chatDiv.appendChild(div);
    scrollBottom();
    return div;
}

function sealAssistant(div, text) {
    div.classList.remove('streaming');
    div.innerHTML = `<span class="msg-prefix">assistant</span>` + parseMarkdown(text);
}

function createThinkingBlock() {
    const wrapper = document.createElement('div');
    wrapper.className = 'thinking-wrapper';
    wrapper.innerHTML = `
        <button class="thinking-header">
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none"
                 stroke="currentColor" stroke-width="2" stroke-linecap="round">
                <path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/>
                <circle cx="12" cy="12" r="10"/>
                <line x1="12" y1="17" x2="12.01" y2="17"/>
            </svg>
            <span class="thinking-label">thinking…</span>
            <svg class="thinking-chevron" width="9" height="9" viewBox="0 0 24 24" fill="none"
                 stroke="currentColor" stroke-width="2.5">
                <polyline points="6 9 12 15 18 9"/>
            </svg>
        </button>
        <div class="thinking-body open"></div>`;
    chatDiv.appendChild(wrapper);
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

function createToolPill(name, args) {
    const div = document.createElement('div');
    div.className = 'tool-pill';
    let icon, label;
    if (name === 'web_search') {
        icon = `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" style="flex-shrink:0"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>`;
        label = `searching&nbsp;<em>${escHtml(args.query || '')}</em>`;
    } else if (name === 'glob') {
        icon = `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="flex-shrink:0"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>`;
        label = `finding&nbsp;<em>${escHtml(args.pattern || '')}</em>`;
    } else if (name === 'grep') {
        icon = `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="flex-shrink:0"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>`;
        label = `searching&nbsp;<em>${escHtml(args.pattern || '')}</em>`;
    } else if (name === 'read') {
        icon = `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="flex-shrink:0"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>`;
        label = `reading&nbsp;<em>${escHtml(args.filePath || '')}</em>`;
    } else if (name === 'write') {
        icon = `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="flex-shrink:0"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>`;
        label = `writing&nbsp;<em>${escHtml(args.filePath || '')}</em>`;
    } else if (name === 'edit') {
        icon = `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="flex-shrink:0"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>`;
        label = `editing&nbsp;<em>${escHtml(args.filePath || '')}</em>`;
    } else {
        icon = `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="flex-shrink:0"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>`;
        label = `running&nbsp;<em>${escHtml(name)}</em>`;
    }
    div.innerHTML = `<span class="tool-spinner"></span>${icon}<span>${label}</span>`;
    chatDiv.appendChild(div);
    scrollBottom();
    return div;
}

function setLoading(on) {
    sendBtn.disabled  = on;
    newChatBtn.disabled = on;
    sendBtn.innerHTML = on
        ? `<span class="dots"><span></span><span></span><span></span></span>`
        : `<svg width="13" height="13" viewBox="0 0 24 24" fill="none"
               stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
               <line x1="22" y1="2" x2="11" y2="13"/>
               <polygon points="22 2 15 22 11 13 2 9 22 2"/>
           </svg>`;
}

// ── Chat list rendering ─────────────────────────────────────────────
async function loadChatList() {
    const resp = await fetch(API_BASE);
    const list = await resp.json();
    chatList.innerHTML = '';
    list.forEach(chat => {
        const div = document.createElement('div');
        div.className = 'chat-item' + (chat.id === activeChatId ? ' active' : '');
        div.dataset.id = chat.id;
        div.innerHTML = `
            <div class="chat-title">${escHtml(chat.title)}</div>
            <div class="chat-path">${chat.working_dir ? truncatePathStart(chat.working_dir) : 'No folder'}</div>
            <button class="chat-menu-btn" data-id="${chat.id}">⋮</button>
        `;
        div.addEventListener('click', (e) => {
            if (e.target.classList.contains('chat-menu-btn')) return;
            switchToChat(chat.id);
        });
        chatList.appendChild(div);
    });

    // Attach menu handlers
    document.querySelectorAll('.chat-menu-btn').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            showChatMenu(btn.dataset.id, btn);
        });
    });
}

function showChatMenu(chatId, button) {
    const existing = document.querySelector('.chat-menu');
    if (existing) existing.remove();

    const menu = document.createElement('div');
    menu.className = 'chat-menu';
    menu.innerHTML = `
        <div class="chat-menu-item" data-action="rename">Rename</div>
        <div class="chat-menu-item" data-action="delete">Delete</div>
    `;
    // Position near button
    const rect = button.getBoundingClientRect();
    menu.style.top = (rect.bottom + 4) + 'px';
    menu.style.left = (rect.left - 100) + 'px';  // rough
    document.body.appendChild(menu);

    menu.querySelector('[data-action="rename"]').onclick = () => {
        menu.remove();
        renameChat(chatId);
    };
    menu.querySelector('[data-action="delete"]').onclick = () => {
        menu.remove();
        deleteChat(chatId);
    };
    // Hide on outside click
    setTimeout(() => {
        document.addEventListener('click', function hide() {
            menu.remove();
            document.removeEventListener('click', hide);
        });
    }, 0);
}

async function renameChat(chatId) {
    const newTitle = prompt('New chat title:');
    if (!newTitle || !newTitle.trim()) return;
    await fetch(`${API_BASE}/${chatId}`, {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ title: newTitle.trim() })
    });
    await loadChatList();
}

async function deleteChat(chatId) {
    if (!confirm('Delete this chat permanently?')) return;
    await fetch(`${API_BASE}/${chatId}`, { method: 'DELETE' });
    if (activeChatId === chatId) {
        activeChatId = null;
        chatDiv.innerHTML = '';
    }
    await loadChatList();
    if (!activeChatId) {
        // Auto-select first or create new
        const first = document.querySelector('.chat-item');
        if (first) switchToChat(first.dataset.id);
        else createNewChat();
    }
}

async function switchToChat(chatId) {
    activeChatId = chatId;
    localStorage.setItem('activeChatId', chatId);
    // Update active class
    document.querySelectorAll('.chat-item').forEach(el => el.classList.remove('active'));
    const activeEl = document.querySelector(`.chat-item[data-id="${chatId}"]`);
    if (activeEl) activeEl.classList.add('active');
    // Load messages
    chatDiv.innerHTML = '';
    const resp = await fetch(`${API_BASE}/${chatId}/messages`);
    const msgs = await resp.json();
    renderMessages(msgs);
    // Load model
    await loadCurrentChatModel();
}

function renderMessages(msgs) {
    chatDiv.innerHTML = '';
    msgs.forEach(msg => {
        if (msg.role === 'user') {
            addUserMsg(msg.content);
        } else if (msg.role === 'assistant') {
            if (msg.content) {
                const div = document.createElement('div');
                div.className = 'msg assistant';
                div.innerHTML = `<span class="msg-prefix">assistant</span>` + parseMarkdown(msg.content);
                chatDiv.appendChild(div);
            }
            // Tool calls are not visually rendered, but we could show them if needed.
        } else if (msg.role === 'tool') {
            // We could show tool results, but for now skip.
        }
    });
    scrollBottom();
}

async function loadCurrentChatModel() {
    if (!activeChatId) return;
    // We need to know the model of the active chat. We could fetch the chat object.
    const resp = await fetch(API_BASE);
    const list = await resp.json();
    const chat = list.find(c => c.id === activeChatId);
    if (chat && chat.model) {
        modelLabel.textContent = chat.model.replace('-free',''); // simple mapping
        // Update dropdown active
        document.querySelectorAll('.model-option').forEach(btn => {
            btn.classList.toggle('active', btn.dataset.model === chat.model);
        });
    }
}

async function createNewChat() {
    const resp = await fetch(API_BASE, { method: 'POST' });
    const chat = await resp.json();
    await loadChatList();
    switchToChat(chat.id);
}

// ── Folder selection from Android ───────────────────────────────────
window.onFolderSelected = async function(path) {
    if (!activeChatId) {
        // If no active chat, create one first
        await createNewChat();
    }
    // Set working directory for current chat
    await fetch(`${API_BASE}/${activeChatId}`, {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ working_dir: path })
    });
    await loadChatList();
};

// ── Model selector ──────────────────────────────────────────────────
modelBtn.onclick = (e) => {
    e.stopPropagation();
    modelDropdown.classList.toggle('hidden');
};
document.addEventListener('click', () => modelDropdown.classList.add('hidden'));
modelDropdown.querySelectorAll('.model-option').forEach(btn => {
    btn.onclick = async (e) => {
        e.stopPropagation();
        const newModel = btn.dataset.model;
        modelLabel.textContent = btn.dataset.label;
        modelDropdown.querySelectorAll('.model-option').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        modelDropdown.classList.add('hidden');
        if (activeChatId) {
            await fetch(`${API_BASE}/${activeChatId}`, {
                method: 'PUT',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ model: newModel })
            });
        }
    };
});

// ── Send message ────────────────────────────────────────────────────
async function send() {
    if (sending || !activeChatId) return;
    const userMsg = input.value.trim();
    if (!userMsg) return;

    input.value = '';
    input.style.height = 'auto';
    addUserMsg(userMsg);

    sending = true;
    setLoading(true);

    let thinkingBlock = null;
    let assistantDiv  = null;
    let toolPill      = null;
    let assistantText = '';

    try {
        const resp = await fetch(`${API_BASE}/${activeChatId}/messages`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ message: userMsg })
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);

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
                        if (!thinkingBlock) thinkingBlock = createThinkingBlock();
                        thinkingBlock.body.textContent += ev.text;
                        scrollBottom();
                        break;
                    }
                    case 'text': {
                        if (thinkingBlock) { sealThinking(thinkingBlock); thinkingBlock = null; }
                        if (toolPill) { toolPill.classList.add('done'); toolPill = null; }
                        if (!assistantDiv) {
                            assistantText = '';
                            assistantDiv = createAssistantShell();
                        }
                        assistantText += ev.text;
                        assistantDiv.innerHTML = `<span class="msg-prefix">assistant</span>`
                            + parseMarkdown(assistantText)
                            + '<span class="cursor"></span>';
                        scrollBottom();
                        break;
                    }
                    case 'tool_use': {
                        if (thinkingBlock) { sealThinking(thinkingBlock); thinkingBlock = null; }
                        if (assistantDiv) { sealAssistant(assistantDiv, assistantText); assistantDiv = null; assistantText = ''; }
                        if (toolPill) toolPill.classList.add('done');
                        toolPill = createToolPill(ev.name, ev.args);
                        break;
                    }
                    case 'tool_done': {
                        if (toolPill) {
                            const spinner = toolPill.querySelector('.tool-spinner');
                            if (spinner) spinner.outerHTML = `<span class="tool-check">✓</span>`;
                        }
                        break;
                    }
                    case 'error': {
                        if (thinkingBlock) { sealThinking(thinkingBlock); thinkingBlock = null; }
                        if (!assistantDiv) { assistantText = ''; assistantDiv = createAssistantShell(); }
                        assistantDiv.classList.remove('streaming');
                        assistantDiv.innerHTML = `<span class="msg-prefix">assistant</span><span class="error-msg">⚠ ${escHtml(ev.text)}</span>`;
                        assistantDiv = null;
                        break;
                    }
                    case 'done': {
                        if (thinkingBlock) { sealThinking(thinkingBlock); thinkingBlock = null; }
                        if (assistantDiv)  { sealAssistant(assistantDiv, assistantText); assistantDiv = null; }
                        if (toolPill)      { toolPill.classList.add('done'); toolPill = null; }
                        break;
                    }
                }
            }
        }

        if (thinkingBlock) sealThinking(thinkingBlock);
        if (assistantDiv)  sealAssistant(assistantDiv, assistantText);
        if (toolPill)      toolPill.classList.add('done');

    } catch (e) {
        const d = assistantDiv || createAssistantShell();
        d.classList.remove('streaming');
        d.innerHTML = `<span class="msg-prefix">assistant</span><span class="error-msg">⚠ ${escHtml(e.message)}</span>`;
    }

    sending = false;
    setLoading(false);
    input.focus();
}

// ── Controls ──────────────────────────────────────────────────────────
newChatBtn.onclick = createNewChat;
sendBtn.onclick = send;
menuBtn.onclick = () => sidebar.classList.toggle('collapsed');

input.onkeydown = e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
};
input.oninput = () => {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 120) + 'px';
};

// ── Init ─────────────────────────────────────────────────────────────
async function init() {
    await loadChatList();
    activeChatId = localStorage.getItem('activeChatId');
    if (activeChatId && document.querySelector(`.chat-item[data-id="${activeChatId}"]`)) {
        switchToChat(activeChatId);
    } else if (document.querySelector('.chat-item')) {
        switchToChat(document.querySelector('.chat-item').dataset.id);
    } else {
        createNewChat();
    }
    setLoading(false);
    input.focus();
}

init();