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
