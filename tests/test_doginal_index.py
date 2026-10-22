"""Our own Doginals index (arcade/doginal_index.py, 2026-10-20): a walker that reads every block in order and
knows where each inscription is now, and helpers that lease small chunks over the mesh, scan them a block at a time
and say each chunk's hash, so two arcades' scans agree or a difference shows. Blocks here are made up, byte for byte,
and read by rawchain like any real one."""
import hashlib
import time

import pytest

from arcade import doginal_index as dx
from arcade import rawchain


# --------------------------------------------------------------------------- made-up chain

def _varint(n):
    return rawchain._varint(n)


def _push(data: bytes) -> bytes:
    if len(data) < 76:
        return bytes([len(data)]) + data
    return b"\x4c" + bytes([len(data)]) + data


def _tx(vin, vout, tag=b""):
    """vin: [(txid hex, vout, script bytes)]; vout: [koinu]. `tag` makes otherwise equal transactions differ."""
    raw = (1).to_bytes(4, "little") + _varint(len(vin))
    for txid, n, script in vin:
        raw += bytes.fromhex(txid)[::-1] + n.to_bytes(4, "little") + _varint(len(script)) + script + b"\xff\xff\xff\xff"
    raw += _varint(len(vout))
    for v in vout:
        spk = b"\x76\xa9\x14" + hashlib.sha256(tag + bytes([len(raw) % 256])).digest()[:20] + b"\x88\xac"
        raw += v.to_bytes(8, "little") + _varint(len(spk)) + spk
    raw += (0).to_bytes(4, "little")
    return raw, rawchain.read_tx(raw).txid


def _coinbase(h):
    script = _push(h.to_bytes(4, "little"))
    raw = ((1).to_bytes(4, "little") + b"\x01" + b"\x00" * 32 + b"\xff\xff\xff\xff" + _varint(len(script)) + script +
           b"\xff\xff\xff\xff" + b"\x01" + (10_000 * 100_000_000).to_bytes(8, "little") + b"\x01\x51" + b"\x00" * 4)
    return raw


def _envelope(ctype=b"text/plain", parts=((0, b"hi"),), lead=b"ord", total=1):
    s = _push(lead) + _push(bytes([total])) + _push(ctype)
    for count, data in parts:
        s += (_push(bytes([count])) if count else b"\x00") + _push(data)
    return s + _push(b"\x30" * 71) + _push(b"\x51" * 30)          # signature and redeem script, as a reveal ends


def _more(parts):
    s = b""
    for count, data in parts:
        s += (_push(bytes([count])) if count else b"\x00") + _push(data)
    return s + _push(b"\x30" * 71) + _push(b"\x51" * 30)


SIG = _push(b"\x30" * 71) + _push(b"\x02" * 33)


class Chain:
    """A Dogecoin node in miniature: blocks by height, getrawtransaction for any transaction in them."""

    def __init__(self):
        self.blocks: list[tuple[str, bytes]] = []
        self.txs: dict[str, bytes] = {}
        self.calls = 0

    def add(self, txs):
        h = dx.BASE + len(self.blocks)
        raws = [_coinbase(h)] + [t[0] for t in txs]
        ids = [rawchain.read_tx(r).txid for r in raws]
        prev = bytes.fromhex(self.blocks[-1][0])[::-1] if self.blocks else b"\x00" * 32
        header = ((1).to_bytes(4, "little") + prev + bytes.fromhex(rawchain.merkle_root(ids))[::-1] +
                  (1_700_000_000 + h).to_bytes(4, "little") + bytes.fromhex("1e0ffff0")[::-1] + b"\x00" * 4)
        raw = header + _varint(len(raws)) + b"".join(raws)
        block_hash = rawchain.sha256d(header)[::-1].hex()
        self.blocks.append((block_hash, raw))
        for r, i in zip(raws, ids):
            self.txs[i] = r
        return block_hash

    def replace_from(self, height):
        del self.blocks[height - dx.BASE:]

    # the RPC face
    def call(self, method, *params):
        self.calls += 1
        if method == "getblockcount":
            return dx.BASE + len(self.blocks) - 1
        if method == "getblockhash":
            return self.blocks[params[0] - dx.BASE][0]
        if method == "getblock":
            return next(raw for h, raw in self.blocks if h == params[0]).hex()
        if method == "getrawtransaction":
            return self.txs[params[0]].hex()
        raise AssertionError(method)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


FUND = "aa" * 32


@pytest.fixture(autouse=True)
def small(monkeypatch):
    monkeypatch.setattr(dx, "CHUNK", 4)
    monkeypatch.setattr(dx, "SETTLE", 0)
    monkeypatch.setattr(dx, "WALK_SETTLE", 0)


def _walker(tmp_path, chain):
    index = dx.DoginalIndex(tmp_path / "walk.sqlite", lambda: chain, lambda: "walk")
    return index


# --------------------------------------------------------------------------- births

def test_a_birth_is_the_transaction_whose_input_opens_an_ord_envelope():
    reveal = _tx([(FUND, 0, _envelope(b"image/png"))], [100_000])
    plain = _tx([(FUND, 1, SIG)], [100_000])
    chain = Chain()
    chain.add([reveal, plain])
    block = rawchain.read_block(chain.blocks[0][1])
    assert dx.births(block) == [(reveal[1], 0, 0, b"image/png".hex())]
    assert dx.birth_of(rawchain.read_tx(reveal[0]))["done"] is True
    # The same block always gives the same line; another birth gives another.
    assert dx.block_hash_of(block.hash, dx.births(block)) == dx.block_hash_of(block.hash, [(reveal[1], 0, 0, b"image/png".hex())])
    assert dx.block_hash_of(block.hash, []) != dx.block_hash_of(block.hash, dx.births(block))


# --------------------------------------------------------------------------- the walker

def test_the_walker_follows_an_inscription_koinu_by_koinu(tmp_path):
    chain = Chain()
    reveal = _tx([(FUND, 0, _envelope())], [100_000, 50_000])
    chain.add([reveal])
    iid = f"{reveal[1]}i0"
    # Sent on as the only input: it stays on the first koinu, so output 0.
    send = _tx([(reveal[1], 0, SIG)], [100_000])
    chain.add([send])
    # Bought: two dummy coins in front (30,000 koinu), so the inscription's koinu is the 30,000th of the inputs, which
    # falls in the second output (outputs 20,000 | 100,000 | ...), 10,000 koinu in.
    dummies = _tx([(FUND, 2, SIG)], [10_000, 20_000], tag=b"d")
    chain.add([dummies])
    buy = _tx([(dummies[1], 0, SIG), (dummies[1], 1, SIG), (send[1], 0, SIG)], [20_000, 100_000, 10_000], tag=b"b")
    chain.add([buy])
    index = _walker(tmp_path, chain)
    index.step()
    where = index.where(iid)
    assert where["output"] == f"{buy[1]}:1" and where["offset"] == 10_000 and where["number"] == 0
    assert index.on(buy[1], 1) == [{"id": iid, "offset": 10_000, "ctype": "text/plain"}]
    assert index.on(reveal[1], 0) == []


def test_an_inscription_spent_past_every_output_goes_to_the_fee(tmp_path):
    chain = Chain()
    big = _tx([(FUND, 0, SIG)], [100_000], tag=b"x")
    reveal = _tx([(FUND, 1, _envelope())], [100_000])
    chain.add([big, reveal])
    # Input 0 is 100,000 koinu of plain coin; the inscription is koinu 100,000; the outputs only add up to 90,000.
    burn = _tx([(big[1], 0, SIG), (reveal[1], 0, SIG)], [90_000])
    chain.add([burn])
    index = _walker(tmp_path, chain)
    index.step()
    where = index.where(f"{reveal[1]}i0")
    assert where["output"] is None and where["burned"] == dx.BASE + 1


