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
- D-pad keys via `ssap://com.webos.service.networkinput/getPointerInputSocket` (`type:button` messages)

## relay.py — fixes instant disconnect (code 1008)

Some TV firmware instantly closes any WebSocket carrying a browser `Origin`
header, while native remote apps pair fine. `relay.py` works around it:

1. In a-Shell, second window (keep the http server running in the first):
   `python3 relay.py`
2. Open the remote page and tap Connect. The page uses the relay automatically
   (`ws://127.0.0.1:8765/?target=...`), which re-opens the connection to the TV
   with no `Origin` header — just like a native app.

Stdlib only, no packages to install. If the relay isn't running, the page falls
back to a direct connection automatically.
