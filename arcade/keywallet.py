"""The node's own coins on a chain whose Core it borrows (2026-10-18: "there should be no extra step").

On a borrowed chain this arcade has no Core wallet, so what it is paid there -- a sharer's tenth, its fees, the
coins it mines -- goes to its identity key's address on that chain (state.key_address). This module is that
address's wallet: its coins are read from the chain's own index (utxos.py watches the address like an account's),
and a send is built and signed here with the identity key, which the Pepecoin testnet Core wallet already holds
(`dumpprivkey`). Nothing is imported anywhere and no other wallet is needed.

Plain pay-to-pubkey-hash spends, legacy signatures (every chain the arcade knows takes them).
"""

from __future__ import annotations

import contextlib
import hashlib
import time
from dataclasses import dataclass

from .escrow import serialize, sighash
from .script import b58check_decode, hash160
from .txbuild import p2pkh_script, push

BYTES_PER_INPUT = 148          # outpoint, a DER signature and a compressed key, sequence
BYTES_PER_OUTPUT = 34
BYTES_OVERHEAD = 10
DUST = 100_000                 # 0.001: the arcade's own chains refuse less than 0.01, Litecoin's dust is far lower


class KeyWalletError(Exception):
    """A send that cannot be made, said in words."""


def _wif_secret(wif: str) -> int:
    _version, payload = b58check_decode(wif)
    if len(payload) == 33 and payload[-1] == 1:
        payload = payload[:-1]
    if len(payload) != 32:
        raise KeyWalletError("the node's key is not a private key")
    return int.from_bytes(payload, "big")


def node_key(state):
    """The identity's signing key, read from the Pepecoin testnet Core wallet that holds it, and checked to be the
    key of the identity's address."""
    from .node_trader import HeldKey
    address = state.derived_address
    if not address:
        raise KeyWalletError("this arcade has no identity address yet")
    with state.messaging.rpc() as rpc:
        secret = _wif_secret(str(rpc.call("dumpprivkey", address)))
    key = HeldKey(f"{secret:064x}")
    if hash160(key.pubkey) != b58check_decode(address)[1]:
        raise KeyWalletError("the wallet's key is not the identity's")
    return key


def coins(state, chain) -> list[dict]:
    """The identity address's unspent coins on `chain`, from that chain's index."""
    from . import utxos as utxoslib
    address = state.key_address(chain)
    if not address:
        return []
    index = state.token_index(chain)
    with contextlib.closing(index.open()) as db:
        return list(utxoslib.unspent(db, address))


def balance(state, chain) -> dict:
    """{balance, address} in the chain's coins, as strings like walletlib.balance's."""
    total = sum(int(c["value"]) for c in coins(state, chain))
    amount = f"{total / 1e8:.8f}"
    return {"balance": amount, "spendable": amount, "address": state.key_address(chain)}


@dataclass
class Prepared:
    raw: str
    txid: str
    fee_sats: int
    to: str
    sats: int


def fee_for(n_inputs: int, n_outputs: int, per_kb: int) -> int:
    size = BYTES_OVERHEAD + n_inputs * BYTES_PER_INPUT + n_outputs * BYTES_PER_OUTPUT
    return max(-(-size * per_kb // 1000), 1000)


def prepare(state, chain, to: str, sats: int, key=None) -> Prepared:
    """A signed send of `sats` to `to` from the identity address's coins on `chain`: nothing is broadcast."""
    if sats < DUST:
        raise KeyWalletError(f"the smallest send is {DUST / 1e8:g}")
    mine = state.key_address(chain)
    per_kb = int(getattr(chain.params, "min_fee_per_kb", 0) or 1_000_000)
    pool = sorted(coins(state, chain), key=lambda c: -int(c["value"]))
    chosen, total = [], 0
    for coin in pool:
        chosen.append(coin)
        total += int(coin["value"])
        if total >= sats + fee_for(len(chosen), 2, per_kb):
            break
    fee = fee_for(len(chosen), 2, per_kb)
    if total < sats + fee:
        have = sum(int(c["value"]) for c in pool)
        raise KeyWalletError(f"the node has {have / 1e8:g} {chain.params.coin} here; this needs "
                             f"{(sats + fee) / 1e8:g} with the network fee")
    outputs = [(sats, p2pkh_script(to))]
    change = total - sats - fee
    if change >= DUST:
        outputs.append((change, p2pkh_script(mine)))
    else:
        fee += change                                      # too small to keep: it goes to the miner
    key = key or node_key(state)
    script = p2pkh_script(mine)
    # Kotia's transactions carry a time, in the bytes and in what each input signs (Params.tx_time): without it its
    # Core refused the node's own send (2026-10-27).
    ntime = int(time.time()) if getattr(chain.params, "tx_time", False) else None
    inputs = [{"txid": c["txid"], "vout": int(c["vout"]), "sequence": 0xFFFFFFFF} for c in chosen]
    signed = []
    for n, coin in enumerate(inputs):
        sig = key.sign(sighash(inputs, outputs, n, script, ntime=ntime))
        signed.append({**coin, "script_sig": push(sig) + push(key.pubkey)})
    raw = serialize(signed, outputs, ntime=ntime)
    txid = hashlib.sha256(hashlib.sha256(raw).digest()).digest()[::-1].hex()
    return Prepared(raw=raw.hex(), txid=txid, fee_sats=fee, to=to, sats=sats)


def broadcast(chain, prepared: Prepared) -> str:
    with chain.rpc() as rpc:
        return str(rpc.call("sendrawtransaction", prepared.raw))