def test_an_envelope_over_two_transactions_closes_in_the_second(tmp_path):
    chain = Chain()
    first = _tx([(FUND, 0, _envelope(parts=((1, b"part one"),), total=2))], [100_000])
    chain.add([first])
    rest = _tx([(first[1], 0, _more(((0, b"part two"),)))], [100_000])
    chain.add([rest])
    index = _walker(tmp_path, chain)
    index.step()
    where = index.where(f"{first[1]}i0")
    assert where["output"] == f"{rest[1]}:0", "numbered and placed when its body is complete"
    assert index.store.q("SELECT pieces, height FROM insc")[0] == (2, dx.BASE + 1)
    assert index.store.q("SELECT COUNT(*) FROM pending")[0][0] == 0


def test_a_spend_of_a_pieces_change_first_does_not_end_its_envelope(tmp_path):
    """2026-10-21, against pepeblocks.com: in PEP block 293,201 a transaction whose input 0 spent a piece's CHANGE
    (output 1, a signature, no piece) came before the next piece (spending output 0). The walk took it for the next
    piece and dropped the envelope; the official indexers keep it ("ParsedInscription::None" removes nothing), and the
    next piece completes it. 862 of PEP's first 2.07 million were lost that way."""
    chain = Chain()
    first = _tx([(FUND, 0, _envelope(parts=((1, b"part one"),), total=2))], [100_000, 5_000_000])
    chain.add([first])
    change = _tx([(first[1], 1, _push(b"\x30" * 71) + _push(b"\x02" * 33))], [4_000_000])
    rest = _tx([(first[1], 0, _more(((0, b"part two"),)))], [100_000])
    chain.add([change, rest])
    index = _walker(tmp_path, chain)
    index.step()
    where = index.where(f"{first[1]}i0")
    assert where is not None and where["output"] == f"{rest[1]}:0", "the next piece still completes it"
    assert index.store.q("SELECT COUNT(*) FROM pending")[0][0] == 0


def test_a_reorg_walks_back_and_takes_the_new_blocks(tmp_path):
    chain = Chain()
    reveal = _tx([(FUND, 0, _envelope())], [100_000])
    chain.add([reveal])
    send = _tx([(reveal[1], 0, SIG)], [100_000])
    chain.add([send])
    index = _walker(tmp_path, chain)
    index.step()
    assert index.where(f"{reveal[1]}i0")["output"] == f"{send[1]}:0"
    # Block 1 is replaced by one where the coin goes elsewhere, and a block on top of it.
    chain.replace_from(dx.BASE + 1)
    other = _tx([(reveal[1], 0, SIG)], [60_000, 40_000], tag=b"o")
    chain.add([other])
    chain.add([])
    index.step()
    assert index.where(f"{reveal[1]}i0")["output"] == f"{other[1]}:0"
    assert index.walker.next_height() == dx.BASE + 3


# --------------------------------------------------------------------------- the shared chunks

def _busy_chain(blocks=12):
    chain = Chain()
    for b in range(blocks):
        chain.add([_tx([(FUND, b, _envelope(parts=((0, bytes([b])),)))], [100_000], tag=bytes([b]))] if b % 3 == 0 else [])
    return chain


class Mesh:
    """Arcades linked to each other: what one posts, every other hears (as DxShare does over the links)."""

    def __init__(self):
        self.members: dict[str, dx.DoginalIndex] = {}
        self.up: set[str] = set()
        self.fetched: list = []

    def join(self, name, index):
        self.members[name] = index
        self.up.add(name)
        mesh = self

        class Share:
            def post(self, frame):
                if name not in mesh.up:
                    return
                for other, idx in mesh.members.items():
                    if other != name and other in mesh.up:
                        idx.heard(name, frame)

            def post_to(self, node, frame):
                if node in mesh.up and name in mesh.up:
                    mesh.members[node].heard(name, frame)

            def get(self, node, ask):                              # DxShare's g / gb, at once
                peer = mesh.members.get(node)
                if peer is None or node not in mesh.up:
                    return
                mesh.fetched.append((name, node, ask.get("what"), ask.get("i")))
                if ask["what"] == "manifest":
                    m = peer.snapshots.manifest(ask["snap"])
                    if m is not None:
                        index.snapshots.got_manifest(node, ask["snap"], m)
                else:
                    data = peer.snapshots.piece(ask["snap"], ask["i"])
                    if data is not None:
                        index.snapshots.got_piece(node, ask["snap"], ask["i"], data)
                    else:
                        index.snapshots.piece_failed(node, ask["snap"], ask["i"])

            def ask_manifest(self, node, sid):
                self.get(node, {"what": "manifest", "snap": sid})

            def ask_piece(self, node, sid, i):
                self.get(node, {"what": "piece", "snap": sid, "i": i})

            def ask(self, question, node=None):
                return mesh.members[node].answer(question)

            def fetch_notes(self, c, node, nd, hash_):           # DxShare's ng / nb, at once
                mesh.fetched.append((name, node, c))
                peer = mesh.members.get(node)
                got = peer.notes_blob(c) if peer is not None and node in mesh.up else None
                if got is None:
                    index.fetch_failed(node, c)
                else:
                    index.got_notes(node, c, got[0], nd, hash_)
        index.share = Share()
        for other, idx in self.members.items():               # link up: each says everything it has
            if other != name:
                idx.heard(name, index.hello())
                index.heard(other, idx.hello())
                if idx.all_mine():
                    index.heard(other, {"op": "dn", "rows": idx.all_mine()})
                for lane in idx.chunks.lanes:                  # and the chunks it holds (DxShare.link_up)
                    index.heard(other, {"op": "ls", "c": lane["c"], "at": lane["at"],
                                        "until": time.time() + dx.LEASE_SECONDS})


def test_a_walkers_chunks_and_a_helpers_scans_agree(tmp_path):
    chain = _busy_chain(12)
    mesh = Mesh()
    walker = _walker(tmp_path, chain)
    helper = dx.DoginalIndex(tmp_path / "help.sqlite", lambda: chain, lambda: "help")
    mesh.join("walker", walker)
    mesh.join("helper", helper)
    walker.step()
    for _ in range(10):
        helper.step()
    status = helper.status()
    assert status["ready"] == 3 and status["agreed"] == 3 and status["differ"] == 0, status
    assert walker.status()["agreed"] == 3
    born = walker.store.q("SELECT COALESCE(SUM(births), 0) FROM seen WHERE node=''")[0][0]
    assert born > 0 and status["agreed_births"] == walker.status()["agreed_births"] == born, \
        "every inscription the two read the same way counts as agreed"
    # The helper read whole chunks, and its hash of each is the walker's.
    for c in range(3):
        assert helper.store.words(c)[""] == walker.store.words(c)[""]


def test_a_helper_that_lies_shows_as_a_difference(tmp_path):
    chain = _busy_chain(8)
    walker = _walker(tmp_path, chain)
    walker.step()
    walker.heard("liar", {"op": "hi", "mode": "help", "rules": dx.ord_rules.rules_for("doge")})
    walker.heard("liar", {"op": "dn", "rows": [[0, "ab" * 32, 0]]})
    status = walker.status()
    assert status["differ"] == 1 and status["differs"][0]["chunk"] == 0
    assert status["agreed"] == 0 and status["agreed_births"] == 0, "one word against a liar agrees on nothing"


def test_a_chunk_whose_helper_went_quiet_goes_to_the_next(tmp_path, monkeypatch):
    chain = _busy_chain(4)                                  # one chunk
    mesh = Mesh()
    a = dx.DoginalIndex(tmp_path / "a.sqlite", lambda: chain, lambda: "help")
    b = dx.DoginalIndex(tmp_path / "b.sqlite", lambda: chain, lambda: "help")
    mesh.join("a", a)
    mesh.join("b", b)
    monkeypatch.setattr(dx, "PROGRESS_EVERY", 1)
    # a takes the chunk and reads one block, then goes off-line.
    a.step(budget=0)
    assert a.chunks.mine["c"] == 0
    monkeypatch.setattr(dx, "AGREE", 1)
    assert b.chunks.wanted(0, time.time()) == 0, "a's lease holds the chunk while it lasts"
    mesh.up.discard("a")
    b.chunks.gone("a")
    b.step()
    assert b.store.words(0).get(""), "b took the chunk once a was gone, and finished it"


