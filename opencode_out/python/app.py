import os
import json
import re
import glob as glob_module
import fnmatch
import requests
from flask import Flask, send_file, request, jsonify, send_from_directory, Response, stream_with_context
from python.config import API_URL, MODEL, HOST, PORT, WORKING_DIR, MAX_TOKENS, COMPACTION_THRESHOLD
from python.compaction import (
    compact_messages,
    build_compacted_messages_for_api,
    estimate_messages_tokens,
    is_overflow,
    split_head_tail,
    generate_summary,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
app = Flask(__name__, template_folder=ROOT, static_folder=ROOT + '/ui')

# ── Per-chat state (replaces single global `history`) ─────────────────
# Each chat_id gets its own history list and compaction summary.
# This allows multiple chats to stream concurrently without clobbering.
chat_histories  = {}   # chat_id -> list of rich turn objects (see turn schema below)
chat_summaries  = {}   # chat_id -> previous compaction summary string
chat_msg_counts = {}   # chat_id -> sequence counter for turn ID generation

# ── Rich turn / message schema ─────────────────────────────────────────
# Each item in chat_histories[chat_id] is ONE of:
#
#   User turn:
#   { "id": "u_<n>", "role": "user", "content": "..." }
#
#   Assistant turn:
#   { "id": "a_<n>", "role": "assistant", "content": "...",
#     "reasoning_content": "..." or null,
#     "tool_calls": [
#       { "id": "tc_<n>", "type": "function",
#         "function": { "name": "...", "arguments": "..." } }
#     ] or []
#   }
#
#   Tool result:
#   { "id": "tr_<n>", "role": "tool",
#     "tool_call_id": "tc_<n>", "content": "..." }
#
# The "id" on every object is unique within the chat so the Pencil overseer
# can reference items by ID and ask the backend to remove them.

working_dir  = WORKING_DIR
working_dirs = []
current_chat_id = None   # kept for compat; not used for history lookup

# ── ID generation ──────────────────────────────────────────────────────
def _next_id(chat_id: str, prefix: str) -> str:
    """Generate a sequential ID unique within a chat, e.g. 'a_7', 'tc_12'."""
    key = f"__seq_{chat_id}"
    n = chat_msg_counts.get(key, 0) + 1
    chat_msg_counts[key] = n
    return f"{prefix}_{n}"

# ── Convert rich history to the flat list the API expects ──────────────
def history_to_api_messages(history: list) -> list:
    """
    Convert our rich turn objects into the OpenAI-compatible messages list.
    - assistant turns: emit reasoning_content field if present (some providers
      use it for context continuity), plus tool_calls if any.
    - tool result turns: emit as role=tool with tool_call_id.
    - user turns: pass through as-is.
    """
    out = []
    for turn in history:
        role = turn.get("role")
        if role == "user":
            out.append({"role": "user", "content": turn.get("content", "")})
        elif role == "assistant":
            msg = {"role": "assistant", "content": turn.get("content", "") or ""}
            rc = turn.get("reasoning_content")
            if rc:
                msg["reasoning_content"] = rc
            tcs = turn.get("tool_calls")
            if tcs:
                msg["tool_calls"] = tcs
            out.append(msg)
        elif role == "tool":
            out.append({
                "role": "tool",
                "tool_call_id": turn.get("tool_call_id", ""),
                "content": turn.get("content", ""),
            })
    return out

# -- Storage dir --
def get_opencode_dir():
    possible_paths = [
        "/data/data/com.opencode.app/files/storage_dir.txt",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "storage_dir.txt"),
    ]
    for storage_file in possible_paths:
        if os.path.isfile(storage_file):
            try:
                with open(storage_file, "r") as f:
                    external_path = f.read().strip()
                if external_path and os.path.isdir(external_path):
                    # external_path already IS the opencode dir (Java writes
                    # /sdcard/opencode into storage_dir.txt, not just /sdcard)
                    os.makedirs(external_path, exist_ok=True)
                    return external_path
            except Exception:
                pass
    base = "/storage/emulated/0"
    if not os.path.isdir(base):
        base = "/sdcard"
    d = os.path.join(base, "opencode")
    os.makedirs(d, exist_ok=True)
    return d


def chats_index_file():
    return os.path.join(get_opencode_dir(), "index.json")

def chat_file(chat_id):
    safe = re.sub(r'[^a-zA-Z0-9_\-]', '_', chat_id)
    return os.path.join(get_opencode_dir(), f"{safe}.json")

def resolve_path(path, cwd=None):
    if not cwd:
        cwd = working_dir
    if not cwd:
        return None
    path = path.strip()
    if os.path.isabs(path):
        return path
    return os.path.normpath(os.path.join(cwd, path))

def is_within_dir(path, dir_path):
    abs_path = os.path.abspath(path)
    abs_dir = os.path.abspath(dir_path)
    return abs_path.startswith(abs_dir + os.sep) or abs_path == abs_dir

# ── Prompts root ───────────────────────────────────────────────────────
# Lives inside the user-accessible opencode dir (e.g. /sdcard/opencode/prompts/)
# so the user can edit system.md and agent .md files directly.
# Goes 3 levels up from app.py: python/ -> opencode_out/ -> project_root/
_BUNDLED_PROMPTS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "prompts")

# ── Hardcoded defaults (used when bundled prompts are missing) ──────────
_DEFAULT_SYSTEM_MD = """You are a coding assistant running on a mobile Android app called OpenCode.

Be direct and concise. No unnecessary preamble, no enthusiasm theater, no filler phrases.
For simple questions, answer in 1-2 sentences. Only elaborate when the task genuinely requires it.
Never start responses with affirmations like "Sure!", "Great!", "Of course!", "Absolutely!" etc."""

