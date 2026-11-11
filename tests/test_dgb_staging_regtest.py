"""The DigiByte staging checks (2026-11-09, security review): the arcade against DigiByte Core 9.26.7 itself, on regtest.

DigiByte Core is an independent implementation of everything the arcade does in its own code for DigiByte -- BIP32,
the three address kinds, BIP143 signing, descriptors -- so each answer here is the arcade's checked by somebody else's:

* **Vectors.** From one public test phrase, the legacy, nested SegWit (P2SH-P2WPKH) and native SegWit addresses on
  m/44', m/49' and m/84' (coin type 3, the mainnet keys, and 1, the test-chain keys), from the browser's coins.js and
  from Python (arcade/script.py over a BIP32 worked out in the tests), are the addresses Core derives from the same
  keys (`getdescriptorinfo`, `deriveaddresses`).
* **Sends.** Each kind is funded, the Wallet's own route builds a send from it (/account/send), coins.js checks and
  signs it as the browser does (verifyOffer, signInput), /account/sign assembles it, and Core takes it: its
  `testmempoolaccept` allows it, its `decoderawtransaction` names the payee and amount verifyOffer named (a P2SH
  payee among them), and its fee is the fee verifyOffer said.
* **Restore.** The restore kit's printed commands (coins.js restoreKit, arcade/restore_kit.py), run as printed against
  a fresh Core descriptor wallet, see the same addresses and the funded balance, and spend it: the twelve words
  recover outside the arcade.
* **Reorgs.** A coin the index credited from a block that is reorged away (invalidateblock, its funding spent elsewhere
  in the new chain) is taken back, and a swap acts on a DigiByte lock only while it stands: Admin's "Every swap waits
  for 1 confirmation" makes a lock in the mempool wait, and a lock reorged out is never claimed or answered.

Regtest's addresses differ from DigiByte testnet's only in the bech32 prefix ("dgbrt" against "dgbt"; P2PKH 126 and
P2SH 140 on both, chainparams.cpp), and its extended keys are tprv/tpub. So the arcade runs its DigiByte testnet chain
here, and a native SegWit address is said to Core in "dgbrt". Skipped where `digibyted` or `node` is not installed.
"""
from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import pathlib
import shlex
import shutil
import subprocess
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_extension import H, _bip32, _pubkey                      # noqa: E402
from test_funding import _sign                                     # noqa: E402
from test_me_page import _seat                                     # noqa: E402
from test_restore_kit import CORE_SAID, PHRASE, descsum            # noqa: E402
from test_trade import _trade                                      # noqa: E402
from address_claim import file_address, register_segwit           # noqa: E402

from arcade import funding, restore_kit, seed, utxos               # noqa: E402
from arcade import node_trader as NT, trade as T                   # noqa: E402
from arcade.config import NETWORKS, RpcCredentials                 # noqa: E402
from arcade.rpc import RpcClient                                   # noqa: E402
from arcade.script import (b58check_encode, b58decode, b58encode, hash160,  # noqa: E402
                           p2sh_p2wpkh_address, p2wpkh_address, segwit_decode, segwit_encode)

ROOT = pathlib.Path(__file__).resolve().parent.parent
COIN = 100_000_000
SEED = seed.to_seed(PHRASE)
#: The arcade's DigiByte testnet, as DigiByte regtest writes it: only the bech32 prefix differs.
TESTNET = NETWORKS["dgbtest"]
REGTEST = dataclasses.replace(TESTNET, bech32_hrp="dgbrt", genesis="")
#: What coins.js electrumWallet takes for a chain's address bytes: DigiByte regtest's.
REGTEST_BYTES = {"pubkeyhash": 126, "scripthash": 140, "bech32Hrp": "dgbrt", "wif": 0xFE}
#: BIP32 versions: xprv/xpub (mainnet) and tprv/tpub (every test chain, regtest too).
XPRV, XPUB, TPRV, TPUB = 0x0488ADE4, 0x0488B21E, 0x04358394, 0x043587CF
KINDS = (("legacy", 44, "pkh({})"), ("p2shSegwit", 49, "sh(wpkh({}))"), ("nativeSegwit", 84, "wpkh({})"))

