"""Watch-only holdings for one Dogecoin address.

What the address owns comes from this arcade: the watcher's coin index (the
balance), the ledger's inscription index (doginals), and the ord index
(DRC-20 balances and names). While this Core is behind, or the chain is
borrowed, those reads go to a linked arcade the way the watcher already
asks (`utxoset.LentSet`, `coinscan`, `DoginalIndex.lookup`). A collection's
name, cover and floor are the arcade's own (the collections in its database,
its Doginal market's listings). Nothing here asks for a key, signs, or spends,
and no other server is asked anything.

Outpoints are asked of the name index inside this process and are not returned.
"""
from __future__ import annotations

import re
import threading
from html import unescape


# A mainnet Dogecoin address. No bech32, no key material, no testnet n-address.
_ADDRESS = re.compile(r"^D[1-9A-HJ-NP-Za-km-z]{25,33}$")
_INSCRIPTION = re.compile(r"^[0-9a-f]{64}i\d{1,6}$")
_SLUG = re.compile(r"^[a-z0-9-]{1,80}$")
_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_DOGE = re.compile(r"^\d+(?:\.\d+)?$")
_TICK = re.compile(r"^\$?[A-Za-z0-9]{1,12}$")
_EMPTY = "Import a watch-only address on this node."

# Spend-shaped keys that must never leave this module, even if a caller passes a wallet row.
_SPEND_KEYS = {
    "scriptPubKey", "scriptPubKeyHex", "script", "txid", "vout", "value",
    "value_koinu", "hex", "witness", "private_key", "privkey", "wif",
}


def valid_address(address: str) -> str:
    text = (address or "").strip()
    if not _ADDRESS.fullmatch(text):
        raise ValueError("not a Dogecoin address")
    return text


def inscriptions_from_utxos(rows) -> list[dict]:
    """Inscription id and type only. Drops scripts, outpoints, and amounts."""
    found: list[dict] = []
    seen: set[str] = set()
    if not isinstance(rows, list):
        return found
    for row in rows:
        nested = row.get("inscriptions") if isinstance(row, dict) else None
        if isinstance(nested, dict):
            nested = [nested]
        for ins in nested or []:
            if not isinstance(ins, dict):
                continue
            iid = str(ins.get("id") or ins.get("inscription_id") or "")
            if not _INSCRIPTION.fullmatch(iid) or iid in seen:
                continue
            seen.add(iid)
            mime = str(ins.get("content_type") or ins.get("mime_type") or ins.get("mime") or "")[:80]
            found.append({"inscription_id": iid, "content_type": mime})
    return found


def parse_names(payload, address: str) -> list[dict]:
    """Names in a payload whose `address` is this one. Sold names (`held` false) are dropped."""
    if not isinstance(payload, dict):
        return []
    said = str(payload.get("address") or "")
    if said and said != address:
        return []
    out = []
    seen: set[str] = set()
    for row in payload.get("names") or []:
        if not isinstance(row, dict) or row.get("held") is False:
            continue
        name = str(row.get("name") or "")
        if not _NAME.fullmatch(name) or name in seen:
            continue
        seen.add(name)
        out.append({
            "name": name,
            "address": address,
            "held": True,
            "verified": bool(row.get("verified")),
        })
    return out


def parse_balance(payload) -> str | None:
    """The public `balance_doge` string. The legacy `balance` field is not DOGE."""
    if not isinstance(payload, dict):
        return None
    raw = payload.get("balance_doge")
    text = "" if raw is None else str(raw).strip()
    if _DOGE.fullmatch(text):
        return text
    return None


def parse_drc20(payload) -> list[dict]:
    rows = payload.get("balances") if isinstance(payload, dict) else None
    out = []
    seen: set[str] = set()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        tick = str(row.get("tick") or row.get("token") or "")
        balance = str(row.get("balance") or "").split(".", 1)[0]
        if not _TICK.fullmatch(tick) or not _DOGE.fullmatch(balance) or tick in seen:
            continue
        seen.add(tick)
        out.append({"tick": tick, "balance": balance})
    return out


