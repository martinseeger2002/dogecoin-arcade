# Remote access

The web interface listens on `127.0.0.1:8420` and nowhere else by default.
That is deliberate: the interface can spend the node's coins, so whoever can
reach the port holds the wallet. This page covers the ways to reach your node
from another device, from the tightest to the most open.

## The rule to keep in mind

On your own machine, the interface is your wallet. From anywhere else, it is
a **public arcade**: the feed, collections, tokens and the chain are readable,
people can sign in with their own accounts, and every route that would spend
the node's wallet is refused. The same process serves both; which one a
request gets is decided per request (see *How it tells outside from inside*).

So opening your node to the internet does not hand out your wallet. To manage
the node itself from elsewhere, you sign in as its operator and use the admin
password (see *Managing your node from elsewhere*).

## Option 1: an SSH port-forward

The tightest option, and nothing else here removes it. From the other device:

```bash
ssh -L 8420:127.0.0.1:8420 <user>@<your-node-ip>
```

then open `http://127.0.0.1:8420` on that device. The request arrives at the
node over loopback, so it is treated exactly like a local one: the full wallet.

## Option 2: a temporary Cloudflare address

In the tunnel section of the **Admin** page, the quick option starts a Cloudflare quick tunnel and
gives you a random hostname such as `four-random-words.trycloudflare.com`.
No Cloudflare account, no domain and no router changes are needed.

- The address changes every time a quick tunnel is started.
- It lasts until you stop it in the admin panel or the interface stops;
  killing the `cloudflared` process is what closes a quick tunnel, and nothing
  stays registered with Cloudflare.
- Everything that arrives through it is treated as coming from outside.

## Option 3: your own domain

The same admin page has a step-by-step wizard for putting your node on a
domain you control in Cloudflare. Each step can be run again safely:

1. **cloudflared** — found on the machine, or on Linux downloaded into
   `~/.local/bin` (no sudo). On Windows use `winget install --id
   Cloudflare.cloudflared`; on a Mac, `brew install cloudflared`.
2. **Sign in** — `cloudflared tunnel login` prints a Cloudflare link; open it
   and pick your domain. `cert.pem` lands in `~/.cloudflared`.
3. **Create a named tunnel** — `cloudflared tunnel create <name>`.
4. **Route the names** — `cloudflared tunnel route dns` for `app.<your-domain>`
   and `pages.<your-domain>` (the second serves inscribed pages).
5. **Write the config** — `~/.cloudflared/dogecoinarcade.yml`, pointing each
   name at `http://127.0.0.1:8420`.
6. **Run it as a service** — a user unit, `dogecoinarcade-tunnel.service`,
   running `cloudflared --no-autoupdate --config ~/.cloudflared/dogecoinarcade.yml tunnel run`.
   To keep it running after logout: `sudo loginctl enable-linger <your-user>`.
7. **Tell the arcade its names** — the `public_hosts` and `pages_host`
   settings.
8. **Check from outside** — the public name should answer with the public
   splash page, not the wallet.

A machine that already runs a Cloudflare tunnel is left alone: an existing
`~/.cloudflared/config.yml` is read (to show which names it serves) but never
written. The wizard uses only its own config file and its own service.

The tunnel wizard, the operator setting and the admin password can only be
changed **at the node machine itself**, never from outside.

## How it tells outside from inside

Not by IP address. `cloudflared` runs on your machine and connects to
`127.0.0.1`, so a phone in another country arrives from localhost. A request is
treated as coming from outside when either:

- its `Host` is one of the names in `public_hosts`, or
- it carries a header only an edge proxy adds: `Cf-Ray`, `Cdn-Loop`,
  `Cf-Connecting-Ip` or `X-Forwarded-For`.

The second rule is the one that matters: a request that crossed Cloudflare is
from outside whatever `Host` it claims, so a name nobody remembered to
configure still lands on the public side. The failure that leaves is a local
request wrongly called public, which shows you the splash page instead of your
wallet: visible, and harmless.

An unfamiliar `Host` on its own is **not** treated as remote.

## What outside visitors can reach

The public side is an **allowlist**, not a blocklist: it serves the routes
named in `arcade/web/door.py` and refuses everything else, including any
route added later. In short:

- **Readable:** the splash and join pages, feed, collections, tokens, NFTs,
  inscriptions, exchange and launch pages, the guide and docs, the source
  archive, and each signed-in account's own pages.
- **Accepted POSTs:** only routes where an account acts for itself: signing
  up and in, the admin login, and the build-and-sign handshake, where an
  account signs with a key the node never sees and the node broadcasts only
  the transaction it offered. The same holds for an account's own swap locks,
  escrows, game rooms and LLM keys. Each one is named in `door.py`, one at a
  time.
- **Always refused from outside:** anything that spends the node's wallet,
  the bot RPC (`/rpc/main`, `/rpc/test`), token creation and sending,
  inscription creation, and wizard previews that read the node's disk.

A signed-in session does not change any of this. The refusal is about the
route, not the visitor.

## Managing your node from elsewhere

1. **At the node machine**, sign in with the account you want to be the
   operator and claim the node (this can only be done from the machine itself,
   and only once, after which only the operator can change it).
2. Still at the node machine, set a **remote-access password** in Admin. It
   must be at least 12 characters. Setting or changing it closes every existing
   admin session.
3. From outside, sign in as the operator and open `/admin/login` with that
   password. The login page is rate-limited.

That password guards the node's coins from anywhere, so treat it like a wallet
passphrase.

## Binding to your local network

`arcade-web --host <address>` makes the interface listen on something other
than loopback. Do this only if you understand that anyone who can reach that
address on your network gets the wallet: a request from a LAN address carries
no edge headers, so it is treated as local. The SSH port-forward is the safer
way to use the wallet from another device on your network.

`arcade-web --public` serves the whole instance as a public arcade, with every
form that spends the node's wallet refused, for a node that is only a public server and not anyone's wallet.
