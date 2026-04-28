import os
import json
import re
import glob as glob_module
import fnmatch
import requests
import uuid
import time
from flask import Flask, send_file, request, jsonify, send_from_directory, Response, stream_with_context

# Dynamic config loading
try:
    from python.config import API_URL, MODEL, HOST, PORT
except ImportError:
    from config import API_URL, MODEL, HOST, PORT

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
app = Flask(__name__, template_folder=ROOT, static_folder=ROOT + '/ui')

# Storage paths
STORAGE_DIR = "/storage/emulated/0/opencode"
CHATS_FILE = os.path.join(STORAGE_DIR, "chats.json")

def ensure_storage():
    """Create storage directory if it doesn't exist"""
    os.makedirs(STORAGE_DIR, exist_ok=True)
    if not os.path.exists(CHATS_FILE):
        with open(CHATS_FILE, 'w') as f:
            json.dump({"chats": {}, "active_chat_id": None}, f)

def load_chats_index():
    """Load the chat index"""
    ensure_storage()
    with open(CHATS_FILE, 'r') as f:
        return json.load(f)

def save_chats_index(data):
    """Save the chat index"""
    ensure_storage()
    with open(CHATS_FILE, 'w') as f:
        json.dump(data, f, indent=2)

def load_chat(chat_id):
    """Load a specific chat's history"""
    chat_file = os.path.join(STORAGE_DIR, f"chat_{chat_id}.json")
    if os.path.exists(chat_file):
        with open(chat_file, 'r') as f:
            return json.load(f)
    return {"history": [], "folders": [], "title": "New Chat"}

def save_chat(chat_id, data):
    """Save a specific chat's history"""
    chat_file = os.path.join(STORAGE_DIR, f"chat_{chat_id}.json")
    with open(chat_file, 'w') as f:
        json.dump(data, f, indent=2)

# In-memory state
active_chat_id = None
working_dirs = []  # Multiple working directories

def resolve_path(path, cwd=None):
    """Resolve a path relative to the first working directory"""
    if not working_dirs:
        return None
    
    if cwd and os.path.isabs(path):
        return path
    
    # Try each working directory
    for wd in working_dirs:
        full = os.path.normpath(os.path.join(wd, path.strip()))
        if os.path.exists(full):
            return full
    
    # Default to first working directory
    return os.path.normpath(os.path.join(working_dirs[0], path.strip()))

def is_within_dirs(path):
    """Check if path is within any working directory"""
    abs_path = os.path.abspath(path)
    return any(abs_path.startswith(os.path.abspath(d) + os.sep) or abs_path == os.path.abspath(d) 
              for d in working_dirs)

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
            "description": "Find files matching a pattern in the working directories.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern (e.g., **/*.js)"},
                    "directory_index": {"type": "integer", "description": "Index of the directory to search (0-based, default searches all)"}
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
                    "directory_index": {"type": "integer", "description": "Index of the directory to search (0-based, default searches all)"},
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
                    "filePath": {"type": "string", "description": "Path to the file (relative or absolute)"},
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
                    "filePath": {"type": "string", "description": "Path to the file (relative or absolute)"}
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
                    "filePath": {"type": "string", "description": "Path to the file (relative or absolute)"},
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

def tool_glob(pattern, directory_index=None):
    global working_dirs
    if not working_dirs:
        return "No working directories set. Use Open Folder to add one."
    
    dirs_to_search = [working_dirs[directory_index]] if directory_index is not None and 0 <= directory_index < len(working_dirs) else working_dirs
    all_matches = []
    
    for base in dirs_to_search:
        try:
            full_pattern = os.path.join(base, pattern)
            matches = glob_module.glob(full_pattern, recursive=True)
            matches = [m for m in matches if is_within_dirs(m)]
            all_matches.extend(matches)
        except Exception as e:
            continue
    
    if not all_matches:
        return f"No files matching '{pattern}'"
    
    all_matches = all_matches[:100]
    results = []
    for m in all_matches:
        # Find which dir it's relative to
        for base in dirs_to_search:
            if m.startswith(base):
                rel = os.path.relpath(m, base)
                if len(dirs_to_search) > 1:
                    rel = f"[{os.path.basename(base)}] {rel}"
                results.append(rel)
                break
    
    return "Found files:\n" + "\n".join(results)

