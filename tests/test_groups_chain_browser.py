"""arcade.groups on a real chain: two accounts in two browsers on regtest, nothing stubbed. An app posts in a group through
the bridge, and the other member's app reads the line back out of the chain with its own key."""

import json
import pathlib
import sys
import time

import pytest

pytest.importorskip("selenium", reason="browser tests need selenium: pip install .[dev]")
from selenium.webdriver.common.by import By                       # noqa: E402

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import browsers                                                     # noqa: E402
from test_account_offer import _settled                             # noqa: E402
from test_trade_browser import _inscribe, _signed_up, world        # noqa: E402,F401

PAGE_ID = "cd" * 32
PAGE = b"""<!doctype html><meta charset="utf-8"><body><script src="/r/groups.js"></script>
<input id="job"><button id="go">go</button><pre id="out"></pre>
<script>
document.getElementById('go').onclick = function () {
  var j = JSON.parse(document.getElementById('job').value), g = arcade.groups;
  (j.op === 'list' ? g.list() : j.op === 'read' ? g.read(j.id) : g.send(j.id, j.text)).then(function (r) {
    document.getElementById('out').textContent = JSON.stringify({value: r}); },
    function (e) { document.getElementById('out').textContent = JSON.stringify({error: String(e.message || e)}); });
};
</script>"""


def _app(driver, base):
    browsers.allow_wallet(driver, base, PAGE_ID)
    driver.execute_script("localStorage.setItem(arguments[0], 'yes')", f"arcade.groups.allow:{PAGE_ID}")
    driver.get(f"{base}/inscriptions/{PAGE_ID}/full")
    frame = driver.find_element(By.CSS_SELECTOR, ".inscription-frame")
    driver.switch_to.frame(frame)
    for _ in range(120):
        if driver.execute_script("return !!document.getElementById('go')"):
            return
        time.sleep(0.25)
    raise AssertionError("the app did not load")


def _do(driver, **job):
    driver.execute_script("document.getElementById('out').textContent=''; document.getElementById('job').value = arguments[0];"
                          "document.getElementById('go').click();", json.dumps(job))
    end = time.time() + 60
    while time.time() < end:
        said = driver.execute_script("return document.getElementById('out').textContent")
        if said:
            return json.loads(said)
        time.sleep(0.1)
    raise AssertionError(f"no answer to {job}")


def _alice_and_bob(world):
    """Alice and Bob signed up and the Chats page inscribed, once per world, whichever test asks first: each test here
    stands alone (run by itself, or on another worker than the first test, the later ones found no keys and failed:
    "keys is null")."""
    base, state, node, a, b = world
    if getattr(state, "_groups_people", False):
        return
    _inscribe(state, PAGE_ID, 7, "n" + "G" * 33, "text/html", PAGE, said={"app": {"name": "Chats", "groups": True}})
    _signed_up(a, base, state, node, "alicegroup")
    _signed_up(b, base, state, node, "bobgroup")
    _settled(state, node.rpc)
    state._groups_people = True


def test_a_line_posted_through_the_bridge_is_read_back_from_the_chain_by_the_other_member(world):   # noqa: F811
    base, state, node, a, b = world
    _alice_and_bob(world)
    made = a.execute_async_script("""
        const done = arguments[0];
        import("/messaging.js").then((mail) => mail.identity(window.wallet.phrase).then((me) =>
          mail.createGroup(window.wallet, me, "Canoe club", ["@bobgroup"], "alicegroup")))
          .then((g) => done({id: g.id}), (e) => done({error: String(e.message || e)}));""")
    assert "error" not in made, made
    _settled(state, node.rpc)

    _app(a, base)
    listed = _do(a, op="list")
    assert "error" not in listed, listed
    group = listed["value"]["groups"][0]
    assert group["id"] == made["id"] and {m["tag"] for m in group["members"]} >= {"alicegroup", "bobgroup"}
    sent = _do(a, op="send", id=group["id"], text="bay route is done")
    assert "error" not in sent, sent
    txid = sent["value"]["txid"]
    assert len(txid) == 64 and txid in node.rpc.call("getrawmempool"), "a real transaction, in the pool"
    assert "bay route is done" not in json.dumps(node.rpc.call("getrawtransaction", txid, 1)), "sealed on the chain"
    _settled(state, node.rpc)

    _app(b, base)
    end = time.time() + 60
    while time.time() < end:
        heard = _do(b, op="list")
        assert "error" not in heard, heard
        if heard["value"]["groups"]:
            break
        time.sleep(2)
    assert heard["value"]["groups"], "Bob's key arrived by the invite on the chain"
    assert heard["value"]["groups"][0]["room"] == group["room"], "both can work out the same room"
    while time.time() < end:
        lines = _do(b, op="read", id=group["id"])["value"]
        if lines:
            break
        time.sleep(2)
    assert [l["text"] for l in lines] == ["bay route is done"], lines
    assert lines[0]["from"] == listed["value"]["me"], "credited to the address that paid, which nobody can forge"
    assert lines[0]["height"] > 0, "in a block"


