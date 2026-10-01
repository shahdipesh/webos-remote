#!/usr/bin/env python3
"""
WebSocket relay for the webOS TV remote page.

Why it exists: some TV firmware instantly closes (code 1008) any WebSocket
that carries a browser Origin header. Native remote apps don't send one, so
they pair fine. This relay accepts a plain local WebSocket from the page and
re-opens it toward the TV with NO Origin header, bridging frames both ways.

Usage (in a-Shell -- just one window, one command):
    python3 relay.py
It serves the remote page on http://127.0.0.1:8000/remote.html AND runs the
WebSocket relay on 127.0.0.1:8765. Open the page in Safari and tap Connect.

The page connects to ws://127.0.0.1:8765/?target=<urlencoded ws/wss URL>
and the relay dials that target. Stdlib only, no pip packages needed.
"""
import functools
import os
import socket
import ssl
import threading
import hashlib
import base64
import struct
import secrets
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

LISTEN = ("127.0.0.1", 8765)
WEB = ("127.0.0.1", 8000)
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def log(*a):
    print("[relay]", *a, flush=True)


def recv_exact(s, n):
    buf = b""
    while len(buf) < n:
        c = s.recv(n - len(buf))
        if not c:
            raise ConnectionError("socket closed")
        buf += c
    return buf


def read_frame(s):
    """Read one WebSocket frame. Returns (opcode, payload), unmasked."""
    b1, b2 = recv_exact(s, 2)
    opcode = b1 & 0x0F
    masked = bool(b2 & 0x80)
    ln = b2 & 0x7F
    if ln == 126:
        ln = struct.unpack("!H", recv_exact(s, 2))[0]
    elif ln == 127:
        ln = struct.unpack("!Q", recv_exact(s, 8))[0]
    key = recv_exact(s, 4) if masked else None
    payload = recv_exact(s, ln) if ln else b""
    if masked:
        payload = bytes(c ^ key[i & 3] for i, c in enumerate(payload))
    return opcode, payload


def send_frame(s, opcode, payload=b"", mask=False):
    h = bytes([0x80 | opcode])
    ln = len(payload)
    m = 0x80 if mask else 0
    if ln < 126:
        h += bytes([m | ln])
    elif ln < 65536:
        h += bytes([m | 126]) + struct.pack("!H", ln)
    else:
        h += bytes([m | 127]) + struct.pack("!Q", ln)
    if mask:
        key = secrets.token_bytes(4)
        h += key
        payload = bytes(c ^ key[i & 3] for i, c in enumerate(payload))
    s.sendall(h + payload)


def read_http(s):
    buf = b""
    while b"\r\n\r\n" not in buf:
        c = s.recv(4096)
        if not c:
            break
        buf += c
    text = buf.split(b"\r\n\r\n")[0].decode("latin1", "replace")
    lines = text.split("\r\n")
    hdr = {}
    for ln in lines[1:]:
        if ":" in ln:
            k, v = ln.split(":", 1)
            hdr[k.strip().lower()] = v.strip()
    return (lines[0] if lines else ""), hdr


def handle(page):
    tv = None
    try:
        req, hdr = read_http(page)
        parts = req.split(" ")
        qs = parse_qs(urlparse(parts[1] if len(parts) > 1 else "/").query)
        target = (qs.get("target") or [None])[0]
        key = hdr.get("sec-websocket-key")
        if not target or not key:
            page.close()
            return
        u = urlparse(target)
        if u.scheme not in ("ws", "wss") or not u.hostname:
            page.close()
            return
        host = u.hostname
        port = u.port or (3001 if u.scheme == "wss" else 3000)
        path = u.path or "/"
        if u.query:
            path += "?" + u.query

        accept = base64.b64encode(
            hashlib.sha1((key + GUID).encode()).digest()).decode()
        page.sendall(("HTTP/1.1 101 Switching Protocols\r\n"
                      "Upgrade: websocket\r\n"
                      "Connection: Upgrade\r\n"
                      "Sec-WebSocket-Accept: " + accept + "\r\n\r\n").encode())
        log("page connected, dialing", u.scheme + "://" + host + ":" + str(port) + path)

        raw = socket.create_connection((host, port), timeout=8)
        if u.scheme == "wss":
            ctx = ssl._create_unverified_context()
            tv = ctx.wrap_socket(raw, server_hostname=host)
        else:
            tv = raw
        ckey = base64.b64encode(secrets.token_bytes(16)).decode()
        # NOTE: deliberately no Origin header -- that is the whole point.
        tv.sendall(("GET " + path + " HTTP/1.1\r\n"
                    "Host: " + host + ":" + str(port) + "\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    "Sec-WebSocket-Key: " + ckey + "\r\n"
                    "Sec-WebSocket-Version: 13\r\n\r\n").encode())
        resp, _ = read_http(tv)
        if "101" not in resp:
            log("target refused upgrade:", resp[:60])
            try:
                send_frame(page, 0x8, b"")
            except OSError:
                pass
            return
        log("target websocket open")

        stop = threading.Event()

        def pump(src, dst, mask, name):
            try:
                while not stop.is_set():
                    op, payload = read_frame(src)
                    if op == 0x8:  # close -> forward and stop
                        try:
                            send_frame(dst, 0x8, payload, mask=mask)
                        except OSError:
                            pass
                        break
                    elif op == 0x9:  # ping -> pong back to sender
                        try:
                            send_frame(src, 0xA, payload, mask=(not mask))
                        except OSError:
                            pass
                    elif op in (0x1, 0x2, 0xA):
                        send_frame(dst, op, payload, mask=mask)
            except (OSError, ConnectionError):
                pass
            finally:
                stop.set()

        t1 = threading.Thread(target=pump, args=(page, tv, True, "page->tv"), daemon=True)
        t2 = threading.Thread(target=pump, args=(tv, page, False, "tv->page"), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
    except (OSError, ConnectionError) as e:
        log("connection error:", e)
    finally:
        for s in (page, tv):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass
        log("closed")


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    # Relay first -- this is the critical path.
    srv = None
    try:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(LISTEN)
        srv.listen(5)
        log("relay listening on %s:%d -- page connects with ?target=<ws/wss url>" % LISTEN)
    except OSError as e:
        log("relay port busy (%s): is another relay.py already running? continuing without relay." % e)
    # File server is best-effort: if :8000 is taken by another server, that's fine.
    try:
        handler = functools.partial(SimpleHTTPRequestHandler, directory=here)
        httpd = ThreadingHTTPServer(WEB, handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        log("serving page on http://%s:%d/remote.html" % WEB)
    except OSError as e:
        log("web port busy (%s): serve the page another way." % e)
    if srv is None:
        log("no relay socket, exiting")
        return
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    main()
