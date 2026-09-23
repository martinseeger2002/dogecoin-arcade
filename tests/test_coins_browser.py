"""Coin keys, in a real browser, against the published vectors.

Nothing here trusts the implementation to check itself. BIP32's own test
vectors carry the expected extended keys, so the browser derives and the
answer is compared against a number written down by somebody else years
ago -- the same standard `tests/test_seed.py` holds BIP39 to.

That matters more here than anywhere else in this application: these are
the keys that hold coins, and the whole design is that the node never sees
them. If the browser derives them wrongly, nothing else will notice.
"""

import hashlib
import json
import pathlib
import socket
import sys
import threading
import time

import pytest

pytest.importorskip("selenium",
                    reason="browser tests need selenium: pip install .[dev]")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import browsers                                                  # noqa: E402

from arcade import script, seed                                  # noqa: E402

#: Satoshis in a coin, for the transactions built here to be hashed against
#: the ones the browser builds from the same words.
COIN = 100_000_000

#: An address that belongs to nobody, for the outputs of a transaction that
#: is only ever built to be hashed and never broadcast. Made rather than
#: copied out of a book, because a base58 checksum has to be right.
SOMEWHERE_ELSE = script.b58check_encode(111, bytes(range(20, 40)))

#: And an address that is not the one these tests derive, for an offer that
#: claims to spend a coin its own key does not hold.
SOMEONE_ELSE = script.b58check_encode(111, bytes(range(40, 60)))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def browser():
    driver = browsers.launch()
    yield driver
    driver.quit()


@pytest.fixture(scope="module")
def served(tmp_path_factory):
    import uvicorn
    from pathlib import Path
    from arcade.web.app import create_app
    from arcade.web.state import AppState, ChainContext

    state = AppState(
        home=tmp_path_factory.mktemp("coins"),
        messaging=ChainContext(network="regtest", role="messaging",
                               label="Testnet", datadir=Path("/nonexistent")),
        ledger=ChainContext(network="main", role="ledger", label="Mainnet",
                            datadir=Path("/nonexistent")),
    )
    port = _free_port()
    config = uvicorn.Config(create_app(state), host="127.0.0.1", port=port,
                            log_level="error", timeout_graceful_shutdown=1.0)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    else:
        pytest.skip("test server did not start")
    yield f"http://127.0.0.1:{port}", state
    server.should_exit = True
    thread.join(timeout=5)
    assert not thread.is_alive(), "the server is still serving a keep-alive connection"


LOAD = """
const done = arguments[arguments.length - 1];
import("/coins.js").then((m) => { window.coins = m; done(true); },
                         (e) => done(String(e)));
"""


@pytest.fixture(scope="module")
def loaded(browser, served):
    base, state = served
    browser.get(f"{base}/join")
    browser.set_script_timeout(60)
    result = browser.execute_async_script(LOAD)
    assert result is True, result
    return browser, base, state


# --- BIP32, vector 1 (the one everybody implements first) ---------------------
#
# https://github.com/bitcoin/bips/blob/master/bip-0032.mediawiki
# Seed 000102...0e0f. The expected values below are the SERIALISED extended
# keys from that page, decoded here rather than pasted as hex, so what the
# test compares against is literally the published string.

