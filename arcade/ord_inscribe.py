"""Inscribing an ord -- a Doginal, a Pepinal, a Bellinal, a Luckinal, a Wojakinal or a DigiByte ordinal -- from the account's own wallet (the operator
2026-10-23: "We created a Doge ord inscriber for completing the anti doginal dogs collection I want to add the option to
inscribe an ORD under the creator tab. Make sure that the inscriber follows the rules of each chain individually because
some of them are different"; asked, DOGE, PEP, BEL and LKY first, the player's own wallet signs).

The layout is the one ~/doginal-mint/inscribe.py wrote the Anti Doginal Dogs with (apezord/doginals' `inscribe()`, byte
for byte): the content cut into parts and carried as

    "ord" <n parts> <content type> <n-1> <part 0> <n-2> <part 1> ... <0> <part n-1>

across a chain of P2SH spends. Each lock is <pubkey> OP_CHECKSIGVERIFY OP_DROP*k OP_TRUE, and the spend of a lock carries
its k pushes, the signature and the lock itself. Transaction 0 (the commit, paid from the account's coins) makes the first
lock; transaction i spends lock i with its batch of pushes and pays lock i+1; the last one pays the POSTAGE coin -- the
ord -- to the account's own address. The inscription id is transaction 1's txid + "i0".

One difference from inscribe.py, so the browser signs only scripts of its own key: the commit pays every later fee up
front (the lock holds the postage plus what each reveal will pay), and every reveal has ONE input, its lock, and ONE
output, the next lock or the ord. No wallet coin is spent after the commit, so the wallet stays free while a big file goes
on, and nothing a reveal does can touch any other coin.

What each chain says (RULES), read from its official indexer and its node's relay policy:

* Dogecoin -- wonky-ord / ord-dogecoin read OP_PUSHDATA2 lengths wrongly (ord_rules "dogecoin" dialect), so a part is at
  most 240 bytes (one OP_PUSHDATA1); a part count of 0 is no inscription there, so an empty file is refused; 0.01 DOGE a
  started kB and Dogecoin's soft-dust fee on every output under 0.01.
* Pepecoin -- ord-pepecoin reads every push length correctly and numbers a 0-part inscription, but every Pepinal is
  written in 240-byte parts and pepeblocks reads them so; Dogecoin's fees (Pepecoin is Dogecoin 1.14's code).
* Bellscoin -- Nintondo's ord reads the Doginals kind beside Taproot ones, in the Dogecoin dialect (240-byte parts, no
  empty file); Bells 3 is Bitcoin Core's relay: 0.0001 BEL a kB and no soft-dust fee.
* Luckycoin -- Dogecoin 1.14's code and the Dogecoin dialect: Dogecoin's numbers.

Every chain: a transaction's scriptSig at most 1,650 bytes and a push at most 520 (the batch is 1,500 bytes of pushes,
inscribe.py's MAX_PAYLOAD_LEN), and at most 25 transactions unconfirmed in a row (MAX_UNCONFIRMED keeps under it, so a
big file waits for a block every so often). Before anything is offered, the planned scripts are read back the way that
chain's indexer reads them (ord_rules.read): an envelope the chain's indexer would not read as this file is never sent.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from . import fees, ltc_ord, ord_rules
from .funding import FundingError, Unsigned, sighash
from .ord_chains import word as _word
from .script import b58check_encode, hash160
from .txbuild import build_raw_tx, p2pkh_script, push


@dataclass(frozen=True)
class Rules:
    """How one chain's ords are written."""
    label: str           # what its ords are called
    dialect: str         # ord_rules' reading of the envelope on this chain
    part: int            # bytes a part carries at most
    empty_ok: bool       # whether a 0-part (empty) inscription is one there
    indexer: str         # whose reading this follows, said on the page


#: Bytes of pushes a reveal carries (inscribe.py MAX_PAYLOAD_LEN): with the signature and the lock it stays well under a
#: standard scriptSig's 1,650.
PAYLOAD = 1500
#: The ord's own coin (inscribe.py POSTAGE): 0.001 of the coin on every chain, above every chain's dust line.
POSTAGE = 100_000
#: Transactions of one ord left unconfirmed at once. The chains' relay refuses a 26th in a row (25 ancestors with
#: Dogecoin 1.14's and Bitcoin Core's defaults); the commit may stand on unconfirmed change of its own, so a few spare.
MAX_UNCONFIRMED = 20
#: The biggest file this inscriber takes: 400 kB is some 270 transactions, a few hours of blocks at most.
MAX_BYTES = 400_000

