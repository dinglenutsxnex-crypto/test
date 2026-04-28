import os
import json
import re
import uuid
import glob as glob_module
import fnmatch
import requests
from flask import Flask, send_file, request, jsonify, send_from_directory, Response, stream_with_context
from python.config import API_URL, MODEL, HOST, PORT, STORAGE_PATH

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
app = Flask(__name__, template_folder=ROOT, static_folder=ROOT + '/ui')

# In‑memory chats dict: id -> { id, title, working_dir, model, messages }
chats = {}

# ── Initialisation ──────────────────────────────────────────────────
def init_chats():
    global chats
    if not STORAGE_PATH:
        return
    opencode_dir = os.path.join(STORAGE_PATH, "opencode")
    os.makedirs(opencode_dir, exist_ok=True)
    for filename in os.listdir(opencode_dir):
        if filename.endswith('.json'):
            try:
                with open(os.path.join(opencode_dir, filename), 'r') as f:
                    chat = json.load(f)
                    chats[chat['id']] = chat
            except Exception:
                pass

def save_chat(chat_id):
    """Persist a single chat to its JSON file."""
    opencode_dir = os.path.join(STORAGE_PATH, "opencode")
    filepath = os.path.join(opencode_dir, f"{chat_id}.json")
    with open(filepath, 'w') as f:
        json.dump(chats[chat_id], f, indent=2)

# ── Path helpers ────────────────────────────────────────────────────
def resolve_path(path, cwd):
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

# ── Tools ───────────────────────────────────────────────────────────
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

def tool_glob(pattern, path, cwd):
    if not cwd:
        return "No working directory set."
    base = resolve_path(path, cwd) if path else cwd
    if not is_within_dir(base, cwd):
        return f"Error: Path '{path}' is outside working directory"
    try:
        full_pattern = os.path.join(base, pattern)
        matches = glob_module.glob(full_pattern, recursive=True)
        matches = [m for m in matches if is_within_dir(m, cwd)]
        if not matches:
            return f"No files matching '{pattern}'"
        rel_matches = [os.path.relpath(m, base) for m in matches[:100]]
        return "Found files:\n" + "\n".join(rel_matches)
    except Exception as e:
        return f"Glob error: {e}"

def tool_grep(pattern, path, include, cwd):
    if not cwd:
        return "No working directory set."
    base = resolve_path(path, cwd) if path else cwd
    if not is_within_dir(base, cwd):
        return f"Error: Path '{path}' is outside working directory"
    try:
        regex = re.compile(pattern)
    except re.error as e:
        return f"Invalid regex: {e}"
    results = []
    try:
        for root, dirs, files in os.walk(base):
            if not is_within_dir(root, cwd):
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

def tool_read(filePath, offset, limit, cwd):
    if not cwd:
        return "No working directory set."
    full_path = resolve_path(filePath, cwd)
    if not full_path or not is_within_dir(full_path, cwd):
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

def tool_write(content, filePath, cwd):
    if not cwd:
        return "No working directory set."
    full_path = resolve_path(filePath, cwd)
    if not full_path or not is_within_dir(full_path, cwd):
        return f"Error: Path '{filePath}' is outside working directory"
    try:
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, 'w', encoding='utf-8') as f:
            f.write(content)
        return f"Written to {filePath} ({len(content)} chars)"
    except Exception as e:
        return f"Write error: {e}"

def tool_edit(filePath, oldString, newString, replaceAll, cwd):
    if not cwd:
        return "No working directory set."
    full_path = resolve_path(filePath, cwd)
    if not full_path or not is_within_dir(full_path, cwd):
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

def run_tool(name, args, cwd):
    if name == "web_search":
        return websearch(args.get("query", ""), args.get("num_results", 8))
    elif name == "web_fetch":
        return webfetch(args.get("url", ""))
    elif name == "glob":
        return tool_glob(args.get("pattern", "*"), args.get("path"), cwd)
    elif name == "grep":
        return tool_grep(args.get("pattern", ""), args.get("path"), args.get("include"), cwd)
    elif name == "read":
        return tool_read(args.get("filePath", ""), args.get("offset"), args.get("limit"), cwd)
    elif name == "write":
        return tool_write(args.get("content", ""), args.get("filePath", ""), cwd)
    elif name == "edit":
        return tool_edit(args.get("filePath", ""), args.get("oldString", ""), args.get("newString", ""), args.get("replaceAll", False), cwd)
    return f"Unknown tool: {name}"

# ── Routes ──────────────────────────────────────────────────────────
@app.route("/")
def home():
    return send_file(os.path.join(ROOT, "index.html"))

@app.route("/ui/<path:filename>")
def static_files(filename):
    return send_from_directory(ROOT + '/ui', filename)