pytestmark = [pytest.mark.skipif(shutil.which("digibyted") is None, reason="digibyted is not installed here"),
              pytest.mark.skipif(shutil.which("node") is None, reason="needs Node")]


# --- the Core, its wallets, and the browser's half ------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class _WalletCreds(RpcCredentials):
    """A node's credentials, aimed at one of its wallets (/wallet/<name>): two wallets are loaded here."""
    wallet: str = ""

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/wallet/{self.wallet}"


def _wallet(node, name: str) -> RpcClient:
    creds = node.rpc._creds
    return RpcClient(_WalletCreds(creds.host, creds.port, creds.user, creds.password, wallet=name), timeout=60)


@pytest.fixture(scope="module")
def dgb():
    from arcade.regtest import RegtestNode
    with RegtestNode(binary="digibyted", cli="digibyte-cli") as node:
        node.rpc.call("createwallet", "miner")
        miner = _wallet(node, "miner")
        node.miner = miner
        node.mine = lambda n=1: node.rpc.call("generatetoaddress", n, miner.call("getnewaddress", "", "legacy"))
        node.mine(150)                                    # past coinbase maturity
        yield node
        miner.close()


def _harness(asked: dict):
    out = subprocess.run(["node", "--experimental-default-type=module", "--import", "./extension/test/register.mjs",
                          str(ROOT / "tests" / "js" / "dgb_staging.mjs"), json.dumps(asked)],
                         capture_output=True, text=True, timeout=180, cwd=ROOT)
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout)


@pytest.fixture(scope="module")
def derived():
    """coins.js's keys and addresses for the test phrase, on DigiByte (coin type 3) and DigiByte testnet (1), and its
    restore kits for both, as restore_kit.chains() hands them to the backup page."""
    rows = [row for row in restore_kit.chains() if row["network"] in ("dgb", "dgbtest")]
    return _harness({"mode": "derive", "seed": SEED.hex(), "networks": ["dgb", "dgbtest"], "v": REGTEST_BYTES,
                     "indexes": 3, "chains": rows})


def _as_test_key(extended: str) -> str:
    """An xprv/xpub said again as tprv/tpub (regtest's), the same key: only the four version bytes differ."""
    raw = b58decode(extended)
    body = raw[:-4]
    version = int.from_bytes(body[:4], "big")
    if version in (TPRV, TPUB):
        return extended
    body = {XPRV: TPRV, XPUB: TPUB}[version].to_bytes(4, "big") + body[4:]
    return b58encode(body + hashlib.sha256(hashlib.sha256(body).digest()).digest()[:4])


def _root(kit: dict) -> str:
    """The kit's master key (the one its descriptors start from), as regtest's Core reads it."""
    desc = next(d["desc"] for d in kit["descriptors"] if d["kind"] == "legacy")
    return _as_test_key(desc[desc.index("(") + 1:desc.index("/")])


def _with_sum(dgb, body: str) -> str:
    """`body`#checksum, the checksum Core's own (getdescriptorinfo), held to BIP380's in Python too."""
    said = dgb.rpc.call("getdescriptorinfo", body)
    assert said["checksum"] == descsum(body), body
    return f"{body}#{said['checksum']}"


def _testnet(address: str) -> str:
    """A regtest address as the arcade says it on DigiByte testnet: dgbrt1... is dgbt1..., base58 is the same."""
    if address.startswith("dgbrt1"):
        version, program = segwit_decode("dgbrt", address)
        return segwit_encode("dgbt", version, program)
    return address


def _regtest(address: str) -> str:
    """An address as the arcade says it on DigiByte testnet, as regtest's Core says it: dgbt1... is dgbrt1...."""
    if address.startswith("dgbt1"):
        version, program = segwit_decode("dgbt", address)
        return segwit_encode("dgbrt", version, program)
    return address


# --- 1. vectors ------------------------------------------------------------------------------------------------------

