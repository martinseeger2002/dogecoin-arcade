"""DigiDollar's transactions, written once: its addresses, the "DD" record a transaction carries, choosing the DGB
coins for a fee, and a send.

This file is the DigiDollar module's (examples/packages/digidollar/lib/ddtx.py), and the arcade's full Wallet runs the
very same bytes (arcade/ddlib/ddtx.py, tests/test_digidollar_core.py checks they are one file), so a DigiDollar sent
from the Lite window's module and one sent from the full Wallet's DigiDollar tile are built by the same code
(2026-11-07, SadFrog: "One implementation, two front doors"). Change it here and copy it there.

Plain arithmetic only: it reads no chain and holds no key. Each front door finds the coins its own way (the module
through the arcade's Cores, the full Wallet from the arcade's index) and hands them in. Transactions are built as
DigiByte Core 9.26.7 builds them (src/digidollar/txbuilder.cpp), so a node checks them the same way."""
from __future__ import annotations

from . import taproot as tr

#: DigiByte mainnet and its test chain, testnet26, as the arcade names them.
MAIN, TEST = "dgb", "dgbtest"
#: A DigiDollar send's transaction version: the "DD" marker 0x0770, type 2 in the top byte (primitives/transaction.h).
SEND_VERSION = (2 << 24) | 0x0770
#: A DigiDollar address (DigiByte Core base58.cpp CDigiDollarAddress): base58check of this two-byte version and a
#: Taproot output key. "DD..." on mainnet, "TD..." on the test chain.
DD_VERSIONS = {MAIN: bytes([0x52, 0x85]), TEST: bytes([0xb1, 0x29])}
#: What one DigiDollar output of a send may hold (consensus: $1 to $100,000), in cents.
LEAST_OUTPUT, MOST_OUTPUT = 100, 10_000_000
#: What DigiByte Core's own DigiDollar transactions pay (txbuilder.cpp): 0.35 DGB a kB, and never less than 0.1 DGB.
FEE_PER_BYTE, LEAST_FEE = 35_000, 10_000_000
COIN = 100_000_000
DUST = 10_000
#: The most a DigiDollar transaction leaves to fees (the module's manifest needs.max_fee, which the arcade enforces).
#: At DigiDollar's rate each coin spent adds about 0.05 DGB, so one spends at most about eight.
MAX_FEE = 50_000_000
#: What one DGB coin (a pay-to-key-hash input) and one DigiDollar coin (a Taproot key-path input and its witness) add.
P2PKH_INPUT, DD_INPUT = 148, 41 + 1 + 65
#: What a wallet of many small coins is told to do first, unless the front door says otherwise.
GATHER = "gather them first (Gather, or ddgather), then try again"


# --- addresses -----------------------------------------------------------------------------------------------------

def address_of_key(net: str, key: bytes) -> str:
    """The DigiDollar address on `net` of a Taproot output key."""
    return tr.base58check(DD_VERSIONS[net] + key)


def dd_address(net: str, owner: bytes) -> str:
    """The DigiDollar address of an x-only wallet key on `net`: its Taproot output, the one a mint pays it."""
    return address_of_key(net, tr.tweak(owner))


def dd_key(net: str, address: str) -> bytes:
    """The Taproot output key a DigiDollar address on `net` names, or ValueError in plain words."""
    try:
        body = tr.unbase58check(str(address).strip())
    except ValueError:
        body = b""
    if len(body) != 34 or body[:2] not in DD_VERSIONS.values():
        raise ValueError(f"{address} is not a DigiDollar address: one starts DD (mainnet) or TD (test chain)")
    if body[:2] != DD_VERSIONS[net]:
        raise ValueError(f"{address} is a DigiDollar address on the other chain: this sends on "
                         f"{'mainnet (DD...)' if net == MAIN else 'the test chain (TD...)'}")
    tr.lift_x(body[2:])                                    # a point on the curve, or ValueError
    return body[2:]


# --- reading a transaction -----------------------------------------------------------------------------------------

def _script(out: dict) -> bytes:
    try:
        return bytes.fromhex((out.get("scriptPubKey") or {}).get("hex", ""))
    except ValueError:
        return b""


def record(tx: dict):
    """(type, its numbers, the outputs its amounts belong to) from a decoded transaction's "DD" OP_RETURN, or None.
    A mint's record is 1 <cents> <lock> <tier> <owner>; a send's 2 <cents>...; a redeem's 3 <change cents>. The amounts
    of a send or a redeem belong to its zero-value Taproot outputs, in order; a mint's to its one."""
    vouts = tx.get("vout") or []
    for out in vouts:
        script = _script(out)
        if script[:1] != bytes([tr.OP_RETURN]):
            continue
        try:
            items = tr.pushes(script[1:])
        except ValueError:
            continue
        if len(items) < 2 or items[0] != b"DD":
            continue
        dd = [int(o.get("n", at)) for at, o in enumerate(vouts) if round(float(o.get("value", 0)) * COIN) == 0
              and len(_script(o)) == 34 and _script(o)[:2] == b"\x51\x20"]
        return tr.read_num(items[1]), items[2:], dd
    return None


