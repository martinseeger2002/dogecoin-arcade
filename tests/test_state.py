"""Protocol state transitions, in isolation from the chain."""

import time

import pytest

from arcade import payload as P
from arcade.config import REGTEST
from arcade.db import Database, StateDB
from arcade.state import Engine, install_schema
from arcade.tx import EncodingClass, ArcadeTransaction

ALICE = "mkHS9ne12qx9pS9VojpwU5xtRd4T7X7ZUt"
BOB = "mzBc4XEFSdzCDcTxAgf6EZXgsZWpztRhef"


@pytest.fixture
def engine(tmp_path):
    db = Database(tmp_path / "state.sqlite")
    install_schema(db)
    state = StateDB(db)
    yield state, Engine(state, REGTEST)
    db.close()


def make_tx(sender=ALICE, reference=BOB, payload=b"", height=1, position=0, txid=None):
    return ArcadeTransaction(
        txid=txid or f"tx{height:04d}{position:02d}" + "0" * 58,
        block_height=height,
        position=position,
        encoding_class=EncodingClass.C,
        sender=sender,
        reference=reference,
        payload=payload,
        fee=1000,
    )


def issue(state, engine, sender=ALICE, amount=1000, height=1, name="Token"):
    msg = P.IssuanceFixed(
        ecosystem=1, property_type=2, previous_property_id=0,
        category="c", subcategory="s", name=name, url="", data="", amount=amount,
    )
    with state.block_context(height, f"h{height}", f"h{height-1}", height * 60, 1, 0):
        result = engine.process(make_tx(sender=sender, payload=msg.encode(), height=height))
    return result


# --- property creation --------------------------------------------------------


def test_fixed_issuance_credits_the_issuer(engine):
    state, eng = engine
    result = issue(state, eng, amount=5000)
    assert result.valid, result.reason

    prop = eng.get_property(3)
    assert prop is not None, "first property must get id 3 (1 and 2 are reserved)"
    assert prop["issuer"] == ALICE
    assert prop["total_tokens"] == 5000
    assert eng.get_balance(ALICE, 3)["balance"] == 5000


def test_property_ids_skip_the_reserved_range(engine):
    state, eng = engine
    issue(state, eng, height=1, name="A")
    issue(state, eng, height=2, name="B")
    assert [r["property_id"] for r in eng.state.db.conn.execute(
        "SELECT property_id FROM property ORDER BY property_id")] == [3, 4]


def test_test_ecosystem_uses_its_own_id_range(engine):
    state, eng = engine
    msg = P.IssuanceFixed(
        ecosystem=2, property_type=2, previous_property_id=0,
        category="", subcategory="", name="TestToken", url="", data="", amount=10,
    )
    with state.block_context(1, "h1", "h0", 60, 1, 0):
        assert eng.process(make_tx(payload=msg.encode())).valid
    assert eng.get_property(0x80000001) is not None


def test_managed_property_has_no_initial_supply(engine):
    state, eng = engine
    msg = P.IssuanceManaged(
        ecosystem=1, property_type=2, previous_property_id=0,
        category="", subcategory="", name="Managed", url="", data="",
    )
    with state.block_context(1, "h1", "h0", 60, 1, 0):
        assert eng.process(make_tx(payload=msg.encode())).valid
    assert eng.get_property(3)["managed"] == 1
    assert eng.get_balance(ALICE, 3)["balance"] == 0


def test_issuance_with_empty_name_is_invalid(engine):
    state, eng = engine
    msg = P.IssuanceFixed(
        ecosystem=1, property_type=2, previous_property_id=0,
        category="", subcategory="", name="", url="", data="", amount=10,
    )
    with state.block_context(1, "h1", "h0", 60, 1, 0):
        result = eng.process(make_tx(payload=msg.encode()))
    assert not result.valid and "name" in result.reason
    assert eng.get_property(3) is None, "an invalid issuance must create nothing"


