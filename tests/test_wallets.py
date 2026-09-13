"""Several wallets per chain, on nodes that have no multiwallet RPC.

These nodes predate createwallet/loadwallet/listwallets entirely -- one wallet
file per process, named with `-wallet=` at startup. So switching is a file swap
around a node stop, and the tests that matter are about not losing anyone's
coins while doing it.

The active file deliberately keeps the default name `wallet.dat`, so switching
never has to edit a service definition and works on installations whose units
this application did not write.
"""

import json
from pathlib import Path

import pytest

from arcade import backup


class FakeNode:
    """A node that stops when asked and is considered down afterwards."""

    def __init__(self):
        self.stops = 0
        self.running = True

    def __call__(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def call(self, method, *args):
        if method == "stop":
            self.stops += 1
            self.running = False
            return "stopping"
        if not self.running:
            raise RuntimeError("connection refused")
        return 0


@pytest.fixture
def node_dir(tmp_path):
    """A testnet-shaped data directory with a wallet already in place."""
    datadir = tmp_path / "node"
    (datadir / "testnet3").mkdir(parents=True)
    (datadir / "testnet3" / "wallet.dat").write_bytes(b"ORIGINAL WALLET")
    return datadir


def test_a_fresh_installation_lists_one_wallet(node_dir):
    entries = backup.list_wallets(node_dir, "test")
    assert len(entries) == 1
    assert entries[0].name == backup.DEFAULT_WALLET
    assert entries[0].active


def test_creating_a_wallet_keeps_the_old_one(node_dir):
    """The new wallet is empty by definition; the old one holds the coins."""
    node = FakeNode()
    result = backup.create_wallet(node, node_dir, "test", "savings")

    assert node.stops == 1
    assert not backup.wallet_path(node_dir, "test").exists(), (
        "the node makes the new wallet itself on startup"
    )
    parked = Path(result["previous_saved_to"])
    assert parked.read_bytes() == b"ORIGINAL WALLET"
    assert backup.active_wallet_name(node_dir, "test") == "savings"


def test_switching_back_restores_the_original_file(node_dir):
    node = FakeNode()
    backup.create_wallet(node, node_dir, "test", "savings")

    # The node has since made a wallet for "savings".
    backup.wallet_path(node_dir, "test").write_bytes(b"SAVINGS WALLET")

    node.running = True
    backup.switch_wallet(node, node_dir, "test", backup.DEFAULT_WALLET)

    assert backup.wallet_path(node_dir, "test").read_bytes() == b"ORIGINAL WALLET"
    assert backup.active_wallet_name(node_dir, "test") == backup.DEFAULT_WALLET
    library = backup.wallet_library(node_dir, "test")
    assert (library / "savings.dat").read_bytes() == b"SAVINGS WALLET", (
        "switching away must keep the wallet being left"
    )


def test_both_wallets_are_listed_with_one_active(node_dir):
    node = FakeNode()
    backup.create_wallet(node, node_dir, "test", "savings")
    backup.wallet_path(node_dir, "test").write_bytes(b"SAVINGS WALLET")

    entries = backup.list_wallets(node_dir, "test")
    by_name = {e.name: e for e in entries}
    assert set(by_name) == {"main", "savings"}
    assert by_name["savings"].active
    assert not by_name["main"].active


def test_the_active_wallet_cannot_be_removed(node_dir):
    """Removing the wallet in use would leave the node with nothing to load."""
    node = FakeNode()
    backup.create_wallet(node, node_dir, "test", "savings")

    with pytest.raises(backup.BackupError) as caught:
        backup.remove_wallet(node_dir, "test", "savings")
    assert "in use" in str(caught.value)


def test_removing_moves_the_file_rather_than_deleting_it(node_dir):
    """A wallet file may hold coins nothing else records. Never unlink it."""
    node = FakeNode()
    backup.create_wallet(node, node_dir, "test", "savings")

    result = backup.remove_wallet(node_dir, "test", "main")

    assert not (backup.wallet_library(node_dir, "test") / "main.dat").exists()
    moved = Path(result["moved_to"])
    assert moved.is_file()
    assert moved.read_bytes() == b"ORIGINAL WALLET"


def test_a_duplicate_name_is_refused(node_dir):
    node = FakeNode()
    backup.create_wallet(node, node_dir, "test", "savings")
    with pytest.raises(backup.BackupError):
        backup.create_wallet(node, node_dir, "test", "Savings")


@pytest.mark.parametrize("name", ["", "   ", "../escape", "with/slash", "x" * 64,
                                  "-leading-hyphen", "tab\there"])
def test_a_bad_name_is_refused(node_dir, name):
    """Names become filenames. A path separator here would write anywhere."""
    with pytest.raises(backup.BackupError):
        backup.check_wallet_name(name, node_dir, "test")


def test_switching_to_something_that_does_not_exist_is_refused(node_dir):
    node = FakeNode()
    with pytest.raises(backup.BackupError):
        backup.switch_wallet(node, node_dir, "test", "imaginary")
    assert node.stops == 0, "nothing should have been stopped"


def test_switching_to_the_active_wallet_is_refused(node_dir):
    node = FakeNode()
    backup.create_wallet(node, node_dir, "test", "savings")
    node.running = True
    with pytest.raises(backup.BackupError):
        backup.switch_wallet(node, node_dir, "test", "savings")


def test_the_manifest_is_readable_json(node_dir):
    node = FakeNode()
    backup.create_wallet(node, node_dir, "test", "savings")
    data = json.loads((backup.wallet_library(node_dir, "test") / "active.json").read_text())
    assert data == {"active": "savings"}


def test_a_missing_manifest_falls_back_to_the_default_name(node_dir):
    assert backup.active_wallet_name(node_dir, "test") == backup.DEFAULT_WALLET


# --- schema migration ---------------------------------------------------------
# Found when a real message arrived from the other machine and the scan died with
# "table contact has no column named updated". `CREATE TABLE IF NOT EXISTS` does
# nothing to a table that already exists, so every store made before the address
# book gained columns kept the old shape and failed on the first write.


OLD_SCHEMA = """
CREATE TABLE contact (pubkey BLOB PRIMARY KEY, name TEXT NOT NULL DEFAULT '',
                      address TEXT NOT NULL DEFAULT '', added INTEGER NOT NULL);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO contact VALUES (X'0102', 'Old Friend', 'nOldAddress', 1);
"""


def _old_store(tmp_path):
    import sqlite3
    path = tmp_path / "old.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(OLD_SCHEMA)
    connection.commit()
    connection.close()
    return path


def test_an_old_store_gains_the_new_columns(tmp_path):
    from arcade.messaging.store import MessageStore

    store = MessageStore(_old_store(tmp_path))
    columns = {row["name"] for row in store.conn.execute("PRAGMA table_info(contact)")}
    assert {"testnet_address", "mainnet_address", "notes", "updated"} <= columns


def test_migrating_keeps_existing_rows(tmp_path):
    """An upgrade must never be able to lose someone's data."""
    from arcade.messaging.store import MessageStore

    store = MessageStore(_old_store(tmp_path))
    row = store.conn.execute("SELECT * FROM contact").fetchone()
    assert row["name"] == "Old Friend"
    assert row["address"] == "nOldAddress"
    assert row["notes"] == ""


def test_the_write_that_was_failing_now_works(tmp_path):
    """The exact crash: a message arriving wrote to `contact` and died."""
    from arcade.messaging.store import MessageStore

    store = MessageStore(_old_store(tmp_path))
    store.add_message(None, "tx", "tx", 1, 0, "nSender", b"\x09" * 32, "me", b"hi")
    assert store.contact_by_key(b"\x09" * 32)["address"] == "nSender"


def test_migration_is_idempotent(tmp_path):
    from arcade.messaging.store import MessageStore

    path = _old_store(tmp_path)
    MessageStore(path).close()
    store = MessageStore(path)
    columns = [row["name"] for row in store.conn.execute("PRAGMA table_info(contact)")]
    assert len(columns) == len(set(columns))


def test_a_contact_table_with_no_id_is_rebuilt(tmp_path):
    """The original table was keyed by pubkey alone and had no `id`.

    SQLite cannot add a primary key with ALTER TABLE, so the column-adding
    migration could not help and every address book read raised IndexError on an
    older store. Found when a real message arrived and the page 500'd.
    """
    import sqlite3
    from arcade.messaging.store import MessageStore

    path = tmp_path / "keyless.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(OLD_SCHEMA)
    connection.commit()
    connection.close()

    store = MessageStore(path)
    columns = [row["name"] for row in store.conn.execute("PRAGMA table_info(contact)")]
    assert "id" in columns
    (row,) = store.contacts()
    assert row["name"] == "Old Friend"
    assert row["address"] == "nOldAddress"
    assert store.contact_by_key(b"\x01\x02")["id"] == row["id"]


def test_rebuilding_the_contact_table_is_idempotent(tmp_path):
    import sqlite3
    from arcade.messaging.store import MessageStore

    path = tmp_path / "keyless.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(OLD_SCHEMA)
    connection.commit()
    connection.close()

    MessageStore(path).close()
    store = MessageStore(path)
    assert len(store.contacts()) == 1
    assert not store.conn.execute(
        "SELECT name FROM sqlite_master WHERE name='contact_old'").fetchone()


# --- the plain send keeps its change where the coins were -----------------------


def test_a_plain_send_returns_change_to_the_address_that_paid(regtest):
    """a test machine: one Wallet-page send of 5,000 spent the messaging identity's
    9,978-coin output, the node parked the 4,978 change on a fresh address of
    its own, and the identity was left with 5 coins and could not send a
    picture. Change now goes back to the address that put in the most."""
    from arcade import wallet as walletlib

    regtest.generate(200)
    identity = regtest.rpc.call("getnewaddress")
    regtest.rpc.call("sendtoaddress", identity, 900)
    regtest.generate(1)
    # Every other output in this wallet is a small one, so the node's coin
    # selection reaches for the 900 whichever way it leans.
    payee = regtest.rpc.call("getnewaddress")

    prepared = walletlib.prepare_send(regtest.rpc, payee, 500 * walletlib.COIN)
    spent = {(v["txid"], v["vout"]) for v in prepared.decoded["vin"]}
    mine = {(u["txid"], u["vout"]): u for u in regtest.rpc.call("listunspent", 0)}
    assert any(mine[o]["address"] == identity for o in spent), "the 900 was spent"

    by_address = {}
    for out in prepared.decoded["vout"]:
        for name in out["scriptPubKey"].get("addresses", []):
            by_address[name] = out["value"]
    assert by_address[payee] == 500
    assert identity in by_address, f"change went elsewhere: {by_address}"
    assert by_address[identity] > 399, "the change is the whole remainder minus fee"
    assert set(by_address) == {payee, identity}, "no fresh change address"

    txid = walletlib.broadcast(regtest.rpc, prepared)
    regtest.generate(1)
    still_there = sum(float(u["amount"]) for u in
                      regtest.rpc.call("listunspent", 1, 9_999_999, [identity]))
    assert still_there > 399, txid
