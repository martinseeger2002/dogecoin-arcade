# Arcade Wallet: an optional browser extension (design draft)

Status: a proposal from SadFrog's side, 2026-11-03, beside the ARCADE Lite charter. No code yet.

## What it is for

The Arcade Wallet is an optional browser extension, like Phantom or MetaMask. It holds a person's arcade keys in the
browser, outside any web page. It lets three kinds of site use them:

1. **Any arcade.** Signing in and signing happen in the extension, so the keys and password never pass through
   pages an operator serves. This answers the reviewer's point that arcade-served wallet code could capture keys.
2. **Builders' own sites** (ARCADE Lite's builder hooks). "Sign in with Arcade" and payments work on any website,
   with no arcade page in between.
3. **Outside marketplaces and apps** that have nothing to do with the arcade. They get a standard provider to
   connect, sign in, sign messages and ask for payments, as they do with other wallets today.

**It stays optional.** Every arcade keeps working in a plain browser with the web wallet it has today. A person
who installs the extension gets the safer path, and nothing else changes for them.

## The same keys, byte for byte

- The extension derives exactly what the arcade's web wallet derives from the same twelve words.
  - The account key is Ed25519 (`signin.js`, `arcade/seed.py`).
  - Coin keys use BIP44 `m/44'/<coin>'/0'/0/<i>` (`coins.js`).
- A person can import their arcade words and see the same @name, addresses and coins.
- **Key derivation joins the conformance vectors** (`tests/conformance`, PR #63). Words in, then account key,
  addresses and WIFs out, checked in the arcade's tests and the extension's.
- **At rest:** an encrypted vault (AES-256-GCM under PBKDF2-HMAC-SHA512, as the web wallet), locked after a timeout.
  Nothing is ever sent anywhere.

## The provider a page sees

The extension injects `window.arcade` and also announces itself with an event, so several wallets can live side by
side. Pages call `arcade.request({method, params})`, the shape other wallets use. Every method that reveals or
signs anything opens the extension's own window. That window shows the requesting site's real origin, and the
person approves or refuses there.

| Method | What it does |
|---|---|
| `arcade_connect` | Ask to connect this origin. Answers the chosen account's @name and public addresses per chain. Remembered per origin until revoked. |
| `arcade_accounts` | The connected account, without a prompt, once connected. |
| `arcade_signIn` | **Sign in with Arcade.** Signs a standard sign-in message with the account key. For arcades the message stays the login message exactly as today (`DogecoinArcade login\n<origin>\n<nonce>`), so arcades need no change. |
| `arcade_signMessage` | Sign text with one chain's address key. The text is shown in full. |
| `arcade_signTransaction` | Sign a transaction the page built. The window shows every output decoded (address, amount, chain), the fee, and any inscription or token it moves. |
| `arcade_sendTransaction` | The same, then broadcast through the person's chosen arcade. |
| `arcade_disconnect` | Forget this origin. |

Events: `accountChanged`, `disconnect`, `locked`.

**The extension never signs without its window.** That includes connected sites; there is no blind signing.

## Sign in with Arcade for outside sites

For a site that is not an arcade, the sign-in message names everything a replay would need to change:
- the site's origin;
- a nonce the site issued;
- the issue time and an expiry;
- the chain;
- the statement the site wants signed.

It is signed with the account's Ed25519 key, so checking it needs no blockchain. A small verifier ships for site
builders, in Python and JavaScript. It returns the account key, and it looks up the @name through any arcade's
`/account/who/` when the site wants the name.

## Safety

- **The real origin is shown,** from the browser and never from the page. A look-alike domain shows its own name.
  Later, the trust badge and trust-circle ideas can mark known arcades.
- **Every sign request is checked.** It is per origin, rate-limited, and refused while the vault is locked.
- **Signing previews are decoded from the transaction itself,** not from what the page says it is.
- **Arcade exchange rules still hold** (`arcade/web/docs/exchange-safety.md`). The extension signs only the sighash
  types those rules allow, and refuses anything that would let someone else change the outputs.
- **Builds are reproducible from the repository** and published with their `sha256`, like releases. Store versions
  are checked against them.

## Where it lives

- **In this repository, under `extension/`.** It shares the conformance vectors and the protocol, and PRs follow the
  Lite charter's rules.
- **Chromium (MV3) first,** then Firefox.
- **No build step,** in the same plain-JavaScript style as `signin.js`.

## Phases

| | What |
|---|---|
| E1 | Import or create words, the vault and lock, `arcade_connect`, `arcade_signIn`. Arcades use the extension for sign-in when it is there. Key derivation added to the conformance vectors. |
| E2 | `arcade_signMessage`, addresses for every chain, and the verifier library for outside sites. |
| E3 | `arcade_signTransaction` / `arcade_sendTransaction` with decoded previews. The arcade's Send and the page API use it when present. |
| E4 | Store listings, reproducible build checks, trust badges. |

## Questions

1. Which name for the extension: "Arcade Wallet"?
2. Should arcades prefer the extension for sign-in when it is installed, or only offer it?
3. Is E3 (transaction signing) in the first public release, or only sign-in and messages?