def _safe_image(src: str) -> str:
    text = unescape(src or "").strip()
    # Only this arcade's own pictures.
    if re.fullmatch(r"/content/[0-9a-fA-F]{64}(i[0-9]{1,6})?", text):
        return text
    return ""


def _koinu_doge(koinu: int) -> str:
    whole, frac = divmod(int(koinu), 100_000_000)
    text = f"{whole}.{frac:08d}".rstrip("0").rstrip(".")
    return text or "0"


def _as_slug(name: str) -> str:
    slug = (name or "").strip().lower()
    return slug if _SLUG.fullmatch(slug) else ""


#: The running arcade's AppState, set by app.py at start. Why (2026-11-06): `arcade.web.state` is the MODULE, which
#: has no ledger, messaging or addons, so _doge_chain() always found nothing and every balance came back "local / 0"
#: -- even while Dogecoin was still syncing and a synced mesh arcade could have lent it.
STATE = None


def _state():
    if STATE is not None:
        return STATE
    from arcade.web import state
    return state


def _doge_chain():
    """The Dogecoin chain this process has, synced or not (a syncing one still borrows from the mesh), or None."""
    state = _state()

    chains = []
    for attr in ("ledger", "messaging"):
        ctx = getattr(state, attr, None)
        if ctx is not None:
            chains.append(ctx)
    chains.extend(list(getattr(state, "addons", None) or []))
    chains.extend(list(getattr(state, "all_chains", None) or []))
    for ctx in chains:
        if getattr(ctx, "is_doge", False):
            return ctx
    return None


def _catalog_maps(catalog: list[dict]) -> tuple[dict, dict]:
    by_slug: dict[str, dict] = {}
    by_name: dict[str, dict] = {}
    for row in catalog or []:
        if not isinstance(row, dict):
            continue
        slug = str(row.get("slug") or "").lower()
        if _SLUG.fullmatch(slug):
            by_slug[slug] = row
        name = str(row.get("name") or "").strip().lower()
        if name:
            by_name[name] = row
    return by_slug, by_name


def cross_reference(held: list[dict], catalog: list[dict]) -> list[dict]:
    """Group held doginals by collection and fill name, cover, and floor from the catalog.

    A held row needs `slug` (or a collection name the catalog uses). Pieces keep
    the edition and inscription id. A slug the catalog does not know stays in
    the list with `known` false and the title the holding already carried.
    """
    by_slug, by_name = _catalog_maps(catalog)
    groups: dict[str, dict] = {}
    order: list[str] = []
    for item in held or []:
        if not isinstance(item, dict):
            continue
        slug = str(item.get("slug") or "").lower()
        title = str(item.get("title") or item.get("name") or "")
        label = title.split(" #", 1)[0].strip() if " #" in title else title.strip()
        cat = by_slug.get(slug) or by_name.get(label.lower()) or by_name.get(slug)
        if not slug and cat:
            slug = str(cat.get("slug") or "")
        key = slug or label or "unlisted"
        if key not in groups:
            image = ""
            floor = None
            name = label or slug or "Unlisted"
            known = cat is not None
            if cat:
                name = str(cat.get("name") or name)
                image = _safe_image(str(cat.get("image") or ""))
                floor = cat.get("floor")
            groups[key] = {
                "slug": slug,
                "name": name,
                "cover": image,
                "floor": floor,
                "href": f"/exchange/ord/{slug}" if slug else "",
                "count": 0,
                "known": known,
                "pieces": [],
            }
            order.append(key)
        group = groups[key]
        try:
            stated = int(item.get("group_count") or 0)
        except (TypeError, ValueError):
            stated = 0
        if stated > group["count"]:
            group["count"] = stated
        elif not stated:
            group["count"] += 1
        piece = {
            "edition": str(item.get("edition") or ""),
            "inscription_id": str(item.get("inscription_id") or ""),
            "title": title,
            "image": _safe_image(str(item.get("image") or "")) or group["cover"],
            "href": str(item.get("href") or ""),
        }
        if piece["inscription_id"] and not _INSCRIPTION.fullmatch(piece["inscription_id"]):
            piece["inscription_id"] = ""
        if piece["href"] and not piece["href"].startswith(("/exchange/ord/", "/inscriptions/")):
            piece["href"] = ""
        group["pieces"].append(piece)
    return [groups[key] for key in order]


