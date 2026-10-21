"""Fruit names for arcade operators, held by @bigchiefenergy's node until a new arcade needs one (2026-10-20:
"Whenever someone spins up a new arcade, it should automatically choose a fruit name for its operator account. We
need to somehow reserve fruit names." and "@bigchiefenergy should reserve the fruit names. 150 well-known fruits
since that is the node operator account").

The chain allows one name per address (tags.py), so the operator's own address cannot hold 150 of them. Each fruit
is claimed by an address of its own in the same node wallet as @bigchiefenergy's key (labelled "fruit:<name>"): the
node holds them, and hands one on with a name transfer to a new arcade's operator. No new chain rule: every arcade's
index reads these as ordinary claims and transfers.

Left out of the list: names already somebody's on the chain when it was made (@apple, @banana, @orange), @strawberry
(already a test machine arcade's operator), and "kaffirlime" (a slur in South Africa).
"""
from __future__ import annotations

import contextlib
import logging
import threading
import time
from typing import Any

from . import payload as P
from . import tags as taglib
from . import tokens as tokenlib

log = logging.getLogger("arcade.fruit")

FRUITS = (
    "apricot", "avocado", "blackberry", "blackcurrant", "blueberry", "boysenberry", "breadfruit", "cantaloupe",
    "cherimoya", "cherry", "clementine", "cloudberry", "coconut", "cranberry", "currant", "damson", "date",
    "dewberry", "dragonfruit", "durian", "elderberry", "feijoa", "fig", "gooseberry", "grape", "grapefruit",
    "guava", "honeydew", "huckleberry", "jabuticaba", "jackfruit", "jujube", "kiwi", "kumquat", "lemon", "lime",
    "lingonberry", "loganberry", "longan", "loquat", "lychee", "mandarin", "mango", "mangosteen", "marionberry",
    "melon", "mulberry", "nectarine", "olive", "papaya", "passionfruit", "peach", "pear", "persimmon", "pineapple",
    "plantain", "plum", "pomegranate", "pomelo", "quince", "rambutan", "raspberry", "redcurrant", "salak",
    "satsuma", "soursop", "starfruit", "tamarind", "tangerine", "tangelo", "watermelon", "yuzu", "ugli", "acai",
    "acerola", "ackee", "bilberry", "bloodorange", "calamansi", "chokeberry", "citron", "crabapple", "custardapple",
    "fingerlime", "goji", "greengage", "kiwano", "jostaberry", "langsat", "lucuma", "mamey", "marula", "medlar",
    "miraclefruit", "nance", "naranjilla", "noni", "pawpaw", "pepino", "physalis", "pitaya", "pitanga", "plumcot",
    "pluot", "pricklypear", "pulasan", "salmonberry", "santol", "sapodilla", "sapote", "seabuckthorn",
    "serviceberry", "sugarapple", "tayberry", "thimbleberry", "honeyberry", "saskatoon", "muscadine", "babaco",
    "barberry", "bael", "buddhashand", "cupuacu", "wineberry", "youngberry", "mirabelle", "quandong", "mamoncillo",
    "camucamu", "sloe", "kiwiberry", "minneola", "casaba", "crenshaw", "cherryplum", "muskmelon", "wintermelon",
    "bitterorange", "limequat", "sweetlemon", "meyerlemon", "keylime", "blackgrape", "redgrape", "concord", "gala",
    "fuji", "honeycrisp", "grannysmith", "bartlett",
)
#: Coins sent to each fruit address: its claim, and later its transfer, each pay a fee from it.
FUND = 3
SETTING = "fruit_stand"
#: How long a fruit stays promised to an arcade whose operator has not signed up yet.
PROMISE_SECONDS = 30 * 86400


