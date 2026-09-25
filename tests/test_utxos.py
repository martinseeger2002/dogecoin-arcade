"""Coins by address, for addresses this node does not own.

The whole reason this exists: `fundrawtransaction` can only choose inputs
from keys the node holds, and in this design it holds none of anybody's.
Without these rows an account can hold coins and never spend one.
"""

import pytest

from arcade import utxos
from arcade.config import NETWORKS
from arcade.db import Database
from arcade.script import b58check_encode
from arcade.txbuild import p2pkh_script
from arcade.state import install_schema

PARAMS = NETWORKS["regtest"]


def an_address(byte: int) -> str:
    return b58check_encode(PARAMS.pubkeyhash_version, bytes([byte]) * 20)


MINE = an_address(0x11)
THEIRS = an_address(0x22)


@pytest.fixture
def db(tmp_path):
    """The index. `StateDB` is what journals changes to it, so blocks are
    connected through one and questions are asked of the other -- which is
    the same split the indexer works with."""
    database = Database(tmp_path / "index.sqlite")
    install_schema(database)
    yield database
    database.close()


def paying(txid: str, *outs, spending=()):
    """A transaction as the node decodes one."""
    return {
        "txid": txid,
        "vin": [{"txid": t, "vout": n} for t, n in spending],
        "vout": [{"n": n, "value": value / 100_000_000,
                  "scriptPubKey": {"hex": p2pkh_script(address).hex()}}
                 for n, (address, value) in enumerate(outs)],
    }


def block(height, *txs):
    return {"hash": f"{height:064x}", "previousblockhash": f"{height - 1:064x}",
            "time": height, "tx": list(txs)}


def rollback(db, height):
    from arcade.db import StateDB

    return StateDB(db).rollback_block(height)


def connect(db, height, *txs):
    from arcade.db import StateDB

    state = StateDB(db)
    with state.block_context(height=height, block_hash=f"{height:064x}",
                             prev_hash=f"{height - 1:064x}", block_time=height,
                             tx_count=len(txs), processed_at=height):
        return utxos.on_block(state, height, block(height, *txs), PARAMS,
                              utxos.watching(db))


# --- what is watched ----------------------------------------------------------

def test_nothing_is_recorded_for_an_address_nobody_asked_about(db):
    """A full UTXO set for a chain this age is millions of rows and would
    be the largest table in the index by an order of magnitude."""
    connect(db, 100, paying("aa" * 32, (MINE, 500), (THEIRS, 700)))
    assert utxos.unspent(db, MINE) == []
    assert utxos.balance(db, MINE) == 0


def test_watching_starts_now_and_says_so(db):
    """Not a rescan. Going back for an address somebody just typed means
    re-reading the chain on a page draw, so it says what is known from here
    instead of pretending."""
    connect(db, 100, paying("aa" * 32, (MINE, 500)))
    utxos.watch(db, MINE, 101, why="an account on this node")
    connect(db, 101, paying("bb" * 32, (MINE, 700)))
    assert utxos.balance(db, MINE) == 700, "the earlier payment is not here"
    assert utxos.since(db, MINE) == 101


def test_watching_twice_does_not_move_the_line(db):
    utxos.watch(db, MINE, 50)
    utxos.watch(db, MINE, 900)
    assert utxos.since(db, MINE) == 50


# --- following the coins -------------------------------------------------------

def test_what_is_paid_in_and_what_is_spent_out(db):
    utxos.watch(db, MINE, 0)
    connect(db, 100, paying("aa" * 32, (MINE, 500), (MINE, 300)))
    assert utxos.balance(db, MINE) == 800
    assert [u["value"] for u in utxos.unspent(db, MINE)] == [500, 300], \
        "largest first, which is the order a funder wants them in"

    connect(db, 101, paying("bb" * 32, (THEIRS, 400),
                            spending=[("aa" * 32, 0)]))
    assert utxos.balance(db, MINE) == 300
    assert [u["vout"] for u in utxos.unspent(db, MINE)] == [1]


