"""DigiDollar in the arcade's own wallet (2026-11-05: "add the digi dollar to the main app ... it should be its own
wallet coin option", then: "It should be able to be added, just like any other coin or chain is added or borrowed ...
not dependent on the installation of a module").

DigiDollar is DigiByte's stablecoin (DigiByte Core 9.26): a dollar amount held by a zero-value Taproot output, the
amount written in the transaction's "DD" OP_RETURN record. It is not a chain of its own: wherever this arcade has
DigiByte (its own Core, or one borrowed over the mesh) and an account has a DigiByte address, the Wallet shows DigiDollar
as a coin beside DGB.

* **The coins** come from this arcade's own index of the chain, as DGB's do (`on_block`, beside utxos.on_block): each
  account that opens DigiDollar is watched from then on (`dd_watched`), and its DigiDollar coins are kept in `dd_coin`,
  journalled, so a reorg gives them back. Coins it got before then are read from the chain's own coin set once
  (`seed_from_core`), where this arcade runs its Core: a borrowed Core serves no `scantxoutset`.
* **An address** is the account's key's Taproot output in DigiByte Core's encoding (base58.cpp CDigiDollarAddress):
  base58check of `52 85` ("DD...") on mainnet or `b1 29` ("TD...") on the test chain, and the 32-byte output key.
* **A send** is built here as DigiByte Core builds one (txbuilder.cpp BuildTransferTransaction, version 0x02000770):
  the account's DigiDollar coins by their Taproot key path, DGB coins for the fee, the payee's output, the DigiDollar
  change, the DGB change, and `OP_RETURN "DD" 2 <each output's cents>`. The browser reads it from its own bytes
  (coins.js verifyOffer) and signs the Taproot inputs with Schnorr, as it signs a module's (modhost.build_offer).

Mints and redeems (vaults) stay in the DigiDollar node module (examples/packages/digidollar).
"""
from __future__ import annotations

import hashlib
from typing import Any, Callable

from . import taproot
from .db import register_journalled_table

#: The chains it is on, and their address versions.
NETS = ("dgb", "dgbtest")
VERSIONS = {"dgb": bytes([0x52, 0x85]), "dgbtest": bytes([0xb1, 0x29])}
#: A DigiDollar send's transaction version: the "DD" marker 0x0770, type 2 in the top byte (primitives/transaction.h).
SEND_VERSION = (2 << 24) | 0x0770
#: What one DigiDollar output may hold (consensus: $1 to $100,000), in cents.
LEAST_OUTPUT, MOST_OUTPUT = 100, 10_000_000
#: What DigiByte Core's own DigiDollar transactions pay (txbuilder.cpp): 0.35 DGB a kB, never less than 0.1 DGB.
FEE_PER_BYTE, LEAST_FEE = 35_000, 10_000_000
#: The most a send leaves to fees: at that rate each DGB coin adds about 0.05 DGB, so a send spends at most about eight.
MAX_FEE = 50_000_000
DUST = 10_000
COIN = 100_000_000
OP_RETURN = 0x6A
#: What one DGB coin and one DigiDollar coin add to a send (a P2PKH input; a Taproot key-path input and its witness).
P2PKH_INPUT, DD_INPUT = 148, 41 + 1 + 65

SCHEMA = """
-- An account's DigiDollar coins, unspent: one row per zero-value Taproot output a "DD" record gives dollars to.
CREATE TABLE IF NOT EXISTS dd_coin (
    txid    TEXT    NOT NULL,
    vout    INTEGER NOT NULL,
    address TEXT    NOT NULL,      -- its DigiDollar address, DD... or TD...
    cents   INTEGER NOT NULL,
    height  INTEGER NOT NULL,
    PRIMARY KEY (txid, vout)
);
CREATE INDEX IF NOT EXISTS dd_coin_address ON dd_coin(address);
-- The DigiDollar addresses followed, since when (the index's height when the account first opened DigiDollar).
CREATE TABLE IF NOT EXISTS dd_watched (
    address TEXT PRIMARY KEY,
    since   INTEGER NOT NULL,
    seeded  INTEGER NOT NULL DEFAULT 0     -- 1 once its coins from before `since` were read from the coin set
);
"""


def install(db) -> None:
    db.conn.executescript(SCHEMA)
    register_journalled_table("dd_coin", ("txid", "vout"))