def _clean_names(rows) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "")
        if not _NAME.fullmatch(name) or name in seen:
            continue
        seen.add(name)
        out.append({"name": name})
    return out


def _clean_tokens(rows) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        tick = str(row.get("tick") or "")
        balance = str(row.get("balance") or "")
        if not _TICK.fullmatch(tick) or not _DOGE.fullmatch(balance) or tick in seen:
            continue
        seen.add(tick)
        out.append({"tick": tick, "balance": balance})
    return out


def _units_text(units, decimals: int) -> str:
    text = str(units or "0").split(".", 1)[0]
    if not text.isdigit():
        return ""
    decimals = max(0, int(decimals or 0))
    if decimals:
        text = text.zfill(decimals + 1)
        cut = len(text) - decimals
        text = (text[:cut] + "." + text[cut:]).rstrip("0").rstrip(".")
    return text


def _tokens_from_index(rows) -> list[dict]:
    out = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        shown = _units_text(row.get("units"), int(row.get("decimals") or 0))
        if not shown:
            continue
        out.append({"tick": str(row.get("tick") or ""), "balance": shown})
    return _clean_tokens(out)


def _names_from_index(rows) -> list[dict]:
    return _clean_names([{"name": row.get("name")} for row in rows or [] if isinstance(row, dict)])


def _pieces_from_index(index, address: str) -> list[dict]:
    """Doginals the ledger says this address holds. Pictures are this node's /content/."""
    found: list[dict] = []
    try:
        groups = index.held_collections(address, cover=1)
    except Exception:
        return found
    for group in groups or []:
        collection = str(group.get("collection") or "")
        try:
            total = int(group.get("count") or 0)
        except (TypeError, ValueError):
            total = 0
        try:
            rows, _total = index.held_pieces(address, collection, limit=24)
        except Exception:
            rows = []
        if not rows:
            if total:
                found.append({
                    "slug": _as_slug(collection),
                    "title": collection or "Unlisted",
                    "edition": "",
                    "inscription_id": "",
                    "image": "",
                    "group_count": total,
                })
            continue
        for row in rows:
            txid = str(row.get("txid") or "")
            edition = row.get("edition")
            title = str(row.get("name") or collection or "")
            image = f"/content/{txid}" if re.fullmatch(r"[0-9a-fA-F]{64}", txid) else ""
            found.append({
                "slug": _as_slug(collection),
                "title": title or collection or "Unlisted",
                "edition": "" if edition is None else str(edition),
                "inscription_id": txid if _INSCRIPTION.fullmatch(txid) else "",
                "image": image,
                "group_count": total or len(rows),
            })
    return found


def _clean_reading(raw) -> dict:
    """Which node answered, and how far this arcade's own Core is. No coin, no outpoint."""
    raw = raw if isinstance(raw, dict) else {}
    via = "peer" if raw.get("via") == "peer" else "local"
    sync = str(raw.get("sync") or "")[:240]
    node = str(raw.get("node") or "this arcade")[:80]
    return {"via": via, "sync": sync, "node": node}


def reading_of(owned) -> dict:
    """The reading line for an API answer. Only via, sync, and node."""
    raw = owned.get("reading") if isinstance(owned, dict) else None
    return _clean_reading(raw)