def test_two_helpers_take_different_chunks(tmp_path):
    chain = _busy_chain(40)                                 # ten chunks
    mesh = Mesh()
    a = dx.DoginalIndex(tmp_path / "a.sqlite", lambda: chain, lambda: "help", cores=lambda: 1)
    b = dx.DoginalIndex(tmp_path / "b.sqlite", lambda: chain, lambda: "help", cores=lambda: 1)
    mesh.join("a", a)
    mesh.join("b", b)
    a.step(budget=0)
    b.step(budget=0)
    # 2026-10-20: handed out in line, never two on the same blocks while any chunk is unscanned.
    assert a.chunks.mine["c"] == 0 and b.chunks.mine["c"] == 1
    assert b.status()["leases"][0]["node"] == "a"
    many = dx.DoginalIndex(tmp_path / "m.sqlite", lambda: chain, lambda: "help", cores=lambda: 3)
    mesh.join("m", many)
    many.step(budget=0)
    assert sorted(lane["c"] for lane in many.chunks.lanes) == [2, 3, 4], "a faster arcade takes the next ones in line"


def test_off_does_nothing(tmp_path):
    chain = _busy_chain(4)
    index = dx.DoginalIndex(tmp_path / "off.sqlite", lambda: chain, lambda: "off")
    index.step()
    assert chain.calls == 0


def test_the_mesh_extension_says_everything_when_a_link_comes_up_and_files_what_it_hears(tmp_path):
    import asyncio
    from arcade.mesh.doginalshare import DxShare
    chain = _busy_chain(8)
    walker = _walker(tmp_path, chain)
    walker.step()
    share = DxShare(walker)

    class Link:
        peer_id = "ff" * 32

        def __init__(self):
            self.sent = []

        async def send(self, header, blob=b""):
            self.sent.append(header)

    class Node:
        links = {}

    link = Link()
    share.node = Node()
    Node.links = {link.peer_id: link}

    async def go():
        await share.link_up(link)
        await share.on_frame(link, {"t": "dx", **walker.hello()}, b"")               # it says hello (its rules) first
        await share.on_frame(link, {"t": "dx", "op": "dn", "net": "doge", "rows": [[0, walker.store.words(0)[""], 1]]}, b"")
        await share.on_frame(link, {"t": "dx", "op": "ls", "net": "doge", "c": 1, "at": dx.BASE + 5,
                                    "until": time.time() + 60}, b"")
        await share.on_frame(link, {"t": "dx", "op": "dn", "rows": [[2, "ab" * 32, 1]]}, b"")   # v1: no chain named
        walker.share.post({"op": "ld", "c": 1})          # as the index posts: through its chain's way out
        await asyncio.sleep(0.05)
    asyncio.run(go())
    ops = [f["op"] for f in link.sent]
    assert ops[0] == "hi" and "dn" in ops and ops[-1] == "ld"
    assert all(f.get("net") == "doge" for f in link.sent)
    assert ("ff" * 32) not in walker.store.words(2), "an older arcade's frame naming no chain is not read"
    assert sorted(r[0] for f in link.sent if f["op"] == "dn" for r in f["rows"]) == [0, 1]
    assert walker.status()["agreed"] == 1
    assert walker.status()["leases"] == [{"node": "ff" * 6, "chunk": 1, "blocks": list(dx.chunk_range(1)),
                                          "at": dx.BASE + 5, "done": 1}]


def test_a_helper_turned_walker_lets_its_chunk_go_at_once(tmp_path):
    chain = _busy_chain(8)
    mode = ["help"]
    index = dx.DoginalIndex(tmp_path / "x.sqlite", lambda: chain, lambda: mode[0])
    said = []

    class Share:
        def post(self, frame):
            said.append(frame)
    index.share = Share()
    index.step(budget=0)
    held = index.chunks.mine["c"]
    mode[0] = "walk"
    index.step()
    assert index.chunks.mine is None and {"op": "ld", "c": held} in said


def test_ords_witness_envelopes_are_read_on_a_taproot_chain():
    """Litecoin's ords are mostly ord's own kind (2026-10-20, the Ords tab): OP_FALSE OP_IF "ord" 1 <type> 0 <body>
    OP_ENDIF in the tapscript of a script-path spend, one inscription per envelope, numbered across the transaction."""
    def tapscript(ctype, body):
        return (_push(b"\x02" * 32) + b"\xac" + b"\x00\x63" + _push(b"ord") + _push(b"\x01") + _push(ctype)
                + b"\x00" + _push(body) + b"\x68")
    control = b"\xc0" + b"\x11" * 32
    one = [b"\x30" * 64, tapscript(b"text/plain", b"hello"), control]
    two = [b"\x30" * 64, tapscript(b"image/webp", b"x" * 100) + b"\x00\x63" + _push(b"ord") + _push(b"\x01")
           + _push(b"text/html") + b"\x00" + _push(b"<b>") + b"\x68", control]
    assert dx.taproot_envelopes(one) == [b"text/plain".hex()]
    assert dx.taproot_envelopes(two) == [b"image/webp".hex(), b"text/html".hex()]
    assert dx.taproot_envelopes(one + [b"\x50annex"]) == [b"text/plain".hex()], "an annex is passed over"
    assert dx.taproot_envelopes([b"\x30" * 64, b"\x02" * 33]) == [], "a key-path spend has no envelope"

    tx = rawchain.Tx(txid="ab" * 32, wtxid="", raw=b"", version=2, locktime=0, stripped_size=0, vout=[(10_000, b"")],
                     vin=[{"txid": "cd" * 32, "vout": 0, "script": b"", "sequence": 0, "witness": one},
                          {"txid": "ef" * 32, "vout": 1, "script": b"", "sequence": 0, "witness": two}])
    assert [(b["n"], b["vin"]) for b in dx.births_of(tx, taproot=True)] == [(0, 0), (1, 1), (2, 1)]
    assert dx.births_of(tx, taproot=False) == [], "Dogecoin and Pepecoin read only the Doginals kind"


def test_each_chain_starts_at_its_official_first_inscription_height(tmp_path):
    assert dx.STARTS == {"doge": 4_600_000, "main": 186_920, "test": 34_237, "ltc": 2_424_429, "ltctest": 2_669_127}
    ltc = dx.DoginalIndex(tmp_path / "ltc.sqlite", None, lambda: "off", net="ltc", mweb=True)
    assert ltc.base == 2_424_429 and ltc.taproot and ltc.walker.mweb
    assert dx.chunk_range(0, ltc.base) == (2_424_429, 2_424_429 + dx.CHUNK)
    assert ltc.status()["source"].startswith("ord-litecoin")


def test_a_helper_says_how_many_it_found_in_the_chunks_it_read(tmp_path):
    chain = _busy_chain(8)
    helper = dx.DoginalIndex(tmp_path / "h.sqlite", lambda: chain, lambda: "help")
    for _ in range(4):
        helper.step()
    assert helper.status()["walk"] is None
    assert helper.status()["found"] == 3, "births at blocks 0, 3 and 6 of the eight"


def test_cores_are_lanes_each_reading_a_chunk_of_its_own_side_by_side(tmp_path, monkeypatch):
    """2026-10-20: "dedicate the number of cores to your ord index". Three cores: three chunks at once, their
    blocks fetched side by side and read in worker processes; the hashes are the same as one core's."""
    chain = _busy_chain(24)                                   # six chunks of four
    many = dx.DoginalIndex(tmp_path / "many.sqlite", lambda: chain, lambda: "help", cores=lambda: 3)
    many.step(budget=0)
    assert len(many.chunks.lanes) == 3 and len({l["c"] for l in many.chunks.lanes}) == 3, "three different chunks"
    for _ in range(6):
        many.step()
    one = dx.DoginalIndex(tmp_path / "one.sqlite", lambda: chain, lambda: "help", cores=lambda: 1)
    for _ in range(8):
        one.step()
    for c in range(6):
        assert many.store.words(c)[""] == one.store.words(c)[""], c
    assert many.status()["cores"] == 3
    # Fewer cores: the newest lanes let go.
    many.cores = lambda: 1
    many.chunks.lanes[:] = [{"c": 0, "at": dx.chunk_range(0)[0]}, {"c": 1, "at": dx.chunk_range(1)[0]}]
    many.step(budget=0)
    assert len(many.chunks.lanes) <= 1


