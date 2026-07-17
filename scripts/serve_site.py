"""Static server for site/ that avoids os.getcwd() (preview-sandbox safe)."""
from __future__ import annotations

import http.server
import os
import socketserver

PORT = int(os.environ.get("PORT", "8742"))
BIND = os.environ.get("BIND", "127.0.0.1")
SITE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "site")


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=SITE_DIR, **kwargs)


if __name__ == "__main__":
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer((BIND, PORT), Handler) as httpd:
        print("serving %s on %s:%d" % (SITE_DIR, BIND, PORT))
        httpd.serve_forever()