_DEFAULT_INDEX_JSON = [
    {"id": "build",   "name": "build",   "description": "Full access — code, write, run commands",     "file": "build.md",   "no_tools": False, "denied_tools": []},
    {"id": "plan",    "name": "plan",    "description": "Read-only analysis — no file writes",          "file": "plan.md",    "no_tools": False, "denied_tools": ["write", "edit", "shell"]},
    {"id": "explore", "name": "explore", "description": "Search & read only — fast codebase nav",       "file": "explore.md", "no_tools": False, "denied_tools": ["write", "edit", "shell", "web_search", "web_fetch"]},
    {"id": "ask",     "name": "ask",     "description": "No tools — pure Q&A, no file access",          "file": "ask.md",     "no_tools": True,  "denied_tools": []},
]

_DEFAULT_AGENT_MDS = {
    "build.md": """You are in BUILD mode — full read/write/execute access.

You can read files, write files, edit files, run shell commands, search the web, and explore GitHub repos.

Rules:
- Fix bugs, write code, and complete tasks end-to-end without asking for permission mid-task.
- Prefer surgical edits over full file rewrites.
- NEVER revert changes you didn't make.
- If a task is ambiguous, make a reasonable assumption, state it briefly, and proceed.
- When coding, show only relevant diffs or final code — not the entire file unless asked.""",

    "plan.md": """You are in PLAN mode — read-only analysis, no writes or shell execution.

You MAY use: read, glob, grep, web_search, web_fetch, github_walk.
You MUST NOT use: write, edit, shell.

Rules:
- Analyze the codebase and produce a clear, numbered action plan.
- State exactly which files need changing and why.
- Do not execute the plan — describe it precisely so it can be handed to build mode.
- Be concise. No padding.""",

    "explore.md": """You are in EXPLORE mode — fast read-only codebase navigation.

You MAY use: read, glob, grep, github_walk.
You MUST NOT use: web_search, web_fetch, write, edit, shell.

Rules:
- Answer structural questions about the codebase quickly.
- Always return exact file paths and line numbers when relevant.
- Do not summarize unnecessarily — show the actual code or path.
- If something isn't found, say so immediately rather than guessing.""",

    "ask.md": """You are in ASK mode — pure question answering, no tools, no file access.

Rules:
- Answer entirely from your own knowledge.
- Never attempt to use any tools.
- If a question requires inspecting code or files, tell the user to switch to build or explore mode.
- Keep answers tight. No filler.""",
}


def get_prompts_dir() -> str:
    base = get_opencode_dir()
    d    = os.path.join(base, "prompts")
    os.makedirs(os.path.join(d, "agents"), exist_ok=True)
    _seed_prompts(d)
    return d


def _seed_prompts(dest: str):
    """Copy bundled prompt files to user dir if they don't exist yet.
    Falls back to hardcoded defaults if the bundled prompts dir is missing."""
    import shutil

    src = _BUNDLED_PROMPTS
    if os.path.isdir(src):
        # Copy from bundled prompts (normal case when APK packages them)
        for root, dirs, files in os.walk(src):
            rel = os.path.relpath(root, src)
            target_dir = os.path.join(dest, rel) if rel != "." else dest
            os.makedirs(target_dir, exist_ok=True)
            for fname in files:
                dst_file = os.path.join(target_dir, fname)
                src_file = os.path.join(root, fname)
                if not os.path.isfile(dst_file):
                    try:
                        shutil.copy2(src_file, dst_file)
                    except Exception:
                        pass
    else:
        # Bundled prompts not found — auto-generate defaults
        _autogenerate_prompts(dest)


def _autogenerate_prompts(dest: str):
    """Write hardcoded default prompts to the user prompts dir."""
    agents_dir = os.path.join(dest, "agents")
    os.makedirs(agents_dir, exist_ok=True)

    system_path = os.path.join(dest, "system.md")
    if not os.path.isfile(system_path):
        try:
            with open(system_path, "w", encoding="utf-8") as f:
                f.write(_DEFAULT_SYSTEM_MD.strip())
        except Exception:
            pass

    index_path = os.path.join(agents_dir, "index.json")
    if not os.path.isfile(index_path):
        try:
            with open(index_path, "w", encoding="utf-8") as f:
                json.dump(_DEFAULT_INDEX_JSON, f, indent=2)
        except Exception:
            pass

    for fname, content in _DEFAULT_AGENT_MDS.items():
        fpath = os.path.join(agents_dir, fname)
        if not os.path.isfile(fpath):
            try:
                with open(fpath, "w", encoding="utf-8") as f:
                    f.write(content.strip())
            except Exception:
                pass


def _load_system_prompt() -> str:
    path = os.path.join(get_prompts_dir(), "system.md")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return _DEFAULT_SYSTEM_MD.strip()


def _load_agents() -> dict:
    agents_dir = os.path.join(get_prompts_dir(), "agents")
    index_path = os.path.join(agents_dir, "index.json")
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            entries = json.load(f)
    except Exception:
        entries = []

    # If still empty after seeding, fall back to hardcoded defaults
    if not entries:
        entries = _DEFAULT_INDEX_JSON

    profiles = {}
    for entry in entries:
        agent_id = entry.get("id", "")
        if not agent_id:
            continue
        md_file = os.path.join(agents_dir, entry.get("file", f"{agent_id}.md"))
        try:
            with open(md_file, "r", encoding="utf-8") as f:
                system_suffix = f.read().strip()
        except Exception:
            # File missing — use hardcoded default for this agent
            fname = entry.get("file", f"{agent_id}.md")
            system_suffix = _DEFAULT_AGENT_MDS.get(fname, f"You are in {agent_id.upper()} mode.")

        profiles[agent_id] = {
            "name":          entry.get("name", agent_id),
            "description":   entry.get("description", ""),
            "system_suffix": system_suffix,
            "no_tools":      entry.get("no_tools", False),
            "denied_tools":  entry.get("denied_tools", []),
        }

    return profiles