def test_a_small_machine_on_one_core_rests_and_a_big_one_goes_straight_on(tmp_path, monkeypatch):
    chain = _busy_chain(8)
    index = dx.DoginalIndex(tmp_path / "p.sqlite", lambda: chain, lambda: "help", cores=lambda: 1)
    monkeypatch.setattr("os.cpu_count", lambda: 4)
    assert index.step() >= 0.5, "one core of four: rests as long as it worked"
    assert dx.default_cores() == 1
    monkeypatch.setattr("os.cpu_count", lambda: 20)
    assert index.step() < 0.1, "a big machine: straight on"
    assert dx.default_cores() == 10


def test_read_only_holds_no_index_and_asks_an_arcade_that_walks(tmp_path):
    """2026-10-20: "read only. Where it can read the index, but it doesn't hold the index on its own copy of
    arcade." The reader scans nothing; its question goes over the mesh to the furthest-walked linked arcade."""
    import asyncio
    from arcade.mesh.doginalshare import DxShare
    chain = Chain()
    reveal = _tx([(FUND, 0, _envelope())], [100_000, 50_000])
    chain.add([reveal])
    send = _tx([(reveal[1], 0, SIG)], [100_000])
    chain.add([send])
    walker = _walker(tmp_path, chain)
    walker.step()
    reader = dx.DoginalIndex(tmp_path / "read.sqlite", lambda: chain, lambda: "read")
    calls = chain.calls
    assert reader.step() == 10.0 and chain.calls == calls, "read only: nothing scanned"
    with pytest.raises(ValueError):
        reader.answer({"what": "where", "id": f"{reveal[1]}i0"})

    class Link:
        def __init__(self, peer_id):
            self.peer_id, self.far = peer_id, None

        async def send(self, header, blob=b""):
            await self.far.share.on_frame(self.far, header, blob)

    class Node:
        def __init__(self):
            self.links = {}
    w_share, r_share = DxShare(walker), DxShare(reader)
    to_walker, to_reader = Link("walker-node"), Link("reader-node")
    to_walker.far, to_reader.far = to_reader, to_walker          # what one sends, the other hears on its own link
    to_reader.share, to_walker.share = w_share, r_share
    w_share.node, r_share.node = Node(), Node()
    r_share.node.links = {"walker-node": to_walker}
    w_share.node.links = {"reader-node": to_reader}

    async def go():
        r_share.loop = w_share.loop = asyncio.get_running_loop()
        await r_share.on_frame(to_walker, {"t": "dx", **walker.hello()}, b"")     # the reader hears the walker's hi
        where = await asyncio.to_thread(reader.lookup, {"what": "where", "id": f"{reveal[1]}i0"})
        on = await asyncio.to_thread(reader.lookup, {"what": "on", "txid": send[1], "vout": 0})
        return where, on
    where, on = asyncio.run(go())
    assert where["where"]["output"] == f"{send[1]}:0" and where["from"] == "arcade walker-node"
    assert on["on"] == [{"id": f"{reveal[1]}i0", "offset": 0, "ctype": "text/plain"}]
    assert reader.store.q("SELECT COUNT(*) FROM insc")[0][0] == 0, "and the reader keeps no index"


def test_status_never_makes_a_page_wait_for_another_working_it_out(tmp_path):
    """2026-10-20 on .25: Admin tabs asked at once when the kept status ran out, every one worked it out again, and
    they held the threads that serve pages. Now one works it out; the rest get the last answer at once."""
    import threading
    chain = _busy_chain(4)
    index = dx.DoginalIndex(tmp_path / "s.sqlite", lambda: chain, lambda: "help")
    first = index.status()
    index._status_kept = (time.monotonic() - 999, first)      # run out
    index._status_lock.acquire()                              # somebody is working it out
    try:
        started = time.monotonic()
        assert index.status() is first and time.monotonic() - started < 0.5
    finally:
        index._status_lock.release()
    assert index.status() is not first, "and once nobody is, it is worked out again"


def test_reads_do_not_wait_for_a_write_in_progress(tmp_path):
    chain = _busy_chain(4)
    index = dx.DoginalIndex(tmp_path / "r.sqlite", lambda: chain, lambda: "walk")
    index.step()
    with index.store.tx():                                    # a write held open on another path
        import threading
        got = []
        t = threading.Thread(target=lambda: got.append(index.store.q("SELECT COUNT(*) FROM block")[0][0]))
        t.start()
        t.join(2)
        assert got == [4], "read on its own connection, not queued behind the writer's lock"


# --------------------------------------------------------------------------- spend notes (ord_notes, 2026-10-20)

def _json(obj) -> bytes:
    import json
    return json.dumps(obj, separators=(",", ":")).encode()


def _tx_to(vin, outs):
    """Like _tx, but each output is (koinu, script): to a chosen address, or to a script that pays none."""
    raw = (1).to_bytes(4, "little") + _varint(len(vin))
    for txid, n, script in vin:
        raw += bytes.fromhex(txid)[::-1] + n.to_bytes(4, "little") + _varint(len(script)) + script + b"\xff\xff\xff\xff"
    raw += _varint(len(outs))
    for v, spk in outs:
        raw += v.to_bytes(8, "little") + _varint(len(spk)) + spk
    raw += (0).to_bytes(4, "little")
    return raw, rawchain.read_tx(raw).txid


def _story_chain():
    """Every kind of thing the walk follows, over three chunks of 4 blocks: an image moved and sold on a later input,
    an envelope across a chunk's edge, an image in two pieces inside a chunk, a token deployed, minted, set aside and
    sent to a script that pays no address, a name, a burn."""
    chain = Chain()
    img = _tx([(FUND, 0, _envelope(b"image/png", parts=((0, b"\x89PNG" + bytes(range(200))),)))], [100_000], tag=b"i")
    big = _tx([(FUND, 8, SIG)], [100_000], tag=b"bg")
    chain.add([img, big])                                                       # block 0
    deploy = _tx([(FUND, 1, _envelope(b"text/plain", parts=((0, _json(
        {"p": "drc-20", "op": "deploy", "tick": "woof", "max": "1000", "lim": "100"})),)))], [100_000], tag=b"dp")
    mint = _tx([(FUND, 2, _envelope(b"text/plain", parts=((0, _json(
        {"p": "drc-20", "op": "mint", "tick": "woof", "amt": "100"})),)))], [100_000], tag=b"mt")
    chain.add([deploy, mint])                                                   # block 1
    send = _tx([(img[1], 0, SIG)], [100_000], tag=b"s")
    two = _tx([(FUND, 3, _envelope(b"image/gif", parts=((1, b"G" * 240),), total=2))], [100_000], tag=b"t1")
    chain.add([send, two])                                                      # block 2
    two_b = _tx([(two[1], 0, _more(((0, b"I" * 240),)))], [100_000], tag=b"t2")
    edge = _tx([(FUND, 4, _envelope(b"text/plain", parts=((1, b'{"p":"dns",'),), total=2))], [100_000], tag=b"e1")
    chain.add([two_b, edge])                                                    # block 3: an envelope begins
    edge_b = _tx([(edge[1], 0, _more(((0, b'"op":"reg","name":"pup.doge"}'),)))], [100_000], tag=b"e2")
    name = _tx([(FUND, 5, _envelope(b"text/plain", parts=((0, b"robin.doge"),)))], [100_000], tag=b"nm")
    chain.add([edge_b, name])                                                   # block 4: and ends, a chunk on
    holder = rawchain.read_tx(mint[0]).vout[0][1]                               # whoever minted the 100
    coin = _tx_to([(FUND, 9, SIG)], [(100_000, holder)])
    chain.add([coin])                                                           # block 5: a coin to the minter
    note = _tx_to([(coin[1], 0, _envelope(b"text/plain", parts=((0, _json(
        {"p": "drc-20", "op": "transfer", "tick": "woof", "amt": "40"})),)))], [(100_000, holder)])
    chain.add([note])                                                           # block 6: 40 set aside
    dummies = _tx([(FUND, 6, SIG)], [10_000, 20_000], tag=b"dm")
    chain.add([dummies])                                                        # block 7
    buy = _tx([(dummies[1], 0, SIG), (dummies[1], 1, SIG), (send[1], 0, SIG)], [20_000, 100_000, 10_000], tag=b"b")
    gift = _tx_to([(note[1], 0, SIG)], [(100_000, b"\x51")])                    # the 40 to a script, no address
    chain.add([buy, gift])                                                      # block 8
    nowhere = _tx_to([(two_b[1], 0, SIG)], [(100_000, b"\x51\x51")])
    burn = _tx([(big[1], 0, SIG), (deploy[1], 0, SIG)], [90_000], tag=b"bn")
    chain.add([nowhere, burn])                                                  # block 9
    chain.add([])
    chain.add([])                                                               # blocks 10, 11
    return chain