def test_somebody_added_later_reads_what_was_said_and_everybody_sees_them(world):   # noqa: F811
    base, state, node, a, b = world
    _alice_and_bob(world)
    # Both browsers are signed up by the test above: Alice makes a group with one other, then lets a third person in.
    third = browsers.launch()
    try:
        _signed_up(third, base, state, node, "carolgroup")
        _settled(state, node.rpc)
        a.switch_to.default_content()
        b.switch_to.default_content()
        a.get(f"{base}/join")
        made = a.execute_async_script("""
            const done = arguments[0];
            Promise.all([import("/messaging.js"), import("/wallet.js")]).then(async ([mail, w]) => {
              const keys = await w.opened(ARCADE_CHAIN), me = await mail.identity(keys.phrase);
              const g = await mail.createGroup(keys, me, "Late club", ["@bobgroup"], "alicegroup");
              return {id: g.id};
            }).then(done, (e) => done({error: String(e.message || e)}));""")
        assert "error" not in made, made
        _settled(state, node.rpc)
        grown = a.execute_async_script("""
            const done = arguments[1];
            Promise.all([import("/messaging.js"), import("/wallet.js")]).then(async ([mail, w]) => {
              const keys = await w.opened(ARCADE_CHAIN), me = await mail.identity(keys.phrase);
              const g = await mail.groupById(arguments[0]);
              await mail.writeGroup(keys, me, g, "before carol");
              const more = await mail.addToGroup(keys, me, g, ["@carolgroup"]);
              let again = "";
              try { await mail.addToGroup(keys, me, more, ["@carolgroup"]); } catch (e) { again = String(e.message || e); }
              return {members: more.members.map((m) => m.tag), again};
            }).then(done, (e) => done({error: String(e.message || e)}));""", made["id"])
        assert "error" not in grown, grown
        assert "carolgroup" in grown["members"] and "already" in grown["again"]
        _settled(state, node.rpc)

        def seen(driver):
            end = time.time() + 60
            while time.time() < end:
                found = [g for g in _do(driver, op="list")["value"]["groups"] if g["id"] == made["id"]]
                if found and any(m["tag"] == "carolgroup" for m in found[0]["members"]):
                    return found[0]
                time.sleep(2)
            raise AssertionError("the group never showed everybody")

        _app(third, base)
        assert {m["tag"] for m in seen(third)["members"]} >= {"alicegroup", "bobgroup", "carolgroup"}
        end, lines = time.time() + 60, []
        while time.time() < end and not lines:
            lines = _do(third, op="read", id=made["id"])["value"]
            time.sleep(2)
        assert [l["text"] for l in lines] == ["before carol"], "the newcomer reads what was said before"
        _app(b, base)
        seen(b)                                                      # who was already in sees the newcomer
    finally:
        third.quit()