SYSTEM_PROMPT_BASE = _load_system_prompt()
AGENT_PROFILES     = _load_agents()

def reload_agents():
    global SYSTEM_PROMPT_BASE, AGENT_PROFILES
    SYSTEM_PROMPT_BASE = _load_system_prompt()
    AGENT_PROFILES     = _load_agents()

def get_tools_for_agent(agent_name: str) -> list:
    fallback = list(AGENT_PROFILES.values())[0] if AGENT_PROFILES else {}
    profile  = AGENT_PROFILES.get(agent_name, fallback)
    if profile.get("no_tools", False):
        return []
    denied = profile.get("denied_tools", [])
    return [t for t in TOOLS if t["function"]["name"] not in denied]

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web for current information, news, and facts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query"},
                    "num_results": {"type": "integer", "description": "Number of results to return (default 8)", "default": 8}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "Fetch and read the text content of a webpage given its URL.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The URL to fetch"}
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "glob",
            "description": "Find files matching a pattern in the working directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern (e.g., **/*.js)"},
                    "path": {"type": "string", "description": "Base directory (defaults to working directory)"}
                },
                "required": ["pattern"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search file contents for a pattern.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex pattern to search for"},
                    "path": {"type": "string", "description": "Directory or file to search (defaults to working directory)"},
                    "include": {"type": "string", "description": "File pattern to include (*.js, *.py, etc.)"}
                },
                "required": ["pattern"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read",
            "description": "Read the contents of a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filePath": {"type": "string", "description": "Path to the file (relative to working directory)"},
                    "offset": {"type": "integer", "description": "Line number to start reading from (1-indexed)"},
                    "limit": {"type": "integer", "description": "Maximum number of lines to read"}
                },
                "required": ["filePath"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write",
            "description": "Write content to a file (creates or overwrites).",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "Content to write to the file"},
                    "filePath": {"type": "string", "description": "Path to the file (relative to working directory)"}
                },
                "required": ["content", "filePath"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "github_walk",
            "description": "Explore a GitHub repository without guessing URLs. Use action='tree' to get the full file/folder structure of any public repo. Use action='read' to get the raw contents of a specific file. Provide repo as 'owner/repo' (e.g. 'torvalds/linux'). Optionally specify a branch (defaults to main/master).",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "description": "Use 'tree' to list all files in the repo, 'read' to fetch a specific file's contents"},
                    "repo": {"type": "string", "description": "GitHub repo in 'owner/repo' format, e.g. 'facebook/react'"},
                    "file_path": {"type": "string", "description": "Path to file within repo (required for action='read'), e.g. 'src/index.js'"},
                    "branch": {"type": "string", "description": "Branch name (optional, auto-detected if omitted)"}
                },
                "required": ["action", "repo"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "shell",
            "description": "Run a shell command using toybox (a lightweight Unix toolbox). Supports common commands: ls, cat, cp, mv, rm, mkdir, rmdir, find, grep, sed, awk, sort, uniq, head, tail, wc, echo, pwd, chmod, touch, diff, tar, gzip, and more. Commands run in the working directory. Use this for file operations, text processing, or exploring the filesystem.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to run (e.g., 'ls -la', 'find . -name *.py', 'cat file.txt | grep error')"},
                    "cwd": {"type": "string", "description": "Working directory override (defaults to project working directory)"}
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "edit",
            "description": "Replace a specific string in a file with new content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filePath": {"type": "string", "description": "Path to the file (relative to working directory)"},
                    "oldString": {"type": "string", "description": "Text to find and replace"},
                    "newString": {"type": "string", "description": "Text to replace it with"},
                    "replaceAll": {"type": "boolean", "description": "Replace all occurrences (default false)", "default": False}
                },
                "required": ["filePath", "oldString", "newString"]
            }
        }
    }
]

def strip_html(html):
    html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'<!--.*?-->', '', html, flags=re.DOTALL)
    html = re.sub(r'<br\s*/?>', '\n', html, flags=re.IGNORECASE)
    html = re.sub(r'<p[^>]*>', '\n', html, flags=re.IGNORECASE)
    html = re.sub(r'<h[1-6][^>]*>', '\n## ', html, flags=re.IGNORECASE)
    html = re.sub(r'<li[^>]*>', '\n- ', html, flags=re.IGNORECASE)
    html = re.sub(r'<[^>]+>', '', html)
    html = html.replace('&nbsp;', ' ').replace('&amp;', '&')
    html = html.replace('&lt;', '<').replace('&gt;', '>').replace('&quot;', '"')
    html = re.sub(r'\n{3,}', '\n\n', html)
    html = re.sub(r'[ \t]+', ' ', html)
    return html.strip()

def _android_webview_fetch(url):
    try:
        from com.opencode.app import MainActivity
        activity = MainActivity.instance
        if activity is None:
            return None
        html = activity.fetchUrlSync(url)
        if html and len(html) > 200:
            return html
    except Exception:
        pass
    return None