TABLES = ("insc", "at", "pending", "tok", "bal", "xfer", "names")


def _dump(index):
    out = {t: sorted(index.store.q(f"SELECT * FROM {t}")) for t in TABLES}
    out["block"] = sorted(index.store.q("SELECT height, hash, line, n, moves FROM block"))
    return out


def test_an_index_stitched_from_notes_is_the_one_walked_from_a_core(tmp_path):
    """2026-10-20: chunks scanned by whoever is free, stitched in order. A walk that reads only another
    arcade's notes -- not one block read from the chain -- ends with every table the same as the walk that read each
    block from its Core: owners, the token, the names, the burn."""
    from arcade import ord_notes
    chain = _story_chain()
    walked = _walker(tmp_path, chain)
    walked.walker.step(chain, dx.BASE + 11, budget=60)
    assert walked.walker.next_height() == dx.BASE + 12
    assert walked.token("woof")["minted"] == "100"
    assert walked.name("pup.doge")["readings"]["json"]["winner"], "the name across a chunk's edge"
    assert any(o.startswith("script:") and int(b) == 40 * 10 ** 18 for o, _t, b, _x in walked.store.q(
        "SELECT * FROM bal")), "the 40 sent to a script that pays no address"
    kept = {c: walked.store.notes_of(c) for c in range(3)}
    assert all(k and k[2] == "" for k in kept.values()), "a walk from its own Core writes each chunk's notes"
    words = walked.store.all_notes_words(3)
    assert all(words[c][""][1] == kept[c][1] for c in range(3)), "and says their digest with the chunk's hash"

    stitched = dx.DoginalIndex(tmp_path / "stitch.sqlite", lambda: chain, lambda: "walk")
    reads = []
    real = chain.call

    def counted(method, *params):
        reads.append(method)
        return real(method, *params)
    chain.call = counted
    got = stitched.walker.step(chain, dx.BASE + 11, budget=60,
                               notes=lambda c: (ord_notes.decode(ord_notes.unpack(kept[c][0])), False)
                               if c in kept else False)
    assert got == 12
    assert "getblock" not in reads, "not one block read from the chain"
    assert _dump(stitched) == _dump(walked)
    assert stitched.store.all_notes_words(3) == {}, "stitched from another's notes: no word of its own"


def test_notes_keep_what_matters_and_zero_what_does_not():
    """An image's body is zeroed (same length, so its size stays), a token's or a name's never; two scans of a chunk
    write the same bytes; a note that lies about a block's transactions does not read."""
    from arcade import ord_notes
    chain = _story_chain()
    blocks = [rawchain.read_block(raw) for _h, raw in chain.blocks]
    first = ord_notes.finish([ord_notes.one_block(b) for b in blocks[0:4]])
    assert first == ord_notes.finish([ord_notes.one_block(b) for b in blocks[0:4]]), "canonical"
    assert b"\x89PNG" not in first and b"G" * 240 not in first and b"I" * 240 not in first
    assert b"woof" in first and b'{"p":"dns",' in first, "a token, and a name begun here, are kept"
    second = ord_notes.finish([ord_notes.one_block(b) for b in blocks[4:8]])
    assert b'"op":"reg","name":"pup.doge"}' in second and b"robin.doge" in second
    decoded = ord_notes.decode(first)
    assert [b.hash for b in decoded] == [b.hash for b in blocks[0:4]]
    assert len(decoded[0].txs[1].vin[0]["script"]) == len(blocks[0].txs[1].vin[0]["script"]), "lengths kept"
    assert len(ord_notes.pack(first)) < len(b"".join(raw for _h, raw in chain.blocks[0:4]))
    bad = bytearray(first)
    at = bad.index(bytes.fromhex(blocks[0].txs[1].txid))
    bad[at] ^= 1
    with pytest.raises(ord_notes.NotesError, match="merkle"):
        ord_notes.decode(bytes(bad))


def test_helpers_cover_in_line_a_holder_stitches_their_notes_then_they_audit(tmp_path):
    """The whole scheme on three arcades (2026-10-20). Two helpers take the chunks in line, none twice while any
    is unscanned; an arcade holding the index -- no Core of its own -- fetches each chunk's notes from whoever scanned
    it and stitches them, ending with the index a walk from a Core makes; then the helpers audit each other's chunks,
    and the confidence goes up; notes the holder has stitched past are let go."""
    chain = _story_chain()
    reference = dx.DoginalIndex(tmp_path / "ref.sqlite", lambda: chain, lambda: "walk")
    reference.walker.step(chain, dx.BASE + 11, budget=60)
    mesh = Mesh()
    a = dx.DoginalIndex(tmp_path / "a.sqlite", lambda: chain, lambda: "help", cores=lambda: 1)
    b = dx.DoginalIndex(tmp_path / "b.sqlite", lambda: chain, lambda: "help", cores=lambda: 1)
    holder = dx.DoginalIndex(tmp_path / "h.sqlite", lambda: chain, lambda: "walk", own_core=lambda: False)
    for name, idx in (("a", a), ("b", b), ("h", holder)):
        mesh.join(name, idx)
        idx.me = name
    seen_twice = []
    for _ in range(12):
        a.step()
        b.step()
        words = a.store.all_words(3)
        if len(words) < 3:
            seen_twice += [c for c, w in words.items() if len(w) > 1]
        holder.step()
        for idx in (a, b):
            idx.heard("h", holder.hello())
    assert not seen_twice, "no chunk scanned twice before every chunk had been scanned once"
    assert holder.error == "" and holder.walker.next_height() == dx.BASE + 12
    assert _dump(holder) == _dump(reference), "stitched from the helpers' notes: the same index"
    assert {node for _h, node, _c in mesh.fetched} <= {"a", "b"} and len({c for *_x, c in mesh.fetched}) == 3
    status = a.status()
    assert status["phase"] == "audit" and status["levels"][2]["chunks"] >= 1, status["levels"]
    for idx in (a, b):
        idx._pruned = 0
        idx._prune_notes()
    assert a.store.q("SELECT COUNT(*) FROM notes")[0][0] == 0 == b.store.q("SELECT COUNT(*) FROM notes")[0][0], \
        "the holder stitched past them: let go"


