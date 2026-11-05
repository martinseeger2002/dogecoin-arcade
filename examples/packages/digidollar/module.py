"""DigiDollar, a node module for ARCADE Lite: DigiByte's stablecoin, read through the arcade's own DigiByte Cores, and
minted and redeemed from the caller's own wallet.

It holds no keys. It reads the Cores through the arcade (only the reads its manifest names), and a mint or a redeem
comes back to the arcade as a prepared transaction: the arcade checks every coin in it is the caller's, and the
caller's wallet shows it and signs it.

A mint spends ordinary DGB coins, which the wallet signs as any send. A redeem spends the vault by its script path and
the DigiDollars by their key path, both Taproot: the wallet signs those with Schnorr (BIP-340), checking first that each
is its own key's. `ddgather` puts many small coins into one first, as a mint may spend only a few.

Every command takes the chain, test (testnet26) unless main is said. Mainnet mints wait for the arcade's operator:
`ddmainnet on yes` (the README says what to check first). A DigiDollar send is not here yet.
"""
from __future__ import annotations

import html
import json
import time
from decimal import Decimal, InvalidOperation

from lib import taproot as tr

#: DigiByte mainnet and its test chain, testnet26, as this arcade names them. Mints and redeems happen on the test
#: chain only: see the module's README.
MAIN, TEST = "dgb", "dgbtest"
CHAINS = {"main": MAIN, "dgb": MAIN, "mainnet": MAIN, "test": TEST, "dgbtest": TEST, "testnet": TEST}
#: A DigiDollar transaction's version: the "DD" marker 0x0770 with its type in the top byte (primitives/transaction.h):
#: 1 a mint, 2 a send, 3 a redeem.
MINT_VERSION = (1 << 24) | 0x0770
REDEEM_VERSION = (3 << 24) | 0x0770
#: Blocks between building a mint and its lock (consensus/digidollar.h MINT_LOCK_CONFIRMATION_BUFFER_BLOCKS): a mint
#: must be in a block within this many blocks of being built.
BUFFER = 100
BLOCKS_PER_DAY = 5760
#: What DigiByte Core's own mint and redeem pay (txbuilder.cpp): 0.35 DGB a kB, and never less than 0.1 DGB.
FEE_PER_BYTE, LEAST_FEE = 35_000, 10_000_000
COIN = 100_000_000
#: The sequence of an input that waits for the transaction's lock time (a vault's CHECKLOCKTIMEVERIFY).
WAITS = 0xFFFFFFFE
DUST = 10_000
#: The most a transaction this module prepares leaves to fees: the manifest's needs.max_fee, which the arcade enforces.
#: At DigiDollar's rate each coin spent adds about 0.05 DGB, so a mint or a redeem spends at most about eight.
MAX_FEE = 50_000_000
#: A gather is an ordinary send to yourself: twice DigiByte Core's relay fee (0.001 DGB a kB), and at most this many
#: coins (the arcade's own limit on a module's spend).
GATHER_FEE_PER_BYTE, GATHER_MOST = 200, 50
#: What DigiByte Core takes in one mint (consensus: $100 to $100,000), in cents.
LEAST_MINT, MOST_MINT = 10_000, 10_000_000


def _chain(name) -> str:
    try:
        return CHAINS[str(name or "test").lower()]
    except KeyError:
        raise ValueError(f"{name} is not a DigiByte chain here: main or test") from None


def _word(net: str) -> str:
    return "main" if net == MAIN else "test"


def _cents(amount) -> int:
    try:
        dollars = Decimal(str(amount))
    except InvalidOperation:
        raise ValueError(f"{amount} is not an amount of dollars, like 100 or 12.50") from None
    if dollars <= 0 or dollars != dollars.quantize(Decimal("0.01")):
        raise ValueError("an amount of dollars and cents, more than zero")
    return int(dollars * 100)


def _sats(value) -> int:
    return int(Decimal(str(value)) * COIN)


def _active(ctx, chain: str) -> dict:
    """The Core's chain, refused in plain words while it has not checked DigiDollar's first block: a Core still
    syncing answers every DigiDollar read with "not yet active", whatever its headers say. The height is the one that
    Core gives with its answer: while it catches up, the arcade reads getblockchaininfo from an arcade sharing the
    chain, which is further on."""
    said = ctx.rpc(chain, "getdigidollardeploymentinfo")
    if not said.get("enabled"):
        start, tip = said.get("activation_height", 0), (said.get("thaw_day") or {}).get("tip_height")
        if tip is None:
            raise ValueError(f"DigiDollar is not active on this arcade's DigiByte Core yet (from block {start:,})")
        raise ValueError(f"DigiDollar starts at block {start:,}, and this arcade's DigiByte Core has checked blocks "
                         f"to {tip:,}" + (": it is still syncing" if tip < start else ""))
    return ctx.rpc(chain, "getblockchaininfo")