def websearch(query, num_results=8):
    import urllib.parse
    encoded = urllib.parse.quote(query)

    html = _android_webview_fetch(f"https://html.duckduckgo.com/html/?q={encoded}")
    if html:
        titles   = re.findall(r'class="result__a"[^>]*>(.*?)</a>', html, re.DOTALL)
        urls     = re.findall(r'class="result__url"[^>]*>(.*?)</span>', html, re.DOTALL)
        snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', html, re.DOTALL)
        titles   = [re.sub(r'<[^>]+>', '', t).strip() for t in titles]
        urls     = [u.strip() for u in urls]
        snippets = [re.sub(r'<[^>]+>', '', s).strip() for s in snippets]
        results  = list(zip(titles, urls, snippets))[:num_results]
        if results:
            lines = [f"Search results for: **{query}**\n"]
            for i, (title, url, snippet) in enumerate(results, 1):
                lines.append(f"{i}. **{title}**")
                if url:     lines.append(f"   {url}")
                if snippet: lines.append(f"   {snippet}")
                lines.append("")
            return "\n".join(lines)

    try:
        resp = requests.get(
            f"https://s.jina.ai/?q={encoded}",
            headers={"Accept": "text/plain", "User-Agent": "Mozilla/5.0"},
            timeout=30
        )
        if resp.status_code == 200 and resp.text.strip():
            return resp.text.strip()[:30000]
    except Exception:
        pass

    try:
        resp = requests.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"},
            timeout=30
        )
        titles   = re.findall(r'class="result__a"[^>]*>(.*?)</a>', resp.text, re.DOTALL)
        urls     = re.findall(r'class="result__url"[^>]*>(.*?)</span>', resp.text, re.DOTALL)
        snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', resp.text, re.DOTALL)
        titles   = [re.sub(r'<[^>]+>', '', t).strip() for t in titles]
        urls     = [u.strip() for u in urls]
        snippets = [re.sub(r'<[^>]+>', '', s).strip() for s in snippets]
        results  = list(zip(titles, urls, snippets))[:num_results]
        if not results:
            return f"No results found for: {query}"
        lines = [f"Search results for: **{query}**\n"]
        for i, (title, url, snippet) in enumerate(results, 1):
            lines.append(f"{i}. **{title}**")
            if url:     lines.append(f"   {url}")
            if snippet: lines.append(f"   {snippet}")
            lines.append("")
        return "\n".join(lines)
    except Exception as e:
        return f"Search error: {e}"


def webfetch(url):
    html = _android_webview_fetch(url)
    if html:
        return strip_html(html)[:30000]

    try:
        resp = requests.get(
            f"https://r.jina.ai/{url}",
            headers={"Accept": "text/plain", "User-Agent": "Mozilla/5.0"},
            timeout=30
        )
        if resp.status_code == 200 and resp.text.strip():
            return resp.text.strip()[:30000]
    except Exception:
        pass

    try:
        resp = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            timeout=30
        )
        return strip_html(resp.text)[:30000]
    except Exception as e:
        return f"Fetch error: {e}"



def tool_glob(pattern, path=None):
    global working_dir, working_dirs
    dirs = working_dirs if working_dirs else ([working_dir] if working_dir else [])
    if not dirs:
        return "No working directory set."
    base_dir = dirs[0]
    base = resolve_path(path, base_dir) if path else base_dir
    if not any(is_within_dir(base, d) for d in dirs):
        return f"Error: Path '{path}' is outside all working directories"
    try:
        full_pattern = os.path.join(base, pattern)
        matches = glob_module.glob(full_pattern, recursive=True)
        matches = [m for m in matches if any(is_within_dir(m, d) for d in dirs)]
        if not matches:
            return f"No files matching '{pattern}'"
        rel_matches = [os.path.relpath(m, base) for m in matches[:100]]
        return "Found files:\n" + "\n".join(rel_matches)
    except Exception as e:
        return f"Glob error: {e}"


def tool_grep(pattern, path=None, include=None):
    global working_dir
    if not working_dir:
        return "No working directory set. Use /set_working_dir to set it first."
    base = resolve_path(path) if path else working_dir
    if not is_within_dir(base, working_dir):
        return f"Error: Path '{path}' is outside working directory"
    try:
        regex = re.compile(pattern)
    except re.error as e:
        return f"Invalid regex: {e}"
    results = []
    try:
        for root, dirs, files in os.walk(base):
            if not is_within_dir(root, working_dir):
                continue
            for name in files:
                if include and not fnmatch.fnmatch(name, include):
                    continue
                fpath = os.path.join(root, name)
                try:
                    with open(fpath, 'r', encoding='utf-8', errors='ignore') as f:
                        for i, line in enumerate(f, 1):
                            if regex.search(line):
                                rel = os.path.relpath(fpath, base)
                                results.append(f"{rel}:{i}: {line.rstrip()}")
                                if len(results) >= 100:
                                    break
                except Exception:
                    pass
            if len(results) >= 100:
                break
    except Exception as e:
        return f"Grep error: {e}"
    if not results:
        return f"No matches for '{pattern}'"
    return "Matches:\n" + "\n".join(results[:100])


def tool_read(filePath, offset=None, limit=None):
    global working_dir
    if not working_dir:
        return "No working directory set. Use /set_working_dir to set it first."
    full_path = resolve_path(filePath)
    if not full_path or not is_within_dir(full_path, working_dir):
        return f"Error: Path '{filePath}' is outside working directory"
    if not os.path.isfile(full_path):
        return f"Error: File not found: {filePath}"
    try:
        with open(full_path, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
        start = (offset - 1) if offset else 0
        end = len(lines) if limit is None else start + limit
        lines = lines[start:end]
        total = len(lines)
        content = "".join(lines)
        prefix = f"Lines {start+1}-{end}:\n" if offset or limit else ""
        if len(content) > 50000:
            content = content[:50000] + f"\n... (truncated at 50000 chars)"
        return prefix + content
    except Exception as e:
        return f"Read error: {e}"


def tool_write(content, filePath):
    global working_dir
    if not working_dir:
        return "No working directory set. Use /set_working_dir to set it first."
    full_path = resolve_path(filePath)
    if not full_path or not is_within_dir(full_path, working_dir):
        return f"Error: Path '{filePath}' is outside working directory"
    try:
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, 'w', encoding='utf-8') as f:
            f.write(content)
        return f"Written to {filePath} ({len(content)} chars)"
    except Exception as e:
        return f"Write error: {e}"


