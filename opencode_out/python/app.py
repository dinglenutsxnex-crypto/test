import os
import json
import re
import glob as glob_module
import fnmatch
import requests
from flask import Flask, send_file, request, jsonify, send_from_directory, Response, stream_with_context
from python.config import API_URL, MODEL, HOST, PORT, WORKING_DIR

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
app = Flask(__name__, template_folder=ROOT, static_folder=ROOT + '/ui')

history = []
working_dir = WORKING_DIR   # kept for compat; primary dirs below
working_dirs = []           # list of active working dirs (multiple folders)
current_chat_id = None

# -- Storage dir (external storage ~/opencode or app internal as fallback) --
def get_opencode_dir():
    # Check for storage path file written by Android Java
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
                    d = os.path.join(external_path, "opencode")
                    os.makedirs(d, exist_ok=True)
                    return d
            except Exception:
                pass
    
    # Fallback to /storage/emulated/0/opencode on external storage
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
    },
    {
        "type": "function",
        "function": {
            "name": "exec_busybox",
            "description": "Execute a shell command using BusyBox on the Android device. Provides access to 300+ Unix commands: ls, cp, mv, rm, mkdir, chmod, grep, sed, awk, cat, head, tail, wc, wget, curl, ping, ps, kill, df, du, tar, gzip, zip and many more. Use this for file operations, text processing, system inspection, and general shell scripting.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command to run (e.g., 'ls -la /sdcard', 'df -h', 'ps aux')"},
                    "cwd": {"type": "string", "description": "Working directory for the command (optional, defaults to app files dir)"}
                },
                "required": ["command"]
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


def websearch(query, num_results=8):
    try:
        resp = requests.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"},
            timeout=30
        )
        titles = re.findall(r'class="result__a"[^>]*>(.*?)</a>', resp.text, re.DOTALL)
        urls = re.findall(r'class="result__url"[^>]*>(.*?)</span>', resp.text, re.DOTALL)
        snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', resp.text, re.DOTALL)
        titles = [re.sub(r'<[^>]+>', '', t).strip() for t in titles]
        urls = [u.strip() for u in urls]
        snippets = [re.sub(r'<[^>]+>', '', s).strip() for s in snippets]
        results = list(zip(titles, urls, snippets))[:num_results]
        if not results:
            return f"No results found for: {query}"
        lines = [f"Search results for: **{query}**\n"]
        for i, (title, url, snippet) in enumerate(results, 1):
            lines.append(f"{i}. **{title}**")
            if url: lines.append(f"   {url}")
            if snippet: lines.append(f"   {snippet}")
            lines.append("")
        return "\n".join(lines)
    except Exception as e:
        return f"Search error: {e}"


def webfetch(url):
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


def _find_busybox():
    """Find libexec.so (busybox) using Chaquopy android API — no guessing needed."""
    # Use Chaquopy's built-in android module to ask Android directly
    # for the native library directory. This is always correct.
    try:
        from android.content import Context
        from android import activity
        ctx = activity.getApplicationContext()
        native_lib_dir = ctx.getApplicationInfo().nativeLibraryDir
        candidate = os.path.join(native_lib_dir, "libexec.so")
        if os.path.isfile(candidate):
            return candidate
    except Exception as e:
        pass

    return None


def tool_exec_busybox(command, cwd=None):
    """Execute a command using the bundled BusyBox binary."""
    import subprocess

    busybox_path = _find_busybox()

    if not busybox_path:
        return (
            "BusyBox not found. Visit http://localhost:5000/debug_busybox for diagnostics."
        )

    if not os.access(busybox_path, os.X_OK):
        try:
            os.chmod(busybox_path, 0o755)
        except Exception as e:
            return (
                f"BusyBox found at {busybox_path} but is not executable and chmod failed: {e}\n"
                "The native library dir should always be exec-allowed — "
                "verify libexec.so is in jniLibs/arm64-v8a/ and the app was freshly installed."
            )

    work_dir = cwd if cwd and os.path.isdir(cwd) else os.path.dirname(busybox_path)

    try:
        result = subprocess.run(
            [busybox_path, "sh", "-c", command],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=work_dir
        )
        output = ""
        if result.stdout:
            output += result.stdout
        if result.stderr:
            output += result.stderr
        if result.returncode != 0:
            output += f"\n[exit code: {result.returncode}]"
        return output.strip() if output.strip() else f"[Command completed with exit code {result.returncode}]"
    except subprocess.TimeoutExpired:
        return "Command timed out after 30 seconds."
    except Exception as e:
        return f"Execution error: {e}"


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
    elif name == "edit":
        return tool_edit(args.get("filePath", ""), args.get("oldString", ""), args.get("newString", ""), args.get("replaceAll", False))
    elif name == "exec_busybox":
        return tool_exec_busybox(args.get("command", ""), args.get("cwd"))
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
    global history, working_dir, working_dirs
    data = request.json
    user_msg = data.get("message", "")
    model = data.get("model", MODEL)
    history.append({"role": "user", "content": user_msg})

    dirs = working_dirs if working_dirs else ([working_dir] if working_dir else [])
    if dirs:
        hints = []
        for d in dirs:
            try:
                files = sorted(os.listdir(d))[:30]
                hints.append(f"Folder: {d}\nContents:\n" + "\n".join(files))
            except Exception:
                hints.append(f"Folder: {d} (unreadable)")
        dir_info = "\n\n".join(hints)
        system_msg = {"role": "system", "content": f"You have access to {len(dirs)} project folder(s):\n\n{dir_info}\n\nAlways use tools relative to these directories. Never navigate above them."}
    else:
        system_msg = {"role": "system", "content": "No working directory set. Ask user to select a project folder first."}
    msgs_with_sys = [system_msg] + list(history)

    def generate():
        global history
        messages = list(msgs_with_sys)
        full_content = ""

        for _round in range(6):
            payload = {
                "model": model,
                "messages": messages,
                "stream": True,
                "tools": TOOLS,
                "tool_choice": "auto"
            }

            try:
                api_resp = requests.post(API_URL, json=payload, stream=True, timeout=180)
                api_resp.raise_for_status()
            except Exception as e:
                yield f"data: {json.dumps({'type': 'error', 'text': str(e)})}\n\n"
                return

            tool_calls_acc = {}
            round_content = ""

            for raw in api_resp.iter_lines():
                if not raw:
                    continue
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

                thinking_chunk = delta.get("reasoning") or delta.get("thinking") or ""
                if thinking_chunk:
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
                break

            tc_list = []
            for idx in sorted(tool_calls_acc.keys()):
                tc = tool_calls_acc[idx]
                tc_list.append({
                    "id": tc["id"] or f"call_{idx}",
                    "type": "function",
                    "function": {"name": tc["name"], "arguments": tc["arguments"]}
                })

            asst_msg = {"role": "assistant", "tool_calls": tc_list}
            if round_content:
                asst_msg["content"] = round_content
            messages.append(asst_msg)

            for tc in tc_list:
                fn_name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    args = {}

                yield f"data: {json.dumps({'type': 'tool_use', 'name': fn_name, 'args': args})}\n\n"
                result = run_tool(fn_name, args)
                yield f"data: {json.dumps({'type': 'tool_done', 'name': fn_name})}\n\n"

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result
                })

        if full_content:
            history.append({"role": "assistant", "content": full_content})
            if len(history) > 40:
                history = history[-40:]
            yield f"data: {json.dumps({'type': 'history_update', 'history': history})}\n\n"

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
    global history
    history = []
    return jsonify({"status": "cleared"})


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
    global history, current_chat_id
    data = request.json
    current_chat_id = data.get("chat_id")
    history = data.get("history", [])
    return jsonify({"status": "ok"})


