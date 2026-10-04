#!/usr/bin/env python3
"""
WebSocket relay for the webOS TV remote page.

Why it exists: some TV firmware instantly closes (code 1008) any WebSocket
that carries a browser Origin header. Native remote apps don't send one, so
they pair fine. This relay accepts a plain local WebSocket from the page and
re-opens it toward the TV with NO Origin header, bridging frames both ways.

It also keeps its OWN persistent connection to the TV (TvLink) and exposes
simple HTTP endpoints so iOS Shortcuts / Siri can control the TV:

    GET /api/tv/status            -> {"ok":true,"connected":true}
    GET /api/tv/<command>         -> runs one command
    GET /api/tv/voice?q=<words>   -> matches plain words to a command

Commands: power_off, volume_up, volume_down, mute, play, pause,
          home, youtube, netflix, channel_up, channel_down, status

Usage (in a-Shell -- just one window, one command):
    python3 relay.py [--tv 10.0.0.40]
It serves the remote page on http://127.0.0.1:8000/remote.html, runs the
WebSocket relay on 127.0.0.1:8765, and the Siri HTTP API on :8000/api/tv/*.

Pairing: the relay registers with the TV as appId "com.lge.test" (same as the
remote page). The client-key is saved to .tv_client_key (git-ignored). It is
learned automatically two ways:
  1. Sniffed from the remote page's own pairing: open remote.html in Safari,
     tap Connect, allow on the TV -- the key passing through the relay is
     saved for the relay's own use.
  2. First direct register: if there is no key yet, the TV shows an "Allow?"
     prompt -- accept it on the TV and the returned key is saved.
Stdlib only, no pip packages needed.
"""
import functools
import json
import os
import socket
import ssl
import sys
import threading
import time
import hashlib
import base64
import struct
import secrets
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

LISTEN = ("127.0.0.1", 8765)
WEB = ("127.0.0.1", 8000)
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
HERE = os.path.dirname(os.path.abspath(__file__))
KEY_FILE = os.path.join(HERE, ".tv_client_key")

TV_LINK = None  # set in main()


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


