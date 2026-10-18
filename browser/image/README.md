# Browser image

The Team Browser's deployable (ADR-0117): Debian-security Chromium, Xvfb, the accessibility bus, the pinned Cua
Driver, and `browser-host`, one standard-library Rust program (`host/`). Team creates one container from this image
for each Browser task and removes it when the task ends.

- `browser-host launch` is the container's command. It reads `BROWSER_PROXY` (the egress's private IPv4 address)
  and `BROWSER_LANG` (`en` or `pt-BR` form), starts Xvfb on its abstract socket, the session bus, the accessibility
  bus, and Chromium, and exits as soon as any of them stops. Chromium's only route is the proxy, loopback excepted,
  and its DevTools endpoint listens on the container's loopback only.
- `browser-host mcp` replaces itself with `cua-driver mcp --grant existing-profile`, the stdio MCP server Team drives
  over `docker exec`. The Cua Driver's telemetry is turned off at launch.
- `browser-host ready` succeeds once the DevTools endpoint answers.
- `browser-host view` streams the screen while Team reads it, for the person watching a task: Xvfb keeps its pixels
  in `/tmp/run/Xvfb_screen0` (`-fbdir`), and every 500 ms the program writes `same` or `rgb 1280 800` and the packed
  RGB pixels. It ends when Team ends the exec's input or stops reading. Team, never Chromium, builds the PNG the
  person's browser decodes.
- `browser-host files list` and `browser-host files read NAME` are Team's only reads of `/downloads`: complete regular
  files with one link, opened without following links, at most 64 listed and 25 MiB each. A partial `.crdownload`
  and a file over the bound are listed as refused by name and size (`incomplete`, `over-limit`) and never read.

`policy.json` is Chromium's managed policy: HTTPS only, downloads to `/downloads` without a prompt and with dangerous
files blocked, QUIC, DNS over HTTPS, and Encrypted Client Hello off, WebRTC limited to the proxy, and sign-in, sync,
password manager, autofill, printing, notifications, geolocation, extensions, file dialogs, and internal schemes off.

`seccomp.json` is Docker's default profile (moby/profiles `6fe7deb1b9fb7c0397a4593480d7d22b9ee8caef`) plus `chroot`,
`clone`, `setns`, and `unshare`, which Chromium's namespace sandbox needs without any capability. Team applies it to
every Browser container; Chromium never runs with `--no-sandbox`.

Every package comes from one Debian snapshot date (`DEBIAN_SNAPSHOT`) over HTTPS, with Chromium pinned to
`CHROMIUM_VERSION`, so a rebuild installs the same Chromium and no older signed index can be replayed. Only `/bin/sh`
remains of the shells, because Xvfb compiles its keyboard map by running `xkbcomp` through it; bash, perl, python,
dpkg, and apt are removed. `.scripts/local-release/browser-smoke.py` proves the hardening of every built image.