def test_digibyte_core_derives_the_arcades_three_kinds_of_address_from_the_same_words(dgb, derived):
    """m/44', m/49', m/84' on coin type 3 (DigiByte's keys) and 1 (DigiByte testnet's), indexes 0-2: coins.js,
    Python and DigiByte Core give the same key's same address of each kind."""
    kits = {kit["network"]: kit for kit in derived["kits"]}
    for network, coin in (("dgb", 3), ("dgbtest", 1)):
        root = _root(kits[network])
        for name, purpose, wrap in KINDS:
            core = dgb.rpc.call("deriveaddresses",
                                _with_sum(dgb, wrap.format(f"{root}/{purpose}h/{coin}h/0h/0/*")), [0, 2])
            for i in range(3):
                said = derived["wallets"][network][i][name]
                key = _bip32(SEED, (purpose | H, coin | H, H, 0, i))
                pub = _pubkey(key)
                assert said["path"] == f"m/{purpose}'/{coin}'/0'/0/{i}"
                assert said["key"] == key.hex() and said["pubkey"] == pub.hex(), (network, name, i)
                python = {"legacy": b58check_encode(REGTEST.pubkeyhash_version, hash160(pub)),
                          "p2shSegwit": p2sh_p2wpkh_address(REGTEST, hash160(pub)),
                          "nativeSegwit": p2wpkh_address(REGTEST, hash160(pub))}[name]
                assert said["address"] == python == core[i], (network, name, i, said["address"], python, core[i])
        # The account's watch-only key (the kit's xpub, m/44'/<coin>'/0') gives Core the same legacy addresses.
        watch = dgb.rpc.call("deriveaddresses", _with_sum(dgb, f"pkh({_as_test_key(kits[network]['xpub'])}/0/*)"),
                             [0, 2])
        assert watch == [derived["wallets"][network][i]["legacy"]["address"] for i in range(3)], network
        # The arcade's own coin key (coinKey, m/44'/<coin>'/0'/0/i) is the legacy kind's key.
        assert all(w["coinKey"]["key"] == w["legacy"]["key"] for w in derived["wallets"][network])
    # And the same keys on DigiByte's mainnet bytes are what DigiByte Core 9.26.7 said on mainnet (test_restore_kit).
    w = derived["wallets"]["dgb"][0]
    main = NETWORKS["dgb"]
    for name, said in (("legacy", CORE_SAID["dgb"]["pkh"]), ("p2shSegwit", CORE_SAID["dgb"]["sh"]),
                       ("nativeSegwit", CORE_SAID["dgb"]["wpkh"])):
        kh = hash160(bytes.fromhex(w[name]["pubkey"]))
        assert {"legacy": b58check_encode(main.pubkeyhash_version, kh), "p2shSegwit": p2sh_p2wpkh_address(main, kh),
                "nativeSegwit": p2wpkh_address(main, kh)}[name] == said, name


# --- 1. sends from each kind, through the Wallet's own routes --------------------------------------------------------

class _Checked:
    """The arcade's way to the Core (chain.rpc()), which asks `testmempoolaccept` before every broadcast and keeps
    what it said: a broadcast the Core would refuse is a failed test, not a 409 to read."""

    def __init__(self, node):
        self.node, self.accepted = node, []

    @contextlib.contextmanager
    def __call__(self):
        yield self

    def call(self, method, *params):
        if method == "sendrawtransaction":
            (verdict,) = self.node.rpc.call("testmempoolaccept", [params[0]])
            assert verdict["allowed"], verdict
            self.accepted.append((params[0], verdict))
        return self.node.rpc.call(method, *params)

    def __getattr__(self, name):
        return getattr(self.node.rpc, name)


@pytest.fixture
def wallet_app(dgb, tmp_path, no_nodes):
    """An arcade running DigiByte testnet as an addon, its Core this regtest one, and a signed-in account."""
    from fastapi.testclient import TestClient

    from arcade.web.app import create_app
    from arcade.web.state import AppState, ChainContext
    (tmp_path / "settings.json").write_text("{}")
    state = AppState(
        home=tmp_path,
        messaging=ChainContext(network="regtest", role="messaging", label="Testnet", datadir=pathlib.Path("/nonexistent")),
        ledger=ChainContext(network="main", role="ledger", label="Mainnet", datadir=pathlib.Path("/nonexistent")),
        more={"dgbtest": ChainContext(network="dgbtest", role="addon", label="DigiByte testnet",
                                      conf=pathlib.Path("/nonexistent/digibyte.conf"))})
    state._sync["dgbtest"] = (time.time() + 86400, {"ready": True, "progress": 1.0, "online": True,
                                                    "blocks": 200, "headers": 200})
    checked = _Checked(dgb)
    object.__setattr__(state.more["dgbtest"], "rpc", checked)
    app = TestClient(create_app(state))
    _seat(app)
    return app, state, state.more["dgbtest"], checked


