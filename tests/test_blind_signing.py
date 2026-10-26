"""The wallet builds every message it signs, and pins its magic (2026-10-26, issue #5, blind signing).

Arcade Hardening round 2, wave 3. SadFrog approved "A. build every signed text in the browser from fixed templates;
check node/seller ids equal this arcade's own; pin the signing magic per chain. B. as a fallback, show the exact text
and require a click for anything unusual." Before, the page signed whatever /realtime/hello, /account/llm/grant and
/account/llm/topup said (the realtime certificate, LLM grants and top-ups), with the magic they said. Now coins.js
builds three shapes of words field by field and signs nothing else (`signWords`), wallet.js fills them from what the
page can check, and a node id other than the one this browser first saw here is shown word for word and signed only
on a click. The server checks the same words rebuilt from its own data. Run in Node against the real wallet.js and
coins.js.
"""

import pathlib
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_htlc import Key                                                # noqa: E402
from test_realtime_web import two_nodes                                  # noqa: E402,F401 -- a fixture
from test_trade_tab import run_wallet_js                                 # noqa: E402

from arcade import llm                                                   # noqa: E402
from arcade.config import NETWORKS                                       # noqa: E402
from arcade.mesh.service import MESSAGE_MAGIC, MeshService, cert_message  # noqa: E402

HERE_NODE, OTHER_NODE = "a1" * 32, "b2" * 32
TXID = "c3" * 32
DOGECOIN_MAGIC = "Dogecoin Signed Message:\n"

#: `key` on `test`; localStorage and arcadeAsk as a browser has them (`data.yes` answers the ask, which is recorded).
HARNESS = """
  const store = new Map();
  globalThis.localStorage = {getItem: (k) => store.has(k) ? store.get(k) : null, setItem: (k, v) => store.set(k, String(v))};
  const asked = [];
  globalThis.arcadeAsk = async (title, why, yes, no, words) => { asked.push({title, why, words}); return data.yes; };
  const mine = {key: c.unhex(data.priv), address: data.address};
  const now = Math.floor(Date.now() / 1000);
  const tryIt = async (fn) => { try { return {ok: await fn()}; } catch (e) { return {error: String(e.message || e)}; } };
"""


def _key():
    key = Key(NETWORKS["test"].pubkeyhash_version)
    return key, {"priv": key.secret.to_bytes(32, "big").hex(), "address": key.address, "yes": False}


def _hello(node=HERE_NODE, network="arcade-test", address="", expires=None):
    expires = expires or MeshService.expiry()
    out = {"online": True, "node": node, "network": network,
           "cert": {"network": network, "node": node, "expires": expires}}
    if address:
        out["sign"] = {"message": cert_message(network, node, address, expires), "expires": expires,
                       "magic": DOGECOIN_MAGIC, "chain": {"network": "test"}}
    return out


def test_the_words_built_in_the_browser_are_the_words_the_server_checks(tmp_path):
    key, data = _key()
    exp = int(time.time()) + 3600
    grant_exp = int(time.time()) + 30 * 86400
    data.update(exp=exp, grant_exp=grant_exp, here=HERE_NODE, other=OTHER_NODE, txid=TXID)
    got = run_wallet_js(tmp_path, HARNESS + """
      const rt = c.realtimeWords("arcade-test", data.here, data.address, data.exp);
      const grant = c.grantWords(data.other, data.address, data.grant_exp);
      const topup = c.topupWords("test", data.txid, data.other, data.address);
      const bare = c.topupWords("test", data.txid, "", data.address);
      return {rt, grant, topup, bare, sigs: [await c.signWords(mine, rt), await c.signWords(mine, grant),
                                             await c.signWords(mine, topup)],
              exported: typeof c.signMessage, magic: c.ARCADE_MAGIC};
    """, data)
    assert got["rt"] == cert_message("arcade-test", HERE_NODE, key.address, exp)
    assert got["grant"] == llm.grant_words(OTHER_NODE, key.address, grant_exp)
    assert got["topup"] == llm.topup_words("test", TXID, OTHER_NODE, key.address)
    assert got["bare"] == llm.topup_words("test", TXID, "", key.address), "an arcade with no mesh sells under no id"
    for words, sig in zip((got["rt"], got["grant"], got["topup"]), got["sigs"]):
        assert llm.signed_by(sig, words, key.address), "signed with the magic the server checks"
    assert got["magic"] == MESSAGE_MAGIC == llm.MAGIC
    assert got["exported"] == "undefined", "no page can sign words of its own choosing with a magic of its own"