def tool_edit(filePath, oldString, newString, replaceAll=False):
    global working_dir
    if not working_dir:
        return "No working directory set. Use /set_working_dir to set it first."
    full_path = resolve_path(filePath)
    if not full_path or not is_within_dir(full_path, working_dir):
        return f"Error: Path '{filePath}' is outside working directory"
    if not os.path.isfile(full_path):
        return f"Error: File not found: {filePath}"
    try:
        with open(full_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        if replaceAll:
            new_content = content.replace(oldString, newString)
            count = content.count(oldString)
        else:
            if oldString not in content:
                return "Text not found in file"
            new_content = content.replace(oldString, newString, 1)
            count = 1
        with open(full_path, 'w', encoding='utf-8') as f:
            f.write(new_content)
        return f"Replaced {count} occurrence(s) in {filePath}"
    except Exception as e:
        return f"Edit error: {e}"


def tool_github_walk(action, repo, file_path=None, branch=None):
    headers = {"User-Agent": "opencode-app", "Accept": "application/vnd.github+json"}

    if not branch:
        try:
            r = requests.get(f"https://api.github.com/repos/{repo}", headers=headers, timeout=15)
            if r.status_code == 404:
                return f"Repo not found: {repo}"
            branch = r.json().get("default_branch", "main")
        except Exception as e:
            return f"GitHub API error: {e}"

    if action == "tree":
        try:
            r = requests.get(
                f"https://api.github.com/repos/{repo}/git/trees/{branch}?recursive=1",
                headers=headers, timeout=20
            )
            if r.status_code != 200:
                return f"Failed to get tree: {r.status_code} {r.text[:200]}"
            data = r.json()
            items = data.get("tree", [])
            truncated = data.get("truncated", False)
            files = [item["path"] for item in items if item["type"] == "blob"]
            total = len(files)
            MAX_FILES = 400
            capped = total > MAX_FILES
            if capped:
                files = files[:MAX_FILES]
            lines = [f"# {repo} @ {branch}  ({total} files total)\n"]
            lines += files
            if capped:
                lines.append(f"\n... showing {MAX_FILES}/{total} files. Use action='read' with a specific file_path to read any file.")
            if truncated:
                lines.append("GitHub truncated the tree (repo is very large). Results may be incomplete.")
            return "\n".join(lines)
        except Exception as e:
            return f"GitHub tree error: {e}"

    elif action == "read":
        if not file_path:
            return "file_path is required for action='read'"
        try:
            raw_url = f"https://raw.githubusercontent.com/{repo}/{branch}/{file_path}"
            r = requests.get(raw_url, headers={"User-Agent": "opencode-app"}, timeout=20)
            if r.status_code == 404:
                return f"File not found: {file_path} on branch {branch}"
            if r.status_code != 200:
                return f"Failed to read file: {r.status_code}"
            content = r.text
            if len(content) > 20000:
                content = content[:20000] + f"\n\n... (truncated, {len(r.text)} chars total)"
            return f"# {repo}/{file_path}\n\n{content}"
        except Exception as e:
            return f"GitHub read error: {e}"

    return f"Unknown action: {action}"


def _get_toybox_path():
    candidates = [
        "/data/data/com.opencode.app/files/toybox_path.txt",
        "/data/user/0/com.opencode.app/files/toybox_path.txt",
    ]
    for p in candidates:
        if os.path.isfile(p):
            try:
                with open(p) as f:
                    path = f.read().strip()
                if path and os.path.isfile(path) and os.access(path, os.X_OK):
                    return path
            except Exception:
                pass
    for p in ["/system/bin/toybox", "/usr/bin/toybox", "/bin/toybox"]:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def tool_shell(command, cwd=None):
    import subprocess
    import shlex
    global working_dir

    toybox = _get_toybox_path()
    if not toybox:
        return "Error: toybox binary not available."

    run_cwd = cwd or working_dir or None
    if run_cwd and not os.path.isdir(run_cwd):
        run_cwd = None

    try:
        parts = shlex.split(command.strip())
    except ValueError as e:
        return f"Parse error: {e}"

    if not parts:
        return "Empty command."

    if parts[0] == "toybox":
        parts = parts[1:]
    if not parts:
        parts = ["help"]

    if parts[0] in ("--help", "-h", "help"):
        parts = ["help"]

    try:
        result = subprocess.run(
            [toybox] + parts,
            cwd=run_cwd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = result.stdout
        if result.stderr:
            output += ("\n" if output else "") + result.stderr
        if not output.strip():
            return f"(exit code {result.returncode}, no output)"
        if len(output) > 20000:
            output = output[:20000] + "\n... (truncated)"
        return output
    except subprocess.TimeoutExpired:
        return "Error: command timed out after 30 seconds"
    except Exception as e:
        return f"Shell error: {e}"


def run_tool(name, args):
    if name == "web_search":
        return websearch(args.get("query", ""), args.get("num_results", 8))
    elif name == "web_fetch":
        return webfetch(args.get("url", ""))
    elif name == "glob":
        return tool_glob(args.get("pattern", "*"), args.get("path"))
    elif name == "grep":
        return tool_grep(args.get("pattern", ""), args.get("path"), args.get("include"))
    elif name == "read":
        return tool_read(args.get("filePath", ""), args.get("offset"), args.get("limit"))
    elif name == "write":
        return tool_write(args.get("content", ""), args.get("filePath", ""))
    elif name == "github_walk":
        return tool_github_walk(args.get("action", "tree"), args.get("repo", ""), args.get("file_path"), args.get("branch"))
    elif name == "edit":
        return tool_edit(args.get("filePath", ""), args.get("oldString", ""), args.get("newString", ""), args.get("replaceAll", False))
    elif name == "shell":
        return tool_shell(args.get("command", ""), args.get("cwd"))
    return f"Unknown tool: {name}"




@app.route("/")
def home():
    return send_file(os.path.join(ROOT, "index.html"))

@app.route("/ui/<path:filename>")
def static_files(filename):
    return send_from_directory(ROOT + '/ui', filename)

@app.route("/working_dir", methods=["GET"])
def get_working_dir():
    return jsonify({"working_dir": working_dir})

@app.route("/working_dir", methods=["POST"])
def set_working_dir():
    global working_dir
    data = request.json
    new_dir = data.get("working_dir", "")
    if new_dir and os.path.isdir(new_dir):
        working_dir = new_dir
        return jsonify({"status": "ok", "working_dir": working_dir})
    elif new_dir:
        return jsonify({"status": "error", "message": "Invalid directory"})
    else:
        working_dir = ""
        return jsonify({"status": "ok", "working_dir": ""})

@app.route("/ping", methods=["GET"])
def ping():
    return jsonify({"status": "ok"})

@app.route("/ls", methods=["GET"])
def list_dir():
    global working_dir
    path = request.args.get("path", working_dir)
    if not working_dir:
        return jsonify({"error": "No working directory set"})
    full_path = resolve_path(path) if path else working_dir
    if not is_within_dir(full_path, working_dir):
        return jsonify({"error": "Path outside working directory"})
    if not os.path.isdir(full_path):
        return jsonify({"error": f"Not a directory: {path}"})
    if not os.access(full_path, os.R_OK):
        return jsonify({"error": f"Permission denied: {path}", "permission_error": True})
    try:
        items = []
        for name in os.listdir(full_path):
            fpath = os.path.join(full_path, name)
            items.append({
                "name": name,
                "is_dir": os.path.isdir(fpath),
                "path": os.path.relpath(fpath, working_dir)
            })
        items.sort(key=lambda x: (not x["is_dir"], x["name"]))
        return jsonify({"items": items, "cwd": working_dir})
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route("/chat", methods=["POST"])
def chat():
    global working_dir, working_dirs
    data = request.json
    user_msg  = data.get("message", "")
    model     = data.get("model", MODEL)
    agent_name = data.get("agent", "build")
    # Use the chat_id sent by the frontend to look up the correct per-chat history.
    # Falls back to "default" so old clients still work.
    chat_id   = data.get("chat_id", "default")

    # Ensure this chat has an entry in our per-chat dicts
    if chat_id not in chat_histories:
        chat_histories[chat_id] = []

    # Work on a reference to this chat's history list
    history = chat_histories[chat_id]

    # Dedup guard: if the last turn is already this exact user message (e.g. from
    # a switch_chat reload that already seeded the history), don't append again.
    # This prevents the model seeing duplicate user turns and looping.
    last = history[-1] if history else None
    if not (last and last.get("role") == "user" and last.get("content") == user_msg):
        history.append({"id": _next_id(chat_id, "u"), "role": "user", "content": user_msg})

    dirs = working_dirs if working_dirs else ([working_dir] if working_dir else [])
    agent_profile = AGENT_PROFILES.get(agent_name) or (list(AGENT_PROFILES.values())[0] if AGENT_PROFILES else {})
    agent_suffix  = agent_profile.get("system_suffix", "")
    active_tools  = get_tools_for_agent(agent_name)

    from datetime import datetime
    now_str = datetime.now().strftime("%A, %B %d, %Y %H:%M")
    base_prompt   = (SYSTEM_PROMPT_BASE + "\n\n") if SYSTEM_PROMPT_BASE else ""
    datetime_line = f"Current date/time: {now_str}\n\n"

    if dirs:
        hints = []
        for d in dirs:
            try:
                files = sorted(os.listdir(d))[:30]
                hints.append(f"Folder: {d}\nContents:\n" + "\n".join(files))
            except Exception:
                hints.append(f"Folder: {d} (unreadable)")
        dir_info = "\n\n".join(hints)
        system_msg = {"role": "system", "content": f"{base_prompt}{datetime_line}You have access to {len(dirs)} project folder(s):\n\n{dir_info}\n\nAlways use tools relative to these directories. Never navigate above them.\n\n---\n{agent_suffix}"}
    else:
        system_msg = {"role": "system", "content": f"{base_prompt}{datetime_line}No working directory set. Ask user to select a project folder first.\n\n---\n{agent_suffix}"}

    def generate():
        import time
        full_content = ""
        full_reasoning = ""
        last_heartbeat = time.time()

        # Use per-chat compaction summary
        previous_summary = chat_summaries.get(chat_id)

        # Convert rich history to flat API messages
        flat_history = history_to_api_messages(list(history))

        compacted, new_summary, did_compact = compact_messages(
            messages=flat_history,
            system_messages=[system_msg],
            api_url=API_URL,
            model=model,
            previous_summary=previous_summary,
            context_limit=COMPACTION_THRESHOLD,
            max_output_tokens=MAX_TOKENS,
        )
        if did_compact:
            chat_summaries[chat_id] = new_summary
            yield f"data: {json.dumps({'type': 'compaction', 'text': 'Context compacted.'})}\n\n"

        messages = [system_msg] + build_compacted_messages_for_api(compacted)

        # Accumulate all rich turns produced this response so we can
        # append them to history atomically at the end.
        new_rich_turns = []

        for _round in range(1000):
            payload = {
                "model": model,
                "messages": messages,
                "stream": True,
                "max_tokens": MAX_TOKENS
            }
            if active_tools:
                payload["tools"] = active_tools
                payload["tool_choice"] = "auto"

            try:
                api_resp = requests.post(API_URL, json=payload, stream=True, timeout=600)
                api_resp.raise_for_status()
            except Exception as e:
                yield f"data: {json.dumps({'type': 'error', 'text': str(e)})}\n\n"
                return

            tool_calls_acc = {}
            round_content = ""
            round_reasoning = ""

            for raw in api_resp.iter_lines():
                if not raw:
                    continue
                if time.time() - last_heartbeat > 8:
                    yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"
                    last_heartbeat = time.time()
                line = raw.decode("utf-8", errors="replace")
                if not line.startswith("data: "):
                    continue
                data_str = line[6:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                choice = (chunk.get("choices") or [{}])[0]
                delta = choice.get("delta", {})

                thinking_chunk = delta.get("reasoning_content") or delta.get("reasoning") or delta.get("thinking") or ""
                if thinking_chunk:
                    round_reasoning += thinking_chunk
                    full_reasoning += thinking_chunk
                    yield f"data: {json.dumps({'type': 'thinking', 'text': thinking_chunk})}\n\n"

                content_chunk = delta.get("content") or ""
                if content_chunk:
                    round_content += content_chunk
                    full_content += content_chunk
                    yield f"data: {json.dumps({'type': 'text', 'text': content_chunk})}\n\n"

                for tc_delta in delta.get("tool_calls", []):
                    idx = tc_delta.get("index", 0)
                    if idx not in tool_calls_acc:
                        tool_calls_acc[idx] = {"id": "", "name": "", "arguments": ""}
                    if tc_delta.get("id"):
                        tool_calls_acc[idx]["id"] = tc_delta["id"]
                    fn = tc_delta.get("function", {})
                    if fn.get("name"):
                        tool_calls_acc[idx]["name"] += fn["name"]
                    if fn.get("arguments"):
                        tool_calls_acc[idx]["arguments"] += fn["arguments"]

            if not tool_calls_acc:
                # No more tool calls — this is the final assistant turn
                rich_asst = {
                    "id": _next_id(chat_id, "a"),
                    "role": "assistant",
                    "content": full_content,
                    "reasoning_content": full_reasoning or None,
                    "tool_calls": [],
                }
                new_rich_turns.append(rich_asst)
                break

            # Build tool-call list; reuse provider-issued IDs
            tc_list = []
            for idx in sorted(tool_calls_acc.keys()):
                tc = tool_calls_acc[idx]
                tc_id = tc["id"] or _next_id(chat_id, "tc")
                tc_list.append({
                    "id": tc_id,
                    "type": "function",
                    "function": {"name": tc["name"], "arguments": tc["arguments"]}
                })

            # Rich assistant turn (intermediate — has tool_calls)
            rich_asst = {
                "id": _next_id(chat_id, "a"),
                "role": "assistant",
                "content": round_content or "",
                "reasoning_content": round_reasoning or None,
                "tool_calls": tc_list,
            }
            new_rich_turns.append(rich_asst)

            # Flat message for next API round
            flat_asst = {"role": "assistant", "tool_calls": tc_list}
            if round_content:
                flat_asst["content"] = round_content
            if round_reasoning:
                flat_asst["reasoning_content"] = round_reasoning
            messages.append(flat_asst)

            for tc in tc_list:
                fn_name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    args = {}

                yield f"data: {json.dumps({'type': 'tool_use', 'name': fn_name, 'args': args})}\n\n"
                result = run_tool(fn_name, args)
                yield f"data: {json.dumps({'type': 'tool_done', 'name': fn_name})}\n\n"

                # Rich tool-result turn
                tr_turn = {
                    "id": _next_id(chat_id, "tr"),
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result,
                }
                new_rich_turns.append(tr_turn)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result,
                })

        if new_rich_turns:
            for t in new_rich_turns:
                history.append(t)

            if len(history) > 40:
                chat_histories[chat_id] = history[-40:]
            else:
                chat_histories[chat_id] = history


        yield f"data: {json.dumps({'type': 'history_update', 'history': chat_histories[chat_id]})}\n\n"

        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return Response(
        stream_with_context(generate()),
        content_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive"
        }
    )