#: Each chain's word for one is ord_chains.WORDS's, the one the wallet, the Exchange and the swaps say too.
RULES = {
    "doge": Rules(_word("doge"), "dogecoin", 240, False, "wonky-ord / ord-dogecoin"),
    "main": Rules(_word("main"), "pepecoin", 240, True, "ord-pepecoin"),
    "bel": Rules(_word("bel"), "dogecoin", 240, False, "Nintondo ord (Doginals kind)"),
    "lky": Rules(_word("lky"), "dogecoin", 240, False, "Doginals-kind indexers"),
    "wjk": Rules(_word("wjk"), "dogecoin", 240, False, "wojakinals (Doginals kind)"),
    # digibyteordinals.js writes 240-byte parts and wonky-ord-digibyte reads a 0-part envelope as nothing (2026-10-25).
    "dgb": Rules(_word("dgb"), "dogecoin", 240, False, "wonky-ord-digibyte (Doginals kind)"),
    # Pepecoin testnet, for trying it with coins that cost nothing.
    "test": Rules(_word("test"), "pepecoin", 240, True, "pepecoin-ord-indexer"),
}
#: The ones the page offers first (the operator's answer); test chains are added where the arcade runs them.
CHAINS = ("doge", "main", "bel", "lky", "wjk", "dgb")


class OrdError(ValueError):
    pass


def rules_of(net: str) -> Rules:
    got = RULES.get(net)
    if got is None:
        raise OrdError("ords are inscribed on Dogecoin, Pepecoin, Bellscoin, Luckycoin, Wojakcoin and DigiByte")
    return got