def test_the_magic_can_not_be_changed_and_only_the_three_shapes_are_signed(tmp_path):
    key, data = _key()
    data.update(here=HERE_NODE, magic=DOGECOIN_MAGIC, other_address=Key().address)
    got = run_wallet_js(tmp_path, HARNESS + """
      const rt = c.realtimeWords("arcade-test", data.here, data.address, now + 3600);
      return {
        extra: await c.signWords(mine, rt, undefined, data.magic),
        plain: await c.signWords(mine, rt),
        rt,
        refused: await Promise.all([
          "I promise to pay 1,000 DOGE",
          rt + "\\nand one more line",
          c.realtimeWords("arcade-test", data.here, data.other_address, now + 3600),
          "arcade-realtime\\narcade-test\\n" + data.here + "\\n" + data.address + "\\n" + (now + 30 * 86400),
          "arcade-llm\\nhere\\n" + data.address + "\\n" + (now + 3600),
          "arcade-llm-topup\\ntest\\nnot-a-txid\\n\\n" + data.address,
        ].map((w) => tryIt(() => c.signWords(mine, w)))),
      };
    """, data)
    assert got["extra"] == got["plain"], "a magic passed in is not used"
    assert llm.signed_by(got["plain"], got["rt"], key.address)
    assert llm.recover_hash160(got["plain"], got["rt"], DOGECOIN_MAGIC) != llm.address_hash(key.address)
    for r in got["refused"]:
        assert r.get("error", "").startswith("Nothing was signed"), r


def test_a_certificate_naming_another_arcade_is_refused(tmp_path):
    key, data = _key()
    own = _hello(address=key.address)
    lying = _hello(address=key.address)
    lying["sign"]["message"] = cert_message("arcade-test", OTHER_NODE, key.address, own["sign"]["expires"])
    mixed = _hello(address=key.address)
    mixed["cert"]["node"] = OTHER_NODE
    odd_net = _hello(network="arcade-test\nx", address=key.address)
    data.update(own=own, lying=lying, mixed=mixed, odd=odd_net, there=_hello(OTHER_NODE))
    got = run_wallet_js(tmp_path, HARNESS + """
      const lying = await tryIt(() => w.realtimeSign(mine, data.lying));
      const mixed = await tryIt(() => w.realtimeSign(mine, data.mixed));
      const odd = await tryIt(() => w.realtimeSign(mine, data.odd));
      const first = await w.realtimeSign(mine, data.own);
      const there = await w.realtimeSign(mine, data.there, {there: true});
      return {lying, mixed, odd, first, there, kept: localStorage.getItem("arcade.node.self"), asked};
    """, data)
    for r in (got["lying"], got["mixed"], got["odd"]):
        assert r.get("error", "").startswith("Nothing was signed"), r
    assert got["first"]["words"] == own["sign"]["message"]
    assert llm.signed_by(got["first"]["sig"], own["sign"]["message"], key.address)
    assert got["kept"] == HERE_NODE, "the first node this browser sees here is kept"
    assert got["there"]["words"] == cert_message("arcade-test", OTHER_NODE, key.address, data["there"]["cert"]["expires"])
    assert got["asked"] == [], "another node's certificate, fetched from it, is not this arcade's"


@pytest.mark.parametrize("yes", [False, True])
def test_this_arcade_calling_itself_another_node_is_shown_word_for_word(tmp_path, yes):
    """The fallback: a node id other than the one kept here is anything but usual. The exact words, a click."""
    key, data = _key()
    data.update(yes=yes, first=_hello(address=key.address), moved=_hello(OTHER_NODE, address=key.address))
    got = run_wallet_js(tmp_path, HARNESS + """
      await w.realtimeSign(mine, data.first);
      const moved = await tryIt(() => w.realtimeSign(mine, data.moved));
      delete globalThis.arcadeAsk;
      store.set("arcade.node.self", data.first.node);
      const nobody = await tryIt(() => w.realtimeSign(mine, data.moved));
      return {moved, nobody, asked};
    """, data)
    assert len(got["asked"]) == 1
    assert got["asked"][0]["words"] == data["moved"]["sign"]["message"], "shown exactly"
    if yes:
        assert llm.signed_by(got["moved"]["ok"]["sig"], data["moved"]["sign"]["message"], key.address)
    else:
        assert got["moved"]["error"] == "Nothing was signed: you said no."
    assert got["nobody"]["error"] == "Nothing was signed: you said no.", "no way to ask: nothing signed"