@app.route("/clear", methods=["POST"])
def clear():
    data = request.json or {}
    chat_id = data.get("chat_id", "default")
    chat_histories.pop(chat_id, None)
    chat_summaries.pop(chat_id, None)
    return jsonify({"status": "cleared"})


@app.route("/compact", methods=["POST"])
def manual_compact():
    """
    Manual 'Compress Chat' triggered by the user from the ⋯ menu.
    Force-runs the compaction summarizer immediately — ignores token threshold.
    """
    data = request.json or {}
    chat_id = data.get("chat_id", "default")
    model   = data.get("model", MODEL)

    if chat_id not in chat_histories or not chat_histories[chat_id]:
        return jsonify({"status": "ok", "compacted": False, "history": []})

    history = chat_histories.get(chat_id, [])
    flat    = history_to_api_messages(history)
    previous_summary = chat_summaries.get(chat_id)

    # Split head/tail and generate summary unconditionally (bypass is_overflow check)
    head, tail = split_head_tail(flat, COMPACTION_THRESHOLD, MAX_TOKENS)
    if not head:
        return jsonify({"status": "ok", "compacted": False, "history": history})

    summary = generate_summary(API_URL, model, head, previous_summary)
    if not summary:
        return jsonify({"status": "error", "message": "Summary generation failed", "history": history})

    chat_summaries[chat_id] = summary
    compaction_marker = {
        "role": "user",
        "content": f"[Context compacted]\n\n{summary}",
        "_compaction": True,
    }
    compacted_flat = [compaction_marker] + tail
    chat_histories[chat_id] = build_compacted_messages_for_api(compacted_flat)

    return jsonify({
        "status": "ok",
        "compacted": True,
        "history": chat_histories[chat_id],
    })