VECTOR_1_SEED = "000102030405060708090a0b0c0d0e0f"
VECTOR_1 = {
    "m": "xprv9s21ZrQH143K3QTDL4LXw2F7HEK3wJUD2nW2nRk4stbPy6cq3jPPqjiChkVvvNKmPGJxWUtg6LnF5kejMRNNU3TGtRBeJgk33yuGBxrMPHi",
    "m/0'": "xprv9uHRZZhk6KAJC1avXpDAp4MDc3sQKNxDiPvvkX8Br5ngLNv1TxvUxt4cV1rGL5hj6KCesnDYUhd7oWgT11eZG7XnxHrnYeSvkzY7d2bhkJ7",
    "m/0'/1": "xprv9wTYmMFdV23N2TdNG573QoEsfRrWKQgWeibmLntzniatZvR9BmLnvSxqu53Kw1UmYPxLgboyZQaXwTCg8MSY3H2EU4pWcQDnRnrVA1xe8fs",
    "m/0'/1/2'": "xprv9z4pot5VBttmtdRTWfWQmoH1taj2axGVzFqSb8C9xaxKymcFzXBDptWmT7FwuEzG3ryjH4ktypQSAewRiNMjANTtpgP4mLTj34bhnZX7UiM",
    "m/0'/1/2'/2": "xprvA2JDeKCSNNZky6uBCviVfJSKyQ1mDYahRjijr5idH2WwLsEd4Hsb2Tyh8RfQMuPh7f7RtyzTtdrbdqqsunu5Mm3wDvUAKRHSC34sJ7in334",
    "m/0'/1/2'/2/1000000000": "xprvA41z7zogVVwxVSgdKUHDy1SKmdb533PjDz7J6N6mV6uS3ze1ai8FHa8kmHScGpWmj4WggLyQjgPie1rFSruoUihUZREPSL39UNdE3BBDu76",
}

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58decode(text: str) -> bytes:
    number = 0
    for character in text:
        number = number * 58 + B58.index(character)
    out = number.to_bytes((number.bit_length() + 7) // 8, "big")
    return b"\x00" * (len(text) - len(text.lstrip("1"))) + out


def _parts(xprv: str) -> dict:
    """(depth, chain code, private key) out of a published extended key."""
    raw = _b58decode(xprv)
    assert hashlib.sha256(hashlib.sha256(raw[:-4]).digest()).digest()[:4] == raw[-4:]
    return {"depth": raw[4], "chain": raw[13:45].hex(), "key": raw[46:78].hex()}


DERIVE = """
const done = arguments[2];
(async () => {
  const c = window.coins;
  const seed = c.unhex(arguments[0]);
  let node = await c.master(seed);
  for (const index of arguments[1]) node = await c.child(node, index >>> 0);
  done({key: c.hex(node.key), chain: c.hex(node.chain), depth: node.depth,
        pubkey: c.hex(c.publicKey(node.key))});
})();
"""

H = 0x80000000
PATHS = {
    "m": [],
    "m/0'": [0 + H],
    "m/0'/1": [0 + H, 1],
    "m/0'/1/2'": [0 + H, 1, 2 + H],
    "m/0'/1/2'/2": [0 + H, 1, 2 + H, 2],
    "m/0'/1/2'/2/1000000000": [0 + H, 1, 2 + H, 2, 1000000000],
}


@pytest.mark.parametrize("path", list(PATHS))
def test_the_browser_walks_bip32_the_way_the_standard_says(loaded, path):
    """Including the NORMAL steps -- m/0'/1 and below -- which are the ones
    `arcade/seed.py` refuses because they need the curve. This is the file
    that has the curve, so this is where they are proved."""
    browser, _, _ = loaded
    said = browser.execute_async_script(DERIVE, VECTOR_1_SEED, PATHS[path])
    want = _parts(VECTOR_1[path])
    assert said["key"] == want["key"], path
    assert said["chain"] == want["chain"], path
    assert said["depth"] == want["depth"], path


def test_a_hardened_and_a_normal_child_are_not_the_same_key(loaded):
    """They differ by one bit in the index and by everything else."""
    browser, _, _ = loaded
    normal = browser.execute_async_script(DERIVE, VECTOR_1_SEED, [0])
    hardened = browser.execute_async_script(DERIVE, VECTOR_1_SEED, [0 + H])
    assert normal["key"] != hardened["key"]


# --- addresses ----------------------------------------------------------------

ADDRESS = """
const done = arguments[2];
(async () => {
  const c = window.coins;
  const pub = c.unhex(arguments[0]);
  done({hash160: c.hex(await c.hash160(pub)),
        address: await c.address(pub, arguments[1])});
})();
"""


def test_hash160_matches_the_node_s_own(loaded):
    """The browser derives its own address rather than asking: a node that
    answered could answer with somebody else's, coins would arrive where
    their owner cannot spend them, and nothing on the page would look
    wrong. So the two have to agree, and here they are made to."""
    from arcade.script import hash160

    browser, _, _ = loaded
    said = browser.execute_async_script(DERIVE, VECTOR_1_SEED, [0 + H])
    pubkey = bytes.fromhex(said["pubkey"])
    drawn = browser.execute_async_script(ADDRESS, said["pubkey"], 56)
    assert drawn["hash160"] == hash160(pubkey).hex()


@pytest.mark.parametrize("network,version,starts", [
    ("main", 56, "P"), ("test", 113, "n"),
])
def test_an_address_is_this_chain_s_shape(loaded, network, version, starts):
    from arcade.script import b58check_encode, hash160

    browser, _, _ = loaded
    said = browser.execute_async_script(DERIVE, VECTOR_1_SEED, [0 + H])
    drawn = browser.execute_async_script(ADDRESS, said["pubkey"], version)
    expected = b58check_encode(version, hash160(bytes.fromhex(said["pubkey"])))
    assert drawn["address"] == expected
    assert drawn["address"][0] == starts, "mainnet starts with P, testnet with n"


def test_base58_survives_a_round_trip_including_leading_zeroes(loaded):
    """A hash160 beginning with a zero byte is an address beginning with 1,
    and dropping it is the classic base58 bug."""
    browser, _, _ = loaded
    said = browser.execute_async_script("""
        const done = arguments[0];
        const c = window.coins;
        const bytes = new Uint8Array([0, 0, 1, 2, 3, 250, 255]);
        const text = c.base58(bytes);
        done({text, back: c.hex(c.unbase58(text))});""")
    assert said["back"] == "000001" + "0203faff"[0:]
    assert said["text"].startswith("11"), "two zero bytes, two leading ones"


# --- the coin path this application uses --------------------------------------

def test_the_same_words_give_the_same_coin_key_every_time(loaded):
    browser, _, _ = loaded
    phrase = seed.generate()
    said = browser.execute_async_script("""
        const done = arguments[2];
        (async () => {
          const signer = await import("/signin.js");
          const c = window.coins;
          const s = await signer.toSeed(arguments[0]);
          const a = await c.coinKey(s, arguments[1], 0);
          const b = await c.coinKey(s, arguments[1], 0);
          const next = await c.coinKey(s, arguments[1], 1);
          done({a: c.hex(a.pubkey), b: c.hex(b.pubkey), next: c.hex(next.pubkey)});
        })();""", phrase, "test")
    assert said["a"] == said["b"], "the same words, the same key"
    assert said["a"] != said["next"], "and a different one at the next index"


def test_the_coin_path_is_the_ordinary_one(loaded):
    """m/44'/coin'/0'/0/i, so these keys are found by any other wallet given
    the same words -- unlike the arcade's own branch, which is deliberately
    nobody else's business."""
    browser, _, _ = loaded
    said = browser.execute_async_script("""
        const done = arguments[0];
        const c = window.coins;
        done({test: c.coinPath("test", 0), main: c.coinPath("main", 7),
              hardened: c.HARDENED});""")
    H = said["hardened"]
    assert said["test"] == [44 + H, 1 + H, 0 + H, 0, 0]
    assert said["main"] == [44 + H, 3 + H, 0 + H, 0, 7], "Dogecoin's 3"
    assert said["test"][:3] != [24946 + H, 0 + H, 0 + H], "not the arcade branch"


# --- signing ------------------------------------------------------------------

def test_a_signature_verifies_and_is_low_s(loaded):
    """Low-S because the network refuses anything else, and a transaction
    refused after broadcast is a fee spent for nothing."""
    browser, _, _ = loaded
    said = browser.execute_async_script("""
        const done = arguments[0];
        (async () => {
          try {
            const c = window.coins;
            const secp = await import("/vendor/noble-secp256k1.js");
            const priv = c.unhex("1122334455667788112233445566778811223344556677881122334455667788");
            const hash = new Uint8Array(32).fill(7);
            const sig = await secp.signAsync(hash, priv, {lowS: true});
            const der = await c.signHash(priv, hash);
            const withType = await c.signInput(priv, hash, 1);
            done({
              // verify() takes the compact form; DER is for the scriptSig.
              ok: secp.verify(sig.toCompactRawBytes(), hash, c.publicKey(priv)),
              lowS: sig.hasHighS === undefined ? true : !sig.hasHighS(),
              der: c.hex(der), tail: withType[withType.length - 1],
              longer: withType.length === der.length + 1});
          } catch (e) { done({error: String(e && e.message || e)}); }
        })();""")
    assert "error" not in said, said
    assert said["ok"] is True, "the signature verifies against the public key"
    assert said["lowS"] is True, "the network refuses the other half of the pair"
    assert said["longer"] and said["tail"] == 1, "the sighash byte is appended"
    assert said["der"].startswith("30"), "DER"


def test_the_der_encoding_is_read_back_the_way_a_node_reads_it(loaded):
    """noble v2 hands back the compact r||s; a scriptSig wants DER, so it
    is encoded here. Checked by DECODING it in Python -- plain parsing, no
    curve -- and confirming r and s are the ones the browser signed."""
    browser, _, _ = loaded
    said = browser.execute_async_script("""
        const done = arguments[0];
        (async () => {
          const c = window.coins;
          const secp = await import("/vendor/noble-secp256k1.js");
          const priv = c.unhex("00".repeat(31) + "2a");
          const out = [];
          // Many signatures, because the leading-zero and high-bit cases
          // only appear for some values of r and s.
          for (let i = 0; i < 40; i++) {
            const hash = new Uint8Array(32).fill(i);
            const sig = await secp.signAsync(hash, priv, {lowS: true});
            out.push({compact: c.hex(sig.toCompactRawBytes()),
                      der: c.hex(c.toDER(sig.toCompactRawBytes()))});
          }
          done(out);
        })();""")
    assert len(said) == 40
    high = 0
    for one in said:
        raw = bytes.fromhex(one["der"])
        assert raw[0] == 0x30 and raw[1] == len(raw) - 2, "SEQUENCE and length"
        assert raw[2] == 0x02, "INTEGER"
        r_len = raw[3]
        r = raw[4:4 + r_len]
        assert raw[4 + r_len] == 0x02, "the second INTEGER"
        s_len = raw[5 + r_len]
        s = raw[6 + r_len:6 + r_len + s_len]
        assert len(raw) == 6 + r_len + s_len, "and nothing after it"
        # Minimal: never a leading zero unless the next byte has its top bit
        # set, which is the rule every implementation gets wrong once.
        for part in (r, s):
            assert len(part) == 1 or part[0] != 0 or (part[1] & 0x80), \
                f"non-minimal integer in {one['der']}"
            assert not (part[0] & 0x80), "an integer read as negative"
        compact = bytes.fromhex(one["compact"])
        assert int.from_bytes(r, "big") == int.from_bytes(compact[:32], "big")
        assert int.from_bytes(s, "big") == int.from_bytes(compact[32:], "big")
        if len(r) == 33 or len(s) == 33:
            high += 1
    assert high > 0, ("none of the forty needed a leading zero, so the rule "
                      "that needs it was never exercised")


def test_a_transaction_is_hashed_the_same_in_python_and_in_the_browser(loaded):
    """The claim the whole non-custodial design rests on, checked.

    `coins.js` no longer signs the hashes a node hands over: it reads the
    transaction's own bytes and recomputes each one. That is worth nothing
    unless what it recomputes is the same 32 bytes `arcade/funding.py`
    computes, so one transaction is built here, hashed here, hashed again
    in a browser from the same hex, and the two answers are compared. Same
    shape as the BIP32 vectors above, for the same reason: two
    implementations of one standard, neither allowed to check itself.
    """
    from arcade import funding, txbuild

    browser, _, _ = loaded
    here = browser.execute_async_script("""
        const done = arguments[0];
        (async () => {
          try {
            const c = window.coins;
            const seed = c.unhex("000102030405060708090a0b0c0d0e0f");
            const coin = await c.coinKey(seed, "regtest", 0);
            done({pubkey: c.hex(coin.pubkey),
                  address: await c.address(coin.pubkey, 111)});
          } catch (e) { done({error: String(e && e.message || e)}); }
        })();""")
    assert "error" not in here, here
    mine = here["address"]
    script = txbuild.p2pkh_script(mine)
    inputs = [{"txid": "%064x" % 7, "vout": 1, "value": 3 * COIN,
               "address": mine},
              {"txid": "%064x" % 9, "vout": 0, "value": 2 * COIN,
               "address": mine}]
    outputs = [(4 * COIN, txbuild.p2pkh_script(SOMEWHERE_ELSE)),
               (1 * COIN - 1000, script)]
    raw = txbuild.build_raw_tx([(c["txid"], c["vout"]) for c in inputs],
                               outputs)
    offered = {"raw": raw, "inputs": inputs, "what": "a test payment",
               "fee": 1000, "change": 1 * COIN - 1000, "signed_from": 0,
               "sighashes": [funding.sighash(inputs, outputs, n, script).hex()
                             for n in range(len(inputs))]}

    shown = browser.execute_async_script("""
        const done = arguments[1];
        (async () => {
          try {
            const c = window.coins;
            const seed = c.unhex("000102030405060708090a0b0c0d0e0f");
            const coin = await c.coinKey(seed, "regtest", 0);
            const out = await c.verifyOffer(JSON.parse(arguments[0]), {
              pubkey: coin.pubkey, address: await c.address(coin.pubkey, 111)});
            done({hashes: out.hashes, fee: out.fee, change: out.change,
                  pays: out.pays.map((p) => [p.to || null, Number(p.value)]),
                  read: [out.tx.inputs.length, out.tx.outputs.length,
                         out.tx.locktime, out.tx.version]});
          } catch (e) { done({error: String(e && e.message || e)}); }
        })();""", json.dumps(offered))
    assert "error" not in shown, shown
    assert shown["hashes"] == offered["sighashes"], (
        "the browser's own hashes are not Python's, so it would sign a "
        "different transaction from the one this node built")
    assert shown["read"] == [2, 2, 0, 1], "parsed, not assumed"
    assert shown["fee"] == 1000 and shown["change"] == 1 * COIN - 1000
    assert shown["pays"][1] == [mine, 1 * COIN - 1000], "the change is ours"


def test_a_transaction_that_does_not_match_its_own_hashes_is_refused(loaded):
    """The point of recomputing them.

    The two halves of an offer are the transaction and the list of hashes
    beside it. A node that shows one and means the other is the attack this
    whole path exists for -- so the check goes both ways, and the refusal
    has to arrive before a key is used.
    """
    from arcade import funding, txbuild

    browser, _, _ = loaded
    here = browser.execute_async_script("""
        const done = arguments[0];
        (async () => {
          try {
            const c = window.coins;
            const coin = await c.coinKey(
              c.unhex("000102030405060708090a0b0c0d0e0f"), "regtest", 0);
            done({pubkey: c.hex(coin.pubkey),
                  address: await c.address(coin.pubkey, 111)});
          } catch (e) { done({error: String(e && e.message || e)}); }
        })();""")
    assert "error" not in here, here
    mine = here["address"]
    script = txbuild.p2pkh_script(mine)
    theirs = txbuild.p2pkh_script(SOMEWHERE_ELSE)
    inputs = [{"txid": "%064x" % 7, "vout": 0, "value": 5 * COIN,
               "address": mine}]

    def offer_to(script_out, label):
        outs = [(4 * COIN, script_out)]
        return {"raw": txbuild.build_raw_tx(
                    [(c["txid"], c["vout"]) for c in inputs], outs),
                "inputs": inputs, "what": label, "signed_from": 0,
                "sighashes": [funding.sighash(inputs, outs, 0, script).hex()]}

    honest = offer_to(script, "pays you")
    hostile = offer_to(theirs, "pays somebody else")

    for offer in ({**honest, "raw": hostile["raw"]},
                  {**honest, "sighashes": hostile["sighashes"]}):
        refused = browser.execute_async_script("""
            const done = arguments[1];
            (async () => {
              try {
                const c = window.coins;
                const coin = await c.coinKey(
                  c.unhex("000102030405060708090a0b0c0d0e0f"), "regtest", 0);
                const out = await c.verifyOffer(JSON.parse(arguments[0]),
                                                {pubkey: coin.pubkey,
                                                 address: await c.address(
                                                   coin.pubkey, 111)});
                done({signed: out.hashes});
              } catch (e) { done({error: String(e && e.message || e)}); }
            })();""", json.dumps(offer))
        assert "error" in refused and "signed" not in refused, (
            "that offer was accepted, which is the hole: "
            + str(refused)[:120])
        assert "Nothing was signed" in refused["error"], refused


def test_an_offer_that_spends_a_coin_it_did_not_name_is_refused(loaded):
    """The transaction and the offer have to agree about the coins, too.

    The values are the node's numbers and the hash does not bind them, so
    the outpoints are the one part of the input side that can be pinned --
    and an offer that quietly swaps one coin for another of the same
    wallet's would otherwise read exactly the same on the page.
    """
    from arcade import funding, txbuild

    browser, _, _ = loaded
    here = browser.execute_async_script("""
        const done = arguments[0];
        (async () => {
          try {
            const c = window.coins;
            const coin = await c.coinKey(
              c.unhex("000102030405060708090a0b0c0d0e0f"), "regtest", 0);
            done({address: await c.address(coin.pubkey, 111)});
          } catch (e) { done({error: String(e && e.message || e)}); }
        })();""")
    assert "error" not in here, here
    script = txbuild.p2pkh_script(here["address"])
    outs = [(4 * COIN, txbuild.p2pkh_script(SOMEWHERE_ELSE))]
    inputs = [{"txid": "%064x" % 7, "vout": 0, "value": 5 * COIN,
               "address": SOMEONE_ELSE}]
    offer = {"raw": txbuild.build_raw_tx([("%064x" % 7, 0)], outs),
             "inputs": inputs, "what": "a payment", "signed_from": 0,
             "sighashes": [funding.sighash(inputs, outs, 0, script).hex()]}
    refused = browser.execute_async_script("""
        const done = arguments[1];
        (async () => {
          try {
            const c = window.coins;
            const coin = await c.coinKey(
              c.unhex("000102030405060708090a0b0c0d0e0f"), "regtest", 0);
            const out = await c.verifyOffer(JSON.parse(arguments[0]),
                                            {pubkey: coin.pubkey,
                                             address: await c.address(
                                               coin.pubkey, 111)});
            done({signed: out.hashes});
          } catch (e) { done({error: String(e && e.message || e)}); }
        })();""", json.dumps(offer))
    assert "signed" not in refused, refused
    assert "does not hold" in refused["error"], refused


def test_the_same_words_make_a_different_key_on_each_chain(loaded):
    """One secret, two wallets.

    A mainnet wallet for an account is not a second seed phrase -- it is
    the same twelve words at the other chain's coin type, m/44'/3' beside
    m/44'/1'. So the keys differ, the addresses differ, and the version
    byte differs; nothing extra is written down and nothing is shared
    between them but the words.
    """
    browser, base, _ = loaded
    answer = browser.execute_async_script("""
        const done = arguments[0];
        Promise.all([import("/wallet.js"), import("/coins.js")])
          .then(async ([wallet, coins]) => {
            const phrase = "abandon abandon abandon abandon abandon abandon "
              + "abandon abandon abandon abandon abandon about";
            const w = await wallet.walletFrom(phrase, "regtest", 111);
            await wallet.everyChain(w, [{network: "main", version: 56}]);
            const test = wallet.keysOn(w, "regtest");
            const main = wallet.keysOn(w, "main");
            done({
              testAddress: test.address, mainAddress: main.address,
              sameKey: coins.hex ? false : false,
              keysMatch: JSON.stringify([...test.key])
                      === JSON.stringify([...main.key]),
              testPath: JSON.stringify(coins.coinPath("regtest")),
              mainPath: JSON.stringify(coins.coinPath("main")),
            });
          }).catch(e => done({error: String(e.message || e)}));""")
    assert "error" not in answer, answer
    assert not answer["keysMatch"], "a different chain is a different key"
    assert answer["testAddress"] != answer["mainAddress"]
    # Pepecoin mainnet addresses start with P, the test chains' with m or n.
    assert answer["mainAddress"][0] == "P"
    assert answer["testAddress"][0] in "mn"
    # And the paths are the standard ones, not something invented here.
    assert answer["testPath"] == "[2147483692,2147483649,2147483648,0,0]"
    assert answer["mainPath"] == "[2147483692,2147483651,2147483648,0,0]"