class FruitStand:
    """The node's fruit names: reserve them (fund each address, then claim), say where each one is, hand one on."""

    def __init__(self, state: Any, chain: Any, index: Any):
        self.state, self.chain, self.index = state, chain, index
        self.busy = ""
        self.error = ""
        self._lock = threading.Lock()

    def _book(self) -> dict:
        return dict(self.state.setting(SETTING) or {})

    def _keep(self, book: dict) -> None:
        self.state.set_setting(SETTING, book)

    def status(self) -> dict:
        book = self._book()
        rows = []
        for name in FRUITS:
            row = book.get(name) or {}
            holder = ""
            try:
                holder = self.index.address_of(name) or ""
            except Exception:                                  # noqa: BLE001 -- index catching up
                pass
            if row.get("given_to"):
                where = "given" if holder == row["given_to"] else "giving"
            elif holder and holder == row.get("address") and self._promised(row):
                where = "promised"
            elif holder and holder == row.get("address"):
                where = "held"
            elif holder:
                where = "taken"
            elif row.get("claim"):
                where = "claiming"
            elif row.get("funded"):
                where = "funded"
            else:
                where = "waiting"
            rows.append({"name": name, "state": where, "address": row.get("address", ""),
                         "given_to": row.get("given_to", ""), "promised_to": row.get("promised_to", "")})
        counts: dict[str, int] = {}
        for r in rows:
            counts[r["state"]] = counts.get(r["state"], 0) + 1
        return {"counts": counts, "rows": rows, "busy": self.busy, "error": self.error}

    @staticmethod
    def _promised(row: dict) -> bool:
        return bool(row.get("promised_to")) and time.time() - float(row.get("promised_at") or 0) < PROMISE_SECONDS

    def promise(self, node: str) -> str | None:
        """The fruit for a new arcade (mesh node id `node`): the one already promised to it, or the next free one,
        kept for it PROMISE_SECONDS while its operator signs up."""
        with self._lock:
            book = self._book()
            for name in FRUITS:
                row = book.get(name) or {}
                if row.get("promised_to") == node and not row.get("given_to") and self._promised(row):
                    return name
            free = self.next_free()
            if free is None:
                return None
            book[free] = {**book[free], "promised_to": node, "promised_at": time.time()}
            self._keep(book)
            return free

    def give_promised(self, node: str, name: str, to: str) -> str:
        """Send `name` to `to`, but only the fruit this node promised to that arcade."""
        row = self._book().get(name) or {}
        if row.get("promised_to") != node:
            raise ValueError(f"@{name} was not promised to that arcade")
        return self.give(name, to)

    def next_free(self) -> str | None:
        for r in self.status()["rows"]:
            if r["state"] == "held":
                return r["name"]
        return None

    # --- reserving -------------------------------------------------------------------------------------------------

    def reserve(self, wait: float = 1800.0) -> None:
        """Every fruit not yet held: an address each, FUND coins each (one transaction), then a claim from each."""
        with self._lock:
            self.error = ""
            try:
                self._reserve(wait)
            except Exception as exc:                           # noqa: BLE001 -- said in the admin panel
                self.error = str(exc)[:300]
                log.warning("fruit stand: %s", exc)
            finally:
                self.busy = ""

    def _reserve(self, wait: float) -> None:
        book = self._book()
        with self.chain.rpc() as rpc:
            self.busy = "making addresses"
            for name in FRUITS:
                if not (book.get(name) or {}).get("address"):
                    book[name] = {"address": rpc.call("getnewaddress", f"fruit:{name}")}
            self._keep(book)
            todo = [n for n in FRUITS if not book[n].get("claim") and not self.index.address_of(n)]
            unfunded = [n for n in todo if self._coins(rpc, book[n]["address"], 0) < FUND * tokenlib.COIN // 2]
            if unfunded:
                self.busy = f"funding {len(unfunded)} addresses"
                with self._sending():
                    txid = self._fund(rpc, [book[n]["address"] for n in unfunded])
                for n in unfunded:
                    book[n]["funded"] = txid
                self._keep(book)
            self.busy = "waiting for the funding to confirm"
            until = time.time() + wait
            while todo and time.time() < until:
                spendable = {u["address"] for u in rpc.call("listunspent", 1, 9999999,
                                                            [book[n]["address"] for n in todo])}
                if all(book[n]["address"] in spendable for n in todo):
                    break
                time.sleep(15)
            sender = tokenlib.TokenSender(rpc, self.chain.params)
            for i, name in enumerate(todo):
                self.busy = f"claiming @{name} ({i + 1} of {len(todo)})"
                if self.index.address_of(name):
                    continue                                   # somebody else's now: left as "taken"
                with self._sending():
                    prepared = sender.prepare(book[name]["address"], P.AnyData(data=taglib.encode(name)).encode())
                    prepared.what = f"reserve @{name}"
                    book[name]["claim"] = sender.broadcast(prepared)
                self._keep(book)

    @staticmethod
    def _coins(rpc, address: str, minconf: int = 1) -> int:
        return sum(int(round(float(u["amount"]) * tokenlib.COIN))
                   for u in rpc.call("listunspent", minconf, 9_999_999, [address]) or [] if u.get("spendable", True))

    @contextlib.contextmanager
    def _sending(self, wait: float = 120.0):
        """The application's one-send-at-a-time lock (state.begin_send), so no other send builds from the same coins."""
        begin = getattr(self.state, "begin_send", None)
        if begin is None:
            yield
            return
        until = time.time() + wait
        while not begin():
            if time.time() > until:
                raise ValueError("the wallet is busy sending something else; try again")
            time.sleep(1)
        try:
            yield
        finally:
            self.state.end_send()

    def _fund(self, rpc, addresses: list[str]) -> str:
        """FUND coins to each address in one transaction, paid from the node's own address (@bigchiefenergy's) with
        the change back to it -- never from another address of the wallet (2026-10-20: `sendmany` spends the wallet's
        default "account", whose bookkeeping on .25 is negative after years of sends, and refused)."""
        from . import fees
        from .txbuild import build_raw_tx, p2pkh_script
        funder = self.state.derived_address
        if not funder:
            raise ValueError("this node has no identity address to pay from")
        outputs = [(FUND * tokenlib.COIN, p2pkh_script(a)) for a in addresses]
        sender = tokenlib.TokenSender(rpc, self.chain.params)
        try:
            inputs = sender._inputs._select_inputs(funder, sum(v for v, _ in outputs) + tokenlib.COIN)
        except tokenlib.SendError as exc:
            raise ValueError(f"the node's address cannot pay for the fruit names: {exc}") from exc
        funded = fees.fund(rpc, build_raw_tx(inputs, outputs), {"changeAddress": funder})
        signed = rpc.call("signrawtransaction", funded["hex"])
        if not signed.get("complete"):
            raise ValueError(f"signing the funding failed: {signed.get('errors')}")
        return rpc.call("sendrawtransaction", signed["hex"])

    # --- handing one on --------------------------------------------------------------------------------------------

    def give(self, name: str, to: str) -> str:
        """Send @name to address `to` (a new arcade's operator). Returns the transfer's txid."""
        with self._lock:
            book = self._book()
            row = book.get(name) or {}
            if name not in FRUITS or not row.get("address"):
                raise ValueError(f"@{name} is not one of this node's fruit names")
            if row.get("given_to"):
                raise ValueError(f"@{name} was already given to {row['given_to']}")
            if self.index.address_of(name) != row["address"]:
                raise ValueError(f"@{name} is not held by this node yet")
            if self.index.tag_of(to):
                raise ValueError(f"that address already holds @{self.index.tag_of(to)}")
            with self.chain.rpc() as rpc, self._sending():
                sender = tokenlib.TokenSender(rpc, self.chain.params)
                prepared = sender.prepare(row["address"],
                                          P.AnyData(data=taglib.encode(name, taglib.KIND_TRANSFER)).encode(), reference=to)
                prepared.what = f"give @{name}"
                txid = sender.broadcast(prepared)
            row.update(given_to=to, given=txid, given_at=time.time())
            book[name] = row
            self._keep(book)
            return txid