def command_ddstats(ctx, chain="test"):
    """DigiDollar on this arcade's DigiByte: the oracle's price, the supply and how well it is backed.

    ddstats [main|test]   the test chain (testnet26) unless you say main"""
    net = _chain(chain)
    info = _active(ctx, net)
    price = ctx.rpc(net, "getoracleprice")
    stats = ctx.rpc(net, "getdigidollarstats")
    return {"chain": info["chain"], "height": info.get("blocks"),
            "dgb_usd": price.get("price_usd"), "oracle": price.get("status"), "stale": price.get("is_stale"),
            "supply_usd": (stats.get("total_dd_supply") or 0) / 100, "collateral_dgb": stats.get("total_collateral_dgb"),
            "health_percent": stats.get("health_percentage"), "health": stats.get("health_status"),
            "positions": stats.get("active_positions"), "minting": stats.get("minting_restricted_reason") or "open"}


def command_ddcollateral(ctx, amount, tier=0, chain="test"):
    """The DGB a mint of this many dollars locks, for this long."""
    net = _chain(chain)
    _active(ctx, net)
    said = ctx.rpc(net, "estimatecollateral", _cents(amount), int(tier))
    return {"dollars": str(amount), "tier": int(tier), "lock_days": said["lock_days"],
            "locks_dgb": said["wallet_collateral_dgb"], "ratio_percent": said["effective_ratio"],
            "minting": said.get("minting_restricted_reason") or "open"}


def _leaves(owner: bytes, cents: int, lock: int) -> tuple[bytes, bytes]:
    """A vault's two script paths (scripts.cpp CreateNormalRedemptionPath, CreateERRPath): both wait for the lock, and
    both are signed by the owner alone. The second is the one for when the system is under 100% backed."""
    dd = tr.push(tr.num(cents))
    normal = tr.push_int(lock) + bytes([tr.OP_CLTV, tr.OP_DROP, tr.OP_DIGIDOLLAR]) + dd + bytes([tr.OP_DDVERIFY]) \
        + tr.push(owner) + bytes([tr.OP_CHECKSIG])
    emergency = tr.push_int(lock) + bytes([tr.OP_CLTV, tr.OP_DROP]) + tr.push(tr.num(100)) \
        + bytes([tr.OP_CHECKCOLLATERAL, tr.OP_NOT, tr.OP_VERIFY, tr.OP_DIGIDOLLAR]) + dd + bytes([tr.OP_DDVERIFY]) \
        + tr.push(owner) + bytes([tr.OP_CHECKSIG])
    return normal, emergency


def _pay(coins: list[dict], outputs: list, need: int, spend_size: int, change_script: bytes, extra: int = 0):
    """(the coins chosen, the fee, the change) to pay `need` plus the fee from `coins`, largest first. `spend_size`
    is what each coin adds to the transaction, `extra` the bytes of anything else it carries (a witness). Each coin
    adds to the fee, and the fee may not pass MAX_FEE: a wallet of many small coins is gathered first (`ddgather`)."""
    chosen, total = [], 0
    for coin in sorted(coins, key=lambda c: -c["value"]):
        chosen.append(coin)
        total += coin["value"]
        size = 10 + extra + spend_size * len(chosen) + sum(9 + len(s) for _, s in outputs) + 9 + len(change_script)
        fee = max(size * FEE_PER_BYTE, LEAST_FEE)
        if fee > MAX_FEE:
            raise ValueError(f"your DGB is in {len(coins)} coins, and this would need more than {len(chosen) - 1} of "
                             f"them, too many for one DigiDollar transaction's fee: gather them first (Gather, or "
                             f"ddgather), then try again")
        if total >= need + fee:
            change = total - need - fee
            return chosen, (fee + change if change < DUST else fee), (change if change >= DUST else 0)
    raise ValueError(f"your wallet has {total / COIN:,.8f} DGB in confirmed coins and this needs "
                     f"{(need + LEAST_FEE) / COIN:,.8f} DGB or more")