def _wants_peer(chain) -> bool:
    """True when this chain is borrowed, or this Core is behind and a linked arcade shares it.

    `core_catchup.borrowing` is the same switch `ChainContext.rpc` uses. A chain with no
    sharer stays on this Core.
    """
    if getattr(chain, "borrowed", False):
        return True
    try:
        from arcade import core_catchup
        return bool(core_catchup.borrowing(chain))
    except Exception:
        return False


def _sync_text(chain, *, wanted_peer: bool, coins_from_peer: bool) -> str:
    """Local sync, from `core_catchup.say` (this Core), never the peer's tip."""
    said = None
    if chain is not None:
        try:
            from arcade import core_catchup
            said = core_catchup.say(chain)
        except Exception:
            said = None
    if said:
        line = str(said)
    elif chain is not None and getattr(chain, "borrowed", False):
        line = "this chain is borrowed; this arcade has no Core of its own"
    elif chain is not None:
        line = "this Core is at the tip"
    else:
        line = ""
    if wanted_peer and not coins_from_peer:
        extra = "No coin set was lent, so the balance is this arcade's own index."
        if extra not in line:
            line = (line.rstrip(".") + ". " + extra).strip()
    return line[:240]


def _as_coin(row) -> dict | None:
    if isinstance(row, dict):
        txid, vout, value = row.get("txid"), row.get("vout"), row.get("value")
    elif isinstance(row, (list, tuple)) and len(row) >= 3:
        txid, vout, value = row[0], row[1], row[2]
    else:
        return None
    txid = str(txid or "")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", txid):
        return None
    try:
        return {"txid": txid, "vout": int(vout), "value": int(value)}
    except (TypeError, ValueError):
        return None


def _rows_from_scan(said, address: str) -> list[dict] | None:
    """Coins a sharer already has. An address still waiting is not an answer."""
    if not isinstance(said, list):
        return None
    for one in said:
        if not isinstance(one, dict):
            continue
        coins = one.get("coins")
        if isinstance(coins, dict) and address in coins:
            return [row for row in (_as_coin(c) for c in (coins.get(address) or [])) if row]
    return None


def _coins_from_peer(chain, address: str) -> list[dict] | None:
    """Coins a linked arcade lent for this address, or None when it lent none.

    Same two doors the watcher uses: `LentSet.fetch` (`_utxoset_lent_pass`), then
    `arcade_coinscan` (`coinscan.collect`). The coin rows stay in this process.
    """
    from arcade import coinscan, utxoset as utxosetlib
    from arcade.rpc import RpcError

    try:
        rpc_cm = chain.rpc()
    except Exception:
        return None
    try:
        with rpc_cm as rpc:
            lent = None
            try:
                lent = utxosetlib.LentSet.fetch(rpc, [address])
            except (RpcError, utxosetlib.UtxoSetError, KeyError, TypeError, ValueError):
                lent = None
            if lent is not None:
                return [row for row in (_as_coin(c) for c in lent.unspent(address)) if row]
            try:
                said = rpc.batch([(coinscan.SCAN_METHOD, [{"addresses": [address]}])])
            except (RpcError, TypeError, ValueError):
                return None
            return _rows_from_scan(said, address)
    except Exception:
        return None


def _core_behind(chain) -> bool:
    """This Core was behind at its last check (core_catchup), whether or not a sharer is linked."""
    try:
        from arcade import core_catchup
        return bool(core_catchup.known_behind(getattr(chain, "network", "") or "doge"))
    except Exception:
        return False


def _coins_from_mesh(address: str) -> tuple[list[dict] | None, str]:
    """A bare address's coins from a synced arcade in the mesh (GET /peer/address/{a}/coins, signed with this
    node's mesh key), and that arcade's host; (None, "") when none could say. Never a third-party site."""
    from arcade import peer_address
    hook = peer_address.PEERS
    if hook is None:
        return None, ""
    try:
        hosts, key, ask = hook()
        said = peer_address.ask_peers(address, list(hosts), ask, key, chain="doge")
    except Exception:
        return None, ""
    if not said:
        return None, ""
    return [row for row in (_as_coin(c) for c in said.get("coins") or []) if row], str(said.get("from") or "")