# --- simple send --------------------------------------------------------------


def test_simple_send_moves_tokens(engine):
    state, eng = engine
    issue(state, eng, amount=1000)

    with state.block_context(2, "h2", "h1", 120, 1, 0):
        result = eng.process(
            make_tx(payload=P.SimpleSend(property_id=3, amount=400).encode(), height=2)
        )
    assert result.valid, result.reason
    assert eng.get_balance(ALICE, 3)["balance"] == 600
    assert eng.get_balance(BOB, 3)["balance"] == 400


def test_send_more_than_held_is_invalid_and_changes_nothing(engine):
    state, eng = engine
    issue(state, eng, amount=100)

    with state.block_context(2, "h2", "h1", 120, 1, 0):
        result = eng.process(
            make_tx(payload=P.SimpleSend(property_id=3, amount=101).encode(), height=2)
        )
    assert not result.valid and "insufficient" in result.reason
    assert eng.get_balance(ALICE, 3)["balance"] == 100
    assert eng.get_balance(BOB, 3)["balance"] == 0


def test_send_of_unknown_property_is_invalid(engine):
    state, eng = engine
    with state.block_context(1, "h1", "h0", 60, 1, 0):
        result = eng.process(make_tx(payload=P.SimpleSend(property_id=99, amount=1).encode()))
    assert not result.valid and "does not exist" in result.reason


def test_send_with_no_reference_is_invalid(engine):
    state, eng = engine
    issue(state, eng, amount=100)
    with state.block_context(2, "h2", "h1", 120, 1, 0):
        result = eng.process(
            make_tx(reference=None, payload=P.SimpleSend(property_id=3, amount=1).encode(), height=2)
        )
    assert not result.valid and "reference" in result.reason


def test_zero_amount_send_is_invalid(engine):
    state, eng = engine
    issue(state, eng, amount=100)
    with state.block_context(2, "h2", "h1", 120, 1, 0):
        result = eng.process(
            make_tx(payload=P.SimpleSend(property_id=3, amount=0).encode(), height=2)
        )
    assert not result.valid and "out of range" in result.reason


# --- send all -----------------------------------------------------------------


def test_send_all_moves_every_balance_in_the_ecosystem(engine):
    state, eng = engine
    issue(state, eng, amount=100, height=1, name="A")
    issue(state, eng, amount=200, height=2, name="B")

    with state.block_context(3, "h3", "h2", 180, 1, 0):
        result = eng.process(make_tx(payload=P.SendAll(ecosystem=1).encode(), height=3))
    assert result.valid, result.reason
    assert eng.get_balance(ALICE, 3)["balance"] == 0
    assert eng.get_balance(ALICE, 4)["balance"] == 0
    assert eng.get_balance(BOB, 3)["balance"] == 100
    assert eng.get_balance(BOB, 4)["balance"] == 200


def test_send_all_with_nothing_to_send_is_invalid(engine):
    state, eng = engine
    with state.block_context(1, "h1", "h0", 60, 1, 0):
        result = eng.process(make_tx(payload=P.SendAll(ecosystem=1).encode()))
    assert not result.valid


def test_send_all_does_not_cross_ecosystems(engine):
    state, eng = engine
    issue(state, eng, amount=100, height=1, name="Main")
    msg = P.IssuanceFixed(
        ecosystem=2, property_type=2, previous_property_id=0,
        category="", subcategory="", name="Test", url="", data="", amount=500,
    )
    with state.block_context(2, "h2", "h1", 120, 1, 0):
        eng.process(make_tx(payload=msg.encode(), height=2))

    with state.block_context(3, "h3", "h2", 180, 1, 0):
        eng.process(make_tx(payload=P.SendAll(ecosystem=1).encode(), height=3))

    assert eng.get_balance(BOB, 3)["balance"] == 100, "main ecosystem moved"
    assert eng.get_balance(ALICE, 0x80000001)["balance"] == 500, "test ecosystem untouched"