def mint(owner: bytes, cents: int, tier: int, height: int, lock_days: int, collateral: int,
         coins: list[dict], change_script: bytes, chain: str = TEST) -> dict:
    """The prepared mint: the vault, the DigiDollar output, the "DD" record and the change, paid from `coins`.

    `owner` is the caller's x-only key: it alone can redeem the vault and spend the DigiDollars."""
    lock = height + (240 if lock_days == 0 else lock_days * BLOCKS_PER_DAY) + BUFFER
    normal, emergency = _leaves(owner, cents, lock)
    vault = tr.p2tr(tr.tweak(tr.NUMS, tr.branch(tr.leaf(normal), tr.leaf(emergency))))
    record = bytes([tr.OP_RETURN]) + tr.push(b"DD") + tr.push(tr.num(1)) + tr.push(tr.num(cents)) \
        + tr.push(tr.num(lock)) + tr.push(tr.num(tier)) + tr.push(owner)
    outputs = [(collateral, vault), (0, tr.p2tr(tr.tweak(owner))), (0, record)]
    chosen, fee, change = _pay(coins, outputs, collateral, 148, change_script)
    if change:
        outputs.append((change, change_script))
    return {"chain": chain, "version": MINT_VERSION, "inputs": [{"txid": c["txid"], "vout": c["vout"]} for c in chosen],
            "outputs": [(v, s.hex()) for v, s in outputs],
            "what": f"mint ${cents / 100:,.2f} DigiDollar, locking {collateral / COIN:,.8f} DGB until block {lock:,}"}


def _record(tx: dict):
    """(type, its numbers, the outputs its amounts belong to) from a transaction's "DD" OP_RETURN, or None.
    A mint's record is 1 <cents> <lock> <tier> <owner>; a send's 2 <cents>...; a redeem's 3 <change cents>. The amounts
    of a send or a redeem belong to its zero-value Taproot outputs, in order; a mint's to its one."""
    for out in tx.get("vout", []):
        script = bytes.fromhex(out["scriptPubKey"]["hex"])
        if script[:1] != bytes([tr.OP_RETURN]):
            continue
        try:
            items = tr.pushes(script[1:])
        except ValueError:
            continue
        if len(items) < 2 or items[0] != b"DD":
            continue
        kind = tr.read_num(items[1])
        dd = [o["n"] for o in tx["vout"] if round(float(o["value"]) * COIN) == 0
              and o["scriptPubKey"]["hex"].startswith("5120") and len(o["scriptPubKey"]["hex"]) == 68]
        return kind, items[2:], dd
    return None


def _holdings(ctx, net: str, owner: bytes) -> list[dict]:
    """The caller's DigiDollar coins: each unspent zero-value Taproot output of their key, with its amount in cents
    read from the record of the transaction that made it."""
    script = tr.p2tr(tr.tweak(owner))
    found = ctx.rpc(net, "scantxoutset", "start", [f"raw({script.hex()})"])
    out = []
    for u in found.get("unspents", []):
        tx = ctx.rpc(net, "getrawtransaction", u["txid"], True)
        rec = _record(tx)
        if rec is None or u["vout"] not in rec[2]:
            continue
        kind, items, dd = rec
        amounts = [tr.read_num(items[0])] if kind in (1, 3) else [tr.read_num(i) for i in items]
        at = dd.index(u["vout"])
        if at < len(amounts):
            out.append({"txid": u["txid"], "vout": u["vout"], "cents": amounts[at], "kind": kind,
                        "record": items, "height": u.get("height")})
    return out


def _vaults(ctx, net: str, owner: bytes, held: list[dict]) -> list[dict]:
    """The caller's vaults still locked: each mint whose own DigiDollar output is still theirs, and whose vault (its
    first output) is unspent. A vault whose dollars were sent on is not found here (it is still the owner's to redeem
    with other DigiDollars)."""
    out = []
    for coin in held:
        if coin["kind"] != 1 or len(coin["record"]) < 4 or coin["record"][3] != owner:
            continue
        vault = ctx.rpc(net, "gettxout", coin["txid"], 0, True)
        if not vault:
            continue
        out.append({"txid": coin["txid"], "cents": tr.read_num(coin["record"][0]),
                    "lock": tr.read_num(coin["record"][1]), "tier": tr.read_num(coin["record"][2]),
                    "collateral": round(float(vault["value"]) * COIN)})
    return out