def test_llm_grants_and_top_ups_are_built_from_what_the_page_knows(tmp_path):
    key, data = _key()
    here_words = llm.topup_words("test", TXID, HERE_NODE, key.address)
    data.update(here=HERE_NODE, other=OTHER_NODE, txid=TXID,
                said={"message": here_words, "magic": DOGECOIN_MAGIC},
                thief={"message": llm.topup_words("test", TXID, HERE_NODE, Key().address)},
                other_tx={"message": llm.topup_words("test", "d4" * 32, HERE_NODE, key.address)},
                anything={"message": "send everything to the operator"},
                elsewhere={"message": llm.topup_words("test", TXID, OTHER_NODE, key.address)})
    got = run_wallet_js(tmp_path, HARNESS + """
      store.set("arcade.node.self", data.here);
      const g = await w.llmGrantSign(mine, data.other);
      const grantHere = await tryIt(() => w.llmGrantSign(mine, data.here));
      const grantBad = await tryIt(() => w.llmGrantSign(mine, "here"));
      const pay = {seller: "here", here: true, network: "test", txid: data.txid};
      const topHere = await w.llmTopupSign(mine, pay, data.said);
      const topThere = await w.llmTopupSign(mine, {seller: data.other, network: "test", txid: data.txid});
      const refused = await Promise.all([data.thief, data.other_tx, data.anything, null]
        .map((said) => tryIt(() => w.llmTopupSign(mine, pay, said))));
      const elsewhere = await tryIt(() => w.llmTopupSign(mine, pay, data.elsewhere));
      return {g, grantHere, grantBad, topHere, topThere, refused, elsewhere, asked};
    """, data)
    g = got["g"]
    assert abs(g["expires"] - (time.time() + 30 * 86400)) < 120, "the expiry is the page's own"
    assert llm.signed_by(g["sig"], llm.grant_words(OTHER_NODE, key.address, g["expires"]), key.address)
    from arcade.mesh.llmshare import grant_ok
    assert grant_ok({"address": key.address, "expires": g["expires"], "sig": g["sig"]}, OTHER_NODE) == key.address
    assert got["grantHere"]["error"].startswith("Nothing was signed"), "this arcade needs no grant"
    assert got["grantBad"]["error"].startswith("Nothing was signed")
    assert llm.signed_by(got["topHere"], here_words, key.address)
    assert llm.signed_by(got["topThere"], llm.topup_words("test", TXID, OTHER_NODE, key.address), key.address)
    for r in got["refused"]:
        assert r.get("error", "").startswith("Nothing was signed"), r
    # This arcade's words naming another seller than the node kept here: asked about, with the exact words.
    assert got["elsewhere"]["error"] == "Nothing was signed: you said no."
    assert [a["words"] for a in got["asked"]] == [data["elsewhere"]["message"]]


def test_the_seller_checks_a_top_up_rebuilt_from_its_own_data(tmp_path):
    """What the browser signs for a top-up is what Seller.topup rebuilds: its own node id, the buyer, the txid."""
    key, data = _key()
    data.update(here=HERE_NODE, txid=TXID)
    got = run_wallet_js(tmp_path, HARNESS + """
      return w.llmTopupSign(mine, {seller: data.here, network: "test", txid: data.txid});
    """, data)
    assert llm.recover_hash160(got, llm.topup_words("test", TXID, HERE_NODE, key.address)) == llm.address_hash(key.address)
    assert llm.recover_hash160(got, llm.topup_words("test", TXID, OTHER_NODE, key.address)) != llm.address_hash(key.address)


def test_the_hello_sends_no_magic(two_nodes):                            # noqa: F811
    (ca, sa), _ = two_nodes
    hello = ca.get("/realtime/hello", headers={"Origin": "https://app.example", "CF-Connecting-IP": "203.0.113.7"}).json()
    assert "magic" not in hello["cert"]
    assert hello["cert"]["node"] == hello["node"] == sa.mesh.node_id and hello["cert"]["network"] == hello["network"]


def test_a_program_s_certificate_uses_its_own_magic_and_shape(monkeypatch):
    """arcade.service.CertRoom (a program joining through a node's public name) signs the same one shape."""
    from arcade import service as S
    key = Key(NETWORKS["test"].pubkeyhash_version)
    expires = MeshService.expiry()
    posted = []

    class Answer:
        def __init__(self, body):
            self.body = body

        def json(self):
            return self.body

    def room(hello):
        r = S.CertRoom(key.secret, key.address, "g1", "town", node="https://arcade.example")
        monkeypatch.setattr(r.http, "get", lambda *a, **k: Answer(hello))
        r._post = lambda path, body: (posted.append(body), {"token": "t", "me": {"address": key.address}})[1]
        return r

    good = {"online": True, "cert": {"network": "arcade-test", "node": HERE_NODE, "expires": expires,
                                     "magic": DOGECOIN_MAGIC}}
    room(good).join()
    words = cert_message("arcade-test", HERE_NODE, key.address, expires)
    assert llm.signed_by(posted[-1]["sig"], words, key.address), "the pinned magic, not the node's"
    for cert in ({**good["cert"], "network": "arcade-test\nI owe"}, {**good["cert"], "node": "here"},
                 {**good["cert"], "expires": expires + 10 * 86400}):
        with pytest.raises(S.ServiceError):
            room({"online": True, "cert": cert}).join()