def tool_grep(pattern, directory_index=None, include=None):
    global working_dirs
    if not working_dirs:
        return "No working directories set."
    
    try:
        regex = re.compile(pattern)
    except re.error as e:
        return f"Invalid regex: {e}"
    
    dirs_to_search = [working_dirs[directory_index]] if directory_index is not None and 0 <= directory_index < len(working_dirs) else working_dirs
    results = []
    
    for base in dirs_to_search:
        try:
            for root, dirs, files in os.walk(base):
                if not is_within_dirs(root):
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
                                    if len(dirs_to_search) > 1:
                                        rel = f"[{os.path.basename(base)}] {rel}"
                                    results.append(f"{rel}:{i}: {line.rstrip()}")
                                    if len(results) >= 100:
                                        break
                    except Exception:
                        pass
                if len(results) >= 100:
                    break
        except Exception:
            continue
    
    if not results:
        return f"No matches for '{pattern}'"
    return "Matches:\n" + "\n".join(results[:100])

def tool_read(filePath, offset=None, limit=None):
    global working_dirs
    full_path = resolve_path(filePath)
    if not full_path:
        return f"Error: Could not resolve path '{filePath}'"
    if not is_within_dirs(full_path):
        return f"Error: Path '{filePath}' is outside working directories"
    if not os.path.isfile(full_path):
        return f"Error: File not found: {filePath}"
    try:
        with open(full_path, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
        start = (offset - 1) if offset else 0
        end = len(lines) if limit is None else start + limit
        lines = lines[start:end]
        content = "".join(lines)
        prefix = f"Lines {start+1}-{end}:\n" if offset or limit else ""
        if len(content) > 50000:
            content = content[:50000] + f"\n... (truncated at 50000 chars)"
        return prefix + content
    except Exception as e:
        return f"Read error: {e}"

def tool_write(content, filePath):
    global working_dirs
    full_path = resolve_path(filePath)
    if not full_path:
        return f"Error: Could not resolve path '{filePath}'"
    if not is_within_dirs(full_path):
        return f"Error: Path '{filePath}' is outside working directories"
    try:
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, 'w', encoding='utf-8') as f:
            f.write(content)
        return f"Written to {filePath} ({len(content)} chars)"
    except Exception as e:
        return f"Write error: {e}"

def tool_edit(filePath, oldString, newString, replaceAll=False):
    global working_dirs
    full_path = resolve_path(filePath)
    if not full_path:
        return f"Error: Could not resolve path '{filePath}'"
    if not is_within_dirs(full_path):
        return f"Error: Path '{filePath}' is outside working directories"
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
        return tool_glob(args.get("pattern", "*"), args.get("directory_index"))
    elif name == "grep":
        return tool_grep(args.get("pattern", ""), args.get("directory_index"), args.get("include"))
    elif name == "read":
        return tool_read(args.get("filePath", ""), args.get("offset"), args.get("limit"))
    elif name == "write":
        return tool_write(args.get("content", ""), args.get("filePath", ""))
    elif name == "edit":
        return tool_edit(args.get("filePath", ""), args.get("oldString", ""), args.get("newString", ""), args.get("replaceAll", False))
    return f"Unknown tool: {name}"

@app.route("/")
def home():
    return send_file(os.path.join(ROOT, "index.html"))

@app.route("/ui/<path:filename>")
def static_files(filename):
    return send_from_directory(ROOT + '/ui', filename)

@app.route("/api/chats", methods=["GET"])
def get_chats():
    data = load_chats_index()
    return jsonify(data)

@app.route("/api/chats", methods=["POST"])
def create_chat():
    data = load_chats_index()
    chat_id = str(uuid.uuid4())[:8]
    title = request.json.get("title", "New Chat")
    
    chat_data = {
        "id": chat_id,
        "title": title,
        "history": [],
        "folders": [],
        "created_at": time.time()
    }
    
    data["chats"][chat_id] = {
        "id": chat_id,
        "title": title,
        "created_at": chat_data["created_at"]
    }
    data["active_chat_id"] = chat_id
    save_chats_index(data)
    save_chat(chat_id, chat_data)
    
    global active_chat_id
    active_chat_id = chat_id
    
    return jsonify(data)

@app.route("/api/chats/<chat_id>", methods=["PUT"])
def update_chat(chat_id):
    data = load_chats_index()
    if chat_id not in data["chats"]:
        return jsonify({"error": "Chat not found"}), 404
    
    updates = request.json
    if "title" in updates:
        data["chats"][chat_id]["title"] = updates["title"]
    if "active" in updates:
        data["active_chat_id"] = chat_id
        global active_chat_id
        active_chat_id = chat_id
    
    save_chats_index(data)
    return jsonify(data)

@app.route("/api/chats/<chat_id>", methods=["DELETE"])
def delete_chat(chat_id):
    data = load_chats_index()
    if chat_id not in data["chats"]:
        return jsonify({"error": "Chat not found"}), 404
    
    del data["chats"][chat_id]
    
    # Delete chat file
    chat_file = os.path.join(STORAGE_DIR, f"chat_{chat_id}.json")
    if os.path.exists(chat_file):
        os.remove(chat_file)
    
    # Set a new active chat if needed
    if data["active_chat_id"] == chat_id:
        if data["chats"]:
            data["active_chat_id"] = list(data["chats"].keys())[0]
        else:
            data["active_chat_id"] = None
            global active_chat_id
            active_chat_id = None
    
    save_chats_index(data)
    return jsonify(data)

