# webos-remote

A single-file web remote control for webOS TVs (RCA / LG). No build step, no dependencies, no extra hardware — just your iPhone.

## How it works

webOS TVs expose a WebSocket API on the local network (`ws://<tv-ip>:3000`). Browsers block internet-hosted pages from reaching devices on your home network, so this page is served from your own phone and talks **directly** to the TV over Wi-Fi.

## Setup (iPhone)

1. **TV:** Settings → Network → make sure **LG Connect Apps** is ON. Find the TV's IP at Settings → Network → Wi-Fi Connection → Advanced → IP address.
2. **iPhone:** install [a-Shell](https://apps.apple.com/app/a-shell/id1473805438) (free) from the App Store.
3. **In a-Shell**, download this file and serve it:
   ```
   curl -LO https://raw.githubusercontent.com/shahdipesh/webos-remote/main/remote.html
   python3 -m http.server 8000 --bind 127.0.0.1
   ```
4. **In Safari**, open `http://127.0.0.1:8000/remote.html`, enter the TV's IP, tap **Connect**, then accept the pairing prompt on the TV (one time).
5. Optional: **Share → Add to Home Screen** for an app-like remote.

a-Shell only needs to stay open until the page loads — afterwards the page talks straight to the TV and a-Shell can be closed.

## Notes

- The TV must be **on** for the remote to connect. Power-on over the network isn't possible from a webpage (no Wake-on-LAN from browsers); everything else works.
- The pairing key is stored in the browser's localStorage — you only accept the TV prompt once.
- The app tries the secure port first (`wss://<tv-ip>:3001`, 2023+ firmware) and falls back to the plain port (`ws://<tv-ip>:3000`). If the secure port fails, open `https://<tv-ip>:3001` in Safari once, accept the certificate warning, then retry.

## Protocol

Implements the webOS pairing handshake (`type: register` with the standard test manifest) and SSAP requests, mirroring [lgtv2](https://github.com/hobbyquaker/lgtv2):
- `ssap://audio/volumeUp` / `volumeDown` / `setMute` / `getVolume` / `setVolume`
- `ssap://system/turnOff`, `ssap://system.launcher/launch`
- `ssap://tv/channelUp` / `channelDown`
- `ssap://media.controls/play|pause|stop|rewind|fastForward`
- Pointer input via `ssap://com.webos.service.networkinput/getPointerInputSocket`: touchpad cursor (`type:move`), tap-to-click (`type:click`), two-finger scroll (`type:scroll`); the socket is re-acquired lazily if the TV drops it while idle
- Named keys (Back/Home/Exit/OK) via `ssap://com.webos.service.networkinput/sendInputButton` (`{"buttonName": ...}`) — some firmware silently ignores `type:button` on the pointer socket

## How the page reaches the TV

Some TV firmware instantly closes (code 1008) any WebSocket carrying a browser
`Origin` header, while native remote apps pair fine. `relay.py --probe` showed
this TV also accepts `Origin: null` — so the page opens its TV connections
from inside a hidden sandboxed iframe (`socket.html`), whose opaque origin
makes the handshake carry `Origin: null`. No relay, no extra software.

Connection order: direct via iframe → local relay → plain direct.

## relay.py — fallback + page server

`relay.py` re-opens the page's connection toward the TV with no `Origin`
header (like a native app), and it also serves the page itself:

1. In a-Shell: `python3 relay.py`
2. In Safari: `http://127.0.0.1:8000/remote.html` → Connect.

The page talks to the relay at `ws://127.0.0.1:8765/?target=...`. Stdlib only,
no packages to install. It also has a `--probe` mode
(`python3 relay.py --probe [tv-ip]`) that tests which handshake headers the TV
accepts — that experiment is what made the direct iframe connection possible.

**iOS note:** iOS pauses a-Shell when it's in the background, which pauses the
relay too (connections stall, they don't die — taps queue and flush on wake).
The page detects this with a heartbeat and shows "Relay asleep — swipe to
a-Shell and back to wake it". A service worker also caches the page, so it
still opens when a-Shell is asleep. The direct iframe route needs no relay at
all, so none of this applies when the direct connection works.
