# Arcade Wallet (optional browser extension), 0.2.0

The person's arcade keys, outside every web page. Design: `docs/arcade-lite/EXTENSION.md`.
- **E1:** a vault, opened by a password and locked after 30 idle minutes; creating a wallet, or importing one from
  twelve words or from an arcade @name and password; `window.arcade` with connect, accounts, sign-in and disconnect.
- **E2:** every chain's address and balance, read from a linked arcade; messages signed per chain, and a verifier;
  sends of coins, Omni tokens, arcade NFTs and inscriptions; transactions a site built, signed or sent.

It is a read-only wallet: it runs no node and keeps no chain data. A **linked arcade** (the popup's "Linked
arcades"; bringing an @name links its arcade) reads the chains for it. The wallet signs in there itself with the
account key and uses the account routes the arcade's own Wallet page uses. Keys never leave the extension.

## Try it

1. Open `chrome://extensions` and turn on Developer mode.
2. Choose **Load unpacked** and pick this `extension/` folder. It needs Chrome 137 or newer, for Ed25519 in
   WebCrypto.
3. Open the toolbar button and bring in your arcade account.
4. On any arcade's **Join** page, use **Sign in with Arcade Wallet**.

## For a page: `window.arcade`

```js
const said = await arcade.request({method: "arcade_connect"});          // {pubkey}, after the person says yes
const keys = await arcade.request({method: "arcade_accounts"});         // [{pubkey}] once connected, else []
const sig  = await arcade.request({method: "arcade_signIn", params: {nonce}});
// {pubkey, origin, nonce, signature}: an Ed25519 signature over
// "DogecoinArcade login\n<origin>\n<nonce>", the message every arcade checks (arcade/accounts.py)
const msg = await arcade.request({method: "arcade_signMessage", params: {chain: "doge", message: "text"}});
// {chain, address, message, signature}: Core's signmessage, checked by `verifymessage` on that chain's node
const tx  = await arcade.request({method: "arcade_signTransaction",
                                  params: {chain, raw, sign: [1], sighash: 0x83, inscriptions: ["<id>i0"]}});
// {raw, signed}: `sign` (default: every input of this wallet's), `sighash` 0x01 (default), 0x81 or 0x83
const sent = await arcade.request({method: "arcade_sendTransaction", params: {chain, raw}});   // {txid}
await arcade.request({method: "arcade_disconnect"});
arcade.on("locked", () => {}); arcade.on("disconnect", () => {}); arcade.on("accountChanged", () => {});
```

How it works:
- **Origin:** the origin in a signature is always the page's real origin, as the browser reports it.
- **Approval:** each request that reveals or signs opens the extension's own window first.
- **Finding it:** the extension also announces itself with `arcade:announceProvider`. Fire `arcade:requestProvider`
  to hear it again.

## Same keys as the arcade

`keys.js` is the web wallet's own derivation (`signin.js`, `wallet_js.js`). `coins.js` and `vendor/` are the web
wallet's own files, byte for byte. `tests/test_extension.py` checks the account key against `arcade/seed.py`, and
every chain's address against the same words worked out in Python, and checks that each file is still the arcade's.

## Messages, and checking them

`arcade_signMessage` signs with that chain's address key under the words its own Core uses (`chains.js` `magic`,
each checked against the chain's Core). Kotia, Wojakcoin and DigiByte sign none until theirs is checked. The arcade's
own words (realtime certificates, LLM grants and top-ups, anything starting `arcade-` or `DogecoinArcade`) are never
signed for a site.

`verify.js` and `verify.py` check a sign-in (`verifySignIn` / `verify_sign_in`) and a message (`verifyMessage` /
`verify_message`) with nothing asked of any server. Copy either into your site.

## Sends and transactions

- **From the popup:** the linked arcade builds the send exactly as for its own Wallet page, by its own rules (coin
  choice, inscription guard, fees). The wallet checks the offer (`sends.js`): every digest worked out from the
  bytes (`coins.js` `verifyOffer`), and the amount, token, NFT or inscription going to the payee asked for. It shows
  the transaction decoded from its bytes (`txview.js`), and signs only on "Sign and send".
- **For a site:** `arcade_signTransaction` / `arcade_sendTransaction` (`sitetx.js`). Which inputs are this
  wallet's, and their values, come from the linked arcade, never from the site. Only the signatures
  `arcade/web/docs/exchange-safety.md` allows: ALL, ALL|ANYONECANPAY, SINGLE|ANYONECANPAY. Never NONE.
- **Inscriptions:** on every chain with ords, a coin is spent only once the arcade's ord index has said what it
  carries. A coin, token or NFT send never spends an inscribed coin. A site's transaction may move one only when it
  names that inscription; the window shows the output where it lands.