# --- unsupported types must stop the indexer ----------------------------------


def test_unsupported_type_raises_rather_than_recording_invalid(engine):
    """Hard rule #2: never skip silently. A type we cannot interpret is fatal."""
    state, eng = engine
    unknown = (0).to_bytes(2, "big") + (4242).to_bytes(2, "big")
    with pytest.raises(P.UnknownMessageType):
        with state.block_context(1, "h1", "h0", 60, 1, 0):
            eng.process(make_tx(payload=unknown))


def test_out_of_scope_type_also_raises(engine):
    state, eng = engine
    crowdsale = (0).to_bytes(2, "big") + (51).to_bytes(2, "big")
    with pytest.raises(P.OutOfScopeMessageType):
        with state.block_context(1, "h1", "h0", 60, 1, 0):
            eng.process(make_tx(payload=crowdsale))


def test_invalid_transactions_are_recorded_not_discarded(engine):
    state, eng = engine
    with state.block_context(1, "h1", "h0", 60, 1, 0):
        eng.process(make_tx(payload=P.SimpleSend(property_id=99, amount=1).encode()))

    row = eng.state.db.conn.execute("SELECT * FROM arcade_tx").fetchone()
    assert row["valid"] == 0
    assert "does not exist" in row["invalid_reason"]
    assert row["message_type"] == 0


# --- a send must not outlive its own service ----------------------------------

def test_shutdown_waits_for_a_send_and_then_refuses_more(tmp_path):
    """a test machine caught a send thread writing to the store 17s after a restart.

    Send threads are daemons, so nothing waits for them: the old process had
    released the port while its thread carried on, and two processes briefly
    shared the database. The pending-send record means a cut-off send can be
    finished rather than lost, but a restart should not land in the middle of a
    wallet operation in the first place.
    """
    from pathlib import Path

    from arcade.web.state import AppState, ChainContext

    state = AppState(
        home=tmp_path,
        messaging=ChainContext(network="regtest", role="messaging",
                               label="Testnet", datadir=Path("/nonexistent")),
        ledger=ChainContext(network="main", role="ledger", label="Mainnet",
                            datadir=Path("/nonexistent")))

    # Nothing in flight: shutdown is immediate.
    assert state.begin_shutdown(grace=0.1) is True

    # And once shutting down, no new send may start -- otherwise a restart
    # begins work it is about to abandon.
    assert state.begin_send() is False


def test_shutdown_reports_a_send_it_could_not_wait_out(tmp_path):
    from arcade.web.state import AppState, ChainContext
    from pathlib import Path

    state = AppState(
        home=tmp_path,
        messaging=ChainContext(network="regtest", role="messaging",
                               label="Testnet", datadir=Path("/nonexistent")),
        ledger=ChainContext(network="main", role="ledger", label="Mainnet",
                            datadir=Path("/nonexistent")))

    assert state.begin_send() is True          # a send is now in flight
    # It never finishes, so the grace expires and this reports False rather
    # than blocking the restart for ever.
    assert state.begin_shutdown(grace=0.2) is False
    state.end_send()
    assert state.begin_shutdown(grace=0.2) is True