def _owner(ctx, net: str) -> tuple[str, bytes]:
    address = ctx.caller.get("address", {}).get(net)
    key = ctx.caller.get("pubkey", {}).get(net)
    if not address:
        raise ValueError("sign in: DigiDollars are paid from and to your own DigiByte wallet")
    if not key:
        raise ValueError("open your wallet in this arcade's window first: a DigiDollar names your key as its owner")
    return address, bytes.fromhex(key)[1:]


#: A mined block's coins can be spent once the chain is this many blocks further (consensus.h COINBASE_MATURITY_2).
RIPE = 100


def _coins(ctx, net: str, address: str) -> tuple[list[dict], bytes]:
    """The caller's coins that can be spent now (a mined block's only once ripe), and the script they pay."""
    found = ctx.rpc(net, "scantxoutset", "start", [f"addr({address})"])
    tip = found.get("height", 0)
    ripe = [u for u in found.get("unspents", []) if not u.get("coinbase") or tip - u.get("height", tip) + 1 >= RIPE]
    coins = [{"txid": u["txid"], "vout": u["vout"], "value": _sats(u["amount"])} for u in ripe]
    return coins, (bytes.fromhex(ripe[0]["scriptPubKey"]) if coins else b"")


def _mainnet_on(ctx) -> bool:
    try:
        return bool(json.loads((ctx.data / "mainnet.json").read_text()).get("on"))
    except (OSError, ValueError, AttributeError):
        return False


def _ready(ctx, net: str) -> dict:
    """The chain's Core, checked to be the chain it is said to be, and DigiDollar active on it."""
    info = _active(ctx, net)
    if (info.get("chain") == "main") != (net == MAIN):
        raise ValueError(f"this arcade's {'DigiByte' if net == MAIN else 'DigiByte testnet'} answers as "
                         f"{info.get('chain')}: refused")
    return info


def command_ddmint(ctx, amount, tier=None, chain="test"):
    """Mint DigiDollars from your own DGB: the DGB stays locked in a vault until the tier's time is up.

    ddmint <dollars> [tier] [main|test]   tier 0 is one hour (test chain only), 1 thirty days, up to 9 for ten years
    Your wallet shows the transaction and signs it. On mainnet only once this arcade's operator has turned it on."""
    net = _chain(chain)
    tier = int(0 if tier is None and net == TEST else 1 if tier is None else tier)
    if net == MAIN and not _mainnet_on(ctx):
        raise ValueError("mainnet mints are off on this arcade: its operator turns them on (ddmainnet), once they "
                         "have read what the module's README asks them to check")
    if net == MAIN and tier == 0:
        raise ValueError("tier 0, one hour, is for the test chain: a mainnet vault locks 30 days (tier 1) or longer")
    if not 0 <= tier <= 9:
        raise ValueError("a tier is 0 to 9")
    cents = _cents(amount)
    if not LEAST_MINT <= cents <= MOST_MINT:
        raise ValueError("a mint is $100 to $100,000")
    _ready(ctx, net)
    address, owner = _owner(ctx, net)
    said = ctx.rpc(net, "estimatecollateral", cents, tier)
    if said.get("minting_restricted"):
        raise ValueError(f"minting is closed now: {said.get('minting_restricted_reason')}")
    coins, script = _coins(ctx, net, address)
    return {"prepared": mint(owner, cents, tier, ctx.rpc(net, "getblockcount"), int(said["lock_days"]),
                             _sats(said["wallet_collateral_dgb"]), coins, script, net)}


def gather(coins: list[dict], script: bytes, chain: str = TEST) -> dict:
    """The prepared gather: up to GATHER_MOST of the caller's smallest coins into one, back to their own address."""
    if len(coins) < 2:
        raise ValueError("your DGB is in one coin already: nothing to gather")
    chosen = sorted(coins, key=lambda c: c["value"])[:GATHER_MOST]
    total = sum(c["value"] for c in chosen)
    fee = (10 + 148 * len(chosen) + 9 + len(script)) * GATHER_FEE_PER_BYTE
    if total - fee < DUST:
        raise ValueError("those coins are worth less than gathering them costs")
    left = len(coins) - len(chosen)
    return {"chain": chain, "version": 2, "inputs": [{"txid": c["txid"], "vout": c["vout"]} for c in chosen],
            "outputs": [(total - fee, script.hex())],
            "what": f"gather {len(chosen)} of your {len(coins)} coins into one ({(total - fee) / COIN:,.8f} DGB) "
                    f"at your own address" + (f"; {left} more after this, gather again once it is in a block"
                                              if left else "")}


