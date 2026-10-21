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
    assert index.on(buy[1], 1) == [{"id": iid, "offset": 10_000}]
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
        index.share = Share()
        for other, idx in self.members.items():               # link up: each says everything it has
            if other != name:
                idx.heard(name, index.hello())
                index.heard(other, idx.hello())
                if idx.all_mine():
                    index.heard(other, {"op": "dn", "rows": idx.all_mine()})


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
    # The helper read whole chunks, and its hash of each is the walker's.
    for c in range(3):
        assert helper.store.words(c)[""] == walker.store.words(c)[""]


def test_a_helper_that_lies_shows_as_a_difference(tmp_path):
    chain = _busy_chain(8)
    walker = _walker(tmp_path, chain)
    walker.step()
    walker.heard("liar", {"op": "hi", "mode": "help", "rules": dx.ord_rules.RULES})
    walker.heard("liar", {"op": "dn", "rows": [[0, "ab" * 32, 0]]})
    status = walker.status()
    assert status["differ"] == 1 and status["differs"][0]["chunk"] == 0
    assert status["agreed"] == 0


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
    a = dx.DoginalIndex(tmp_path / "a.sqlite", lambda: chain, lambda: "help")
    b = dx.DoginalIndex(tmp_path / "b.sqlite", lambda: chain, lambda: "help")
    mesh.join("a", a)
    mesh.join("b", b)
    a.step(budget=0)
    b.step(budget=0)
    # Each wants a chunk nobody else holds while there are such chunks (AGREE 2: a held chunk may still be chosen by a
    # second helper, which is a second scan of it, never a wrong one).
    assert a.chunks.mine and b.chunks.mine
    assert b.status()["leases"][0]["node"] == "a"


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
    assert on["on"] == [{"id": f"{reveal[1]}i0", "offset": 0}]
    assert reader.store.q("SELECT COUNT(*) FROM insc")[0][0] == 0, "and the reader keeps no index"