# --- script numbers and pushes, as Bitcoin Core's CScript writes them ---------------------------------------------

def num(n: int) -> bytes:
    """CScriptNum::serialize: little-endian, sign in the top bit, nothing for zero."""
    if n == 0:
        return b""
    out, neg, n = bytearray(), n < 0, abs(n)
    while n:
        out.append(n & 0xFF)
        n >>= 8
    if out[-1] & 0x80:
        out.append(0x80 if neg else 0)
    elif neg:
        out[-1] |= 0x80
    return bytes(out)


def push(data: bytes) -> bytes:
    if len(data) < 0x4C:
        return bytes([len(data)]) + data
    if len(data) <= 0xFF:
        return b"\x4c" + bytes([len(data)]) + data
    return b"\x4d" + len(data).to_bytes(2, "little") + data


def pushes(script: bytes) -> list:
    """A script's items: each push's bytes, and each opcode as an int."""
    out, at = [], 0
    while at < len(script):
        op = script[at]
        at += 1
        if 1 <= op <= 0x4B:
            size = op
        elif op in (0x4C, 0x4D):
            width = 1 if op == 0x4C else 2
            size, at = int.from_bytes(script[at:at + width], "little"), at + width
        else:
            out.append(op)
            continue
        if at + size > len(script):
            raise ValueError("that script ends inside a push")
        out.append(script[at:at + size])
        at += size
    return out


def read_num(data) -> int:
    if isinstance(data, int):
        if data == 0:
            return 0
        if 0x51 <= data <= 0x60:
            return data - 0x50
        raise ValueError(f"opcode {data:#x} is not a number")
    if not data:
        return 0
    n = int.from_bytes(data, "little")
    if data[-1] & 0x80:
        return -(n & ~(0x80 << (8 * (len(data) - 1))))
    return n