@app.route("/api/chats/<chat_id>/history", methods=["GET"])
def get_chat_history(chat_id):
    chat_data = load_chat(chat_id)
    return jsonify(chat_data)

@app.route("/api/folders", methods=["GET"])
def get_folders():
    global working_dirs
    return jsonify({"folders": working_dirs})

@app.route("/api/folders", methods=["POST"])
def add_folder():
    global working_dirs
    folder = request.json.get("folder", "").strip()
    if folder and os.path.isdir(folder) and folder not in working_dirs:
        working_dirs.append(folder)
        # Save to active chat
        if active_chat_id:
            chat_data = load_chat(active_chat_id)
            chat_data["folders"] = working_dirs
            save_chat(active_chat_id, chat_data)
    return jsonify({"folders": working_dirs})

@app.route("/api/folders/<int:index>", methods=["DELETE"])
def remove_folder(index):
    global working_dirs
    if 0 <= index < len(working_dirs):
        working_dirs.pop(index)
        if active_chat_id:
            chat_data = load_chat(active_chat_id)
            chat_data["folders"] = working_dirs
            save_chat(active_chat_id, chat_data)
    return jsonify({"folders": working_dirs})

@app.route("/ls", methods=["GET"])
def list_dir():
    global working_dirs
    path = request.args.get("path", "")
    
    if not working_dirs:
        return jsonify({"error": "No working directories set"})
    
    # If path is empty, list all working directories
    if not path:
        items = []
        for i, wd in enumerate(working_dirs):
            items.append({
                "name": f"[Folder {i}] {os.path.basename(wd)}",
                "is_dir": True,
                "path": wd,
                "folder_index": i
            })
        return jsonify({"items": items, "folders": working_dirs})
    
    # Otherwise list specific directory
    full_path = resolve_path(path)
    if not full_path or not is_within_dirs(full_path):
        return jsonify({"error": "Path outside working directories"})
    if not os.path.isdir(full_path):
        return jsonify({"error": f"Not a directory: {path}"})
    
    try:
        items = []
        for name in sorted(os.listdir(full_path)):
            fpath = os.path.join(full_path, name)
            items.append({
                "name": name,
                "is_dir": os.path.isdir(fpath),
                "path": fpath
            })
        return jsonify({"items": items, "cwd": full_path})
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route("/chat", methods=["POST"])
def chat():
    global active_chat_id, working_dirs
    data = request.json
    user_msg = data.get("message", "")
    model = data.get("model", MODEL)
    chat_id = data.get("chat_id", active_chat_id)
    
    if not chat_id:
        return jsonify({"error": "No active chat"}), 400
    
    # Load chat history
    chat_data = load_chat(chat_id)
    history = chat_data.get("history", [])
    working_dirs = chat_data.get("folders", [])
    
    history.append({"role": "user", "content": user_msg})
    
    if working_dirs:
        dir_list = "\n".join(f"[{i}] {d}" for i, d in enumerate(working_dirs))
        system_msg = {
            "role": "system", 
            "content": f"Working directories:\n{dir_list}\n\nYou can access files from any of these directories. Use paths relative to one of them."
        }
    else:
        system_msg = {
            "role": "system", 
            "content": "No working directories set. Ask the user to add project folders."
        }
    
    msgs_with_sys = [system_msg] + list(history)
    
    def generate():
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
            if len(history) > 50:
                history = history[-50:]
            
            # Save chat
            chat_data["history"] = history
            chat_data["folders"] = working_dirs
            
            # Auto-generate title from first message if still default
            chats_index = load_chats_index()
            if chat_id in chats_index["chats"] and chats_index["chats"][chat_id]["title"] == "New Chat":
                # Generate title from first user message
                first_user_msg = next((m["content"] for m in history if m["role"] == "user"), "")
                title = first_user_msg[:40] + ("..." if len(first_user_msg) > 40 else "")
                chat_data["title"] = title
                chats_index["chats"][chat_id]["title"] = title
                save_chats_index(chats_index)
            
            save_chat(chat_id, chat_data)
        
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
    global active_chat_id
    if active_chat_id:
        chat_data = load_chat(active_chat_id)
        chat_data["history"] = []
        save_chat(active_chat_id, chat_data)
    return jsonify({"status": "cleared"})

if __name__ == "__main__":
    print(f"OpenCode — http://localhost:{PORT}")
    app.run(host=HOST, port=PORT, debug=True)