def test_notes_cross_a_link_in_parts_and_a_wrong_part_is_refused(tmp_path, monkeypatch):
    """DxShare's ng / nb: a chunk's notes go in slices under a frame's size, are put back together and checked; a
    sender that has let them go says so, and the asker asks elsewhere."""
    import asyncio
    from arcade.mesh import doginalshare
    from arcade.mesh.doginalshare import DxShare
    monkeypatch.setattr(doginalshare, "PART", 100)            # many parts even for a small chunk
    chain = _story_chain()
    helper = dx.DoginalIndex(tmp_path / "help.sqlite", lambda: chain, lambda: "help", cores=lambda: 1)
    for _ in range(4):
        helper.step()
    holder = dx.DoginalIndex(tmp_path / "hold.sqlite", lambda: chain, lambda: "walk", own_core=lambda: False)
    serve, ask = DxShare(helper), DxShare(holder)

    class Link:
        def __init__(self, me, other_share):
            self.peer_id, self.other, self.sent = me, other_share, []

        async def send(self, header, blob=b""):
            self.sent.append(header)
            await self.other.on_frame(self.back, header, blob)

    to_helper, to_holder = Link("help", serve), Link("hold", ask)    # named for the arcade each leads to
    to_helper.back, to_holder.back = to_holder, to_helper

    class Node:
        def __init__(self, links):
            self.links = links

    serve.node, ask.node = Node({"hold": to_holder}), Node({"help": to_helper})

    async def go():
        serve.loop = ask.loop = asyncio.get_running_loop()
        word = helper.store.all_notes_words(3)
        for c in range(3):
            h, nd, _n = word[c][""]
            await asyncio.to_thread(holder.share.fetch_notes, c, "help", nd, h)
        for _ in range(100):
            await asyncio.sleep(0.02)
            if all(holder.store.notes_of(c) for c in range(3)):
                break
        helper.store.x("DELETE FROM notes WHERE chunk=0")
        holder.store.x("DELETE FROM notes WHERE chunk=0")
        await asyncio.to_thread(holder.share.fetch_notes, 0, "help", word[0][""][1], word[0][""][0])
        await asyncio.sleep(0.1)
    asyncio.run(go())
    parts = [f for f in to_holder.sent if f["op"] == "nb"]
    assert max(f["seq"] for f in parts) > 3, "sent in slices"
    assert all(holder.store.notes_of(c) and holder.store.notes_of(c)[2] == "help" for c in (1, 2))
    assert holder.store.notes_of(0) is None and "help" in holder._notes_failed[0], "let go there: ask elsewhere"
    blob, nd, _src = holder.store.notes_of(1)
    h = helper.store.all_notes_words(3)[1][""][0]
    assert holder.got_notes("liar", 1, blob, "00" * 32, h) is False, "not the digest it said"
    assert holder.got_notes("liar", 1, blob, nd, "00" * 32) is False, "its blocks do not make the hash it said"
    assert holder.got_notes("liar", 1, blob[:-3], nd, h) is False, "cut short"


def test_a_chunk_stitched_from_notes_the_audit_outvotes_is_said_never_wiped(tmp_path):
    """The holder trusted one helper's notes for a chunk during the cover; two auditors later say otherwise of it. It is
    said for the operator; the index is kept (2026-10-20: never lose an index by itself)."""
    chain = _story_chain()
    mesh = Mesh()
    a = dx.DoginalIndex(tmp_path / "a.sqlite", lambda: chain, lambda: "help", cores=lambda: 1)
    holder = dx.DoginalIndex(tmp_path / "h.sqlite", lambda: chain, lambda: "walk", own_core=lambda: False)
    mesh.join("a", a)
    mesh.join("h", holder)
    for _ in range(6):
        a.step()
        holder.step()
    assert holder.walker.next_height() == dx.BASE + 12
    assert holder.store.q("SELECT COUNT(*) FROM stitched")[0][0] == 3
    holder._pruned = 0
    holder._prune_notes()
    assert holder.walker.next_height() == dx.BASE + 12, "one word, not outvoted: kept"
    for liar in ("x", "y"):
        holder.heard(liar, {"op": "hi", "mode": "help", "rules": dx.ord_rules.rules_for("doge")})
        holder.heard(liar, {"op": "dn", "rows": [[1, "cd" * 32, 0, "ef" * 32]]})
    kept = holder.store.q("SELECT COUNT(*) FROM insc")[0][0]
    holder._pruned = 0
    holder._prune_notes()
    assert holder.status()["disputed"] == [1] or holder.disputed == [1]
    assert holder.walker.next_height() == dx.BASE + 12 and holder.store.q("SELECT COUNT(*) FROM insc")[0][0] == kept
    for liar in ("p", "q"):                              # other notes, the same births: not a dispute
        holder.heard(liar, {"op": "hi", "mode": "help", "rules": dx.ord_rules.rules_for("doge")})
    assert holder.disputed == [1]


def _hold_round(mesh, *indexes):
    for idx in indexes:
        idx.step()
    for name, idx in mesh.members.items():                 # every arcade's hello, as the mesh says it each minute
        for other, peer in mesh.members.items():
            if other != name and other in mesh.up:
                peer.heard(name, idx.hello())


def test_the_whole_index_comes_from_the_arcades_that_hold_it(tmp_path, monkeypatch):
    """2026-10-20: "Anyone who wants to hold the full index can request it, and it will be served torrent style
    from the rest of its peers that hold it". A newcomer far behind asks; the furthest holder makes a snapshot; its
    pieces come from every arcade that has them -- a second newcomer takes some from the first, still fetching; the
    copy's moves are checked with a second holder, and it takes the newcomer's index's place."""
    from arcade import ord_snapshot
    monkeypatch.setattr(ord_snapshot, "WORTH_BLOCKS", 4)
    monkeypatch.setattr(ord_snapshot, "PIECE", 4096)
    chain = _story_chain()
    mesh = Mesh()
    first = dx.DoginalIndex(tmp_path / "first.sqlite", lambda: chain, lambda: "walk", cores=lambda: 1)
    second = dx.DoginalIndex(tmp_path / "second.sqlite", lambda: chain, lambda: "walk", cores=lambda: 1)
    newcomer = dx.DoginalIndex(tmp_path / "new.sqlite", lambda: chain, lambda: "walk", own_core=lambda: False)
    later = dx.DoginalIndex(tmp_path / "later.sqlite", lambda: chain, lambda: "walk", own_core=lambda: False)
    first.walker.step(chain, dx.BASE + 11, budget=60)
    second.walker.step(chain, dx.BASE + 11, budget=60)
    for idx in (first, second):                          # long stitched past: no notes left to stitch from
        idx.store.x("DELETE FROM notes")
    for name, idx in (("first", first), ("second", second), ("new", newcomer)):
        mesh.join(name, idx)
        idx.me = name
    for n in range(40):
        if n == 3:
            mesh.join("later", later)
            later.me = "later"
        _hold_round(mesh, first, newcomer, *([later] if n >= 3 else []))
        if all(i.snapshots.said.startswith("the index came") for i in (newcomer, later)):
            break
    assert first.snapshots.made is not None, "asked, the holder made one"
    assert newcomer.snapshots.said.startswith("the index came"), newcomer.snapshots.said
    assert newcomer.walker.next_height() == dx.BASE + 12
    assert _dump(newcomer) == _dump(first)
    assert newcomer.store.q("SELECT COUNT(*) FROM notes")[0][0] == 0, "no work in hand came with it"
    assert not newcomer.snapshots.single, "a second holder's moves were compared"
    pieces = [x for x in mesh.fetched if x[0] == "new" and x[2] == "piece"]
    assert len(pieces) == len(first.snapshots.made["manifest"]["pieces"]) > 1
    assert _dump(later) == _dump(first)
    assert any(x[0] == "later" and x[1] == "new" and x[2] == "piece" for x in mesh.fetched), \
        "pieces from an arcade still fetching them: torrent style"


def test_a_fetch_of_a_snapshot_nobody_offers_any_more_is_let_go_and_the_newest_taken(tmp_path, monkeypatch):
    """2026-10-22: @yourfirstname's Pepecoin walk sat at its first block all day. Its fetch was of a snapshot the holder
    had let go of and made again under a new id: no piece of the old one ever came, and the walk waits while a fetch
    runs. A fetch that gets nothing for FETCH_STALL is let go of, and the copy on offer now is fetched."""
    from arcade import ord_snapshot
    monkeypatch.setattr(ord_snapshot, "WORTH_BLOCKS", 4)
    monkeypatch.setattr(ord_snapshot, "PIECE", 4096)
    chain = _story_chain()
    mesh = Mesh()
    holder = dx.DoginalIndex(tmp_path / "h.sqlite", lambda: chain, lambda: "walk", cores=lambda: 1)
    newcomer = dx.DoginalIndex(tmp_path / "new.sqlite", lambda: chain, lambda: "walk", own_core=lambda: False)
    holder.walker.step(chain, dx.BASE + 11, budget=60)
    holder.store.x("DELETE FROM notes")
    holder._snap_asked = True
    for name, idx in (("holder", holder), ("new", newcomer)):
        mesh.join(name, idx)
        idx.me = name
    for _ in range(6):
        _hold_round(mesh, holder, newcomer)
        if newcomer.snapshots.fetch is not None:
            break
    f = newcomer.snapshots.fetch
    assert f is not None, newcomer.snapshots.said
    f.update(id="0" * 64, manifest=None, have=set(), asked={})   # the copy it began on: gone, nobody offers it
    f.pop("moved", None)
    for _ in range(4):
        _hold_round(mesh, holder, newcomer)
    assert newcomer.snapshots.fetch is not None and newcomer.walker.next_height() == dx.BASE, "waits, as it did"
    newcomer.snapshots.fetch["started"] -= ord_snapshot.FETCH_STALL + 1
    for _ in range(12):
        _hold_round(mesh, holder, newcomer)
        if newcomer.snapshots.said.startswith("the index came"):
            break
    assert newcomer.snapshots.said.startswith("the index came"), newcomer.snapshots.said
    assert newcomer.walker.next_height() == dx.BASE + 12
    assert _dump(newcomer) == _dump(holder)