def _fund(dgb, address: str, amount: float) -> dict:
    """Coins at `address` from the miner, in a block; the coin as the arcade's index keeps it."""
    txid = dgb.miner.call("sendtoaddress", _regtest(address), amount)
    dgb.mine(1)
    tx = dgb.rpc.call("getrawtransaction", txid, True)
    out = next(o for o in tx["vout"] if o["scriptPubKey"].get("address") == _regtest(address))
    return {"txid": txid, "vout": out["n"], "address": address, "value": round(out["value"] * COIN),
            "height": dgb.rpc.get_block_count()}


def test_a_send_from_each_kind_is_built_by_the_wallet_checked_by_the_browser_and_taken_by_digibyte_core(
        dgb, derived, wallet_app):
    app, state, chain, checked = wallet_app
    mine = derived["wallets"]["dgbtest"][0]           # the account's keys on DigiByte testnet, m/4x'/1'/0'/0/0
    theirs = derived["wallets"]["dgbtest"][1]         # somebody to pay: index 1's three addresses
    as_arcade = {name: _testnet(mine[name]["address"]) for name, _, _ in KINDS}
    payee = {name: _testnet(theirs[name]["address"]) for name, _, _ in KINDS}

    # The account as sign-up leaves it, then the two SegWit kinds registered on the Wallet page, each with its key's
    # proof (the nested one's key is also what its address hides).
    pep_test = b58check_encode(state.messaging.params.pubkeyhash_version,
                               hash160(bytes.fromhex(derived["wallets"]["dgbtest"][0]["coinKey"]["pubkey"])))
    assert file_address(app, pep_test, int(mine["legacy"]["key"], 16)).status_code == 200
    r = file_address(app, as_arcade["legacy"], int(mine["legacy"]["key"], 16), chain="dgbtest")
    assert r.status_code == 200, r.text
    r = register_segwit(app, as_arcade["nativeSegwit"], int(mine["nativeSegwit"]["key"], 16), chain="dgbtest")
    assert r.status_code == 200 and r.json()["kind"] == "p2wpkh", r.text
    r = register_segwit(app, as_arcade["p2shSegwit"], int(mine["p2shSegwit"]["key"], 16), chain="dgbtest")
    assert r.status_code == 200 and r.json()["kind"] == "p2sh-p2wpkh", r.text

    funded = {kind: _fund(dgb, as_arcade[kind], 5) for kind in as_arcade}
    with state.token_index(chain).open() as db:
        for coin in funded.values():
            utxos.watch(db, coin["address"], coin["height"])
            db.conn.execute("INSERT OR REPLACE INTO utxo(txid,vout,address,value,height) VALUES (?,?,?,?,?)",
                            (coin["txid"], coin["vout"], coin["address"], coin["value"], coin["height"]))
        db.conn.commit()

    # Every kind pays another kind: legacy pays a P2SH address, nested a native one, native a legacy one.
    plan = (("legacy", "p2shSegwit", "p2pkh"), ("p2shSegwit", "nativeSegwit", "p2sh-p2wpkh"),
            ("nativeSegwit", "legacy", "p2wpkh"))
    for source, to_kind, flag in plan:
        asked = {"to": payee[to_kind], "amount": "1.5", "chain": "dgbtest",
                 **({"from": as_arcade[source]} if source != "legacy" else {})}
        r = app.post("/account/send", json=asked)
        assert r.status_code == 200, (source, r.text)
        offer = r.json()
        assert [i.get("segwit") or "p2pkh" for i in offer["inputs"]] == [flag], (source, offer["inputs"])
        assert [(i["txid"], i["vout"]) for i in offer["inputs"]] == [(funded[source]["txid"], funded[source]["vout"])]

        keys = {"key": mine[source]["key"], "pubkey": mine[source]["pubkey"], "address": as_arcade[source],
                "network": "dgbtest", "version": TESTNET.pubkeyhash_version, "nested": source == "p2shSegwit"}
        (shown,) = _harness({"mode": "sign", "offers": [{"offer": offer, "keys": keys}]})
        assert "refused" not in shown, (source, shown)
        paid = [p for p in shown["pays"] if not p["mine"]]
        assert paid == [{"to": payee[to_kind], "value": 150_000_000, "mine": False}], (source, shown["pays"])
        assert shown["fee"] == offer["fee"], "the browser's fee, read off the bytes, is the node's"

        before = len(checked.accepted)
        r = app.post("/account/sign", json={"offer": offer["offer"], "signatures": shown["signatures"],
                                            "pubkey": mine[source]["pubkey"]})
        assert r.status_code == 200, (source, r.text)
        txid = r.json()["txid"]
        assert len(checked.accepted) == before + 1, "testmempoolaccept was asked, and allowed it"
        raw, verdict = checked.accepted[-1]
        assert verdict["txid"] == txid and round(verdict["fees"]["base"] * COIN) == shown["fee"], (source, verdict)

        decoded = dgb.rpc.call("decoderawtransaction", raw)
        assert [(v["txid"], v["vout"]) for v in decoded["vin"]] == [(funded[source]["txid"], funded[source]["vout"])]
        outs = [(round(v["value"] * COIN), v["scriptPubKey"].get("address")) for v in decoded["vout"]]
        assert outs[0] == (150_000_000, _regtest(payee[to_kind])), (source, outs)
        assert outs[1] == (funded[source]["value"] - 150_000_000 - shown["fee"], _regtest(as_arcade[source])), \
            "the change back to the address it came from"
        assert sum(v for v, _ in outs) + shown["fee"] == funded[source]["value"]
        assert txid in dgb.rpc.call("getrawmempool")
    dgb.mine(1)
    assert not dgb.rpc.call("getrawmempool"), "all three are in a block"