def number(n: int) -> bytes:
    """A countdown or part count as doginals.js `numberToChunk` writes it: OP_0, OP_1..OP_16, else 1 or 2 bytes."""
    if n == 0:
        return b"\x00"
    if n <= 16:
        return bytes([0x50 + n])
    if n < 128:
        return b"\x01" + bytes([n])
    if n < 0x8000:
        return b"\x02" + bytes([n % 256, n // 256])
    raise OrdError("that file has too many parts")


def pushes_of(content_type: str, content: bytes, rules: Rules) -> list[bytes]:
    """Every push of the envelope, each already script: "ord", the part count, the type, then countdown and part."""
    kind = (content_type or "").strip()
    if not kind or len(kind.encode()) > 80:
        raise OrdError("a content type is required (at most 80 bytes)")
    if not content and not rules.empty_ok:
        raise OrdError(f"an empty file is not an inscription on this chain ({rules.indexer})")
    if len(content) > MAX_BYTES:
        raise OrdError(f"{len(content):,} bytes is more than this inscriber takes ({MAX_BYTES:,})")
    parts = [content[i:i + rules.part] for i in range(0, len(content), rules.part)]
    out = [push(b"ord"), number(len(parts)), push(kind.encode())]
    for n, part in enumerate(parts):
        out += [number(len(parts) - n - 1), ltc_ord.data(part)]
    return out


def batches(pushes: list[bytes]) -> list[list[bytes]]:
    """The pushes cut into one batch per reveal, the way inscribe.py cuts them: "ord" goes with the first, then whole
    (countdown, part) pairs while the batch stays within PAYLOAD."""
    chunks = list(pushes)
    out, first = [], True
    while chunks:
        batch = []
        if first:
            batch.append(chunks.pop(0))
            first = False
        while sum(map(len, batch)) <= PAYLOAD and chunks:
            batch += [chunks.pop(0), chunks.pop(0)]
        if sum(map(len, batch)) > PAYLOAD and len(batch) > 2:
            b, a = batch.pop(), batch.pop()
            chunks = [a, b] + chunks
        out.append(batch)
    return out


def lock_of(pubkey: bytes, batch: list[bytes]) -> bytes:
    return push(pubkey) + b"\xad" + b"\x75" * len(batch) + b"\x51"


def p2sh(script: bytes) -> bytes:
    return b"\xa9\x14" + hash160(script) + b"\x87"


def _reveal_size(batch: list[bytes], lock: bytes, out_script: bytes) -> int:
    script_sig = sum(map(len, batch)) + 74 + len(push(lock))        # a signature is 73 bytes at most, plus its push
    return (4 + 1 + 36 + (1 if script_sig < 253 else 3) + script_sig + 4
            + 1 + 8 + 1 + len(out_script) + 4)


@dataclass
class Plan:
    net: str
    content_type: str
    size: int
    sha256: str
    batches: list[list[bytes]]
    locks: list[bytes]
    values: list[int]        # values[i]: what lock i holds (i = 0 is the commit's output)
    fees: list[int]          # fees[i]: what reveal i pays
    to: str                  # where the ord lands: the account's own address

    @property
    def reveals(self) -> int:
        return len(self.batches)

    @property
    def commit_value(self) -> int:
        return self.values[0]

    def out_script(self, i: int) -> bytes:
        return p2sh(self.locks[i + 1]) if i + 1 < len(self.locks) else p2pkh_script(self.to)

    def out_value(self, i: int) -> int:
        return self.values[i + 1] if i + 1 < len(self.values) else POSTAGE


def plan(net: str, params, content_type: str, content: bytes, pubkey: bytes, to: str) -> Plan:
    """The whole chain of reveals, worked backwards from the ord: the last reveal pays POSTAGE to `to`, and every lock
    holds what the reveals after it pay plus the postage, at this chain's own fee rate and dust rule. Read back through
    the chain's indexer rules before it is returned."""
    rules = rules_of(net)
    if len(pubkey) != 33:
        raise OrdError("a compressed public key is required")
    bs = batches(pushes_of(content_type, content, rules))
    locks = [lock_of(pubkey, b) for b in bs]
    rate = fees.rate_for(params, fees.MIN_FEE_PER_KB)
    soft = bool(getattr(params, "soft_dust", True))
    n = len(bs)
    values, paid = [0] * n, [0] * n
    after = POSTAGE                                  # what the output of the reveal being priced holds
    for i in range(n - 1, -1, -1):
        out = p2sh(locks[i + 1]) if i + 1 < n else p2pkh_script(to)
        fee = fees.fee_for(_reveal_size(bs[i], locks[i], out), 1, rate)
        if soft:
            fee += fees.soft_dust_fee([(after, out)])
        paid[i] = fee
        values[i] = after + fee
        after = values[i]
    got = Plan(net, content_type.strip(), len(content), hashlib.sha256(content).hexdigest(), bs, locks, values, paid,
               to)
    read_back(got, content)
    return got


def read_back(p: Plan, content: bytes) -> None:
    """The reveals' scripts as the chain's indexer reads them (ord_rules.read, its dialect): one complete inscription
    of exactly this type and these bytes, or OrdError and nothing is sent."""
    rules = rules_of(p.net)
    scripts = [b"".join(b) + push(b"\x30" * 72) + push(lock) for b, lock in zip(p.batches, p.locks)]
    state, ctype, body, _tags = ord_rules.read(scripts, rules.dialect)
    if state != "complete" or ctype != p.content_type.encode() or body != content:
        raise OrdError(f"this file would not read back as one inscription under {rules.indexer}'s rules; nothing was sent")
    # And no earlier reveal completes it early (a chain stops being followed once it is complete).
    for k in range(1, len(scripts)):
        early = ord_rules.read(scripts[:k], rules.dialect)[0]
        if early != "partial":
            raise OrdError("the envelope would end before its last part; nothing was sent")


def build_reveal(params, p: Plan, i: int, lock_coin: dict, what: str = "") -> Unsigned:
    """Reveal i, unsigned: lock i ({txid, vout}) in, carrying batch i, and lock i+1 (or the ord) out. The browser signs
    over the lock (coins.js `noteKey`: its own key's) and checks the output is the next lock of its own key
    (`next_lock`) or its own address."""
    lock = p.locks[i]
    value = p.values[i]
    outputs = [(p.out_value(i), p.out_script(i))]
    given = [{"txid": str(lock_coin["txid"]), "vout": int(lock_coin["vout"]), "value": value,
              "address": b58check_encode(params.scripthash_version, hash160(lock)),
              "redeem": lock.hex(), "envelope": b"".join(p.batches[i]).hex()}]
    raw = build_raw_tx([(given[0]["txid"], given[0]["vout"])], outputs)
    return Unsigned(raw=raw, inputs=given, outputs=outputs, sighashes=[sighash(given, outputs, 0, lock).hex()],
                    fee=value - outputs[0][0], change=0, what=what)


def costs(p: Plan) -> dict:
    """What the ord costs, in the chain's smallest units: the reveals' fees, the postage (it stays the account's: it
    is the ord), and how many transactions; the commit's own fee is the wallet's and is added by the caller."""
    return {"reveals": p.reveals, "reveal_fees": sum(p.fees), "postage": POSTAGE, "locked": p.commit_value,
            "blocks_at_least": max(1, -(-(p.reveals + 1) // MAX_UNCONFIRMED))}