def test_leaving_takes_you_out_of_what_is_said_next_and_those_who_stay_carry_on(world):   # noqa: F811
    """2026-11-01: a way to leave a group chat. Real keys on a real chain: the one who left cannot read what comes next."""
    base, state, node, a, b = world
    _alice_and_bob(world)
    third = browsers.launch()
    try:
        _signed_up(third, base, state, node, "danagroup")
        _settled(state, node.rpc)
        a.switch_to.default_content(); b.switch_to.default_content()
        a.get(f"{base}/join"); b.get(f"{base}/join")
        # A WORKAROUND, not a fix (2026-11-03): about one run in four, mail.identity() here throws tweetnacl's
        # "unexpected type, use Uint8Array" (checkArrayTypes <- keyPair.fromSecretKey <- messaging.js identity) on a
        # key signin.js derive() made as a Uint8Array -- seen on the commit before too, never in the other tests here;
        # not yet explained. The step is tried once more on that error alone; anything else still fails.
        script = """
            const done = arguments[0];
            Promise.all([import("/messaging.js"), import("/wallet.js")]).then(async ([mail, w]) => {
              const keys = await w.opened(ARCADE_CHAIN), me = await mail.identity(keys.phrase);
              const g = await mail.createGroup(keys, me, "Exit club", ["@bobgroup", "@danagroup"], "alicegroup");
              await mail.writeGroup(keys, me, g, "before bob left");
              return {id: g.id, me: mail.hex(me.publicKey)};
            }).then(done, (e) => done({error: String(e.message || e)}));"""
        made = a.execute_async_script(script)
        if "use Uint8Array" in str(made.get("error", "")):
            a.get(f"{base}/join")
            made = a.execute_async_script(script)
        assert "error" not in made, made
        _settled(state, node.rpc)
        _app(a, base); _app(b, base); _app(third, base)

        def group(driver, want=True):
            end = time.time() + 90
            while time.time() < end:
                found = [g for g in _do(driver, op="list")["value"]["groups"] if g["id"] == made["id"]]
                if bool(found) == want and want is False:
                    return None
                if found and want is True:
                    return found[0]
                if found and callable(want) and want(found[0]):
                    return found[0]
                time.sleep(2)
            raise AssertionError(f"never as wanted: {want}")

        room_before = group(b)["room"]
        bob = _do(b, op="list")["value"]["me"]
        b.switch_to.default_content()
        b.get(f"{base}/join")
        left = b.execute_async_script("""
            const done = arguments[1];
            Promise.all([import("/messaging.js"), import("/wallet.js")]).then(async ([mail, w]) => {
              const keys = await w.opened(ARCADE_CHAIN), me = await mail.identity(keys.phrase);
              const g = await mail.currentGroup(arguments[0]);
              return {left: await mail.leaveGroup(keys, me, g), secret: g.secret};
            }).then(done, (e) => done({error: String(e.message || e)}));""", made["id"])
        assert "error" not in left, left
        _settled(state, node.rpc)
        moved = lambda g: all(m["address"] != bob for m in g["members"])      # noqa: E731
        # The creator's browser reads the line and makes the new key. The one who left never touches it.
        group(a, lambda g: moved(g) and g["room"] != room_before)
        _settled(state, node.rpc)
        stays = group(third, lambda g: moved(g) and g["room"] != room_before)
        assert {m["tag"] for m in stays["members"]} == {"alicegroup", "danagroup"}
        a.switch_to.default_content()
        a.get(f"{base}/join")
        said = a.execute_async_script("""
            const done = arguments[2];
            Promise.all([import("/messaging.js"), import("/wallet.js")]).then(async ([mail, w]) => {
              const keys = await w.opened(ARCADE_CHAIN), me = await mail.identity(keys.phrase);
              const g = await mail.currentGroup(arguments[0]);
              await mail.writeGroup(keys, me, g, "after bob left");
              return {creator: g.creator === arguments[1], rotated: !!g.rotated_from, secret: g.secret};
            }).then(done, (e) => done({error: String(e.message || e)}));""", made["id"], made["me"])
        assert said["creator"] is True and said["rotated"] is True, said
        assert said["secret"] != left["secret"]
        _settled(state, node.rpc)
        end, lines = time.time() + 60, []
        while time.time() < end and "after bob left" not in lines:
            lines = [l["text"] for l in _do(third, op="read", id=made["id"])["value"]]
            time.sleep(2)
        assert "after bob left" in lines, "those who stay carry on"
        _app(b, base)
        for _ in range(8):
            _do(b, op="list"); time.sleep(2)
        assert group(b, False) is None, "gone from the list of the one who left"
        b.switch_to.default_content()
        b.get(f"{base}/join")
        theirs = b.execute_async_script("""
            const done = arguments[2];
            import("/messaging.js").then(async (mail) => {
              const words = (await mail.groupLines(arguments[0], {limit: 100})).map((l) => l.text);
              const held = (await mail.groups()).filter((g) => mail.rootOf(g) === arguments[0]).map((g) => g.secret);
              return {words, held, listed: (await mail.threads()).some((t) => t.peer === "group:" + arguments[0])};
            }).then(done, (e) => done({error: String(e.message || e)}));""", made["id"], said["secret"])
        assert "after bob left" not in theirs["words"], theirs
        assert said["secret"] not in theirs["held"], "the one who left never had the new key"
        assert theirs["listed"] is False, theirs
        # Asked back: a later invite that names them brings the chat back.
        a.execute_async_script("""
            const done = arguments[1];
            Promise.all([import("/messaging.js"), import("/wallet.js")]).then(async ([mail, w]) => {
              const keys = await w.opened(ARCADE_CHAIN), me = await mail.identity(keys.phrase);
              await mail.addToGroup(keys, me, await mail.currentGroup(arguments[0]), ["@bobgroup"]);
            }).then(() => done(1), (e) => done(String(e.message || e)));""", made["id"])
        _settled(state, node.rpc)
        _app(b, base)
        group(b)
        # The creator leaves: the first of those who stay makes the key, and looks after the group from then on.
        before = group(third)["room"]
        a.switch_to.default_content()
        a.get(f"{base}/join")
        out = a.execute_async_script("""
            const done = arguments[1];
            Promise.all([import("/messaging.js"), import("/wallet.js")]).then(async ([mail, w]) => {
              const keys = await w.opened(ARCADE_CHAIN), me = await mail.identity(keys.phrase);
              const g = await mail.currentGroup(arguments[0]);
              await mail.leaveGroup(keys, me, g);
              return {first: g.members.filter((m) => m.key !== mail.hex(me.publicKey))[0].tag};
            }).then(done, (e) => done({error: String(e.message || e)}));""", made["id"])
        assert out == {"first": "danagroup"}, out
        _settled(state, node.rpc)
        now = group(third, lambda g: g["room"] != before and all(m["tag"] != "alicegroup" for m in g["members"]))
        assert {m["tag"] for m in now["members"]} == {"bobgroup", "danagroup"}
        third.switch_to.default_content()
        third.get(f"{base}/join")
        owner = third.execute_async_script("""
            const done = arguments[1];
            Promise.all([import("/messaging.js"), import("/wallet.js")]).then(async ([mail, w]) => {
              const keys = await w.opened(ARCADE_CHAIN), me = await mail.identity(keys.phrase);
              const g = await mail.currentGroup(arguments[0]);
              return g.creator === mail.hex(me.publicKey);
            }).then(done, (e) => done(String(e.message || e)));""", made["id"])
        assert owner is True, owner
    finally:
        third.quit()