B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _sha2(data: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def base58check(body: bytes) -> str:
    whole = body + _sha2(body)[:4]
    n, out = int.from_bytes(whole, "big"), ""
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    return "1" * (len(whole) - len(whole.lstrip(b"\0"))) + out


def unbase58check(text: str) -> bytes:
    n = 0
    for ch in text:
        at = B58.find(ch)
        if at < 0:
            raise ValueError("not base58")
        n = n * 58 + at
    whole = n.to_bytes((n.bit_length() + 7) // 8, "big")
    whole = b"\0" * (len(text) - len(text.lstrip("1"))) + whole
    if len(whole) < 5 or _sha2(whole[:-4])[:4] != whole[-4:]:
        raise ValueError("its check bytes are wrong")
    return whole[:-4]


# --- addresses ---------------------------------------------------------------------------------------------------

def output_key(pubkey: bytes) -> bytes:
    """The Taproot output key of a compressed public key (its key path only, no scripts)."""
    return taproot.tweak(taproot.xonly(pubkey))[0]


def address_of_key(net: str, key: bytes) -> str:
    return base58check(VERSIONS[net] + key)


def address(net: str, pubkey: bytes) -> str:
    """The DigiDollar address of a wallet key on `net`: the one a mint pays it, and a send to it pays."""
    return address_of_key(net, output_key(pubkey))


def key_of(net: str, text: str) -> bytes:
    """The Taproot output key a DigiDollar address on `net` names, or ValueError in plain words."""
    try:
        body = unbase58check(str(text).strip())
    except ValueError:
        body = b""
    if len(body) != 34 or body[:2] not in VERSIONS.values():
        raise ValueError(f"{text} is not a DigiDollar address: one starts DD (mainnet) or TD (test chain)")
    if body[:2] != VERSIONS[net]:
        raise ValueError(f"{text} is a DigiDollar address on the other chain: this sends on "
                         f"{'mainnet (DD...)' if net == 'dgb' else 'the test chain (TD...)'}")
    taproot.lift_x(body[2:])                                 # a point on the curve, or ValueError
    return body[2:]


# --- reading transactions ----------------------------------------------------------------------------------------

def _script(out: dict) -> bytes:
    try:
        return bytes.fromhex((out.get("scriptPubKey") or {}).get("hex", ""))
    except ValueError:
        return b""


def _zero(out: dict) -> bool:
    return round(float(out.get("value", 0)) * COIN) == 0


def dollars_out(tx: dict) -> list[tuple[int, int, bytes]]:
    """[(vout, cents, output key)] for each DigiDollar output of a decoded transaction: its zero-value Taproot outputs,
    in order, with the amounts its "DD" record gives them (a mint's 1 <cents> ..., a send's 2 <cents>..., a redeem's 3
    <change cents>). Empty for a transaction without one."""
    vouts = tx.get("vout") or []
    for out in vouts:
        script = _script(out)
        if script[:1] != bytes([OP_RETURN]):
            continue
        try:
            items = pushes(script[1:])
        except ValueError:
            continue
        if len(items) < 2 or items[0] != b"DD":
            continue
        try:
            kind = read_num(items[1])
            amounts = [read_num(items[2])] if kind in (1, 3) else [read_num(i) for i in items[2:]]
        except (ValueError, IndexError):
            return []
        taps = [(int(o.get("n", at)), _script(o)) for at, o in enumerate(vouts)
                if _zero(o) and len(_script(o)) == 34 and _script(o)[:2] == b"\x51\x20"]
        return [(n, cents, script[2:]) for (n, script), cents in zip(taps, amounts) if cents > 0]
    return []


def watching(db) -> set[str]:
    return {row[0] for row in db.conn.execute("SELECT address FROM dd_watched")}


def watch(db, dd_address: str, height: int) -> bool:
    """Follow a DigiDollar address from `height` on. True when it is new."""
    cur = db.conn.execute("INSERT INTO dd_watched (address, since) VALUES (?, ?) ON CONFLICT(address) DO NOTHING",
                          (dd_address, int(height)))
    return cur.rowcount > 0


def coins(db, dd_address: str) -> list[dict[str, Any]]:
    return [{"txid": r[0], "vout": int(r[1]), "cents": int(r[2]), "height": int(r[3])} for r in db.conn.execute(
        "SELECT txid, vout, cents, height FROM dd_coin WHERE address = ? ORDER BY cents DESC, txid", (dd_address,))]


def watched_row(db, dd_address: str) -> dict | None:
    row = db.conn.execute("SELECT since, seeded FROM dd_watched WHERE address = ?", (dd_address,)).fetchone()
    return {"since": int(row[0]), "seeded": bool(row[1])} if row else None


def on_block(state, height: int, block: dict[str, Any], net: str, watched: set[str]) -> dict[str, int]:
    """What this block pays the watched DigiDollar addresses, then what it spends of theirs (payments first, as
    utxos.on_block, for a coin made and spent in one block). Journalled through `state`."""
    if not watched or net not in VERSIONS:
        return {"added": 0, "spent": 0}
    added = spent = 0
    txs = block.get("tx") or []
    for tx in txs:
        for vout, cents, key in dollars_out(tx):
            where = address_of_key(net, key)
            if where in watched:
                state.insert("dd_coin", {"txid": tx["txid"], "vout": vout, "address": where, "cents": cents,
                                         "height": height})
                added += 1
    for tx in txs:
        for vin in tx.get("vin") or []:
            previous, index = vin.get("txid"), vin.get("vout")
            if previous is None or index is None:
                continue
            row = state.db.conn.execute("SELECT 1 FROM dd_coin WHERE txid = ? AND vout = ?",
                                        (previous, int(index))).fetchone()
            if row is not None:
                state.delete("dd_coin", {"txid": previous, "vout": int(index)})
                spent += 1
    return {"added": added, "spent": spent}


def seed_from_core(index, rpc, net: str, dd_address: str, upto: int) -> int | None:
    """The DigiDollar coins `dd_address` holds now, from the Core's own coin set, up to the index's height `upto`
    (the coins it got before it was watched; utxos.seed_from_core does the same for DGB). None when the Core has no
    `scantxoutset` (a borrowed one)."""
    from .rpc import RpcError
    script = taproot.p2tr(key_of(net, dd_address))
    try:
        said = rpc.call("scantxoutset", "start", [f"raw({script.hex()})"]) or {}
    except RpcError as exc:
        if exc.code == -32601:
            return None
        raise
    filed = 0
    with index.open() as db:
        for coin in said.get("unspents") or []:
            height = int(coin.get("height") or 0)
            if height <= 0 or height > int(upto):
                continue
            tx = rpc.call("getrawtransaction", coin["txid"], True)
            for vout, cents, key in dollars_out(tx):
                if vout == int(coin["vout"]) and address_of_key(net, key) == dd_address:
                    cur = db.conn.execute("INSERT OR IGNORE INTO dd_coin (txid, vout, address, cents, height) "
                                          "VALUES (?,?,?,?,?)", (coin["txid"], vout, dd_address, cents, height))
                    filed += cur.rowcount
        db.conn.execute("UPDATE dd_watched SET seeded = 1 WHERE address = ?", (dd_address,))
    return filed


# --- a send --------------------------------------------------------------------------------------------------------

def _pay(coins_: list[dict], outputs: list, change_script: bytes, extra: int) -> tuple[list[dict], int, int]:
    """(the DGB coins chosen, the fee, the change) for a transaction carrying `outputs` and `extra` bytes more, largest
    coins first, at DigiDollar's fee. Refused past MAX_FEE: a wallet of many small coins is gathered first."""
    chosen, total = [], 0
    for coin in sorted(coins_, key=lambda c: -c["value"]):
        chosen.append(coin)
        total += coin["value"]
        size = 10 + extra + P2PKH_INPUT * len(chosen) + sum(9 + len(s) for _, s in outputs) + 9 + len(change_script)
        fee = max(size * FEE_PER_BYTE, LEAST_FEE)
        if fee > MAX_FEE:
            raise ValueError(f"your DGB is in many small coins, and this would need more than {len(chosen) - 1} of "
                             f"them for its fee: send your DGB to yourself in one coin first, then try again")
        if total >= fee:
            change = total - fee
            return chosen, (fee + change if change < DUST else fee), (change if change >= DUST else 0)
    raise ValueError(f"a DigiDollar send pays its network fee (about 0.15 DGB) from your DGB, and your wallet has "
                     f"{total / COIN:,.8f} DGB it can spend")


def prepare_send(net: str, pubkey: bytes, held: list[dict], dgb: list[dict], change_script: bytes, to: str,
                 cents: int) -> dict:
    """The send of `cents` DigiDollars to the DigiDollar address `to`, as DigiByte Core builds one: `held` are the
    sender's DigiDollar coins ({txid, vout, cents}), `dgb` its DGB coins ({txid, vout, value}) for the fee."""
    to_key = key_of(net, to)
    if not LEAST_OUTPUT <= cents <= MOST_OUTPUT:
        raise ValueError("a DigiDollar send is $1 to $100,000")
    spend, have = [], 0
    for coin in sorted(held, key=lambda c: -c["cents"]):
        if have >= cents:
            break
        spend.append(coin)
        have += coin["cents"]
    if have < cents:
        raise ValueError(f"you hold ${have / 100:,.2f} in DigiDollars and this sends ${cents / 100:,.2f}")
    back = have - cents
    if 0 < back < LEAST_OUTPUT:
        raise ValueError(f"that would leave ${back / 100:,.2f} in DigiDollars as change, under DigiDollar's $1 least: "
                         f"send ${have / 100:,.2f}, or at most ${(have - LEAST_OUTPUT) / 100:,.2f}")
    outputs = [(0, taproot.p2tr(to_key))] + ([(0, taproot.p2tr(output_key(pubkey)))] if back else [])
    record = bytes([OP_RETURN]) + push(b"DD") + push(num(2)) + push(num(cents)) + (push(num(back)) if back else b"")
    chosen, _fee, change = _pay(dgb, outputs + [(0, record)], change_script, extra=2 + DD_INPUT * len(spend))
    outputs += ([(change, change_script)] if change else []) + [(0, record)]
    inputs = [{"txid": c["txid"], "vout": c["vout"], "taproot": {"key": True}} for c in spend]
    inputs += [{"txid": c["txid"], "vout": c["vout"]} for c in chosen]
    return {"chain": net, "version": SEND_VERSION, "inputs": inputs, "outputs": [(v, s.hex()) for v, s in outputs],
            "what": f"send ${cents / 100:,.2f} in DigiDollars to {to}" + (f", ${back / 100:,.2f} back to you" if back else "")}


def offer(prepared: dict, params, call: Callable, dgb_address: str, pubkey: bytes):
    """The unsigned offer for a prepared send: every input read from the chain and checked to be the sender's
    (modhost.build_offer), the fee at most MAX_FEE."""
    from . import modhost
    return modhost.build_offer("this send", prepared, params, call, dgb_address, pubkey, max_fee=MAX_FEE,
                               title="DigiDollar")