# --- 2. restore, outside the arcade ----------------------------------------------------------------------------------

def test_the_restore_kits_commands_open_the_twelve_words_in_a_fresh_digibyte_core(dgb, derived):
    """The DigiByte testnet kit's lines, run as printed (only `-testnet` said as this regtest Core's datadir and port),
    into a Core that never saw the words: the same addresses are its own, it finds the coins on all five kinds, and it
    can spend them."""
    kit = next(k for k in derived["kits"] if k["network"] == "dgbtest")
    addresses = {"legacy": kit["address"], "p2sh-segwit": kit["segwit"][0]["address"],
                 "bech32": _regtest(kit["segwit"][1]["address"]), "bech32m": _regtest(kit["taproot"]["address"]),
                 # the Wallet's own Taproot address type (BIP-86, m/86', 2026-11-07), a key of its own
                 "bip86": _regtest(next(one for one in kit["segwit"] if one.get("kind") == "taproot")["address"])}
    assert addresses["legacy"] == derived["wallets"]["dgbtest"][0]["legacy"]["address"]
    funded = {kind: dgb.miner.call("sendtoaddress", address, 2 + n) for n, (kind, address) in enumerate(addresses.items())}
    dgb.mine(1)
    assert len(funded) == 5

    assert len(kit["commands"]) == 2 and kit["recipe"] == "descriptors"
    for line in kit["commands"]:
        words = shlex.split(line)
        assert words[:2] == ["digibyte-cli", "-testnet"], line
        out = subprocess.run([dgb.cli, "-regtest", f"-datadir={dgb.datadir}", f"-rpcport={dgb.rpc_port}", *words[2:]],
                             capture_output=True, text=True, timeout=120)
        assert out.returncode == 0, (line[:80], out.stderr)
        if "importdescriptors" in line:
            said = json.loads(out.stdout)
            assert len(said) == 10 and all(one["success"] for one in said), said

    restored = _wallet(dgb, "arcade")
    try:
        for kind, address in addresses.items():
            info = restored.call("getaddressinfo", address)
            assert info["ismine"] and info["solvable"], (kind, address)
        held = {(u["txid"], u["address"]): u["amount"] for u in restored.call("listunspent")}
        for n, (kind, address) in enumerate(addresses.items()):
            assert held.get((funded[kind], address)) == 2 + n, (kind, "its coins are found")
        # All of them: what the restored wallet holds is everything the chain holds on the kit's descriptors, read
        # straight from the coin set rather than from a wallet's rescan.
        scanned = dgb.rpc.call("scantxoutset", "start", [{"desc": d["desc"], "range": [0, 20]}
                                                          for d in kit["descriptors"]])
        assert round(restored.call("getbalance") * COIN) == round(scanned["total_amount"] * COIN) >= 14 * COIN
        # And spent: the keys are there, not only the addresses.
        back = dgb.miner.call("getnewaddress", "", "bech32")
        txid = restored.call("sendtoaddress", back, 13.5)            # more than any one kind holds
        assert txid in dgb.rpc.call("getrawmempool")
        dgb.mine(1)
    finally:
        restored.call("unloadwallet")
        restored.close()