def test_change_back_to_the_same_address_survives_its_own_spend(db):
    """Every send in this application pays change back to the sender, so
    the spend and the payment are in one transaction. A spend names the
    previous transaction's id, so recording payments first cannot eat it."""
    utxos.watch(db, MINE, 0)
    connect(db, 100, paying("aa" * 32, (MINE, 1000)))
    connect(db, 101, paying("bb" * 32, (THEIRS, 200), (MINE, 800),
                            spending=[("aa" * 32, 0)]))
    assert utxos.balance(db, MINE) == 800
    assert utxos.unspent(db, MINE)[0]["txid"] == "bb" * 32


def test_a_coin_made_and_spent_inside_one_block_leaves_nothing_behind(db):
    """The node mines a block to confirm a faucet gift, and whatever the
    account spends that gift on can be mined in the same block. The spend
    names a coin this block only just made, so it has to be looked for after
    the payments are recorded -- and a row that outlives its coin stays for
    ever, because no later block ever mentions that outpoint again. On the
    live node that was 34 rows and 1814 coins of balance nobody could spend."""
    utxos.watch(db, MINE, 0)
    moved = connect(db, 100, paying("aa" * 32, (MINE, 1000)),
                    paying("bb" * 32, (THEIRS, 200), (MINE, 799),
                           spending=[("aa" * 32, 0)]))
    assert moved == {"added": 2, "spent": 1}
    assert utxos.balance(db, MINE) == 799
    assert [u["txid"] for u in utxos.unspent(db, MINE)] == ["bb" * 32]

    rollback(db, 100)
    assert utxos.balance(db, MINE) == 0, "and a reorg takes both halves of it back"


def test_a_payload_output_is_not_a_coin(db):
    """A bare multisig output carries Class B data and is never spent in
    practice. Keeping them is how the index stopped being lean last time."""
    from arcade.txbuild import multisig_script

    utxos.watch(db, MINE, 0)
    tx = paying("aa" * 32, (MINE, 500))
    tx["vout"].append({"n": 1, "value": 0.0000006, "scriptPubKey": {
        "hex": multisig_script([b"\x02" + b"\x11" * 32,
                                b"\x03" + b"\x22" * 32], 1).hex()}})
    connect(db, 100, tx)
    assert len(utxos.unspent(db, MINE)) == 1, "the payload output is not a coin"


def test_a_coinbase_spends_nothing(db):
    utxos.watch(db, MINE, 0)
    coinbase = {"txid": "cc" * 32, "vin": [{"coinbase": "03"}],
                "vout": [{"n": 0, "value": 0.00000500,
                          "scriptPubKey": {"hex": p2pkh_script(MINE).hex()}}]}
    connect(db, 100, coinbase)
    assert utxos.balance(db, MINE) == 500


# --- a reorg has to give the coins back ----------------------------------------

def test_a_disconnected_block_unspends_what_it_spent(db):
    """A table written outside the journal survives a reorg and lies
    afterwards, which for coins means offering an input that no longer
    exists and a transaction the network refuses."""
    utxos.watch(db, MINE, 0)
    connect(db, 100, paying("aa" * 32, (MINE, 1000)))
    connect(db, 101, paying("bb" * 32, (THEIRS, 900),
                            spending=[("aa" * 32, 0)]))
    assert utxos.balance(db, MINE) == 0

    rollback(db, 101)
    assert utxos.balance(db, MINE) == 1000, "the coins came back"
    assert utxos.unspent(db, MINE)[0]["txid"] == "aa" * 32


def test_a_disconnected_block_takes_back_what_it_paid(db):
    utxos.watch(db, MINE, 0)
    connect(db, 100, paying("aa" * 32, (MINE, 1000)))
    connect(db, 101, paying("bb" * 32, (MINE, 2000)))
    assert utxos.balance(db, MINE) == 3000
    rollback(db, 101)
    assert utxos.balance(db, MINE) == 1000