@app.route("/working_dirs", methods=["POST"])
def set_working_dirs():
    global working_dirs, working_dir
    data = request.json
    dirs = data.get("working_dirs", [])
    valid = [d for d in dirs if d and os.path.isdir(d)]
    invalid = [d for d in dirs if d and not os.path.isdir(d)]
    working_dirs = valid
    working_dir = valid[0] if valid else ""
    return jsonify({"status": "ok", "working_dirs": working_dirs, "invalid_dirs": invalid})


@app.route("/switch_chat", methods=["POST"])
def switch_chat():
    global current_chat_id
    data = request.json
    chat_id = data.get("chat_id")
    current_chat_id = chat_id
    # Seed per-chat history from the saved data the frontend sends.
    # This keeps the backend in sync when loading persisted chats.
    if chat_id:
        raw_history = data.get("history", [])
        # Strip any _pending preview turns written by the frontend before the stream completed.
        # Real turns from history_update never have _pending set.
        chat_histories[chat_id] = [t for t in raw_history if not t.get("_pending")]
        if "summary" in data:
            chat_summaries[chat_id] = data["summary"]
        # Restore ID sequence counter from max existing ID number
        max_seq = 0
        for t in chat_histories[chat_id]:
            tid = t.get("id", "")
            parts = tid.split("_")
            if len(parts) == 2 and parts[1].isdigit():
                max_seq = max(max_seq, int(parts[1]))
        seq_key = f"__seq_{chat_id}"
        chat_msg_counts[seq_key] = max(chat_msg_counts.get(seq_key, 0), max_seq)
    return jsonify({"status": "ok"})


