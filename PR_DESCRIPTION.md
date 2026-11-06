# An operator API key must not print keys or spend the node wallet

Draft for the operator. Not opened as a pull request. Line numbers below are the base commit `97e1b74b` (before this change), so they can be checked against that tree.

## Why

A leaked operator bearer key (`Authorization: Bearer ak_...`, owner `node`) is currently treated as the local operator. It skips CSRF, and a key works from anywhere by default. That lets a remote caller run `dumpprivkey` on every address, run `importprivkey`, and fund, sign and broadcast a spend from the node wallet.

The door decides the caller is outside, then returns immediately when the key is an operator's, before the public allow-list, the cross-site check, and the block on `/rpc`:

- `arcade/web/app.py:771` sets `outside = _request_outside(...)`.
- `arcade/web/app.py:773-775` reads the current key and, when `key.operator`, `return await call_next(request)`.

`_request_outside` itself forces that same key to look local. `arcade/web/app.py:681-683`: if `apikeylib.CURRENT.get()` is an operator key, return `False`. Callers that ask "is this request from outside?" are told no.

CSRF does not apply to any API key. `arcade/web/app.py:1493-1494` (`check_csrf`): if `apikeylib.CURRENT.get()` is not `None`, return. A browser on another site cannot attach the bearer token, so the skip is right for a browser, and it also means the key never has to present the form token.

New keys default to the whole internet. `arcade/web/apikeys_web.py:109`: `str(said.get("where") or "anywhere")`. A key made without a `where` is accepted from any address.

With those four, a remote holder of the key can:

- `POST /backup/{which}/print` renders every private key. `arcade/web/app.py:2888-2898` checks CSRF, then `backup.private_keys(rpc)`. `arcade/backup.py:204-206` calls `dumpprivkey` on each address.
- `POST /backup/{which}/import-key` adds a key to the node wallet. `arcade/web/app.py:2911-2922` calls `backup.import_private_key`. `arcade/backup.py:268-269` calls `importprivkey` (and then `dumpprivkey` to name the address that arrived).
- `POST /wallet/send` with `confirmed=yes` funds, signs and broadcasts. `arcade/web/app.py:5436-5458` calls `wallet.prepare_send` and, when confirmed, `wallet.broadcast`. `prepare_send` is `arcade/wallet.py:162` `createrawtransaction`, `arcade/fees.py:160-161` `fundrawtransaction`, and `arcade/wallet.py:193` `signrawtransaction`. `broadcast` is `arcade/wallet.py:231-233` `sendrawtransaction`.

`POST /admin/api/send/confirm` already does not do this. `arcade/web/app.py:31940-31948`: it asks `_admin_check(write=True)`, and when the caller is not local and the admin password does not check, it returns 403 `"the admin password, to send the node's coins"`. An operator key is explicitly not local there. `arcade/web/app.py:29346-29349` (`_admin_check`) returns `{"local": False, "key": key.id}` and does not treat the key as the machine. Confirm therefore already demands the admin password from a remote caller, including a caller who only holds the bearer key. The print, import and send routes never consulted that check. The door had already let the key through as the operator at the machine.

The goal is plain: no node owner's keys or coins should be reachable by anyone who doesn't own that wallet. A bearer operator key alone must not return private keys, import keys, or sign or broadcast a spend from the node wallet.

## What changed

The routes that return the node wallet's secrets, or that sign or broadcast a spend from it, now use the same rule as `POST /admin/api/send/confirm`. One helper, `_require_local_or_admin_password` in `arcade/web/app.py`, calls the existing `_admin_check` and, only when that says the caller is not local, the existing `_admin_password_ok`. A person at the machine never reaches the password hash. A refusal is 403 `"the admin password, to send the node's coins"` and it is raised before any wallet RPC.

Gated, before any wallet call:

- `POST /backup/{which}/print`, `/import-key`, `/save` (`backupwallet` writes the wallet file, which is the keys), and `/restore` (replaces `wallet.dat`).
- `POST /wallet/send` and `POST /wallet/split` (sign and broadcast).
- `POST /node/wallet/send`, `/node/wallet/token/send`, and `/node/wallet/nft/send`. Each of these signs while preparing, before confirm.
- `POST /tokens/send` (sign and broadcast from the node's token wallet). The password is a form field and is not copied into the page's fields.
- `POST /admin/api/send` when the caller is an operator key. Prepare signs. A remote admin session that already opened the panel is unchanged: that session still prepares without a password, and confirm still asks for the password before broadcast.
- `POST /admin/api/send/confirm` now calls the same helper, before its `try`, so the 403 is not turned into a 400. The refusal text is unchanged.
- Lite `send`, from `/lite/api/console` and from `POST /api/v1/run`. An operator key prepares a send only when the body includes the admin password (`password`). `confirm` is untouched: it still refuses anyone who is not at the machine, and it still does not take a password.

Not gated, on purpose:

- `listunspent` and the other reads (`whoami`, module list, balances). They do not return keys and they do not sign or spend. Read-only use of a key keeps working.
- Wallet new, switch, and remove, and `POST /wallet/receive` (`getnewaddress`).
- Bot RPC, which still requires the RPC cookie.
- Messages, the feed, inscriptions, the faucet, the exchange, and `/fund`. Those can spend as a side effect of a product action. This change is the node-wallet routes that exist to reveal keys or to sign and spend. Operator-key automation of the other features is unchanged.

`arcade/web/docs/exchange-safety.md` is unchanged. Nothing about how a transaction is built, signed, or which inputs it may use has moved. Only who may ask the node wallet to do it.

## Compatibility

- A genuinely local operator, at the machine, keeps today's behavior and does not send a password. The password is not hashed on that path.
- A remote admin session (signed in, admin cookie, not a bearer key) still prepares on `POST /admin/api/send` without a password, and still confirms with the password.
- An operator key from outside gets 403 on the gated routes unless the body carries the correct admin password. Wrong or missing passwords are refused before any wallet RPC. The existing scrypt and the per-address attempt limit inside `_admin_password_ok` are unchanged.
- Lite `confirm` stays stronger than the password: the console never broadcasts from anywhere but the machine, even if the key presented the right password at `send`.
- Form posts from the machine still require the CSRF token. A key still skips CSRF, and the new check runs after that skip.

## Tests

`tests/test_wallet_gate.py` covers each gated route four ways: an operator key from outside with no password is refused and the wallet RPC is never opened; the same key with the correct admin password gets past the gate and the routes that talk to the wallet do open it; a local operator, with no key and no password, is allowed as before; a wrong password is refused with no wallet RPC. A further test checks that `whoami`, the module list, and `listunspent` are not the password refusal, and that `confirm` with the key and the correct password is still refused because it is not the machine (`sendrawtransaction` is not called).

Results on this branch:

- `tests/test_wallet_gate.py`: 13 passed.
- `tests/test_apikeys.py`, `tests/test_admin.py`, `tests/test_lite_wallet.py`, `tests/test_public.py`, `tests/test_node_wallet.py`, and `tests/test_web.py`, filtered to backup, csrf, wallet, apikey, admin, lite, public, and send: 229 passed, 16 skipped, 7 errors.
- Those 7 errors are all `RuntimeError: 'pepecoind' not found on PATH` at fixture setup (`test_lite_wallet.py` console tests and `tests/test_node_wallet.py`). The same 7 errors, and the same 3 passing tests in those two files, happen on the unmodified base commit `97e1b74b`.
- `tests/test_tokens_web.py` drives `POST /tokens/send` against a real regtest node. It was not run: this machine has no `pepecoind`. The local form path is what the gate preserves, and the filtered `test_web.py` run covers the backup and wallet forms without a node.