# --- 4. reorgs --------------------------------------------------------------------------------------------------------

def _python_coin(dgb, secret: int, amount: float) -> dict:
    """A coin at a P2PKH key only this test holds, confirmed."""
    pub = _pubkey(secret.to_bytes(32, "big"))
    address = b58check_encode(REGTEST.pubkeyhash_version, hash160(pub))
    coin = _fund(dgb, address, amount)
    return {**coin, "secret": secret, "pubkey": pub}


def _spend(coin: dict, pays: list[tuple[int, bytes]]) -> str:
    """`coin` spent to `pays` (the rest to fee), signed here: built and assembled by arcade/funding.py."""
    raw = funding.build_raw_tx([(coin["txid"], coin["vout"])], pays)
    script = funding.p2pkh_script(coin["address"])
    digest = funding.sighash([coin], pays, 0, script)
    unsigned = funding.Unsigned(raw=raw, inputs=[coin], outputs=pays, sighashes=[digest.hex()], fee=0, change=0)
    return funding.assemble(unsigned, [_sign(coin["secret"], digest).hex()], coin["pubkey"])


def _reorg_out(dgb, block: str, conflict: str) -> None:
    """Take `block` away and put `conflict` (which spends what the block's transaction spent) in the new chain, one
    block taller, as a miner working against the lock would."""
    dgb.rpc.call("invalidateblock", block)
    address = dgb.miner.call("getnewaddress", "", "legacy")
    dgb.rpc.call("generateblock", address, [conflict])
    dgb.mine(1)


def _p2pkh(address: str) -> bytes:
    return funding.p2pkh_script(address)


def test_a_coin_credited_from_a_block_that_is_reorged_away_is_taken_back(dgb, tmp_path):
    """The index's credit is the chain's: the coin, its balance and its activity go when the block goes, and the coin
    that took its place in the new chain is credited instead."""
    from arcade.ledger import LedgerIndex
    payer = _python_coin(dgb, 0x5151 << 200, 10)
    params = dataclasses.replace(REGTEST, activation_height=dgb.rpc.get_block_count() + 1,
                                 marker_address=dgb.miner.call("getnewaddress", "", "legacy"))
    index = LedgerIndex(tmp_path / "dgb-ledger.sqlite", params, lambda: contextlib.nullcontext(dgb.rpc))
    credited = b58check_encode(126, hash160(_pubkey((0x5252 << 200).to_bytes(32, "big"))))
    elsewhere = b58check_encode(126, hash160(_pubkey((0x5353 << 200).to_bytes(32, "big"))))
    with index.open() as db:
        utxos.watch(db, credited, 0)
        utxos.watch(db, elsewhere, 0)
    dgb.mine(1)                                    # a block of its own first, below the one that will go
    assert index.sync() is not None

    paid = _spend(payer, [(9 * COIN, _p2pkh(credited))])
    dgb.rpc.call("sendrawtransaction", paid)
    (block,) = dgb.mine(1)
    index.sync()
    with index.open() as db:
        assert utxos.balance(db, credited) == 9 * COIN, "credited from the block"

    _reorg_out(dgb, block, _spend(payer, [(9 * COIN, _p2pkh(elsewhere))]))
    result = index.sync()
    assert result is not None and result.reorged, "the index saw the reorg"
    with index.open() as db:
        assert utxos.balance(db, credited) == 0 and not utxos.unspent(db, credited), "the credit went with its block"
        assert utxos.balance(db, elsewhere) == 9 * COIN, "and the coin that replaced it is the one credited"
        assert not db.conn.execute("SELECT 1 FROM activity WHERE address=?", (credited,)).fetchall()


