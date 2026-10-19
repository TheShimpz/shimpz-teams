# Browser image

The Team Browser's deployable (ADR-0117): Debian-security Chromium on KasmVNC's X server, the accessibility bus, the
pinned Cua Driver, and `browser-host`, one standard-library Rust program (`host/`). Team creates one container from this
image for each Browser task and removes it when the task ends.

- `browser-host launch` is the container's command. It reads `BROWSER_PROXY` (the egress's private IPv4 address)
  and `BROWSER_LANG` (`en` or `pt-BR` form), starts the display, the session bus, the accessibility bus, and Chromium
  in kiosk mode (no tab strip, no address bar), and exits as soon as any of them stops. Chromium's only route is the
  proxy, loopback excepted, and its DevTools endpoint listens on the container's loopback only.
- The display is `Xkasmvnc` (KasmVNC 1.5.0), started in a user and network namespace of its own whose only interface
  is its loopback (`host/src/display.rs`): its WebSocket port, the HTTP API it serves there, and the UDP listener it
  always opens are unreachable from the container's network, and X clients reach the display through
  `/tmp/.X11-unix/X99`. It refuses every viewer's pointer, key, clipboard, and resize input, never asks for its public
  address, and shares one session among its viewers.
- `browser-host mcp` replaces itself with `cua-driver mcp --grant existing-profile`, the stdio MCP server Team drives
  over `docker exec`. The Cua Driver's telemetry is turned off at launch.
- `browser-host ready` succeeds once the DevTools endpoint answers.
- `browser-host relay` carries one viewer's WebSocket to the display: it enters the display's namespaces, connects to
  its port 6901, drops every capability entering them gave it, and copies its standard streams to that connection.
- `browser-host files list` and `browser-host files read NAME` are Team's only reads of `/downloads`: complete regular
  files with one link, opened without following links, at most 64 listed and 25 MiB each. A partial `.crdownload`
  and a file over the bound are listed as refused by name and size (`incomplete`, `over-limit`) and never read.

`policy.json` is Chromium's managed policy: HTTPS only, downloads to `/downloads` without a prompt and with dangerous
files blocked, at most 32 connections to the proxy (more filled the egress on a heavy page), QUIC, DNS over HTTPS, and
Encrypted Client Hello off, WebRTC limited to the proxy, and sign-in, sync,
password manager, autofill, printing, notifications, geolocation, extensions, file dialogs, and internal schemes off.

`seccomp.json` is Docker's default profile (moby/profiles `6fe7deb1b9fb7c0397a4593480d7d22b9ee8caef`) plus `chroot`,
`clone`, `setns`, and `unshare`, which Chromium's namespace sandbox needs without any capability. Team applies it to
every Browser container; Chromium never runs with `--no-sandbox`.

Every package comes from one Debian snapshot date (`DEBIAN_SNAPSHOT`) over HTTPS, with Chromium pinned to
`CHROMIUM_VERSION`, so a rebuild installs the same Chromium and no older signed index can be replayed. Only `/bin/sh`
remains of the shells, because the X server compiles its keyboard map by running `xkbcomp` through it; bash, perl, python,
dpkg, and apt are removed. `.scripts/local-release/browser-smoke.py` proves the hardening of every built image.

KasmVNC is GPL-2.0. The image carries `/usr/bin/Xkasmvnc` unmodified from the release package, and its corresponding
source beside it in `/usr/share/doc/kasmvnc/` (`SOURCES` maps the binary to the KasmVNC tag archive and the X.Org server
it builds on, each pinned by SHA-256). Only that binary is installed: not its perl wrapper, web client, or tools.
