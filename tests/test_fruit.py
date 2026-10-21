"""Fruit names for new arcades' operators, held by the node (arcade/fruit.py, 2026-10-20: "@bigchiefenergy
should reserve the fruit names. 150 well-known fruits since that is the node operator account")."""
import pytest

from arcade import fruit, payload as P, tags


class Index:
    def __init__(self):
        self.names = {}                 # tag -> address

    def address_of(self, tag):
        return self.names.get(tag)

    def tag_of(self, address):
        return next((t for t, a in self.names.items() if a == address), None)


class Node:
    def __init__(self):
        self.made = 0
        self.sent = []
        self.funded = set()

    def call(self, method, *params):
        if method == "getnewaddress":
            self.made += 1
            return f"addr{self.made}"
        if method == "listunspent":
            return [{"address": a, "amount": 3.0} for a in params[2] if a in self.funded]
        raise AssertionError(method)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Chain:
    params = None

    def __init__(self, node):
        self.node = node

    def rpc(self):
        return self.node


class State:
    def __init__(self):
        self.s = {}

    def setting(self, k, d=None):
        return self.s.get(k, d)

    def set_setting(self, k, v):
        self.s[k] = v


@pytest.fixture
def stand(monkeypatch):
    index, node = Index(), Node()
    broadcast = []

    class Sender:
        def __init__(self, rpc, params):
            pass

        def prepare(self, sender, payload, reference=None):
            data = P.decode(payload).data
            kind, name = data[5], data[7:].decode()
            class Prepared:
                pass
            p = Prepared()
            p.args = (sender, kind, name, reference)
            return p

        def broadcast(self, p):
            sender, kind, name, reference = p.args
            broadcast.append(p.args)
            if kind == tags.KIND_CLAIM:
                index.names[name] = sender
            else:
                index.names[name] = reference
            return f"tx{len(broadcast)}"
    monkeypatch.setattr(fruit.tokenlib, "TokenSender", Sender)

    def fund(self, rpc, addresses):
        node.sent.append(addresses)
        node.funded.update(addresses)
        return "fundtx"
    monkeypatch.setattr(fruit.FruitStand, "_fund", fund)
    s = fruit.FruitStand(State(), Chain(node), index)
    return s, index, node, broadcast


def test_the_list_is_150_valid_names_and_leaves_out_what_was_already_somebodys():
    assert len(fruit.FRUITS) == 150 == len(set(fruit.FRUITS))
    for name in fruit.FRUITS:
        assert tags.validate(name) == name
    for gone in ("apple", "banana", "orange", "strawberry", "kaffirlime"):
        assert gone not in fruit.FRUITS


def test_each_fruit_is_claimed_from_an_address_of_its_own(stand):
    s, index, node, broadcast = stand
    index.names["mango"] = "somebody"                 # claimed by another before the stand got to it
    s.reserve(wait=0)
    assert s.error == ""
    assert len(node.sent) == 1 and len(node.sent[0]) == 149, "one funding transaction, every unclaimed fruit"
    senders = [b[0] for b in broadcast]
    assert len(senders) == 149 == len(set(senders)), "one address a name: the chain's rule"
    counts = s.status()["counts"]
    assert counts == {"held": 149, "taken": 1}
    # Again: nothing more to do.
    s.reserve(wait=0)
    assert len(broadcast) == 149 and len(node.sent) == 1


def test_a_fruit_is_handed_on_once_and_only_to_an_address_without_a_name(stand):
    s, index, node, broadcast = stand
    s.reserve(wait=0)
    first = s.next_free()
    index.names["already"] = "addr-x"
    with pytest.raises(ValueError):
        s.give(first, "addr-x")
    txid = s.give(first, "new-operator")
    assert index.address_of(first) == "new-operator" and txid
    assert broadcast[-1][1] == tags.KIND_TRANSFER and broadcast[-1][3] == "new-operator"
    with pytest.raises(ValueError):
        s.give(first, "someone-else")
    assert s.next_free() != first
    assert s.status()["counts"]["given"] == 1


