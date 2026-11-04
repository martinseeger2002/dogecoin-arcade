"""DigiDollar, a node module for ARCADE Lite: DigiByte's stablecoin, read through the arcade's own DigiByte Core and
minted from the caller's own wallet.

It holds no keys. It reads the Core through the arcade (only the reads its manifest names), and a mint comes back to
the arcade as a prepared transaction: the arcade checks every coin in it is the caller's, and the caller's wallet shows
it and signs it.

A mint is the only DigiDollar transaction here. Its inputs are ordinary DGB coins, which the arcade's wallet signs. A
send or a redeem spends DigiDollar's own Taproot outputs, which need a BIP-340 signature that wallet cannot make yet,
so this module mints on test chains only: on mainnet a vault it made could not be redeemed from the same wallet."""
from __future__ import annotations

import html
from decimal import Decimal, InvalidOperation

from lib import taproot as tr

CHAIN = "dgb"
#: A DigiDollar transaction's version: the "DD" marker 0x0770 with its type in the top byte (primitives/transaction.h).
MINT_VERSION = (1 << 24) | 0x0770
#: Blocks between building a mint and its lock (consensus/digidollar.h MINT_LOCK_CONFIRMATION_BUFFER_BLOCKS): a mint
#: must be in a block within this many blocks of being built.
BUFFER = 100
BLOCKS_PER_DAY = 5760
#: What DigiByte Core's own mint pays (txbuilder.cpp): 0.35 DGB a kB, and never less than 0.1 DGB.
FEE_PER_BYTE, LEAST_FEE = 35_000, 10_000_000
COIN = 100_000_000


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


def _active(ctx) -> dict:
    """The Core's chain, refused in plain words while it has not checked DigiDollar's first block: a Core still
    syncing answers every DigiDollar read with "not yet active", whatever its headers say."""
    chain = ctx.rpc(CHAIN, "getblockchaininfo")
    said = ctx.rpc(CHAIN, "getdigidollardeploymentinfo")
    if not said.get("enabled"):
        syncing = f" of {chain['headers']:,}, still syncing" if chain.get("headers", 0) > chain["blocks"] else ""
        raise ValueError(f"DigiDollar starts at block {said.get('activation_height', 0):,}, and this arcade's "
                         f"DigiByte Core has checked blocks to {chain['blocks']:,}{syncing}")
    return chain


def command_ddstats(ctx):
    """DigiDollar on this arcade's DigiByte: the oracle's price, the supply and how well it is backed."""
    chain = _active(ctx)
    price = ctx.rpc(CHAIN, "getoracleprice")
    stats = ctx.rpc(CHAIN, "getdigidollarstats")
    return {"chain": chain["chain"],
            "dgb_usd": price.get("price_usd"), "oracle": price.get("status"), "stale": price.get("is_stale"),
            "supply_usd": (stats.get("total_dd_supply") or 0) / 100, "collateral_dgb": stats.get("total_collateral_dgb"),
            "health_percent": stats.get("health_percentage"), "health": stats.get("health_status"),
            "positions": stats.get("active_positions"), "minting": stats.get("minting_restricted_reason") or "open"}


def command_ddcollateral(ctx, amount, tier=0):
    """The DGB a mint of this many dollars locks, for this long."""
    _active(ctx)
    said = ctx.rpc(CHAIN, "estimatecollateral", _cents(amount), int(tier))
    return {"dollars": str(amount), "tier": int(tier), "lock_days": said["lock_days"],
            "locks_dgb": said["wallet_collateral_dgb"], "ratio_percent": said["effective_ratio"],
            "minting": said.get("minting_restricted_reason") or "open"}