def _fetch_setup(tmp_path, monkeypatch, own_core=False):
    from arcade import ord_snapshot
    monkeypatch.setattr(ord_snapshot, "WORTH_BLOCKS", 4)
    monkeypatch.setattr(ord_snapshot, "PIECE", 4096)
    monkeypatch.setattr(ord_snapshot, "IN_FLIGHT", 1)            # a piece a pass: a fetch caught half way
    monkeypatch.setattr(ord_snapshot, "FETCH_SAVE", 0)
    chain = _story_chain()
    mesh = Mesh()
    holder = dx.DoginalIndex(tmp_path / "h.sqlite", lambda: chain, lambda: "walk", cores=lambda: 1)
    newcomer = dx.DoginalIndex(tmp_path / "new.sqlite", lambda: chain, lambda: "walk", own_core=lambda: own_core,
                               cores=lambda: 1)
    holder.walker.step(chain, dx.BASE + 11, budget=60)
    holder.store.x("DELETE FROM notes")
    holder._snap_asked = True
    for name, idx in (("holder", holder), ("new", newcomer)):
        mesh.join(name, idx)
        idx.me = name
    return chain, mesh, holder, newcomer


def test_a_fetch_cut_short_by_a_restart_carries_on_with_the_pieces_it_had(tmp_path, monkeypatch):
    """2026-10-22: @yourfirstname's fetch went from 420 of 2,978 pieces back to 13 when its arcade restarted. Where a
    fetch got to is kept beside its .part; after a restart each kept piece is checked again by its hash (one half
    written is asked for again) and the rest are fetched."""
    chain, mesh, holder, newcomer = _fetch_setup(tmp_path, monkeypatch)
    for _ in range(40):
        _hold_round(mesh, holder, newcomer)
        f = newcomer.snapshots.fetch
        if f and f["manifest"] and len(f["have"]) >= 3:
            break
    f = newcomer.snapshots.fetch
    total = len(f["manifest"]["pieces"])
    assert 3 <= len(f["have"]) < total, (len(f["have"]), total)
    kept = sorted(f["have"])
    with open(newcomer.snapshots.part_path, "r+b") as part:      # the arcade stopped half way through a write
        part.seek(kept[0] * 4096)
        part.write(b"\0" * 16)
    newcomer._snapshots = None                                   # the restart: everything in memory gone
    again = newcomer.snapshots.fetch
    assert again is not None and again["have"] == set(kept[1:]), "kept, each checked again"
    assert "carried on after a restart" in newcomer.snapshots.said
    before = len([x for x in mesh.fetched if x[0] == "new" and x[2] == "piece"])
    for _ in range(4 * total):
        _hold_round(mesh, holder, newcomer)
        if newcomer.snapshots.said.startswith("the index came"):
            break
    assert newcomer.snapshots.said.startswith("the index came"), newcomer.snapshots.said
    after = len([x for x in mesh.fetched if x[0] == "new" and x[2] == "piece"]) - before
    assert after == total - len(kept) + 1, (after, total, len(kept))
    assert _dump(newcomer) == _dump(holder)
    assert not newcomer.snapshots.fetch_path.exists() and not newcomer.snapshots.part_path.exists()


def test_a_stalled_fetch_of_a_copy_still_offered_keeps_its_pieces(tmp_path, monkeypatch):
    from arcade import ord_snapshot
    chain, mesh, holder, newcomer = _fetch_setup(tmp_path, monkeypatch)
    for _ in range(40):
        _hold_round(mesh, holder, newcomer)
        f = newcomer.snapshots.fetch
        if f and f["manifest"] and len(f["have"]) >= 2:
            break
    f = newcomer.snapshots.fetch
    had = set(f["have"])
    f["moved"] -= ord_snapshot.FETCH_STALL + 1
    newcomer.snapshots.step(newcomer.share)
    assert newcomer.snapshots.fetch is f and had <= f["have"], "asked again, nothing thrown away"


def test_the_walk_goes_on_from_its_own_core_while_a_copy_comes_and_the_first_there_wins(tmp_path, monkeypatch):
    """2026-10-22: "Both" -- fetching the copy and walking from its own node at once; once the walk passes the
    copy's block, the copy is let go of."""
    chain, mesh, holder, newcomer = _fetch_setup(tmp_path, monkeypatch)
    for _ in range(6):
        _hold_round(mesh, holder, newcomer)
        if newcomer.snapshots.fetch is not None:
            break
    assert newcomer.snapshots.fetch is not None
    newcomer.own_core = lambda: True                              # its own node, caught up meanwhile
    newcomer.tip = dx.BASE + 11
    newcomer._walk(chain, 60)                                     # walks on while the copy comes
    assert newcomer.snapshots.fetch is not None or "walked past" in newcomer.snapshots.said
    assert newcomer.walker.next_height() == dx.BASE + 12
    newcomer.snapshots.step(newcomer.share)
    assert newcomer.snapshots.fetch is None and "walked past" in newcomer.snapshots.said
    assert not newcomer.snapshots.part_path.exists()


def test_a_snapshot_whose_moves_another_holder_does_not_share_is_refused(tmp_path, monkeypatch):
    from arcade import ord_snapshot
    monkeypatch.setattr(ord_snapshot, "WORTH_BLOCKS", 4)
    chain = _story_chain()
    mesh = Mesh()
    liar = dx.DoginalIndex(tmp_path / "liar.sqlite", lambda: chain, lambda: "walk", cores=lambda: 1)
    honest = dx.DoginalIndex(tmp_path / "honest.sqlite", lambda: chain, lambda: "walk", cores=lambda: 1)
    newcomer = dx.DoginalIndex(tmp_path / "new.sqlite", lambda: chain, lambda: "walk", own_core=lambda: False)
    liar.walker.step(chain, dx.BASE + 11, budget=60)
    honest.walker.step(chain, dx.BASE + 11, budget=60)
    # The liar moves one inscription somewhere else in its own copy, moves and all.
    liar.store.x("UPDATE block SET moves='ab' WHERE height=?", (dx.BASE + 8,))
    for idx in (liar, honest):
        idx.store.x("DELETE FROM notes")
    monkeypatch.setattr(ord_snapshot, "MOVES_SAMPLE", 3)
    for name, idx in (("liar", liar), ("honest", honest), ("new", newcomer)):
        mesh.join(name, idx)
        idx.me = name
    liar._snap_asked = True
    honest.walker.next_height = lambda: dx.BASE + 5       # not ahead enough to be the one asked
    for _ in range(40):
        _hold_round(mesh, liar, newcomer)
        if "refused" in newcomer.snapshots.said:
            break
    assert "refused" in newcomer.snapshots.said and "moves in chunk 2" in newcomer.snapshots.said
    assert newcomer.store.q("SELECT COUNT(*) FROM insc")[0][0] == 0, "nothing of it was used"


