"""The wallet's Schnorr signing (coins.js, 2026-11-04), in a real browser: BIP-340's own vector, and a DigiDollar
redeem offer checked and signed as the console's wallet signs it. What the browser works out is held to the arcade's
own digests (arcade/taproot.py), and its signatures to BIP-340 verification; a real DigiByte Core takes the same
signatures in test_digidollar_regtest.py."""
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("selenium", reason="browser tests need selenium: pip install .[dev]")
sys.path.insert(0, str(Path(__file__).parent))
from test_coins_browser import browser, loaded, served  # noqa: E402,F401
from test_module_host import Chain, redeem_offer  # noqa: E402
from test_module_sandbox import _module  # noqa: E402
from test_taproot import tweaked_secret  # noqa: E402

from arcade import modhost, packages, taproot  # noqa: E402
from arcade.config import NETWORKS  # noqa: E402

RUN = """
const done = arguments[arguments.length - 1];
const c = window.coins;
(async () => { %s })().then(done, (e) => done({error: String(e && e.message || e)}));
"""


def _run(driver, body: str, *args):
    out = driver.execute_async_script(RUN % body, *args)
    assert not (isinstance(out, dict) and "error" in out), out
    return out


def test_the_wallet_signs_bip340s_first_vector(loaded):
    driver, _, _ = loaded
    sig = _run(driver, """
        const key = new Uint8Array(32); key[31] = 3;
        return c.hex(await c.signSchnorr(key, new Uint8Array(32), false, new Uint8Array(32)));""")
    assert sig.upper() == ("E907831F80848D1069A5371B402410364BDF1C5F8307B0084C55F1CE2DCA8215"
                           "25F66A4A85EA8B71E482A74F382D2CE5EBEEE8FDB2172F477DF4900D310536C0")


def test_the_wallet_checks_a_redeem_and_signs_its_vault_and_digidollars_with_schnorr(loaded, tmp_path):
    driver, _, _ = loaded
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text("{}")
    pid, row = packages.install(home, _module(tmp_path))
    chain = Chain()
    host = modhost.Host(home, chain)
    pub, secret, _, unsigned = redeem_offer(host, {"packages": {pid: row}}, chain)
    offer = {**unsigned.as_json(), "chain": "main"}
    keys = {"key": secret.to_bytes(32, "big").hex(), "pubkey": pub.hex(), "address": chain.address,
            "version": NETWORKS["main"].pubkeyhash_version, "network": "main"}
    said = _run(driver, """
        const offer = arguments[0], k = arguments[1];
        const keys = {...k, key: c.unhex(k.key), pubkey: c.unhex(k.pubkey)};
        const shown = await c.verifyOffer(offer, keys);
        const sigs = [];
        for (const [n, h] of shown.hashes.entries()) {
          sigs.push(shown.signers[n] ? c.hex(await c.signSchnorr(keys.key, c.unhex(h), shown.signers[n] === "key")) : null);
        }
        return {hashes: shown.hashes, signers: shown.signers, sigs, change: shown.change};""", offer, keys)
    assert said["hashes"] == unsigned.sighashes, "the browser's digests are the arcade's"
    assert said["signers"] == ["leaf", "key", None]
    assert taproot.verify(pub[1:], bytes.fromhex(unsigned.sighashes[0]), bytes.fromhex(said["sigs"][0]))
    output_key = chain.outs["cc" * 32, 1][1][2:]
    assert taproot.verify(output_key, bytes.fromhex(unsigned.sighashes[1]), bytes.fromhex(said["sigs"][1]))
    assert tweaked_secret(secret) and said["change"] == sum(v for v, _ in unsigned.outputs)

    # Another key's wallet refuses it, as it refuses any coin it does not hold.
    from test_taproot import keypair
    other, other_secret = keypair()
    refused = driver.execute_async_script(RUN % """
        const offer = arguments[0], k = arguments[1];
        try { await c.verifyOffer(offer, {...k, key: c.unhex(k.key), pubkey: c.unhex(k.pubkey)}); return "signed"; }
        catch (e) { return String(e.message); }""", offer, {**keys, "pubkey": other.hex(),
                                                            "key": other_secret.to_bytes(32, "big").hex()})
    assert "Nothing was signed" in refused
    # A digest the node made over other bytes is refused too.
    bent = json.loads(json.dumps(offer))
    bent["sighashes"][1] = "00" * 32
    refused = driver.execute_async_script(RUN % """
        const offer = arguments[0], k = arguments[1];
        try { await c.verifyOffer(offer, {...k, key: c.unhex(k.key), pubkey: c.unhex(k.pubkey)}); return "signed"; }
        catch (e) { return String(e.message); }""", bent, keys)
    assert "different bytes" in refused


