"""Static server for site-analytics/ that avoids os.getcwd() (preview-sandbox safe)."""
from __future__ import annotations

import http.server
import os
import socketserver

PORT = int(os.environ.get("PORT", "8755"))
SITE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "site-analytics")


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=SITE_DIR, **kwargs)


if __name__ == "__main__":
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("", PORT), Handler) as httpd:
        print("serving %s on :%d" % (SITE_DIR, PORT))
        httpd.serve_forever()