def command_ddgather(ctx, chain="test"):
    """Put many small coins of yours into one, so a mint or a redeem can pay from it.

    ddgather [main|test]   up to 50 coins at a time, back to your own address; your wallet signs it"""
    net = _chain(chain)
    _ready(ctx, net)
    address, _ = _owner(ctx, net)
    coins, script = _coins(ctx, net, address)
    return {"prepared": gather(coins, script, net)}


def command_ddvaults(ctx, chain="test"):
    """Your DigiDollars and your vaults: what each vault holds, and when it unlocks.

    ddvaults [main|test]"""
    net = _chain(chain)
    _ready(ctx, net)
    _, owner = _owner(ctx, net)
    height = ctx.rpc(net, "getblockcount")
    held = _holdings(ctx, net, owner)
    vaults = []
    for v in _vaults(ctx, net, owner, held):
        left = v["lock"] - height
        vaults.append({"vault": v["txid"], "dollars": v["cents"] / 100, "locks_dgb": v["collateral"] / COIN,
                       "unlocks_at": v["lock"], "tier": v["tier"],
                       "redeem": "now" if left <= 0 else f"in {left} blocks ({_about(left)})",
                       **({"actions": [{"label": "Redeem", "line": f"ddredeem {v['txid']} {_word(net)}"}]}
                          if left <= 0 else {})})
    return {"chain": _word(net), "height": height, "digidollars": sum(c["cents"] for c in held) / 100,
            "coins": [{"txid": c["txid"], "vout": c["vout"], "dollars": c["cents"] / 100} for c in held],
            "vaults": vaults}


def _about(blocks: int) -> str:
    minutes = blocks * 15 // 60
    return f"about {minutes} minutes" if minutes < 120 else f"about {minutes // 60} hours" if minutes < 2880 \
        else f"about {minutes // 1440} days"


def redeem(vault: dict, owner: bytes, held: list[dict], coins: list[dict], pay_script: bytes,
           chain: str = TEST) -> dict:
    """The prepared redeem of `vault`: the vault by its normal path, DigiDollars of the owner's to burn at least what
    it minted, and a coin of theirs for the fee. Every DGB in the vault comes back to `pay_script`, and DigiDollars
    over the vault's amount come back as DigiDollars (validation.cpp ValidateRedemptionTransaction)."""
    normal, emergency = _leaves(owner, vault["cents"], vault["lock"])
    key, parity = tr.tweaked(tr.NUMS, tr.branch(tr.leaf(normal), tr.leaf(emergency)))
    control = bytes([0xC0 | parity]) + tr.NUMS + tr.leaf(emergency)
    burn, burned = [], 0
    for coin in sorted(held, key=lambda c: (c["txid"] != vault["txid"], -c["cents"])):
        if burned >= vault["cents"]:
            break
        burn.append(coin)
        burned += coin["cents"]
    if burned < vault["cents"]:
        raise ValueError(f"this vault minted ${vault['cents'] / 100:,.2f} and you hold ${burned / 100:,.2f} in "
                         f"DigiDollars: a redeem burns at least what the vault minted")
    outputs = [(vault["collateral"], pay_script)]
    if burned > vault["cents"]:
        outputs += [(0, tr.p2tr(tr.tweak(owner))),
                    (0, bytes([tr.OP_RETURN]) + tr.push(b"DD") + tr.push(tr.num(3))
                     + tr.push(tr.num(burned - vault["cents"])))]
    witness = (1 + 65 + 1 + len(normal) + 1 + len(control)) + (1 + 65) * len(burn)
    chosen, fee, change = _pay(coins, outputs, 0, 148, pay_script, extra=41 * (1 + len(burn)) + witness)
    if change:
        outputs.append((change, pay_script))
    inputs = [{"txid": vault["txid"], "vout": 0, "sequence": WAITS,
               "taproot": {"leaf": normal.hex(), "control": control.hex()}}]
    inputs += [{"txid": c["txid"], "vout": c["vout"], "sequence": WAITS, "taproot": {"key": True}} for c in burn]
    inputs += [{"txid": c["txid"], "vout": c["vout"]} for c in chosen]
    return {"chain": chain, "version": REDEEM_VERSION, "locktime": vault["lock"], "inputs": inputs,
            "outputs": [(v, s.hex()) for v, s in outputs],
            "what": f"redeem ${vault['cents'] / 100:,.2f} DigiDollar: burn ${burned / 100:,.2f} and take back "
                    f"{vault['collateral'] / COIN:,.8f} DGB from its vault"}