def mint(owner: bytes, cents: int, tier: int, height: int, lock_days: int, collateral: int,
         coins: list[dict], change_script: bytes) -> dict:
    """The prepared mint: the vault, the DigiDollar output, the "DD" record and the change, paid from `coins`.

    `owner` is the caller's x-only key: it alone can redeem the vault and spend the DigiDollars."""
    lock = height + (240 if lock_days == 0 else lock_days * BLOCKS_PER_DAY) + BUFFER
    dd = tr.push(tr.num(cents))
    normal = tr.push_int(lock) + bytes([tr.OP_CLTV, tr.OP_DROP, tr.OP_DIGIDOLLAR]) + dd + bytes([tr.OP_DDVERIFY]) \
        + tr.push(owner) + bytes([tr.OP_CHECKSIG])
    emergency = tr.push_int(lock) + bytes([tr.OP_CLTV, tr.OP_DROP]) + tr.push(tr.num(100)) \
        + bytes([tr.OP_CHECKCOLLATERAL, tr.OP_NOT, tr.OP_VERIFY, tr.OP_DIGIDOLLAR]) + dd + bytes([tr.OP_DDVERIFY]) \
        + tr.push(owner) + bytes([tr.OP_CHECKSIG])
    vault = tr.p2tr(tr.tweak(tr.NUMS, tr.branch(tr.leaf(normal), tr.leaf(emergency))))
    record = bytes([tr.OP_RETURN]) + tr.push(b"DD") + tr.push(tr.num(1)) + dd + tr.push(tr.num(lock)) \
        + tr.push(tr.num(tier)) + tr.push(owner)
    outputs = [(collateral, vault), (0, tr.p2tr(tr.tweak(owner))), (0, record)]
    chosen, total = [], 0
    for coin in sorted(coins, key=lambda c: -c["value"]):
        size = 10 + 148 * (len(chosen) + 1) + sum(9 + len(s) for _, s in outputs) + 9 + len(change_script)
        chosen.append(coin)
        total += coin["value"]
        fee = max(size * FEE_PER_BYTE, LEAST_FEE)
        if total >= collateral + fee:
            break
    else:
        raise ValueError(f"your wallet has {total / COIN:,.8f} DGB in confirmed coins and this mint needs "
                         f"{(collateral + LEAST_FEE) / COIN:,.8f} DGB or more")
    change = total - collateral - fee
    if change >= 1000:
        outputs.append((change, change_script))
    return {"chain": CHAIN, "version": MINT_VERSION, "inputs": [{"txid": c["txid"], "vout": c["vout"]} for c in chosen],
            "outputs": [(v, s.hex()) for v, s in outputs],
            "what": f"mint ${cents / 100:,.2f} DigiDollar, locking {collateral / COIN:,.8f} DGB until block {lock:,}"}


def command_ddmint(ctx, amount, tier=0):
    """Mint DigiDollars from your own DGB: the DGB stays locked in a vault until the tier's time is up.

    ddmint <dollars> [tier]   tier 0 is one hour (test chains), 1 thirty days, up to 9 for ten years
    Your wallet shows the transaction and signs it. Test chains only for now: see the module's README."""
    if _active(ctx)["chain"] == "main":
        raise ValueError("this module mints on DigiByte's test chains only: this arcade's wallet cannot yet sign a "
                         "redeem, so DGB locked on mainnet would wait for another wallet")
    address = ctx.caller.get("address", {}).get(CHAIN)
    key = ctx.caller.get("pubkey", {}).get(CHAIN)
    if not address:
        raise ValueError("sign in: a mint is paid from your own DigiByte wallet")
    if not key:
        raise ValueError("open your wallet in this arcade's window first: a mint names your key as its owner")
    cents, tier = _cents(amount), int(tier)
    said = ctx.rpc(CHAIN, "estimatecollateral", cents, tier)
    if said.get("minting_restricted"):
        raise ValueError(f"minting is closed now: {said.get('minting_restricted_reason')}")
    found = ctx.rpc(CHAIN, "scantxoutset", "start", [f"addr({address})"])
    coins = [{"txid": u["txid"], "vout": u["vout"], "value": _sats(u["amount"])} for u in found.get("unspents", [])]
    script = bytes.fromhex(found["unspents"][0]["scriptPubKey"]) if coins else b""
    return {"prepared": mint(bytes.fromhex(key)[1:], cents, tier, ctx.rpc(CHAIN, "getblockcount"),
                             int(said["lock_days"]), _sats(said["wallet_collateral_dgb"]), coins, script)}


def page(ctx, method, path, query, body):
    """/m/digidollar/: the numbers, and how to mint from the console."""
    try:
        stats = command_ddstats(ctx)
        rows = "".join(f"<tr><th>{html.escape(k)}</th><td>{html.escape(str(v))}</td></tr>" for k, v in stats.items())
    except Exception as exc:                               # noqa: BLE001 -- the page says what the Core said
        rows = f"<tr><td>DigiByte's Core did not answer: {html.escape(str(exc))}</td></tr>"
    return {"type": "text/html", "body": PAGE.format(rows=rows)}


PAGE = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>DigiDollar</title><link rel="stylesheet" href="/m/digidollar/files/page.css">
<h1>DigiDollar</h1><p>DigiByte's stablecoin, read from this arcade's DigiByte Core.</p>
<table>{rows}</table>
<p>To mint, open the arcade's window and type <code>ddmint 100 0</code> in its console: your wallet shows the
transaction and signs it. <code>ddcollateral 100 0</code> says what a mint locks.</p>"""
