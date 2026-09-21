# Bot RPC — Omni Core's methods, for scripts

DogecoinArcade serves a JSON-RPC endpoint for programs: airdrop bots, faucets,
leaderboards, anything that reads token balances or moves tokens without a
person clicking. It speaks in [Omni Core's method
names](https://github.com/OmniLayer/omnicore/blob/master/src/omnicore/doc/rpc-api.md),
so anyone who has scripted against Omni knows it already. The code is
`arcade/web/rpc.py`; the tests are `tests/test_rpc_web.py`; a working bot is
`examples/airdrop.py`.

## Where and how

| | |
|---|---|
| URL | `http://127.0.0.1:8420/rpc/test` for testnet, `http://127.0.0.1:8420/rpc/main` for mainnet |
| Auth | HTTP basic auth with the contents of `~/.dogecoinarcade/rpc.cookie` (`__cookie__:<secret>`, mode 0600, rewritten at every start of arcade-web) |
| Body | `{"id": 1, "method": "omni_getbalance", "params": ["nAddress", 3]}` — positional or named params, batches as a list |
| Answer | `{"result": ..., "error": null, "id": 1}`; on failure `error` is `{"code", "message"}` in bitcoind's codes (`src/rpc/protocol.h`): -32601 unknown method, -32602 wrong arguments, -8 bad value, -5 bad address, -4 not this wallet's address |
| Shell | `arcade-rpc omni_listproperties`, `arcade-rpc -main omni_getinfo` — reads the cookie for you; testnet unless `-main`. The installer puts it beside `dogecoinarcade` (`~/.local/bin`, or `%LOCALAPPDATA%\DogecoinArcade\bin` on Windows); an older installation gets it at its next `dogecoinarcade-update` |

Nothing listens anywhere but loopback, and the cookie is the whole key: a
browser tab cannot present it, so this endpoint is out of reach of the
cross-site posts the forms guard against with their CSRF token, and a script
never has a password to keep. If the server restarts, the cookie changes —
read it when you start, as you would bitcoind's.

Python, in full:

```python
import base64, json, urllib.request
from pathlib import Path

cookie = (Path.home() / ".dogecoinarcade" / "rpc.cookie").read_text().strip()
auth = base64.b64encode(cookie.encode()).decode()

def rpc(method, *params):
    body = json.dumps({"id": 1, "method": method, "params": list(params)}).encode()
    req = urllib.request.Request("http://127.0.0.1:8420/rpc/test", data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Basic {auth}"})
    answer = json.loads(urllib.request.urlopen(req).read())
    if answer["error"]:
        raise RuntimeError(answer["error"]["message"])
    return answer["result"]

for holder in rpc("omni_getallbalancesforid", 3):
    print(holder["address"], holder["balance"])
```

## Methods

Reading (no wallet needed):

| Method | Returns |
|---|---|
| `omni_getinfo` | `network`, `mainnet`, `activationblock`, `block` (indexed), `nodeblock`, `behind`, `current`, `stopped` |
| `omni_listproperties` | every token: `propertyid`, `name`, `category`, `subcategory`, `data`, `url`, `divisible` |
| `omni_getproperty propertyid` | the above plus `issuer`, `creationtxid`, `creationblock`, `fixedissuance`, `managedissuance`, `totaltokens`, `holders` |
| `omni_getbalance address propertyid` | `balance`, `reserved`, `frozen` |
| `omni_getallbalancesforid propertyid` | every holder, largest first: `address`, `balance`, `reserved`, `frozen` |
| `omni_getallbalancesforaddress address` | every token one address holds |
| `omni_getwalletbalances` / `omni_getwalletaddressbalances` | what this node's wallet holds, per token / per address |
| `omni_gettransaction txid` | one indexed token transaction: `sendingaddress`, `referenceaddress`, `block`, `blockhash`, `blocktime`, `positioninblock`, `confirmations`, `type_int`, `type`, `valid`, `invalidreason`, `propertyid`, `propertyname`, `divisible`, `amount` |
| `omni_listtransactions [address="*" count=10 skip=0 startblock endblock]` | recent token transactions, oldest first |
| `omni_listblocktransactions height` | txids of the token transactions in a block |
| `help [method]` | usage lines |

Amounts are strings the way Omni gives them: `"1000.00000000"` for a
divisible token, `"1000"` for whole units. `reserved` and `frozen` are always
zero — the arcade has no exchange or freezing yet — and are there so an Omni
client reading them does not break.

Writing (needs the chain's node to have a wallet, and `fromaddress` in it):

| Method | Prepares |
|---|---|
| `omni_send from to propertyid amount` | a send |
| `omni_sendissuancefixed from ecosystem type previousid category subcategory name url data amount` | a fixed-supply token (`type` 1 whole units, 2 divisible; `ecosystem` 1; `previousid` 0) |
| `omni_sendissuancemanaged from ecosystem type previousid category subcategory name url data` | a managed-supply token |
| `omni_sendgrant from to propertyid amount [memo]` | a grant (`""` as `to` grants to the issuer) |
| `omni_sendrevoke from propertyid amount [memo]` | a revoke from the issuer's own balance |
| `omni_sendchangeissuer from to propertyid` | handing the token to a new issuer |
| `omni_broadcast txid` | **sends** a prepared transaction; returns the txid |

A program that is *not* trusted to spend on its own asks instead:

| Method | |
|---|---|
| `da_requestsend from to amount [note]` | ask the owner to send coins; `""` as `from` lets the wallet choose |
| `da_requesttoken from to propertyid amount [note]` | ask the owner to send tokens |
| `da_requestinscription to inscription [note]` | ask the owner to hand over an inscription, by number or txid |
| `da_request id` | the answer: `pending`, `sent` (with `txid`), `denied`, `failed` (with `error`) or `expired` |
| `da_requests [count]` | the ones waiting, then the newest decided |

### Sending is two calls

This is the one place the RPC is deliberately not Omni's. In Omni Core
`omni_send` broadcasts and returns a txid. Here every `omni_send*` method
builds, funds and signs the transaction and returns it **unsent**:

```json
{"txid": "2ea169a4…", "hex": "…",
 "sendingaddress": "nYW2BPLENpu2nGa7WCExvzxD3hQYueULFa",
 "referenceaddress": "ncpXrSCQx667v4y92zSWidxs96N5TtPjTE",
 "class": "C", "size": 257,
 "fee": "0.00257000", "outputscost": "0.01000000", "total": "0.01257000",
 "outputs": [
   {"value": "0.00000000", "to": "token data (OP_RETURN)", "change": false, "recipient": false},
   {"value": "0.01000000", "to": "ncpXrSCQx667v4y92zSWidxs96N5TtPjTE", "change": false, "recipient": true},
   {"value": "1.48683000", "to": "nYW2BPLENpu2nGa7WCExvzxD3hQYueULFa", "change": true, "recipient": false}],
 "broadcast": false}
```

(a real `omni_send` of 1 Arcade Test on testnet; the fee is what the node's
`getmempoolentry` reported for the send before it, byte for byte the same
size)

`omni_broadcast <txid>` then sends exactly those bytes. A bot sees the fee
before it pays it, a typo in an amount costs nothing, and the rule the whole
interface follows — broadcast what was shown (D-016) — holds for scripts too.
A bot that wants Omni's behaviour calls the two in a row. Prepared
transactions are kept in the server's memory (the newest hundred) and
forgotten on restart; `omni_broadcast` of a txid it does not hold is refused,
never rebuilt.

Prepare and broadcast **one at a time**. Two prepares in a row from the same
address pick the same coins, and the second broadcast would be rejected as a
double spend. After a broadcast the change is spendable at once, so the next
prepare chains on it — up to the node's limit of 25 unconfirmed transactions
in a chain (`validation.h: DEFAULT_ANCESTOR_LIMIT`), after which
`omni_broadcast` fails with `too-long-mempool-chain` until a block lands.
`examples/airdrop.py` waits for the block and carries on.

Every refusal happens before anything is spent: a send of more than the
address holds, an address on the other chain, an address this wallet cannot
sign for, an amount with too many decimals, a grant by anyone but the issuer.

### Asking is one call

`omni_send` + `omni_broadcast` is for a program that holds the wallet: the
cookie is the same authority as the shell. A marketplace bot, a game server,
or anything a stranger's code can drive should not have that, and does not
need it. It files a request:

```json
{"method": "da_requesttoken",
 "params": ["", "ncpXrSCQx667v4y92zSWidxs96N5TtPjTE", 3, "5", "tournament prize"]}
```

```json
{"id": "402d9a3126e70394", "network": "test", "kind": "token", "status": "pending",
 "from": null, "to": "ncpXrSCQx667v4y92zSWidxs96N5TtPjTE", "amount": "5",
 "propertyid": 3, "propertyname": "Arcade Test", "origin": "rpc",
 "note": "tournament prize", "txid": null, "error": null, ...}
```

Nothing is built. The request appears on the wallet's Approvals page — on
this machine and on the phone over the remote tunnel — where the owner sees
the transaction it would be, fee and every output, and presses **Approve and
send** or **Refuse**. The bot polls `da_request <id>` until `status` is no
longer `pending`; `sent` carries the `txid`, and `sent` is not `confirmed`:
the node's own `gettransaction <txid>` says how deep it is, as does
`GET /r/tx/<txid>` on the wallet. A request nobody answers within an hour
expires, and no more than twenty may wait at once.

What can be refused at once is refused at once, with `-8`: an address on the
other chain, a token that does not exist, an inscription this wallet does not
hold, an address it cannot sign for. Whether a balance covers it is checked
when the owner looks, because that is when it matters.

The same queue answers an inscribed page over `POST /r/send`
(inscription-api.md), so a page and a bot asking for the same thing look the
same to the person deciding.

### Airdrops

The shape is `omni_getallbalancesforid` for the holders, then `omni_send` +
`omni_broadcast` per holder. That is what `examples/airdrop.py` does:

```
python3 examples/airdrop.py --from nYourAddress --holders-of 3 --token 3 --amount 5
python3 examples/airdrop.py --from nYourAddress --holders-of 3 --token 3 --amount 5 --send
```

Dry run without `--send`; testnet without `--main`. It records each txid in a
done-file so a stopped run resumes without paying anyone twice. Omni's
send-to-owners (type 3), which does this in one transaction, is not
implemented in the engine and a transaction of that type would stop the
index; do not construct one.

### Mainnet

`/rpc/main` answers the same methods. Whether a send there works depends on
the mainnet node having a wallet (it runs `disablewallet=1` until deliberately
changed) — and on the operator: a bot with the cookie can spend from that
wallet, which is the same authority as the shell that runs `pepecoin-cli`.
Test on `/rpc/test` first, as everything here was.