def _swap(role: str, dgb_amount: int = 2 * COIN):
    """A swap whose other side locks DigiByte (the leg this trader claims), and this side Dogecoin: both mainnet tier,
    so the operator's lines apply. Locks are read from this regtest Core."""
    tr, _, _, _ = _trade(role, give_net="doge", get_net="dgb")
    tr.get = dataclasses.replace(tr.get, amount=dgb_amount)
    return tr


class _Settings:
    def __init__(self, **settings):
        self.settings, self.all_chains = settings, []

    def setting(self, key, default=None):
        return self.settings.get(key, default)


def test_a_swap_acts_on_a_digibyte_lock_only_while_the_chain_keeps_it(dgb):
    """Rule 9 on a real DigiByte Core: with "Every swap waits for 1 confirmation" on, the other side's lock in the
    mempool is waited for; once in a block it is acted on; and once that block is reorged away and the lock's coins
    spent elsewhere, neither side acts on it again -- the responder never locks against it, the initiator never claims
    it, and a responder already locked waits to refund."""
    for role in (T.RESPONDER, T.INITIATOR):
        tr = _swap(role)
        lines_off, lines_on = NT.swap_lines(_Settings()), NT.swap_lines(_Settings(**{NT.CONFIRM_ALL: True}))
        assert T.confirmations_for(tr.give, tr.get, lines_off) == 0, "2 DGB is below the line: zero-conf by default"
        tr.need = T.confirmations_for(tr.give, tr.get, lines_on)
        assert tr.need == 1, "the switch makes it wait"

        locker = _python_coin(dgb, (0x6161 if role == T.RESPONDER else 0x6262) << 200, 5)
        lock_script = funding.p2sh_script(hash160(tr.their_redeem()))
        lock = _spend(locker, [(tr.get.amount, lock_script)])
        txid = dgb.rpc.call("sendrawtransaction", lock)
        pointer = {"txid": txid, "vout": 0}
        now = int(time.time())
        mine = None if role == T.RESPONDER else T.LockSeen("ab" * 32, 0, tr.give.amount,
                                                           funding.p2sh_script(hash160(tr.my_redeem())).hex(), 1)
        expect = T.LOCK if role == T.RESPONDER else T.CLAIM

        seen = NT.observe(dgb.rpc, pointer, set())
        assert seen is not None and seen.confirmations == 0 and not seen.replaceable
        assert T.next_step(tr, T.Facts(mine=mine, theirs=seen), now)[0] == T.WAIT, (role, "in the mempool")
        tr.need = 0
        assert T.next_step(tr, T.Facts(mine=mine, theirs=seen), now)[0] == expect, (role, "without the switch")
        tr.need = 1

        (block,) = dgb.mine(1)
        seen = NT.observe(dgb.rpc, pointer, set())
        assert seen.confirmations == 1
        assert T.next_step(tr, T.Facts(mine=mine, theirs=seen), now)[0] == expect, (role, "in a block")

        # The block goes, and the lock's coins go elsewhere in the new chain.
        _reorg_out(dgb, block, _spend(locker, [(tr.get.amount, _p2pkh(locker["address"]))]))
        assert dgb.rpc.call("getrawtransaction", txid, True)["confirmations"] == 0, "the Core still finds it"
        seen = NT.observe(dgb.rpc, pointer, set())
        assert seen is None, (role, "a lock that is nowhere is not a lock in the mempool", seen)
        for need in (1, 0):
            tr.need = need
            step = T.next_step(tr, T.Facts(mine=mine, theirs=seen), now)[0]
            assert step == T.WAIT, (role, need, step)
        if role == T.RESPONDER:
            # Already locked when the reorg came: never more than waiting to refund.
            ours = T.LockSeen("cd" * 32, 0, tr.give.amount, funding.p2sh_script(hash160(tr.my_redeem())).hex(), 1)
            assert T.next_step(tr, T.Facts(mine=ours, theirs=seen), now)[0] == T.WAIT
            assert T.next_step(tr, T.Facts(mine=ours, theirs=seen), tr.my_locktime)[0] == T.REFUND