def amounts(kind: int, items: list) -> list[int]:
    """The cents a record gives its DigiDollar outputs, in order: a mint's and a redeem's one, a send's each."""
    return [tr.read_num(items[0])] if kind in (1, 3) else [tr.read_num(i) for i in items]


def dollars_out(tx: dict) -> list[tuple[int, int, bytes]]:
    """[(vout, cents, output key)] for each DigiDollar output of a decoded transaction. Empty for a transaction
    without a "DD" record, or with one that does not read."""
    try:
        found = record(tx)
        if found is None:
            return []
        kind, items, dd = found
        cents = amounts(kind, items)
    except (ValueError, IndexError):
        return []
    keys = {int(o.get("n", at)): _script(o)[2:] for at, o in enumerate(tx.get("vout") or [])}
    return [(n, c, keys[n]) for n, c in zip(dd, cents) if c > 0]


# --- paying and sending --------------------------------------------------------------------------------------------

def pay(coins: list[dict], outputs: list, need: int, spend_size: int, change_script: bytes, extra: int = 0,
        fresh=None, many: str = GATHER) -> tuple[list[dict], int, int]:
    """(the coins chosen, the fee, the change) to pay `need` plus the fee from `coins` ({txid, vout, value}), largest
    first. `spend_size` is what each coin adds to the transaction, `extra` the bytes of anything else it carries (its
    other inputs and witnesses). Each coin adds to the fee, and the fee may not pass MAX_FEE: a wallet of many small
    coins is told `many`. `fresh(coin)` False passes a coin over (one a transaction still waiting has spent)."""
    chosen, total = [], 0
    for coin in sorted(coins, key=lambda c: -c["value"]):
        if fresh is not None and not fresh(coin):
            continue
        chosen.append(coin)
        total += coin["value"]
        size = 10 + extra + spend_size * len(chosen) + sum(9 + len(s) for _, s in outputs) + 9 + len(change_script)
        fee = max(size * FEE_PER_BYTE, LEAST_FEE)
        if fee > MAX_FEE:
            raise ValueError(f"your DGB is in {len(coins)} coins, and this would need more than {len(chosen) - 1} of "
                             f"them, too many for one DigiDollar transaction's fee: {many}")
        if total >= need + fee:
            change = total - need - fee
            return chosen, (fee + change if change < DUST else fee), (change if change >= DUST else 0)
    raise ValueError(f"your wallet has {total / COIN:,.8f} DGB in confirmed coins and this needs "
                     f"{(need + LEAST_FEE) / COIN:,.8f} DGB or more for its network fee")


def send(owner: bytes, held: list[dict], coins: list[dict], pay_script: bytes, to_key: bytes, cents: int,
         chain: str = TEST, to_address: str = "", fresh=None, many: str = GATHER) -> dict:
    """The prepared send of `cents` DigiDollars to the Taproot output key `to_key`, as DigiByte Core builds one
    (txbuilder.cpp BuildTransferTransaction): the sender's DigiDollar coins (`held`, {txid, vout, cents}) by their key
    path, then DGB coins (`coins`) for the fee; the payee's output, the sender's DigiDollar change, the DGB change to
    `pay_script`, and the record of each output's dollars. `owner` is the sender's x-only key."""
    if not LEAST_OUTPUT <= cents <= MOST_OUTPUT:
        raise ValueError("a DigiDollar send is $1 to $100,000")
    spend, have = [], 0
    for coin in sorted(held, key=lambda c: -c["cents"]):
        if have >= cents:
            break
        if fresh is not None and not fresh(coin):
            continue
        spend.append(coin)
        have += coin["cents"]
    if have < cents:
        raise ValueError(f"you hold ${have / 100:,.2f} in DigiDollars here and this sends ${cents / 100:,.2f}")
    back = have - cents
    if 0 < back < LEAST_OUTPUT:
        raise ValueError(f"that would leave ${back / 100:,.2f} in DigiDollars as change, under DigiDollar's $1 "
                         f"least: send ${have / 100:,.2f}, or at most ${(have - LEAST_OUTPUT) / 100:,.2f}")
    outputs = [(0, tr.p2tr(to_key))] + ([(0, tr.p2tr(tr.tweak(owner)))] if back else [])
    rec = bytes([tr.OP_RETURN]) + tr.push(b"DD") + tr.push(tr.num(2)) + tr.push(tr.num(cents)) \
        + (tr.push(tr.num(back)) if back else b"")
    chosen, _fee, change = pay(coins, outputs + [(0, rec)], 0, P2PKH_INPUT, pay_script,
                               extra=2 + DD_INPUT * len(spend), fresh=fresh, many=many)
    outputs += ([(change, pay_script)] if change else []) + [(0, rec)]
    inputs = [{"txid": c["txid"], "vout": c["vout"], "taproot": {"key": True}} for c in spend]
    inputs += [{"txid": c["txid"], "vout": c["vout"]} for c in chosen]
    return {"chain": chain, "version": SEND_VERSION, "inputs": inputs, "outputs": [(v, s.hex()) for v, s in outputs],
            "what": f"send ${cents / 100:,.2f} DigiDollar to {to_address or to_key.hex()}"
                    + (f", ${back / 100:,.2f} back to you" if back else "")}