def test_shutdown_joins_a_collection_run_and_not_just_a_send(tmp_path, monkeypatch):
    """A run asleep between two pieces looks exactly like an idle process.

    `sends.close` waits for a lane, and a run holds one only while a piece is
    actually going out. Between pieces it holds nothing -- it is asleep waiting
    for the next block, for up to a minute -- so a restart could honestly say
    nothing was in flight while a thread was still in the middle of somebody's
    collection, writing into a job book the new process had already opened
    (2026-09-22, the same bug the lanes were meant to close).
    """
    from pathlib import Path

    from arcade import collections as C
    from arcade.web.state import AppState, ChainContext
    from test_collections import FakeSender, hashlips, runner_for

    state = AppState(
        home=tmp_path,
        messaging=ChainContext(network="regtest", role="messaging",
                               label="Testnet", datadir=Path("/nonexistent")),
        ledger=ChainContext(network="main", role="ledger", label="Mainnet",
                            datadir=Path("/nonexistent")))

    build = C.read_build(hashlips(tmp_path / "build"))

    # A run that is waiting for a block rather than sending: the node takes one
    # piece and refuses the next until the chain moves.
    sender = FakeSender()
    real_send_all = sender.send_all
    refusals = []

    def send_all(address, payloads, approve=None, on_broadcast=None, on_progress=None):
        if len(sender.sent) >= 1 and sender.height == 100:
            refusals.append(len(sender.sent))
            raise RuntimeError("sendrawtransaction: [-26] 64: too-long-mempool-chain")
        return real_send_all(address, payloads, approve=approve,
                             on_broadcast=on_broadcast)

    sender.send_all = send_all
    here = tmp_path / "runs"
    here.mkdir()
    jobs, runner = runner_for(here, sender)
    state._collections = (jobs, runner)
    job_id = jobs.create("regtest", "nSender", build)
    monkeypatch.setattr(C, "POLL", 30.0)         # it is asleep, not idle
    monkeypatch.setattr(C, "POLL_MAX", 30.0)
    runner.start(job_id)
    deadline = time.time() + 5
    while ("waiting for a block" not in jobs.get(job_id)["note"]
           and time.time() < deadline):
        time.sleep(0.01)
    assert "waiting for a block" in jobs.get(job_id)["note"], jobs.get(job_id)["note"]
    time.sleep(0.3)                       # parked in the wait, not between asks

    # The lanes are free, which is what the old shutdown checked, and what it
    # therefore got wrong.
    assert state.sends.close(0.0) is True, "no lane held"
    assert runner.running(job_id) is True

    assert state.begin_shutdown(grace=2.0) is True
    assert not runner.running(job_id), "the run was waited out, not abandoned"
    assert jobs.get(job_id)["status"] == "paused"


# --- managed properties: grant, revoke, change issuer ------------------------

def issue_managed(state, engine, sender=ALICE, height=1, name="Managed"):
    msg = P.IssuanceManaged(
        ecosystem=1, property_type=2, previous_property_id=0,
        category="c", subcategory="s", name=name, url="", data="",
    )
    with state.block_context(height, f"h{height}", f"h{height-1}", height * 60, 1, 0):
        return engine.process(make_tx(sender=sender, payload=msg.encode(), height=height))


def apply(state, engine, payload, height, sender=ALICE, reference=BOB):
    with state.block_context(height, f"h{height}", f"h{height-1}", height * 60, 1, 0):
        return engine.process(make_tx(sender=sender, reference=reference,
                                      payload=payload, height=height))


def test_a_grant_creates_tokens_for_the_reference(engine):
    state, eng = engine
    issue_managed(state, eng)
    result = apply(state, eng, P.Grant(property_id=3, amount=500).encode(), height=2)
    assert result.valid, result.reason
    assert eng.get_balance(BOB, 3)["balance"] == 500
    assert eng.get_balance(ALICE, 3)["balance"] == 0
    assert eng.get_property(3)["total_tokens"] == 500


def test_a_grant_with_no_reference_goes_to_the_issuer(engine):
    """tx.cpp:672-675 -- 'assume grant to self'."""
    state, eng = engine
    issue_managed(state, eng)
    result = apply(state, eng, P.Grant(property_id=3, amount=7).encode(), height=2,
                   reference=None)
    assert result.valid, result.reason
    assert eng.get_balance(ALICE, 3)["balance"] == 7


def test_only_the_issuer_may_grant(engine):
    state, eng = engine
    issue_managed(state, eng)
    result = apply(state, eng, P.Grant(property_id=3, amount=5).encode(), height=2,
                   sender=BOB, reference=BOB)
    assert not result.valid and "not the issuer" in result.reason
    assert eng.get_balance(BOB, 3)["balance"] == 0
    assert eng.get_property(3)["total_tokens"] == 0