def tv_handshake(host, port=3001):
    """Open a client WebSocket to the TV with NO Origin header."""
    raw = socket.create_connection((host, port), timeout=8)
    tv = ssl._create_unverified_context().wrap_socket(raw, server_hostname=host)
    ckey = base64.b64encode(secrets.token_bytes(16)).decode()
    tv.sendall(("GET / HTTP/1.1\r\n"
                "Host: " + host + ":" + str(port) + "\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                "Sec-WebSocket-Key: " + ckey + "\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n").encode())
    resp, _ = read_http(tv)
    if "101" not in resp:
        tv.close()
        raise ConnectionError("TV refused websocket upgrade: " + resp[:60])
    return tv


# Same identity the remote page pairs with, so a key learned from the page
# (or from an earlier relay run) is accepted without a new TV prompt.
RELAY_MANIFEST = {
    "forcePairing": False,
    "pairingType": "PROMPT",
    "manifest": {
        "manifestVersion": 1,
        "appVersion": "1.0",
        "appId": "com.lge.test",
        "vendorId": "com.lge",
        "localizedAppNames": {"": "LG Remote App"},
        "permissions": [
            "TEST_SECURE", "CONTROL_INPUT_TEXT", "CONTROL_MOUSE_AND_KEYBOARD",
            "READ_INSTALLED_APPS", "READ_NOTIFICATIONS", "SEARCH",
            "WRITE_SETTINGS", "CONTROL_POWER", "READ_CURRENT_CHANNEL",
            "READ_RUNNING_APPS", "LAUNCH", "LAUNCH_WEBAPP", "APP_TO_APP",
            "CLOSE", "TEST_OPEN", "TEST_PROTECTED", "CONTROL_AUDIO",
            "CONTROL_DISPLAY", "CONTROL_INPUT_MEDIA_PLAYBACK",
            "CONTROL_INPUT_TV", "CONTROL_POWER", "READ_APP_STATUS",
            "READ_TV_CHANNEL_LIST", "CONTROL_TV_SCREEN", "CONTROL_TV_STANBY",
            "READ_TV_PROGRAM_INFO", "CONTROL_TV_POWER", "CONTROL_WOL",
            "READ_POWER_STATE", "READ_SETTINGS",
        ],
    },
}


class TvLink:
    """The relay's own persistent connection to the TV.

    Keeps one registered WebSocket open in a background thread, reconnecting
    with backoff. Commands from the HTTP API go through request()/notify().
    """

    def __init__(self, host):
        self.host = host
        self.wlock = threading.Lock()   # guards sock + waiters
        self.sock = None
        self.seq = 0
        self.waiters = {}               # id -> [Event, box]
        self.registered = threading.Event()
        self.key = self._load_key()
        threading.Thread(target=self._loop, daemon=True).start()

    def _load_key(self):
        try:
            with open(KEY_FILE) as f:
                return f.read().strip()
        except OSError:
            return ""

    def _save_key(self, k):
        if not k or k == self.key:
            return
        self.key = k
        try:
            with open(KEY_FILE, "w") as f:
                f.write(k)
            os.chmod(KEY_FILE, 0o600)
            log("saved TV client-key")
        except OSError as e:
            log("could not save client-key:", e)

    def note_key(self, k):
        """A client-key observed on the wire (e.g. the page's pairing)."""
        self._save_key(k)

    def _loop(self):
        backoff = 2
        while True:
            try:
                self._serve()
                backoff = 2
            except Exception as e:
                log("tv link down:", e)
            with self.wlock:
                self.sock = None
            self.registered.clear()
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)

    def _serve(self):
        tv = tv_handshake(self.host, 3001)
        with self.wlock:
            self.sock = tv
        payload = dict(RELAY_MANIFEST)
        payload["client-key"] = self.key
        send_frame(tv, 0x1, json.dumps(
            {"id": "relay_reg", "type": "register", "payload": payload}).encode())
        log("registering with TV as com.lge.test"
            + (" (have key)" if self.key else " (no key yet -- accept the prompt on the TV)"))
        tv.settimeout(60)
        while True:
            op, data = read_frame(tv)
            if op == 0x8:
                raise ConnectionError("TV closed the connection")
            if op == 0x9:  # ping -> pong
                with self.wlock:
                    try:
                        send_frame(tv, 0xA, data)
                    except OSError:
                        pass
                continue
            if op != 0x1:
                continue
            try:
                m = json.loads(data.decode())
            except ValueError:
                continue
            if m.get("type") == "registered":
                k = (m.get("payload") or {}).get("client-key")
                if k:
                    self._save_key(k)
                self.registered.set()
                log("TV registered")
                continue
            mid = m.get("id")
            if mid:
                with self.wlock:
                    w = self.waiters.pop(mid, None)
                if w:
                    ev, box = w
                    box["msg"] = m
                    ev.set()

    def _send(self, uri, payload, want_reply, timeout):
        if not self.registered.wait(timeout=25):
            raise TimeoutError("TV not registered (is it on and on the same Wi-Fi?)")
        with self.wlock:
            s = self.sock
            if s is None:
                raise ConnectionError("TV socket not open")
            self.seq += 1
            mid = "siri%d" % self.seq
            ev = None
            box = {}
            if want_reply:
                ev = threading.Event()
                self.waiters[mid] = (ev, box)
            msg = {"type": "request", "id": mid, "uri": uri}
            if payload:
                msg["payload"] = payload
            send_frame(s, 0x1, json.dumps(msg).encode())
        if want_reply:
            if not ev.wait(timeout):
                with self.wlock:
                    self.waiters.pop(mid, None)
                raise TimeoutError("no reply for " + uri)
            return (box.get("msg") or {}).get("payload") or {}
        return {}

    def notify(self, uri, payload=None):
        """Fire-and-forget command."""
        self._send(uri, payload, False, 0)

    def request(self, uri, payload=None, timeout=8):
        """Command that waits for the TV's reply payload."""
        return self._send(uri, payload, True, timeout)


# ---------------------------------------------------------------------------
# Siri command layer
# ---------------------------------------------------------------------------

def tv_home(link):
    r = link.request("ssap://com.webos.service.applicationManager/listApps")
    apps = r.get("apps") or []
    pick = None
    for a in apps:
        aid = a.get("id", "")
        if "home" in aid and "homebrew" not in aid:
            pick = aid
            break
    if not pick:
        raise RuntimeError("could not find the launcher app")
    link.notify("ssap://system.launcher/launch", {"id": pick})
    return "Back home"


def tv_mute(link):
    v = link.request("ssap://audio/getVolume")
    muted = not v.get("muted", False)
    link.notify("ssap://audio/setMuted", {"muted": muted})
    return "Muted" if muted else "Unmuted"


SIMPLE_CMDS = {
    # name: (uri, payload, spoken confirmation)
    "power_off":    ("ssap://system/turnOff", {}, "Turning the TV off"),
    "volume_up":    ("ssap://audio/volumeUp", {}, "Volume up"),
    "volume_down":  ("ssap://audio/volumeDown", {}, "Volume down"),
    "play":         ("ssap://media.controls/play", {}, "Playing"),
    "pause":        ("ssap://media.controls/pause", {}, "Paused"),
    "channel_up":   ("ssap://tv/channelUp", {}, "Channel up"),
    "channel_down": ("ssap://tv/channelDown", {}, "Channel down"),
    "youtube":      ("ssap://system.launcher/launch", {"id": "youtube.leanback.v4"}, "Opening YouTube"),
    "netflix":      ("ssap://system.launcher/launch", {"id": "netflix"}, "Opening Netflix"),
}

SPECIAL_CMDS = {"mute": tv_mute, "home": tv_home}
ALL_CMDS = set(SIMPLE_CMDS) | set(SPECIAL_CMDS)


def run_cmd(link, name, repeat=1):
    if name in SIMPLE_CMDS:
        uri, payload, say = SIMPLE_CMDS[name]
        for i in range(max(1, repeat)):
            if i:
                time.sleep(0.25)
            link.notify(uri, payload)
        return say
    if name in SPECIAL_CMDS:
        return SPECIAL_CMDS[name](link)
    raise ValueError("unknown command: " + name)


def voice_to_cmd(q):
    """Match plain words to (command, repeat). Returns (None, 0) if no match."""
    q = (q or "").lower()
    if not q.strip():
        return None, 0
    # more specific matches first
    if "youtube" in q or "you tube" in q:
        return "youtube", 1
    if "netflix" in q:
        return "netflix", 1
    for w in ("turn off", "power off", "switch off", "shut off", "turn the tv off"):
        if w in q:
            return "power_off", 1
    if "mute" in q or "silence" in q:
        return "mute", 1
    if "channel up" in q or "next channel" in q:
        return "channel_up", 1
    if "channel down" in q or "previous channel" in q:
        return "channel_down", 1
    if "volume up" in q or "louder" in q or "turn it up" in q or "turn up" in q:
        n = 3 if any(w in q for w in ("a lot", "way up", "much")) else 1
        return "volume_up", n
    if "volume down" in q or "quieter" in q or "turn it down" in q or "turn down" in q:
        n = 3 if any(w in q for w in ("a lot", "way down", "much")) else 1
        return "volume_down", n
    if "pause" in q or "hold on" in q:
        return "pause", 1
    if q.strip().startswith("play") or " resume" in q:
        return "play", 1
    if "home" in q or "main menu" in q:
        return "home", 1
    return None, 0


class ApiHandler(SimpleHTTPRequestHandler):
    """File server + /api/tv/* JSON endpoints for Siri Shortcuts."""

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _api(self):
        u = urlparse(self.path)
        path = u.path
        if path == "/api/tv/status":
            ok = TV_LINK is not None and TV_LINK.registered.is_set()
            return self._json({"ok": True, "connected": ok})
        if path == "/api/tv/voice":
            q = (parse_qs(u.query).get("q") or [""])[0]
            cmd, n = voice_to_cmd(q)
            if not cmd:
                return self._json({"ok": False,
                                   "message": "Didn't catch that. Try 'turn off', 'volume up', 'mute', 'YouTube'…"})
            try:
                msg = run_cmd(TV_LINK, cmd, n)
                return self._json({"ok": True, "cmd": cmd, "message": msg})
            except Exception as e:
                log("voice cmd failed:", e)
                return self._json({"ok": False, "message": "TV didn't respond. Is it on?"})
        if path.startswith("/api/tv/"):
            cmd = path.rsplit("/", 1)[-1]
            if cmd not in ALL_CMDS:
                return self._json({"ok": False, "message": "unknown command: " + cmd}, 404)
            try:
                msg = run_cmd(TV_LINK, cmd)
                return self._json({"ok": True, "cmd": cmd, "message": msg})
            except Exception as e:
                log("cmd failed:", e)
                return self._json({"ok": False, "message": "TV didn't respond. Is it on?"})
        return None

    def do_GET(self):
        if self.path.startswith("/api/tv/"):
            try:
                self._api()
            except (OSError, ConnectionError):
                pass
            return
        super().do_GET()

    def log_message(self, *a):
        pass  # keep a-Shell output clean


def sniff_registered(payload):
    """Save a client-key seen in a bridged page<->TV registration."""
    try:
        m = json.loads(payload.decode())
    except ValueError:
        return
    if m.get("type") == "registered":
        k = (m.get("payload") or {}).get("client-key")
        if k and TV_LINK:
            TV_LINK.note_key(k)


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

        tv = tv_handshake(host, port)
        log("target websocket open")

        stop = threading.Event()

        def pump(src, dst, mask, sniff=None):
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
                        if sniff and op == 0x1:
                            try:
                                sniff(payload)
                            except Exception:
                                pass
            except (OSError, ConnectionError):
                pass
            finally:
                stop.set()

        t1 = threading.Thread(target=pump, args=(page, tv, True), daemon=True)
        t2 = threading.Thread(target=pump, args=(tv, page, False, sniff_registered), daemon=True)
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


def probe(tv_host):
    """One-time experiment: which handshake headers does the TV accept?

    The relay works because it sends NO Origin header. If the TV also
    accepts 'Origin: null' (+ a Safari UA, i.e. what a sandboxed iframe
    would send), the page can talk to the TV directly and the relay can
    be deleted entirely.
    """
    safari_ua = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
                 "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 "
                 "Mobile/15E148 Safari/604.1")
    variants = [
        ("no Origin, no User-Agent  [baseline: what the relay sends]", {}),
        ("Origin: null", {"Origin": "null"}),
        ("Origin: null + Safari iPhone UA  [simulates sandboxed iframe]",
         {"Origin": "null", "User-Agent": safari_ua}),
        ("Origin: http://127.0.0.1:8000 + Safari UA  [simulates the page]",
         {"Origin": "http://127.0.0.1:8000", "User-Agent": safari_ua}),
    ]
    manifest = {"manifestVersion": 1, "signatures": [
        {"signatureVersion": 1, "appId": "com.lge.test",
         "vendorId": "com.lge", "signature": "LEGACY"}]}
    for i, (name, extra) in enumerate(variants):
        if i:
            time.sleep(4)  # stay clear of the TV's pairing throttle
        print("---", name, flush=True)
        tv = None
        try:
            raw = socket.create_connection((tv_host, 3001), timeout=8)
            ctx = ssl._create_unverified_context()
            tv = ctx.wrap_socket(raw, server_hostname=tv_host)
            ckey = base64.b64encode(secrets.token_bytes(16)).decode()
            head = ["GET / HTTP/1.1", "Host: %s:3001" % tv_host,
                    "Upgrade: websocket", "Connection: Upgrade",
                    "Sec-WebSocket-Key: " + ckey, "Sec-WebSocket-Version: 13"]
            for k, v in extra.items():
                head.append(k + ": " + v)
            tv.sendall(("\r\n".join(head) + "\r\n\r\n").encode())
            resp, _ = read_http(tv)
            if "101" not in resp:
                print("    handshake refused:", resp[:60], flush=True)
                continue
            reg = {"id": "probe", "type": "register",
                   "payload": {"forcePairing": False, "pairingType": "PIN",
                               "client-key": "", "manifest": manifest}}
            send_frame(tv, 0x1, json.dumps(reg).encode())
            tv.settimeout(5)
            verdict = "no answer, connection stayed OPEN"
            try:
                while True:
                    op, payload = read_frame(tv)
                    if op == 0x8:
                        code = int.from_bytes(payload[:2], "big") if len(payload) >= 2 else 0
                        verdict = "CLOSED by TV (code %d)" % code
                        break
                    elif op == 0x1:
                        try:
                            m = json.loads(payload.decode())
                        except ValueError:
                            m = {}
                        p = m.get("payload") or {}
                        if p.get("errorCode") == "403":
                            verdict = "THROTTLED by TV (403: too many pairing requests)"
                        else:
                            verdict = "OPEN, TV answered: %s" % str(m)[:110]
                        break
            except socket.timeout:
                pass
            print("   ", verdict, flush=True)
        except (OSError, ConnectionError) as e:
            print("    error:", e, flush=True)
        finally:
            if tv is not None:
                try:
                    tv.close()
                except OSError:
                    pass
    print("done.")


def main():
    global TV_LINK
    args = sys.argv[1:]
    if args and args[0] == "--probe":
        probe(args[1] if len(args) > 1 else "10.0.0.40")
        return
    tv_host = "10.0.0.40"
    if "--tv" in args:
        i = args.index("--tv")
        if i + 1 < len(args):
            tv_host = args[i + 1]
    # The relay's own TV link (for the Siri HTTP API) starts first so it is
    # ready even if nobody opens the remote page.
    TV_LINK = TvLink(tv_host)
    log("Siri TV api on http://127.0.0.1:8000/api/tv/<command>  (tv=%s)" % tv_host)
    # Relay socket -- this is the critical path.
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
        handler = functools.partial(ApiHandler, directory=HERE)
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