def test_a_chunk_only_an_older_arcade_said_is_scanned_here_not_waited_for(tmp_path):
    """An arcade from before the notes says a chunk's hash and keeps no notes: a holder on many cores does not wait
    for notes nobody can send -- while plenty of chunks further on are still unscanned (the cover far from done), its
    lane takes that chunk next."""
    chain = _busy_chain(40)                                 # ten chunks

    def holder_after_three_passes(name, needed=True):
        holder = dx.DoginalIndex(tmp_path / f"{name}.sqlite", lambda: chain, lambda: "walk", cores=lambda: 2)
        if not needed:
            holder._needed = lambda: []                   # as it was before: the lane goes on with the cover
        Mesh().join(name, holder)
        holder.heard("old", {"op": "hi", "mode": "help", "rules": dx.ord_rules.rules_for("doge"), "v": 2})
        holder.heard("old", {"op": "dn", "rows": [[1, "ab" * 32, 0]]})
        for _ in range(3):
            holder.step()
        return holder.walker.next_height() - dx.BASE
    assert holder_after_three_passes("before", needed=False) == 4, "it waited at chunk 1 for notes nobody has"
    assert holder_after_three_passes("now") >= 8


def test_an_envelope_begun_in_an_earlier_chunk_is_followed_back_and_zeroed_when_it_cannot_matter(tmp_path):
    """2026-10-20: PEP's big images in hundreds of pieces cross every chunk's edge; kept whole, a chunk's notes were as
    big as its blocks (27 MB). Followed back to its first piece through the Core, an image's later pieces are zeroed;
    a name's are kept; and the index stitched from those notes is still the one a walk from a Core makes."""
    from arcade import ord_notes
    ord_notes._GENESIS.clear()
    chain = Chain()
    a = _tx([(FUND, 0, _envelope(b"image/jpeg", parts=((2, b"\xff\xd8" + b"J" * 200),), total=3))], [100_000], tag=b"a")
    n = _tx([(FUND, 1, _envelope(b"text/plain", parts=((1, b"pup"),), total=2))], [100_000], tag=b"n")
    chain.add([a, n])                                                           # block 0
    chain.add([])
    chain.add([])
    chain.add([])                                                               # blocks 1-3: the chunk ends
    b = _tx([(a[1], 0, _more(((1, b"K" * 200),)))], [100_000], tag=b"b")
    m = _tx([(n[1], 0, _more(((0, b".doge"),)))], [100_000], tag=b"m")
    chain.add([b, m])                                                           # block 4: both carry on
    c = _tx([(b[1], 0, _more(((0, b"L" * 200),)))], [100_000], tag=b"c")
    chain.add([c])                                                              # block 5: the image ends
    chain.add([])
    chain.add([])                                                               # blocks 6, 7
    blocks = [rawchain.read_block(raw) for _h, raw in chain.blocks]
    lookup = lambda txid: chain.txs[txid]
    second = ord_notes.finish([ord_notes.one_block(x) for x in blocks[4:8]], lookup)
    assert b"K" * 200 not in second and b"L" * 200 not in second, "the image's later pieces, zeroed"
    assert b".doge" in second, "a name's piece is kept: plain text can matter"
    kept_whole = ord_notes.finish([ord_notes.one_block(x) for x in blocks[4:8]])
    assert b"K" * 200 in kept_whole, "with no way to look back, kept whole"
    assert ord_notes.finish([ord_notes.one_block(x) for x in blocks[4:8]], lookup) == second, "canonical"

    walked = _walker(tmp_path, chain)
    walked.walker.step(chain, dx.BASE + 7, budget=60)
    assert walked.name("pup.doge")["readings"]["spec"]["winner"], "the name across the edge"
    assert ord_notes.unpack(walked.store.notes_of(1)[0]) == second, "a walk follows them back through its Core too"
    assert walked.store.q("SELECT size, pieces FROM insc WHERE tx=? AND n=0", (bytes.fromhex(a[1]),))[0] == (602, 3)
    notes = {0: ord_notes.finish([ord_notes.one_block(x) for x in blocks[0:4]], lookup), 1: second}
    stitched = dx.DoginalIndex(tmp_path / "s.sqlite", lambda: chain, lambda: "walk")
    stitched.walker.step(chain, dx.BASE + 7, budget=60, notes=lambda k: (ord_notes.decode(notes[k]), False))
    assert _dump(stitched) == _dump(walked)


def test_a_small_machine_never_fetches_the_whole_index(tmp_path, monkeypatch):
    """2026-10-20: .25 (four cores, a spinning disk, the live site) fetched two 6 GB copies at once and the site stopped
    answering. A machine under BIG_FROM_CORES walks on instead, and says so; nor does it ask a holder to make one."""
    from arcade import ord_snapshot
    monkeypatch.setattr(ord_snapshot, "WORTH_BLOCKS", 4)
    chain = _story_chain()
    mesh = Mesh()
    holder = dx.DoginalIndex(tmp_path / "h.sqlite", lambda: chain, lambda: "walk", cores=lambda: 1)
    small = dx.DoginalIndex(tmp_path / "s.sqlite", lambda: chain, lambda: "walk", own_core=lambda: False)
    holder.walker.step(chain, dx.BASE + 11, budget=60)
    holder.store.x("DELETE FROM notes")
    holder._snap_asked = True
    for name, idx in (("holder", holder), ("small", small)):
        mesh.join(name, idx)
        idx.me = name
    monkeypatch.setattr(ord_snapshot.os, "cpu_count", lambda: 4)
    for _ in range(6):
        _hold_round(mesh, holder, small)
    assert small.snapshots.fetch is None and small.walker.next_height() == dx.BASE
    assert "fewer than 8 cores" in small.snapshots.said
    assert not any(x[2] == "piece" for x in mesh.fetched if x[0] == "small")
    monkeypatch.setattr(ord_snapshot.os, "cpu_count", lambda: 20)
    assert "free disk" in small.snapshots.may_fetch(10 ** 15), "and a big one with too little room says so"


def test_the_status_says_how_many_arcades_hold_each_stretch_and_agree(tmp_path, monkeypatch):
    """2026-10-21/22: the Ords ring's inner circle red where one arcade holds a stretch, yellow where two do,
    green where three or more do -- counted by the walks that have passed each chunk, a chunk whose words disagree
    counting only as many as agree. agree_runs are [first chunk, last + 1, holding-and-agreeing (0-3)]."""
    chain = Chain()
    index = _walker(tmp_path, chain)
    index.tip = dx.BASE + 6 * dx.CHUNK - 1 + dx.SETTLE                 # six whole chunks ready
    C = dx.CHUNK
    holders = [("", dx.BASE + 5 * C), ("b" * 64, dx.BASE + 3 * C), ("c" * 64, dx.BASE + 1 * C)]
    monkeypatch.setattr(index, "holders", lambda: holders)
    st = index.store
    st.say(2, "", "x", 0); st.say(2, "b" * 64, "y", 0)                  # chunk 2: two hold it but disagree
    runs = index.status()["agree_runs"]
    assert runs == [[0, 1, 3], [1, 2, 2], [2, 5, 1], [5, 6, 0]], runs   # chunk 2 disagrees: 1, as chunks 3-4 held by one


def test_the_status_says_each_linked_arcades_copy_fetch():
    """2026-10-22: two arcades sat at one block for hours, and nothing said whether they were fetching a copy."""
    assert dx._copy_said({"id": "a" * 64, "h": 1009720, "n": 2978, "have": [0, 1, 2]}) == \
        {"copy": {"h": 1009720, "of": 2978, "have": 3}}
    assert dx._copy_said({"id": "a" * 64, "h": 5, "n": 9, "have": "all"})["copy"]["have"] == "all"
    assert dx._copy_said(None) == {}


def test_the_walk_goes_to_two_blocks_under_the_tip_and_chunks_stay_twelve_under(tmp_path, monkeypatch):
    """2026-10-22: "Why can't strawberry keep up with the confirmed blocks?" -- it sat 12 under the tip. The walk
    now goes to WALK_SETTLE (2) under it, reading the last blocks from its Core; a chunk, compared across arcades, is
    still only whole SETTLE (12) under the tip."""
    monkeypatch.setattr(dx, "WALK_SETTLE", 2)
    monkeypatch.setattr(dx, "SETTLE", 12)
    chain = _story_chain()
    tip = dx.BASE + 11
    index = dx.DoginalIndex(tmp_path / "w.sqlite", lambda: chain, lambda: "walk", cores=lambda: 1)
    index.walker.step(chain, tip, budget=60)
    assert index.walker.next_height() == tip - 2 + 1
    assert dx.ready_chunks(tip, dx.BASE) == 0, "no chunk is whole twelve under this tip"