def _postage_first(coins: list[dict]) -> list[dict]:
    from arcade.doginals import POSTAGE_MAX

    def key(coin: dict) -> tuple:
        value = int(coin.get("value") or 0)
        return (0 if 0 < value <= POSTAGE_MAX else 1, value)

    return sorted(coins, key=key)


def _is_postage(value: int) -> bool:
    from arcade.doginals import POSTAGE_MAX
    return 0 < int(value) <= POSTAGE_MAX


def _ask_ord(ordix, question: dict) -> tuple[dict | None, str]:
    """`DoginalIndex.lookup`: the most complete walker, then this arcade. (`via`, from its `from`.)"""
    if ordix is None:
        return None, "local"
    said = None
    try:
        said = ordix.lookup(question)
    except Exception:
        try:
            said = ordix.answer(question)
        except Exception:
            return None, "local"
        if isinstance(said, dict):
            said = {**said, "from": "this arcade"}
    if not isinstance(said, dict):
        return None, "local"
    origin = str(said.get("from") or "")
    return said, ("peer" if origin.startswith("arcade ") else "local")


def _pieces_on(ordix, coins: list[list]) -> tuple[list[dict], str, str, bool]:
    """Inscriptions on coins, from `lookup` `on`. Text, JSON, and scripts stay out.

    The fourth value is whether a walker answered. Coins are not returned.
    """
    found: list[dict] = []
    seen: set[str] = set()
    via = "local"
    node = "this arcade"
    answered = False
    for txid, vout in coins[:24]:
        said, one_via = _ask_ord(ordix, {"what": "on", "txid": txid, "vout": int(vout)})
        if said is None or "on" not in said:
            continue
        answered = True
        if one_via == "peer":
            via = "peer"
            node = str(said.get("from") or node)
        for row in said.get("on") or []:
            if not isinstance(row, dict):
                continue
            iid = str(row.get("id") or "")
            if not _INSCRIPTION.fullmatch(iid) or iid in seen:
                continue
            ctype = str(row.get("ctype") or "")
            if ctype.startswith("text/") or ctype.startswith("application/json") or "javascript" in ctype:
                continue
            seen.add(iid)
            found.append({
                "slug": "",
                "title": "Doginal",
                "edition": "",
                "inscription_id": iid,
                "image": f"/content/{iid[:64]}",
            })
    return found, via, node, answered


def _note_peer(any_peer: bool, peer_node: str, via: str, said) -> tuple[bool, str]:
    if via == "peer" and isinstance(said, dict):
        return True, peer_node or str(said.get("from") or "")
    return any_peer, peer_node