def command_ddredeem(ctx, vault, chain="test"):
    """Redeem a vault once it unlocks: burn the DigiDollars it minted and take back all its DGB.

    ddredeem <vault> [main|test]   the vault's transaction id, from ddvaults
    Your wallet shows the transaction and signs it (with Schnorr, as DigiDollar's Taproot coins need)."""
    net = _chain(chain)
    _ready(ctx, net)
    address, owner = _owner(ctx, net)
    held = _holdings(ctx, net, owner)
    found = [v for v in _vaults(ctx, net, owner, held) if v["txid"].startswith(str(vault).lower())]
    if len(found) != 1:
        raise ValueError("that is not one of your locked vaults: ddvaults lists them" if not found else
                         "more than one of your vaults starts like that: give more of its id")
    height = ctx.rpc(net, "getblockcount")
    if height < found[0]["lock"]:
        raise ValueError(f"that vault unlocks at block {found[0]['lock']:,} and the chain is at {height:,}: "
                         f"{found[0]['lock'] - height} blocks more, {_about(found[0]['lock'] - height)}")
    coins, script = _coins(ctx, net, address)
    if not coins:
        raise ValueError("a redeem pays its fee (about 0.15 DGB) from your own DGB, and your wallet has none here")
    return {"prepared": redeem(found[0], owner, held, coins, script, net)}


def command_ddmainnet(ctx, state="status", confirm=""):
    """Whether this arcade's people may mint DigiDollars on DigiByte mainnet, with real DGB (operator only).

    ddmainnet [status|on|off] [yes]   on needs yes; off at once. Off until turned on."""
    if ctx.caller.get("audience") != "operator":
        raise ValueError("only this arcade's operator turns mainnet mints on or off")
    state = str(state or "status").lower()
    path = ctx.data / "mainnet.json"
    if state == "on":
        if str(confirm).lower() != "yes":
            raise ValueError("mainnet mints lock real DGB until their tier's time is up (30 days or more). Read the "
                             "module's README first, then: ddmainnet on yes")
        path.write_text(json.dumps({"on": True, "since": int(time.time())}))
    elif state == "off":
        path.unlink(missing_ok=True)
    elif state != "status":
        raise ValueError("ddmainnet status, ddmainnet on yes, or ddmainnet off")
    on = _mainnet_on(ctx)
    return {"mainnet_mints": "on" if on else "off",
            "means": "people here may mint on DigiByte mainnet with their own DGB" if on else
                     "mints on DigiByte mainnet are refused here; the test chain is open"}


def page(ctx, method, path, query, body):
    """/m/digidollar/: the numbers on both chains, and how to use the forms above it or the console."""
    rows = ""
    for chain, title in (("test", "DigiByte testnet"), ("main", "DigiByte mainnet")):
        rows += f"<tr><th colspan=2>{title}</th></tr>"
        try:
            stats = command_ddstats(ctx, chain)
            rows += "".join(f"<tr><th>{html.escape(k)}</th><td>{html.escape(str(v))}</td></tr>"
                            for k, v in stats.items())
        except Exception as exc:                           # noqa: BLE001 -- the page says what the Core said
            rows += f"<tr><td colspan=2>Its Core did not answer: {html.escape(str(exc))}</td></tr>"
    mainnet = "open on this arcade" if _mainnet_on(ctx) else "off on this arcade until its operator turns them on"
    return {"type": "text/html", "body": PAGE.format(rows=rows, mainnet=mainnet)}


PAGE = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>DigiDollar</title><link rel="stylesheet" href="/m/digidollar/files/page.css">
<h1>DigiDollar</h1><p>DigiByte's stablecoin, read from this arcade's DigiByte Cores. Lock DGB in a vault, get dollars
for it, and burn them later to take every DGB back.</p>
<table>{rows}</table>
<h2>How</h2>
<ol><li><b>Cost:</b> what a mint locks, for each lock time.</li>
<li><b>Gather:</b> many small coins into one; a mint spends only a few.</li>
<li><b>Mint:</b> your wallet shows the transaction and signs it.</li>
<li><b>My vaults:</b> each vault, and when it unlocks; Redeem appears once it has.</li></ol>
<p>Use the forms above this page in the arcade's window, or type the same in its console: <code>ddcollateral 100 0</code>,
<code>ddgather</code>, <code>ddmint 100 0</code>, <code>ddvaults</code>, <code>ddredeem &lt;vault&gt;</code>. Each takes
<code>main</code> or <code>test</code> last. Mainnet mints are {mainnet}.</p>"""