def test_a_fixed_property_cannot_be_granted(engine):
    state, eng = engine
    issue(state, eng, amount=100)
    result = apply(state, eng, P.Grant(property_id=3, amount=5).encode(), height=2)
    assert not result.valid and "not managed" in result.reason
    assert eng.get_property(3)["total_tokens"] == 100


def test_a_grant_cannot_exceed_the_largest_possible_supply(engine):
    from arcade.state import MAX_AMOUNT
    state, eng = engine
    issue_managed(state, eng)
    assert apply(state, eng, P.Grant(property_id=3, amount=MAX_AMOUNT - 1).encode(),
                 height=2).valid
    result = apply(state, eng, P.Grant(property_id=3, amount=2).encode(), height=3)
    assert not result.valid and "can ever exist" in result.reason
    assert eng.get_property(3)["total_tokens"] == MAX_AMOUNT - 1


def test_a_holder_may_revoke_their_own_tokens(engine):
    state, eng = engine
    issue_managed(state, eng)
    apply(state, eng, P.Grant(property_id=3, amount=500).encode(), height=2)
    # Bob holds them and is not the issuer; Omni lets any holder destroy.
    result = apply(state, eng, P.Revoke(property_id=3, amount=200).encode(), height=3,
                   sender=BOB, reference=None)
    assert result.valid, result.reason
    assert eng.get_balance(BOB, 3)["balance"] == 300
    assert eng.get_property(3)["total_tokens"] == 300


def test_revoking_more_than_held_changes_nothing(engine):
    state, eng = engine
    issue_managed(state, eng)
    apply(state, eng, P.Grant(property_id=3, amount=10).encode(), height=2)
    result = apply(state, eng, P.Revoke(property_id=3, amount=11).encode(), height=3,
                   sender=BOB, reference=None)
    assert not result.valid and "insufficient" in result.reason
    assert eng.get_balance(BOB, 3)["balance"] == 10
    assert eng.get_property(3)["total_tokens"] == 10


def test_change_issuer_hands_control_to_the_reference(engine):
    state, eng = engine
    issue_managed(state, eng)
    result = apply(state, eng, P.ChangeIssuer(property_id=3).encode(), height=2)
    assert result.valid, result.reason
    assert eng.get_property(3)["issuer"] == BOB
    # The old issuer can no longer grant; the new one can.
    assert not apply(state, eng, P.Grant(property_id=3, amount=1).encode(), height=3).valid
    assert apply(state, eng, P.Grant(property_id=3, amount=1).encode(), height=4,
                 sender=BOB, reference=ALICE).valid
    assert eng.get_balance(ALICE, 3)["balance"] == 1


def test_change_issuer_needs_a_reference_and_the_issuer(engine):
    state, eng = engine
    issue(state, eng, amount=100)
    assert not apply(state, eng, P.ChangeIssuer(property_id=3).encode(), height=2,
                     reference=None).valid
    assert not apply(state, eng, P.ChangeIssuer(property_id=3).encode(), height=3,
                     sender=BOB, reference=BOB).valid
    assert eng.get_property(3)["issuer"] == ALICE


def test_grant_and_change_issuer_roll_back_with_their_block(engine):
    """Every mutation these add must be in the undo journal."""
    state, eng = engine
    issue_managed(state, eng)
    apply(state, eng, P.Grant(property_id=3, amount=50).encode(), height=2)
    apply(state, eng, P.ChangeIssuer(property_id=3).encode(), height=3)
    assert eng.get_property(3)["issuer"] == BOB
    state.rollback_block(3)
    assert eng.get_property(3)["issuer"] == ALICE
    assert eng.get_property(3)["total_tokens"] == 50
    state.rollback_block(2)
    assert eng.get_property(3)["total_tokens"] == 0
    assert eng.get_balance(BOB, 3)["balance"] == 0