def index_owned(address: str) -> dict:
    """Balance, doginals, DRC-20, and names.

    On a synced Core this is the local indexes. While this Core is behind, or the
    chain is borrowed, coins come from the sharer (`LentSet`, then `arcade_coinscan`)
    and DRC-20, names, and inscriptions come from `DoginalIndex.lookup`. A sharer
    that lent no coin set falls back to this arcade's own index. Coin rows are
    summed and handed to the name index. They are not part of the result.
    """
    address = valid_address(address)
    out: dict = {
        "balance_doge": None, "names": [], "tokens": [], "pieces": [],
        "reading": _clean_reading({}),
    }
    chain = _doge_chain()
    if chain is None:
        return out
    wanted_peer = _wants_peer(chain)
    peer_rows = _coins_from_peer(chain, address) if wanted_peer else None
    mesh_host = ""
    if peer_rows is None and (wanted_peer or _core_behind(chain)):
        peer_rows, mesh_host = _coins_from_mesh(address)
        wanted_peer = wanted_peer or peer_rows is not None
    coins_from_peer = peer_rows is not None
    state = _state()
    from arcade import utxos as utxoslib
    import contextlib

    coins: list[list] = []
    index = None
    if coins_from_peer:
        total = 0
        for coin in _postage_first(peer_rows):
            total += int(coin["value"])
            coins.append([coin["txid"], coin["vout"], coin["value"]])
        out["balance_doge"] = _koinu_doge(total)
    else:
        try:
            index = state.token_index(chain)
            with contextlib.closing(index.open()) as db:
                out["balance_doge"] = _koinu_doge(utxoslib.balance(db, address))
                for coin in utxoslib.unspent(db, address)[:3000]:
                    txid = str(coin.get("txid") or "")
                    if re.fullmatch(r"[0-9a-fA-F]{64}", txid):
                        coins.append([txid, int(coin.get("vout") or 0), int(coin.get("value") or 0)])
            out["pieces"] = _pieces_from_index(index, address)
        except Exception:
            index = None
    try:
        ordix = state.ord_index(getattr(chain, "network", "") or "doge", wait=0.8)
    except Exception:
        ordix = None
    any_peer = coins_from_peer
    peer_node = ""
    if ordix is not None and wanted_peer:
        said, via = _ask_ord(ordix, {"what": "balances", "owner": address})
        if isinstance(said, dict) and "balances" in said:
            out["tokens"] = _tokens_from_index(said.get("balances"))
            any_peer, peer_node = _note_peer(any_peer, peer_node, via, said)
        else:
            try:
                out["tokens"] = _tokens_from_index(ordix.balances(address))
            except Exception:
                pass
        name_coins = [[c[0], c[1]] for c in coins if _is_postage(c[2])][:300]
        if name_coins:
            said, via = _ask_ord(ordix, {"what": "names_on", "coins": name_coins})
            rows = said.get("names_on") if isinstance(said, dict) else None
            if rows is not None:
                out["names"] = _names_from_index(rows)
                any_peer, peer_node = _note_peer(any_peer, peer_node, via, said)
            else:
                try:
                    out["names"] = _names_from_index(ordix.names_on(name_coins))
                except Exception:
                    pass
        if coins_from_peer and coins:
            ordered = [[c[0], c[1]] for c in sorted(coins, key=lambda c: (0 if _is_postage(c[2]) else 1, c[2]))]
            pieces, via, node, answered = _pieces_on(ordix, ordered[:24])
            if answered:
                out["pieces"] = pieces
                if via == "peer":
                    any_peer = True
                    peer_node = peer_node or node
            else:
                try:
                    index = index or state.token_index(chain)
                    out["pieces"] = _pieces_from_index(index, address)
                except Exception:
                    pass
        elif coins_from_peer:
            out["pieces"] = []
    elif ordix is not None:
        try:
            out["tokens"] = _tokens_from_index(ordix.balances(address))
        except Exception:
            pass
        if coins:
            try:
                out["names"] = _names_from_index(ordix.names_on([[c[0], c[1]] for c in coins]))
            except Exception:
                pass
    elif coins_from_peer:
        try:
            index = index or state.token_index(chain)
            out["pieces"] = _pieces_from_index(index, address)
        except Exception:
            out["pieces"] = []
    out["reading"] = _clean_reading({
        "via": "peer" if any_peer else "local",
        "sync": _sync_text(chain, wanted_peer=wanted_peer, coins_from_peer=coins_from_peer),
        "node": peer_node or mesh_host or ("a peer on the arcade network" if any_peer else "this arcade"),
    })
    return out


def _name_body(said) -> dict | None:
    if not isinstance(said, dict):
        return None
    if "readings" in said:
        return said
    inner = said.get("name")
    return inner if isinstance(inner, dict) else None


