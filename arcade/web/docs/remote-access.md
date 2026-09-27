# Remote — your wallet on your phone, for a while

The interface binds loopback because it can spend. That is right, and it also
meant the only way to reach it from a phone was an SSH port-forward. The
**Remote** page puts a door in that wall: one you open deliberately, can see is
open, and that shuts itself.

Press *Open the tunnel*, choose how long, scan the QR code with your phone's
camera. That is the whole of it — no account, no port forwarding, nothing to
change on your router.

## What actually protects it

A Cloudflare quick tunnel gives out a random hostname
(`four-random-words.trycloudflare.com`). That is not security: a URL is read
over a shoulder, kept in a phone's history, and seen by Cloudflare's edge. So
the tunnel is not what protects the wallet. These are:

| | |
|---|---|
| **A key** | The QR code carries a key as well as the address. Every request from outside must present it. Type the address on its own and you get a locked page — which is what anyone who reads it over your shoulder gets. |
| **A deadline** | 4 hours, 12 hours or a day. The tunnel closes on its own; a door left open by accident is the failure this exists to prevent, and nobody remembers reliably. Pick the shortest that covers what you are doing — a day is a long time for a wallet to be reachable. |
| **No bot RPC** | `/rpc/main` and `/rpc/test` are refused through the tunnel outright (`arcade/web/app.py`, `remote_guard`). The RPC has its own key, in a file on that machine, and it can spend. |

Scanning the code gives whoever scanned it your wallet: sending coins, reading
messages, everything the page in front of you can do. Do not photograph it, do
not forward it, do not leave it on screen.

## How it knows a phone from this machine

Not by address. cloudflared runs on your machine and connects to `127.0.0.1`,
so a phone in another country arrives from localhost. Measured against a live
tunnel: `client 127.0.0.1`, `Host: <name>.trycloudflare.com`, with `Cf-Ray`,
`Cdn-Loop` and `Cf-Connecting-Ip` set. Those headers are what tells them apart,
and Cloudflare overwrites them at the edge, so a client cannot forge them away.

An unfamiliar `Host` on its own is **not** treated as remote — someone reaching
their own wallet at `192.168.1.5:8420` over their own network has not gone
through Cloudflare and is not locked out by a feature they never turned on.

## Closing it

*Close it now* on the page, the deadline, or stopping the interface: any of the
three ends every phone that is signed in. Nothing stays registered with
Cloudflare — killing the process is what closes a quick tunnel — and the key
dies with it, so the link in a phone's history opens nothing afterwards.

## cloudflared

The installer fetches it (step 6) into the same folder as the node binaries, and
the updater keeps it current. It is pinned by version *and* by SHA-256:
Cloudflare publishes no checksum file beside the binaries, so the hashes were
read from GitHub's asset digests and written into `installer/install.py` — the
installer checks against something it brought with it rather than a number from
the same download. Builds are pinned for Linux (x86_64, arm64, armhf, i686),
macOS (Intel and Apple silicon) and Windows.

A machine Cloudflare has no build for still installs; the Remote page simply
says cloudflared is missing and the button is disabled.

## What it is not

It is not a permanent address: every tunnel gets a new hostname. It is not a
way to run the wallet on a server for other people. It is not a substitute for
the SSH port-forward if you already have one — that is still the tighter option,
and nothing here removes it.