@app.route("/storage_dir", methods=["GET"])
def storage_dir():
    return jsonify({"path": get_opencode_dir()})


@app.route("/save_chats", methods=["POST"])
def save_chats():
    """Save all chats: index.json for ordering + one file per chat."""
    data = request.json
    chats = data.get("chats", [])
    active_id = data.get("activeChatId")
    try:
        odir = get_opencode_dir()
        # Write each chat to its own file
        for chat in chats:
            cid = chat.get("id", "")
            if not cid:
                continue
            with open(chat_file(cid), "w", encoding="utf-8") as f:
                json.dump(chat, f, ensure_ascii=False, indent=2)
        # Write index (just ids + titles for listing, no history)
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
    """Load all chats from per-chat files via index."""
    try:
        with open(chats_index_file(), "r", encoding="utf-8") as f:
            index = json.load(f)
        chats = []
        for cid in index.get("chatIds", []):
            try:
                with open(chat_file(cid), "r", encoding="utf-8") as f:
                    chats.append(json.load(f))
            except Exception:
                pass  # Skip missing/corrupt chat files gracefully
        return jsonify({"chats": chats, "activeChatId": index.get("activeChatId")})
    except FileNotFoundError:
        return jsonify({"chats": [], "activeChatId": None})
    except Exception as e:
        return jsonify({"chats": [], "activeChatId": None, "error": str(e)})


@app.route("/delete_chat", methods=["POST"])
def delete_chat():
    """Delete a single chat's JSON file."""
    data = request.json
    cid = data.get("chat_id", "")
    if not cid:
        return jsonify({"status": "error", "message": "No chat_id"})
    try:
        path = chat_file(cid)
        if os.path.isfile(path):
            os.remove(path)
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


@app.route("/exec_busybox", methods=["POST"])
def exec_busybox_route():
    """HTTP endpoint to run a BusyBox command directly."""
    data = request.json or {}
    command = data.get("command", "")
    cwd = data.get("cwd")
    if not command:
        return jsonify({"status": "error", "message": "No command provided"})
    result = tool_exec_busybox(command, cwd)
    return jsonify({"status": "ok", "output": result})




@app.route("/debug_busybox", methods=["GET"])
def debug_busybox():
    """Diagnostic endpoint — shows busybox discovery state."""
    info = {}
    # Try to get native lib dir from Android
    try:
        from android.content import Context
        from android import activity
        ctx = activity.getApplicationContext()
        native_lib_dir = ctx.getApplicationInfo().nativeLibraryDir
        info["native_lib_dir"] = native_lib_dir
        candidate = os.path.join(native_lib_dir, "libexec.so")
        info["libexec_path"] = candidate
        info["libexec_exists"] = os.path.isfile(candidate)
        if os.path.isfile(candidate):
            info["libexec_size"] = os.path.getsize(candidate)
            info["libexec_executable"] = os.access(candidate, os.X_OK)
    except Exception as e:
        info["android_api_error"] = str(e)

    info["finder_result"] = _find_busybox()

    # List native lib dir contents safely
    try:
        nld = info.get("native_lib_dir", "")
        if nld and os.path.isdir(nld):
            info["native_lib_dir_contents"] = os.listdir(nld)
    except Exception as e:
        info["native_lib_dir_ls_error"] = str(e)

    return jsonify(info)

if __name__ == "__main__":
    print(f"OpenCode — http://localhost:{PORT}")
    app.run(host=HOST, port=PORT, debug=True)