def local_name(name: str) -> dict | None:
    """One name from this arcade's name index. The claim's outpoint is dropped.

    While this Core is behind, the question goes to `DoginalIndex.lookup` (the most
    complete linked walker, then this arcade).
    """
    chain = _doge_chain()
    if chain is None:
        return None
    state = _state()

    try:
        ordix = state.ord_index(getattr(chain, "network", "") or "doge", wait=0.8)
    except Exception:
        return None
    if ordix is None:
        return None
    try:
        if _wants_peer(chain):
            said, _via = _ask_ord(ordix, {"what": "name", "name": name})
            said = _name_body(said)
        else:
            said = ordix.name(name)
    except Exception:
        return None
    if not isinstance(said, dict):
        return None
    wanted = str(said.get("name") or name).strip().lower()
    for reading in (said.get("readings") or {}).values():
        winner = (reading or {}).get("winner") or {}
        iid = str(winner.get("inscription") or "")
        if _INSCRIPTION.fullmatch(iid):
            return {"name": wanted, "inscription": iid}
    return None


def build_holdings(
    address: str,
    owned: dict,
    catalog: list[dict],
    *,
    imported: bool,
) -> dict:
    """Label index holdings with the public collection catalog. Ownership is already decided."""
    address = valid_address(address)
    owned = owned if isinstance(owned, dict) else {}
    balance = owned.get("balance_doge")
    if balance is not None and not _DOGE.fullmatch(str(balance)):
        balance = None
    names = _clean_names(owned.get("names"))
    tokens = _clean_tokens(owned.get("tokens"))
    collections = cross_reference(list(owned.get("pieces") or []), catalog)
    profile = next((row["name"] for row in names if row["name"].endswith(".dogefeed")), "")
    empty = ""
    if not imported and not names and not collections and not tokens and balance is None:
        empty = _EMPTY
    elif imported and balance is None and not names and not collections and not tokens:
        empty = "This node's index has no holdings for that address yet."
    return {
        "read_only": True,
        "imported": bool(imported),
        "addresses": [address],
        "balance_doge": balance,
        "names": names,
        "tokens": tokens,
        "collections": collections,
        "profile": profile,
        "empty": empty,
        "source": "node",
        "reading": _clean_reading(owned.get("reading")),
    }


def _prefer_reading(parts: list[dict]) -> dict:
    """A peer answer wins over this Core when one address was read from a linked arcade."""
    readings = [_clean_reading(part.get("reading")) for part in parts if isinstance(part, dict)]
    for reading in readings:
        if reading["via"] == "peer":
            return reading
    return readings[0] if readings else _clean_reading({})


def merge_holdings(parts: list[dict]) -> dict:
    """Several watched addresses, one window. Pieces stay deduped by inscription id."""
    parts = [part for part in parts if isinstance(part, dict) and part.get("addresses")]
    if not parts:
        return empty_holdings()
    if len(parts) == 1:
        first = dict(parts[0])
        first["imported"] = True
        first["empty"] = "" if first.get("names") or first.get("collections") else first.get("empty") or ""
        first["reading"] = _clean_reading(first.get("reading"))
        first["source"] = "node"
        return first
    addresses: list[str] = []
    names: list[dict] = []
    tokens: list[dict] = []
    seen_names: set[str] = set()
    seen_ticks: set[str] = set()
    seen_ins: set[str] = set()
    held: list[dict] = []
    balance = None
    for part in parts:
        for addr in part.get("addresses") or []:
            if addr not in addresses:
                addresses.append(addr)
        if balance is None and part.get("balance_doge"):
            balance = part["balance_doge"]
        for row in part.get("names") or []:
            if row.get("name") and row["name"] not in seen_names:
                seen_names.add(row["name"])
                names.append(row)
        for row in part.get("tokens") or []:
            if row.get("tick") and row["tick"] not in seen_ticks:
                seen_ticks.add(row["tick"])
                tokens.append(row)
        for group in part.get("collections") or []:
            for piece in group.get("pieces") or []:
                iid = piece.get("inscription_id") or ""
                if iid and iid in seen_ins:
                    continue
                if iid:
                    seen_ins.add(iid)
                held.append({
                    "slug": group.get("slug") or "",
                    "edition": piece.get("edition") or "",
                    "inscription_id": iid,
                    "title": piece.get("title") or "",
                    "image": piece.get("image") or "",
                    "href": piece.get("href") or "",
                })
    collections = cross_reference(held, [
        {"slug": group.get("slug"), "name": group.get("name"), "image": group.get("cover"),
         "floor": group.get("floor")}
        for part in parts for group in (part.get("collections") or [])
    ])
    profile = next((row["name"] for row in names if str(row.get("name") or "").endswith(".dogefeed")), "")
    return {
        "read_only": True,
        "imported": True,
        "addresses": addresses,
        "balance_doge": balance,
        "names": names,
        "tokens": tokens,
        "collections": collections,
        "profile": profile,
        "empty": "" if names or collections or tokens or balance else "This node's index has no holdings for that wallet yet.",
        "source": "node",
        "reading": _prefer_reading(parts),
    }