@app.route("/storage_dir", methods=["GET"])
def storage_dir():
    return jsonify({"path": get_opencode_dir()})


@app.route("/save_chats", methods=["POST"])
def save_chats():
    data = request.json
    chats = data.get("chats", [])
    active_id = data.get("activeChatId")
    try:
        odir = get_opencode_dir()
        for chat in chats:
            cid = chat.get("id", "")
            if not cid:
                continue
            with open(chat_file(cid), "w", encoding="utf-8") as f:
                json.dump(chat, f, ensure_ascii=False, indent=2)
        index = {
            "activeChatId": active_id,
            "chatIds": [c["id"] for c in chats if c.get("id")]
        }
        with open(chats_index_file(), "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False, indent=2)
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


@app.route("/load_chats", methods=["GET"])
def load_chats():
    try:
        with open(chats_index_file(), "r", encoding="utf-8") as f:
            index = json.load(f)
        chats = []
        loaded_ids = set()
        for cid in index.get("chatIds", []):
            try:
                with open(chat_file(cid), "r", encoding="utf-8") as f:
                    chats.append(json.load(f))
                loaded_ids.add(cid)
            except Exception:
                # File was deleted externally — purge in-memory state too
                chat_histories.pop(cid, None)
                chat_summaries.pop(cid, None)
        # Purge any in-memory chats not in the index at all
        for cid in list(chat_histories.keys()):
            if cid not in loaded_ids:
                chat_histories.pop(cid, None)
                chat_summaries.pop(cid, None)
        active = index.get("activeChatId")
        if active not in loaded_ids:
            active = chats[0]["id"] if chats else None
        return jsonify({"chats": chats, "activeChatId": active})
    except FileNotFoundError:
        chat_histories.clear()
        chat_summaries.clear()
        return jsonify({"chats": [], "activeChatId": None})
    except Exception as e:
        return jsonify({"chats": [], "activeChatId": None, "error": str(e)})


@app.route("/delete_chat", methods=["POST"])
def delete_chat():
    data = request.json
    cid = data.get("chat_id", "")
    if not cid:
        return jsonify({"status": "error", "message": "No chat_id"})
    # Also clean up in-memory state for deleted chat
    chat_histories.pop(cid, None)
    chat_summaries.pop(cid, None)
    try:
        path = chat_file(cid)
        if os.path.isfile(path):
            os.remove(path)
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


@app.route("/agents", methods=["GET"])
def list_agents():
    agents_list = [
        {
            "id":          agent_id,
            "name":        profile.get("name", agent_id),
            "description": profile.get("description", ""),
        }
        for agent_id, profile in AGENT_PROFILES.items()
    ]
    return jsonify({"agents": agents_list})


@app.route("/prompts_dir", methods=["GET"])
def prompts_dir_route():
    return jsonify({"path": get_prompts_dir()})


@app.route("/reload_agents", methods=["POST"])
def reload_agents_route():
    reload_agents()
    return jsonify({"status": "ok", "agents": list(AGENT_PROFILES.keys())})


if __name__ == "__main__":
    print(f"OpenCode -- http://localhost:{PORT}")
    app.run(host=HOST, port=PORT, debug=True, threaded=True)
