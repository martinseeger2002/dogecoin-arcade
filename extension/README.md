# Arcade Wallet (optional browser extension), 0.1.0

The person's arcade keys, outside every web page. Design: `docs/arcade-lite/EXTENSION.md`. This is phase **E1**:
- a vault, opened by a password and locked after 30 idle minutes;
- creating a wallet, or importing one from twelve words or from an arcade @name and password;
- `window.arcade` with connect, accounts, sign-in and disconnect.

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
await arcade.request({method: "arcade_disconnect"});
arcade.on("locked", () => {}); arcade.on("disconnect", () => {}); arcade.on("accountChanged", () => {});
```

How it works:
- **Origin:** the origin in a signature is always the page's real origin, as the browser reports it.
- **Approval:** each request that reveals or signs opens the extension's own window first.
- **Finding it:** the extension also announces itself with `arcade:announceProvider`. Fire `arcade:requestProvider`
  to hear it again.

## Same keys as the arcade

`keys.js` is the web wallet's own derivation (`signin.js`, `wallet_js.js`). `tests/test_extension.py` checks it
against `arcade/seed.py` byte for byte, and checks that its signatures pass the arcade's own check.