def empty_holdings() -> dict:
    return {
        "read_only": True,
        "imported": False,
        "addresses": [],
        "balance_doge": None,
        "names": [],
        "tokens": [],
        "collections": [],
        "profile": "",
        "empty": _EMPTY,
        "source": "node",
        "reading": _clean_reading({}),
    }


def _node_watch_addresses() -> list[str]:
    """Address strings the Dogecoin wallet already watches. No coins, no scripts."""
    state = _state()

    found: list[str] = []
    contexts = [getattr(state, "messaging", None), getattr(state, "ledger", None)]
    contexts += list(getattr(state, "addons", None) or [])
    for ctx in contexts:
        if ctx is None or not getattr(ctx, "is_doge", False):
            continue
        try:
            with ctx.rpc() as rpc:
                rows = rpc.call("listreceivedbyaddress", 0, True, True) or []
        except Exception:
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            addr = str(row.get("address") or "")
            if _ADDRESS.fullmatch(addr) and addr not in found:
                found.append(addr)
    return found


def node_watch_addresses() -> list[str]:
    """The node's watch-only Dogecoin addresses, or nothing if the node does not answer quickly."""
    box: list[list[str]] = []

    def work() -> None:
        try:
            box.append(_node_watch_addresses())
        except Exception:
            box.append([])

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    thread.join(2.0)
    return list(box[0]) if box else []


_cache: dict[str, tuple[float, object]] = {}


def _cached(key: str, ttl: float):
    import time
    hit = _cache.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    return None


def _store(key: str, value, ttl: float) -> None:
    import time
    _cache[key] = (time.time() + ttl, value)


async def catalog_collections() -> list[dict]:
    """The collections kept in this arcade's database, with floors from its own market (apps.LOCAL)."""
    import asyncio

    from . import apps

    hit = _cached("market-catalog", 120)
    if isinstance(hit, list):
        return hit
    market = getattr(apps.LOCAL, "market", None)
    try:
        rows = (await asyncio.to_thread(market)).get("collections") if market else []
    except Exception:
        rows = []
    rows = rows if isinstance(rows, list) else []
    _store("market-catalog", rows, 120)
    return rows


async def holdings_for(address: str, *, imported: bool) -> dict:
    """Index holdings for one address, labeled from the arcade's collections.

    `imported` is true only when the address is already in the node wallet.
    The catalog names collections. It does not decide what the address owns.
    """
    import asyncio

    address = valid_address(address)
    owned = await asyncio.to_thread(index_owned, address)
    catalog = await catalog_collections()
    return build_holdings(address, owned, catalog, imported=imported)


def public_view(result: dict) -> dict:
    """Drop anything that is not a display field. Defense in case a caller stuffed a wallet row in."""
    if not isinstance(result, dict):
        return empty_holdings()
    cleaned = {key: value for key, value in result.items() if key not in _SPEND_KEYS}
    return cleaned