def test_the_wallet_says_a_digidollar_send_as_dollars_to_their_address_and_signs_by_key_path(loaded, tmp_path):
    """2026-11-05, send and receive: on DigiByte, a zero-value Taproot output is said as the dollars the transaction's
    "DD" record gives it, to its DigiDollar address (TD... on the test chain), and the signer's own DigiDollar change
    as theirs; the DigiDollar coin is signed by the tweaked key."""
    import importlib
    from test_taproot import keypair
    from arcade.script import b58check_encode, hash160
    from test_module_host import p2pkh_script
    driver, _, _ = loaded
    pilot_dir = str(Path(__file__).parent.parent / "examples" / "packages" / "digidollar")
    sys.path.insert(0, pilot_dir)
    try:
        tr, pilot = importlib.import_module("lib.taproot"), importlib.import_module("module")
    finally:
        sys.path.remove(pilot_dir)
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text("{}")
    pid, row = packages.install(home, _module(tmp_path))
    chain = Chain()
    host = modhost.Host(home, chain)
    found = host.installed
    host.installed = lambda s: {k: (v[0], {**v[1], "needs": {**v[1]["needs"], "max_fee": 50_000_000}})
                                for k, v in found(s).items()}
    pub, secret = keypair()
    payee, _ = keypair()
    chain.address = b58check_encode(NETWORKS["main"].pubkeyhash_version, hash160(pub))
    chain.outs = {("dd" * 32, 1): (0, tr.p2tr(tr.tweak(pub[1:])))}
    to_key = tr.tweak(payee[1:])
    to_address = pilot.dd_address("dgbtest", payee[1:])
    prepared = {**pilot.send(pub[1:], [{"txid": "dd" * 32, "vout": 1, "cents": 10000}],
                             [{"txid": "aa" * 32, "vout": 0, "value": 150_000_000}], p2pkh_script(chain.address),
                             to_key, 4000, "main", to_address), "chain": "main"}
    _, unsigned = host.prepare({"packages": {pid: row}}, "hello", prepared, chain.address, pub)
    offer = {**unsigned.as_json(), "chain": "dgbtest"}
    keys = {"key": secret.to_bytes(32, "big").hex(), "pubkey": pub.hex(), "address": chain.address,
            "version": NETWORKS["main"].pubkeyhash_version, "network": "dgbtest"}
    said = _run(driver, """
        const offer = arguments[0], k = arguments[1];
        const keys = {...k, key: c.unhex(k.key), pubkey: c.unhex(k.pubkey)};
        const shown = await c.verifyOffer(offer, keys);
        const sig = c.hex(await c.signSchnorr(keys.key, c.unhex(shown.hashes[0]), true));
        return {pays: shown.pays, signers: shown.signers, sig, own: await c.ddAddress("dgbtest", c.unhex(arguments[2]))};""",
                offer, keys, tr.tweak(pub[1:]).hex())
    dd = [p for p in said["pays"] if "dd" in p]
    assert dd == [{"to": to_address, "dd": 4000, "value": 0, "mine": False},
                  {"to": pilot.dd_address("dgbtest", pub[1:]), "dd": 6000, "value": 0, "mine": True}]
    assert said["own"] == pilot.dd_address("dgbtest", pub[1:]), "the browser's address is the module's"
    assert said["signers"] == ["key", None]
    assert taproot.verify(tr.tweak(pub[1:]), bytes.fromhex(unsigned.sighashes[0]), bytes.fromhex(said["sig"]))


def test_the_wallet_reads_a_mints_record_whose_last_item_is_its_owners_key(loaded, tmp_path):
    """2026-11-05, on the test node: a mint was refused with "a DigiDollar record's number is too long". A mint's record
    ends in the owner's 32-byte key, and the wallet read every item as a number. It reads the dollars, says the
    DigiDollar output as the signer's own, and the vault as a payment."""
    import importlib
    from test_taproot import keypair
    from test_module_host import p2pkh_script
    from arcade.script import b58check_encode, hash160
    driver, _, _ = loaded
    pilot_dir = str(Path(__file__).parent.parent / "examples" / "packages" / "digidollar")
    sys.path.insert(0, pilot_dir)
    try:
        importlib.import_module("lib.taproot")
        pilot = importlib.import_module("module")
    finally:
        sys.path.remove(pilot_dir)
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text("{}")
    pid, row = packages.install(home, _module(tmp_path))
    chain = Chain()
    host = modhost.Host(home, chain)
    found = host.installed
    host.installed = lambda s: {k: (v[0], {**v[1], "needs": {**v[1]["needs"], "max_fee": 50_000_000}})
                                for k, v in found(s).items()}
    pub, secret = keypair()
    chain.address = b58check_encode(NETWORKS["main"].pubkeyhash_version, hash160(pub))
    prepared = {**pilot.mint(pub[1:], 10000, 0, 598_633, 0, 10 ** 8, [{"txid": "aa" * 32, "vout": 0,
                                                                       "value": 150_000_000}],
                             p2pkh_script(chain.address)), "chain": "main"}
    _, unsigned = host.prepare({"packages": {pid: row}}, "hello", prepared, chain.address, pub)
    offer = {**unsigned.as_json(), "chain": "dgbtest"}
    keys = {"key": secret.to_bytes(32, "big").hex(), "pubkey": pub.hex(), "address": chain.address,
            "version": NETWORKS["main"].pubkeyhash_version, "network": "dgbtest"}
    pays = _run(driver, """
        const offer = arguments[0], k = arguments[1];
        return (await c.verifyOffer(offer, {...k, key: c.unhex(k.key), pubkey: c.unhex(k.pubkey)})).pays;""",
                offer, keys)
    vault, dd, record = pays[0], pays[1], pays[2]
    assert vault["value"] == 10 ** 8 and "Taproot output" in vault["to"] and not vault["mine"]
    assert dd == {"to": pilot.dd_address("dgbtest", pub[1:]), "dd": 10000, "value": 0, "mine": True}
    assert record["bytes"] > 32 and record["value"] == 0