# ── Chat management ─────────────────────────────────────────────────
@app.route("/chats", methods=["GET"])
def list_chats():
    """Return all chats with summary info (no full messages)."""
    chat_list = []
    for c in chats.values():
        chat_list.append({
            "id": c["id"],
            "title": c["title"],
            "working_dir": c["working_dir"],
            "model": c["model"]
        })
    return jsonify(chat_list)

@app.route("/chats", methods=["POST"])
def create_chat():
    """Create a brand‑new chat and return it."""
    chat_id = str(uuid.uuid4())
    chat = {
        "id": chat_id,
        "title": "New Chat",
        "working_dir": "",
        "model": MODEL,
        "messages": []
    }
    chats[chat_id] = chat
    save_chat(chat_id)
    return jsonify(chat)

@app.route("/chats/<chat_id>", methods=["PUT"])
def update_chat(chat_id):
    """Update title, working_dir and/or model."""
    if chat_id not in chats:
        return jsonify({"error": "Chat not found"}), 404
    data = request.json or {}
    if "title" in data:
        chats[chat_id]["title"] = data["title"]
    if "working_dir" in data:
        chats[chat_id]["working_dir"] = data["working_dir"]
    if "model" in data:
        chats[chat_id]["model"] = data["model"]
    save_chat(chat_id)
    return jsonify(chats[chat_id])

@app.route("/chats/<chat_id>", methods=["DELETE"])
def delete_chat(chat_id):
    """Remove a chat and its file."""
    if chat_id not in chats:
        return jsonify({"error": "Chat not found"}), 404
    del chats[chat_id]
    opencode_dir = os.path.join(STORAGE_PATH, "opencode")
    filepath = os.path.join(opencode_dir, f"{chat_id}.json")
    try:
        os.remove(filepath)
    except OSError:
        pass
    return jsonify({"status": "deleted"})

@app.route("/chats/<chat_id>/messages", methods=["GET"])
def get_messages(chat_id):
    """Return the full message history of a chat."""
    if chat_id not in chats:
        return jsonify({"error": "Chat not found"}), 404
    return jsonify(chats[chat_id]["messages"])

@app.route("/chats/<chat_id>/messages", methods=["POST"])
def send_message(chat_id):
    """Process a user message and stream the assistant response."""
    if chat_id not in chats:
        return jsonify({"error": "Chat not found"}), 404

    data = request.json or {}
    user_msg = data.get("message", "")
    model_override = data.get("model")

    chat = chats[chat_id]
    if model_override:
        chat["model"] = model_override

    # Append user message
    chat["messages"].append({"role": "user", "content": user_msg})
    save_chat(chat_id)

    cwd = chat["working_dir"]
    if cwd:
        try:
            top_files = sorted(os.listdir(cwd))[:30]
            file_hint = "\n".join(top_files)
        except Exception:
            file_hint = "(unreadable)"
        system_msg = {"role": "system", "content": f"Working directory: {cwd}\nTop-level contents:\n{file_hint}\n\nAlways use tools relative to the working directory. Never navigate above it."}
    else:
        system_msg = {"role": "system", "content": "No working directory set. Ask user to select a project folder first."}

    def generate():
        messages = [system_msg] + list(chat["messages"])

        for _round in range(6):
            payload = {
                "model": chat["model"],
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
                # Normal assistant reply, no tool calls
                if round_content:
                    messages.append({"role": "assistant", "content": round_content})
                    chat["messages"].append({"role": "assistant", "content": round_content})
                    save_chat(chat_id)
                break

            # Build tool call list and assistant message
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
            chat["messages"].append(asst_msg)

            for tc in tc_list:
                fn_name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    args = {}

                yield f"data: {json.dumps({'type': 'tool_use', 'name': fn_name, 'args': args})}\n\n"
                result = run_tool(fn_name, args, cwd)
                yield f"data: {json.dumps({'type': 'tool_done', 'name': fn_name})}\n\n"

                tool_msg = {
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result
                }
                messages.append(tool_msg)
                chat["messages"].append(tool_msg)

            save_chat(chat_id)

        # Final assistant message with content only (already handled)
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

@app.route("/chats/<chat_id>/clear", methods=["POST"])
def clear_chat(chat_id):
    """Clear all messages of a chat."""
    if chat_id not in chats:
        return jsonify({"error": "Chat not found"}), 404
    chats[chat_id]["messages"] = []
    save_chat(chat_id)
    return jsonify({"status": "cleared"})

if __name__ == "__main__":
    # Standalone debug run (not used under Chaquopy)
    init_chats()
    print(f"OpenCode — http://localhost:{PORT}")
    app.run(host=HOST, port=PORT, debug=True)