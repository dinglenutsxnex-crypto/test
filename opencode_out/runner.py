import sys
import os

_here = os.path.dirname(os.path.abspath(__file__))
if _here not in sys.path:
    sys.path.insert(0, _here)

from python.app import app, init_chats
from python.config import HOST, PORT, STORAGE_PATH

def set_storage_path(path):
    import python.config as config
    config.STORAGE_PATH = path

def run():
    # Make sure chats are loaded after storage path is known
    init_chats()
    print(f"OpenCode - http://localhost:{PORT}")
    app.run(host=HOST, port=PORT, debug=False, use_reloader=False, threaded=True)

if __name__ == "__main__":
    run()