def test_the_funding_is_paid_from_the_nodes_own_address_with_change_back_to_it(monkeypatch):
    """2026-10-20 on .25: `sendmany` spent the wallet's default account and refused; the funding now picks coins of
    the node's identity address only, change to it."""
    from arcade import fees
    calls = {}

    class Inputs:
        def _select_inputs(self, sender, needed):
            calls["from"], calls["needed"] = sender, needed
            return ["in"]

    class Sender:
        def __init__(self, rpc, params):
            self._inputs = Inputs()
    monkeypatch.setattr(fruit.tokenlib, "TokenSender", Sender)
    monkeypatch.setattr("arcade.txbuild.build_raw_tx", lambda ins, outs: calls.setdefault("outs", outs) and "raw")
    monkeypatch.setattr("arcade.txbuild.p2pkh_script", lambda a: a.encode())
    monkeypatch.setattr(fees, "fund", lambda rpc, raw, opts: calls.setdefault("opts", opts) and {"hex": "ff"})

    class Rpc:
        def call(self, method, *params):
            return {"complete": True, "hex": "ff"} if method == "signrawtransaction" else "txid"
    state = State()
    state.derived_address = "nodeaddr"
    stand = fruit.FruitStand(state, Chain(None), Index())
    assert stand._fund(Rpc(), ["a1", "a2"]) == "txid"
    assert calls["from"] == "nodeaddr" and calls["opts"] == {"changeAddress": "nodeaddr"}
    assert [v for v, _ in calls["outs"]] == [fruit.FUND * fruit.tokenlib.COIN] * 2


def test_a_fruit_address_whose_coins_went_elsewhere_is_funded_again(stand):
    """2026-10-20: the gather swept 149 funded fruit addresses home before their claims; a second run must fund them
    again rather than trust that they once were."""
    s, index, node, broadcast = stand
    real_fund = fruit.FruitStand._fund
    s.reserve(wait=0)
    assert len(node.sent) == 1
    # Forget the claims and empty the addresses, as the sweep did.
    book = s._book()
    for name in fruit.FRUITS[1:]:
        book[name].pop("claim", None)
        index.names.pop(name, None)
        node.funded.discard(book[name]["address"])
    s._keep(book)
    s.reserve(wait=0)
    assert len(node.sent) == 2 and len(node.sent[1]) == 149


def test_the_gather_leaves_the_fruit_addresses_alone():
    import inspect
    from arcade.web import app as webapp
    source = inspect.getsource(webapp)
    assert 'keep += [row.get("address") for row in (state.setting("fruit_stand") or {}).values()]' in source


# --------------------------------------------------------------------------- promises and the mesh

def test_a_fruit_is_promised_to_one_arcade_and_given_only_to_it(stand):
    s, index, node, broadcast = stand
    s.reserve(wait=0)
    first = s.promise("arcade-a")
    assert s.promise("arcade-a") == first, "asking again gets the same one"
    second = s.promise("arcade-b")
    assert second and second != first
    with pytest.raises(ValueError):
        s.give_promised("arcade-b", first, "addr-b")
    s.give_promised("arcade-a", first, "addr-a")
    assert index.address_of(first) == "addr-a"
    assert s.status()["counts"] == {"held": 148, "promised": 1, "given": 1}


def test_the_mesh_asks_offers_and_hands_the_fruit_over(stand):
    import asyncio
    from arcade.mesh.fruitshare import FruitShare
    s, index, node, broadcast = stand
    s.reserve(wait=0)

    class Arcade:
        def __init__(self, holder):
            self.settings = {"fruit_stand": {"x": 1}} if holder else {}
            self.operator = ""

        def setting(self, k, d=None):
            return self.settings.get(k, d)

        def set_setting(self, k, v):
            self.settings[k] = v

    class Link:
        def __init__(self, peer_id):
            self.peer_id, self.other, self.share = peer_id, None, None

        async def send(self, header, blob=b""):
            await self.other.share.on_frame(self.other, header, blob)

    new, old = Arcade(False), Arcade(True)
    asker, holder = FruitShare(new, lambda: None), FruitShare(old, lambda: s)
    to_holder, to_asker = Link("old"), Link("new")          # each side's link, named by who is at the other end
    to_holder.other, to_asker.other = to_asker, to_holder
    to_holder.share, to_asker.share = asker, holder

    class Node:
        def __init__(self, links):
            self.links = links
    asker.node, holder.node = Node({"old": to_holder}), Node({"new": to_asker})

    async def go():
        assert asker.wants() and not holder.wants()
        await asker.link_up(to_holder)
        offer = new.settings["fruit_offer"]
        assert offer["from"] == "old" and offer["name"] in fruit.FRUITS
        assert not asker.wants(), "an arcade with an offer asks no more"
        asker.loop = asyncio.get_running_loop()
        asker.ask_to_give("operator-address")
        await asyncio.sleep(0.05)
        return offer["name"]
    name = asyncio.run(go())
    assert index.address_of(name) == "operator-address"
    assert new.settings["fruit_offer"]["txid"]
