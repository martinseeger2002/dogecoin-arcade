"""A JSON-RPC endpoint for bots, in Omni Core's vocabulary.

Anyone who has scripted against Omni Core knows `omni_getbalance`,
`omni_getallbalancesforid`, `omni_send` and the rest
(https://github.com/OmniLayer/omnicore/blob/master/src/omnicore/doc/rpc-api.md).
The same names answer here, with the same fields where the index holds them,
so an airdrop bot written for Omni needs a URL change and little else.

Two things are deliberately not Omni's:

* **Sending is two calls.** `omni_send` and the other `omni_send*` methods
  build and sign a transaction and return it decoded -- fee, outputs, txid --
  without broadcasting. `omni_broadcast <txid>` then sends exactly those bytes.
  This is the rule the whole interface follows (D-016: broadcast what was
  shown); a bot gets to see the fee before paying it, and nothing goes out on
  a typo. A bot that wants Omni's one-call behaviour calls both in a row.

* **One endpoint per chain.** Omni Core serves the chain its node is on. The
  arcade indexes both, so `/rpc/main` speaks for mainnet and `/rpc/test` for
  testnet. A bot names its chain in the URL; nothing about the Tokens page's
  own main/test switch leaks into what a bot sees.

Authentication is bitcoind's cookie scheme: the server writes
`~/.dogecoinarcade/rpc.cookie` as `__cookie__:<secret>` (mode 0600) at every
start, and a caller sends that as HTTP basic auth. No password to configure,
nothing to paste into a script, and a browser tab cannot present it -- which
is what keeps this endpoint out of reach of the cross-site posts the forms'
CSRF token guards against.

Error codes are bitcoind's (src/rpc/protocol.h): -32601 unknown method,
-32602 bad arguments, -8 invalid parameter, -5 bad address, -4 wallet error.
"""

from __future__ import annotations

import base64
import inspect
import os
import secrets
import time
from pathlib import Path
from typing import Any, Callable

from fastapi import Request
from fastapi.responses import JSONResponse

from .. import __version__, tokens as tokenlib
from ..ledger import COIN, AmountError, LedgerIndex, parse_amount
from ..state import ECOSYSTEM_MAIN, ECOSYSTEM_TEST

# bitcoind's codes, so a client library that maps them keeps working.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
MISC_ERROR = -1
TYPE_ERROR = -3
WALLET_ERROR = -4
INVALID_ADDRESS = -5
INVALID_PARAMETER = -8

COOKIE_USER = "__cookie__"

#: Prepared-but-unsent transactions kept per process. A bot prepares and
#: broadcasts one at a time (two prepares in a row would pick the same coins),
#: so this only needs to outlast a slow loop, not an airdrop's whole run.
KEEP_PREPARED = 100


class RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# --- the cookie ---------------------------------------------------------------


def write_cookie(path: Path, secret: str) -> None:
    """Write `__cookie__:<secret>` readable by this user alone.

    Created 0600 rather than chmod-ed afterwards, so there is no moment when
    the file exists world-readable.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(f"{COOKIE_USER}:{secret}\n")
    tmp.replace(path)


def read_cookie(path: Path) -> tuple[str, str]:
    """(user, secret) from a cookie file, for clients."""
    text = path.read_text().strip()
    user, _, secret = text.partition(":")
    if not secret:
        raise ValueError(f"{path} does not hold a user:secret cookie")
    return user, secret


def authorised(request: Request, secret: str) -> bool:
    header = request.headers.get("authorization") or ""
    scheme, _, blob = header.partition(" ")
    if scheme.lower() != "basic":
        return False
    try:
        user, _, given = base64.b64decode(blob.strip()).decode().partition(":")
    except Exception:
        return False
    return user == COOKIE_USER and secrets.compare_digest(given, secret)


# --- amounts, Omni style ------------------------------------------------------


def omni_amount(units: int, divisible: bool) -> str:
    """Omni renders every amount as a string: 8 places if divisible, else whole."""
    if not divisible:
        return str(units)
    whole, frac = divmod(units, COIN)
    return f"{whole}.{frac:08d}"


def _int(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise RpcError(TYPE_ERROR, f"{what} must be an integer")
    try:
        return int(value)
    except ValueError:
        raise RpcError(TYPE_ERROR, f"{what} must be an integer") from None


def _str(value: Any, what: str) -> str:
    if not isinstance(value, str):
        raise RpcError(TYPE_ERROR, f"{what} must be a string")
    return value


# --- the methods --------------------------------------------------------------


class OmniRpc:
    """The method table for one chain.

    Every public `omni_*` method (and `help`) is callable over the wire; the
    docstring's first line is its usage, which is what `help` prints and what
    a wrong-arity call is answered with.
    """

    def __init__(self, state: Any, chain: Any):
        self.state = state
        self.chain = chain

    @property
    def index(self) -> LedgerIndex:
        return self.state.token_index(self.chain)

    def _rpc(self):
        return self.chain.rpc()

    # -- dispatch --------------------------------------------------------------

    def methods(self) -> dict[str, Callable]:
        return {name: getattr(self, name) for name in dir(self)
                if name == "help" or name.startswith("omni_")}

    def call(self, method: str, params: Any) -> Any:
        fn = self.methods().get(method)
        if fn is None:
            raise RpcError(METHOD_NOT_FOUND, f"Method not found: {method}")
        if params is None:
            params = []
        if not isinstance(params, (list, dict)):
            raise RpcError(INVALID_REQUEST, "params must be an array or an object")
        try:
            bound = (inspect.signature(fn).bind(*params) if isinstance(params, list)
                     else inspect.signature(fn).bind(**params))
        except TypeError as exc:
            raise RpcError(INVALID_PARAMS, f"{exc}. Usage: {self._usage(fn)}") from None
        try:
            return fn(*bound.args, **bound.kwargs)
        except RpcError:
            raise
        except (tokenlib.TokenError, AmountError) as exc:
            raise RpcError(INVALID_PARAMETER, str(exc)) from exc
        except Exception as exc:
            raise RpcError(MISC_ERROR, f"{exc.__class__.__name__}: {exc}") from exc

    @staticmethod
    def _usage(fn: Callable) -> str:
        return (fn.__doc__ or "").strip().splitlines()[0]

    def help(self, method: str | None = None) -> str:
        """help ( "method" ) -- list the methods, or explain one."""
        table = self.methods()
        if method is not None:
            fn = table.get(_str(method, "method"))
            if fn is None:
                raise RpcError(METHOD_NOT_FOUND, f"Method not found: {method}")
            return inspect.cleandoc(fn.__doc__ or "")
        lines = [self._usage(fn) for _, fn in sorted(table.items())]
        lines.append("")
        lines.append("Sending is two calls: an omni_send* method returns the prepared "
                     "transaction unsent; omni_broadcast <txid> sends it.")
        return "\n".join(lines)

    # -- reading ---------------------------------------------------------------

    def omni_getinfo(self) -> dict[str, Any]:
        """omni_getinfo -- where this index stands on this chain."""
        index = self.index
        status = index.status(node_tip=self.state.ledger_tips.get(self.chain.network))
        stopped = status["stopped"]
        return {
            "arcadeversion": __version__,
            "network": self.chain.network,
            "mainnet": bool(self.chain.is_mainnet),
            "activationblock": status["activation_height"],
            "block": status["indexed_height"],
            "nodeblock": status["node_tip"],
            "behind": status["behind"],
            "current": status["current"],
            "stopped": None if stopped is None else
                       {"block": stopped.height, "reason": stopped.reason},
        }

    def omni_listproperties(self) -> list[dict[str, Any]]:
        """omni_listproperties -- every token on this chain."""
        return [self._property_brief(p) for p in self.index.properties()]

    def omni_getproperty(self, propertyid: Any) -> dict[str, Any]:
        """omni_getproperty propertyid -- one token in full."""
        prop = self._property(propertyid)
        out = self._property_brief(prop)
        out.update({
            "issuer": prop["issuer"],
            "creationtxid": prop["creation_txid"],
            "creationblock": prop["creation_block"],
            "fixedissuance": not prop["managed"],
            "managedissuance": bool(prop["managed"]),
            "totaltokens": omni_amount(prop["total_tokens"], prop["divisible"]),
            "holders": prop["holder_count"],
        })
        return out

    def omni_getbalance(self, address: Any, propertyid: Any) -> dict[str, str]:
        """omni_getbalance "address" propertyid -- what one address holds of one token."""
        prop = self._property(propertyid)
        units = self.index.balance(_str(address, "address"), prop["property_id"])
        return self._balance(units, prop["divisible"])

    def omni_getallbalancesforid(self, propertyid: Any) -> list[dict[str, str]]:
        """omni_getallbalancesforid propertyid -- every holder of one token, largest first."""
        prop = self._property(propertyid)
        return [{"address": h["address"], **self._balance(h["balance"], prop["divisible"])}
                for h in self.index.holders(prop["property_id"])]

    def omni_getallbalancesforaddress(self, address: Any) -> list[dict[str, Any]]:
        """omni_getallbalancesforaddress "address" -- every token one address holds."""
        rows = self.index.balances([_str(address, "address")])
        return [{"propertyid": r["property_id"], "name": r["name"],
                 **self._balance(r["balance"], r["divisible"])} for r in rows]

    def omni_getwalletaddressbalances(self) -> list[dict[str, Any]]:
        """omni_getwalletaddressbalances -- tokens held by each address in this node's wallet."""
        with self._rpc() as rpc:
            owned = _wallet_addresses(rpc)
        by_address: dict[str, list] = {}
        for r in self.index.balances(owned):
            by_address.setdefault(r["address"], []).append(
                {"propertyid": r["property_id"], "name": r["name"],
                 **self._balance(r["balance"], r["divisible"])})
        return [{"address": a, "balances": b} for a, b in by_address.items()]

    def omni_getwalletbalances(self) -> list[dict[str, Any]]:
        """omni_getwalletbalances -- tokens held by this node's wallet, summed per token."""
        with self._rpc() as rpc:
            owned = _wallet_addresses(rpc)
        totals: dict[int, dict[str, Any]] = {}
        for r in self.index.balances(owned):
            entry = totals.setdefault(r["property_id"], {
                "propertyid": r["property_id"], "name": r["name"],
                "divisible": r["divisible"], "units": 0})
            entry["units"] += r["balance"]
        return [{"propertyid": e["propertyid"], "name": e["name"],
                 **self._balance(e["units"], e["divisible"])} for e in totals.values()]

    def omni_gettransaction(self, txid: Any) -> dict[str, Any]:
        """omni_gettransaction "txid" -- one token transaction the index recorded."""
        entry = self.index.transaction(_str(txid, "txid"))
        if entry is None:
            raise RpcError(INVALID_ADDRESS, "the index holds no token transaction with that txid")
        return self._transaction(entry)

    def omni_listtransactions(self, addressfilter: Any = "*", count: Any = 10, skip: Any = 0,
                              startblock: Any = 0, endblock: Any = 999_999_999,
                              ) -> list[dict[str, Any]]:
        """omni_listtransactions ( "addressfilter" count skip startblock endblock ) -- recent token transactions, oldest first; "*" for every address."""
        address = _str(addressfilter, "addressfilter")
        count, skip = _int(count, "count"), _int(skip, "skip")
        start, end = _int(startblock, "startblock"), _int(endblock, "endblock")
        if count < 0 or skip < 0:
            raise RpcError(INVALID_PARAMETER, "count and skip must not be negative")
        rows = self.index.history(address=None if address == "*" else address,
                                  limit=count + skip + 1_000_000)
        rows = [r for r in rows if start <= r["block_height"] <= end]
        chosen = rows[skip:skip + count]                     # newest first
        return [self._transaction(r) for r in reversed(chosen)]

    def omni_listblocktransactions(self, height: Any) -> list[str]:
        """omni_listblocktransactions height -- txids of the token transactions in one block."""
        height = _int(height, "height")
        with self.index.open() as db:
            rows = db.conn.execute(
                "SELECT txid FROM arcade_tx WHERE block_height = ? ORDER BY position",
                (height,)).fetchall()
        return [r["txid"] for r in rows]

    # -- sending: prepare, then broadcast ----------------------------------------

    def omni_send(self, fromaddress: Any, toaddress: Any, propertyid: Any, amount: Any,
                  ) -> dict[str, Any]:
        """omni_send "fromaddress" "toaddress" propertyid "amount" -- prepare a send (unsent until omni_broadcast)."""
        sender = self._own_address(fromaddress)
        to = self._address(toaddress, "toaddress")
        prop, units = self._amount(propertyid, amount)
        held = self.index.balance(sender, prop["property_id"])
        if units > held:
            raise RpcError(INVALID_PARAMETER,
                           f"{sender} holds {omni_amount(held, prop['divisible'])} of "
                           f"token {prop['property_id']}, not "
                           f"{omni_amount(units, prop['divisible'])}")
        return self._prepare("send", sender, tokenlib.send_payload(prop["property_id"], units), to)

    def omni_sendissuancefixed(self, fromaddress: Any, ecosystem: Any, type: Any,
                               previousid: Any, category: Any, subcategory: Any,
                               name: Any, url: Any, data: Any, amount: Any) -> dict[str, Any]:
        """omni_sendissuancefixed "fromaddress" ecosystem type previousid "category" "subcategory" "name" "url" "data" "amount" -- prepare a fixed-supply token (type 1 whole units, 2 divisible; ecosystem 1)."""
        sender = self._own_address(fromaddress)
        divisible = self._divisible(type)
        self._ecosystem(ecosystem)
        _int(previousid, "previousid")
        payload = tokenlib.issuance_payload(
            name=_str(name, "name"), divisible=divisible, managed=False,
            amount=parse_amount(str(amount), divisible),
            test_ecosystem=_int(ecosystem, "ecosystem") == ECOSYSTEM_TEST,
            category=_str(category, "category"), subcategory=_str(subcategory, "subcategory"),
            url=_str(url, "url"), data=_str(data, "data"))
        return self._prepare("create", sender, payload, None)

    def omni_sendissuancemanaged(self, fromaddress: Any, ecosystem: Any, type: Any,
                                 previousid: Any, category: Any, subcategory: Any,
                                 name: Any, url: Any, data: Any) -> dict[str, Any]:
        """omni_sendissuancemanaged "fromaddress" ecosystem type previousid "category" "subcategory" "name" "url" "data" -- prepare a managed-supply token (granted later)."""
        sender = self._own_address(fromaddress)
        divisible = self._divisible(type)
        self._ecosystem(ecosystem)
        _int(previousid, "previousid")
        payload = tokenlib.issuance_payload(
            name=_str(name, "name"), divisible=divisible, managed=True, amount=None,
            test_ecosystem=_int(ecosystem, "ecosystem") == ECOSYSTEM_TEST,
            category=_str(category, "category"), subcategory=_str(subcategory, "subcategory"),
            url=_str(url, "url"), data=_str(data, "data"))
        return self._prepare("create", sender, payload, None)

    def omni_sendgrant(self, fromaddress: Any, toaddress: Any, propertyid: Any, amount: Any,
                       memo: Any = "") -> dict[str, Any]:
        """omni_sendgrant "fromaddress" "toaddress" propertyid "amount" ( "memo" ) -- prepare a grant of new tokens; "" as toaddress grants to the issuer."""
        prop, units = self._amount(propertyid, amount)
        sender = self._issuer(fromaddress, prop)
        to = self._address(toaddress, "toaddress") if _str(toaddress, "toaddress") else None
        if to == sender:
            to = None
        payload = tokenlib.grant_payload(prop["property_id"], units, _str(memo, "memo"))
        return self._prepare("grant", sender, payload, to)

    def omni_sendrevoke(self, fromaddress: Any, propertyid: Any, amount: Any, memo: Any = "",
                        ) -> dict[str, Any]:
        """omni_sendrevoke "fromaddress" propertyid "amount" ( "memo" ) -- prepare a revoke of tokens the issuer holds."""
        prop, units = self._amount(propertyid, amount)
        sender = self._issuer(fromaddress, prop)
        held = self.index.balance(sender, prop["property_id"])
        if units > held:
            raise RpcError(INVALID_PARAMETER,
                           f"the issuer holds {omni_amount(held, prop['divisible'])}, "
                           f"which is all that can be revoked")
        payload = tokenlib.revoke_payload(prop["property_id"], units, _str(memo, "memo"))
        return self._prepare("revoke", sender, payload, None)

    def omni_sendchangeissuer(self, fromaddress: Any, toaddress: Any, propertyid: Any,
                              ) -> dict[str, Any]:
        """omni_sendchangeissuer "fromaddress" "toaddress" propertyid -- prepare handing a token to a new issuer."""
        prop = self._property(propertyid)
        sender = self._issuer(fromaddress, prop)
        to = self._address(toaddress, "toaddress")
        if to == sender:
            raise RpcError(INVALID_PARAMETER, "that address is already the issuer")
        return self._prepare("change issuer", sender,
                             tokenlib.change_issuer_payload(prop["property_id"]), to)

    def omni_broadcast(self, txid: Any) -> str:
        """omni_broadcast "txid" -- send a transaction an omni_send* call prepared; returns the txid."""
        txid = _str(txid, "txid")
        key = (self.chain.network, txid)
        prepared = self.state.prepared_tokens.get(key)
        if prepared is None:
            raise RpcError(INVALID_PARAMETER,
                           "no prepared transaction with that txid: prepare it with an "
                           "omni_send* call first (a restart forgets prepared ones)")
        with self._rpc() as rpc:
            sent = tokenlib.TokenSender(rpc, self.chain.params).broadcast(prepared)
        self.state.prepared_tokens.pop(key, None)
        # The Tokens page says "broadcast, waiting for its block" for these too.
        self.state.pending_tokens.append(
            {"txid": sent, "what": prepared.what, "at": time.time(),
             "network": self.chain.network})
        return sent

    # -- helpers ---------------------------------------------------------------

    def _property(self, propertyid: Any) -> dict[str, Any]:
        pid = _int(propertyid, "propertyid")
        prop = self.index.property(pid)
        if prop is None:
            raise RpcError(INVALID_PARAMETER, f"there is no token {pid}")
        return prop

    def _amount(self, propertyid: Any, amount: Any) -> tuple[dict[str, Any], int]:
        prop = self._property(propertyid)
        if isinstance(amount, bool) or not isinstance(amount, (str, int, float)):
            raise RpcError(TYPE_ERROR, "amount must be a string such as \"12.5\"")
        try:
            units = parse_amount(str(amount), prop["divisible"])
        except AmountError as exc:
            raise RpcError(INVALID_PARAMETER, str(exc)) from None
        return prop, units

    def _address(self, value: Any, what: str) -> str:
        from .app import _check_address
        address = _str(value, what).strip()
        complaint = _check_address(address, mainnet=self.chain.is_mainnet)
        if complaint:
            raise RpcError(INVALID_ADDRESS, f"{what}: {complaint}")
        return address

    def _own_address(self, value: Any) -> str:
        """An address this wallet can sign for; a send from any other is refused early."""
        address = self._address(value, "fromaddress")
        with self._rpc() as rpc:
            info = rpc.call("validateaddress", address)
        if not info.get("ismine"):
            raise RpcError(WALLET_ERROR, f"fromaddress {address} is not in this node's wallet")
        return address

    def _issuer(self, value: Any, prop: dict[str, Any]) -> str:
        sender = self._own_address(value)
        if sender != prop["issuer"]:
            raise RpcError(INVALID_PARAMETER,
                           f"only the issuer ({prop['issuer']}) can do that to token "
                           f"{prop['property_id']}")
        return sender

    @staticmethod
    def _divisible(value: Any) -> bool:
        kind = _int(value, "type")
        if kind not in (1, 2):
            raise RpcError(INVALID_PARAMETER, "type must be 1 (whole units) or 2 (divisible)")
        return kind == 2

    @staticmethod
    def _ecosystem(value: Any) -> None:
        if _int(value, "ecosystem") not in (ECOSYSTEM_MAIN, ECOSYSTEM_TEST):
            raise RpcError(INVALID_PARAMETER, "ecosystem must be 1 (main) or 2 (test)")

    def _prepare(self, what: str, sender: str, payload: bytes, reference: str | None,
                 ) -> dict[str, Any]:
        index = self.index
        if not index.enabled:
            raise RpcError(MISC_ERROR, f"{self.chain.label} has no start block for tokens yet")
        with self._rpc() as rpc:
            prepared = tokenlib.TokenSender(rpc, self.chain.params).prepare(
                sender, payload, reference)
        prepared.what = what
        cache = self.state.prepared_tokens
        cache[(self.chain.network, prepared.txid)] = prepared
        while len(cache) > KEEP_PREPARED:
            del cache[next(iter(cache))]
        return {
            "txid": prepared.txid,
            "hex": prepared.hex,
            "sendingaddress": prepared.sender,
            "referenceaddress": prepared.reference,
            "class": prepared.encoding_class,
            "size": prepared.size,
            "fee": omni_amount(prepared.fee_sats, True),
            "outputscost": omni_amount(prepared.dust_sats, True),
            "total": omni_amount(prepared.total_sats, True),
            "outputs": [{"value": omni_amount(int(round(o["value"] * COIN)), True),
                         "to": o["where"], "change": o["is_change"],
                         "recipient": o["is_recipient"]} for o in prepared.outputs],
            "broadcast": False,
        }

    @staticmethod
    def _balance(units: int, divisible: bool) -> dict[str, str]:
        # `reserved` and `frozen` are Omni's exchange and freeze buckets; the
        # arcade has neither yet, and a client that reads them gets zero.
        zero = omni_amount(0, divisible)
        return {"balance": omni_amount(units, divisible), "reserved": zero, "frozen": zero}

    @staticmethod
    def _property_brief(prop: dict[str, Any]) -> dict[str, Any]:
        return {
            "propertyid": prop["property_id"],
            "name": prop["name"],
            "category": prop["category"],
            "subcategory": prop["subcategory"],
            "data": prop["data"],
            "url": prop["url"],
            "divisible": bool(prop["divisible"]),
        }

    def _transaction(self, entry: dict[str, Any]) -> dict[str, Any]:
        tip = self.state.ledger_tips.get(self.chain.network)
        out: dict[str, Any] = {
            "txid": entry["txid"],
            "sendingaddress": entry["sender"],
            "referenceaddress": entry["reference"],
            "block": entry["block_height"],
            "blockhash": entry.get("block_hash"),
            "blocktime": entry.get("block_time"),
            "positioninblock": entry["position"],
            "confirmations": (tip - entry["block_height"] + 1) if tip is not None else None,
            "version": entry["message_version"],
            "type_int": entry["message_type"],
            "type": entry["type_name"],
            "valid": bool(entry["valid"]),
        }
        if not entry["valid"]:
            out["invalidreason"] = entry["invalid_reason"]
        prop = None
        if entry.get("property_id") is not None:
            prop = self.index.property(entry["property_id"])
        elif entry["message_type"] in (50, 54) and entry["valid"]:
            # A creation names no property id; the engine assigned one.
            with self.index.open() as db:
                row = db.conn.execute("SELECT property_id FROM property WHERE creation_txid = ?",
                                      (entry["txid"],)).fetchone()
            prop = self.index.property(row["property_id"]) if row else None
        if prop is not None:
            out["propertyid"] = prop["property_id"]
            out["propertyname"] = prop["name"]
            out["divisible"] = bool(prop["divisible"])
        elif entry.get("name"):
            out["propertyname"] = entry["name"]
        if entry.get("amount") is not None:
            divisible = bool(prop["divisible"]) if prop else False
            out["amount"] = omni_amount(entry["amount"], divisible)
        return out


def _wallet_addresses(rpc) -> list[str]:
    from .app import _ledger_addresses
    return _ledger_addresses(rpc)


# --- the HTTP side ------------------------------------------------------------


def _error(code: int, message: str, request_id: Any = None) -> dict[str, Any]:
    return {"result": None, "error": {"code": code, "message": message}, "id": request_id}


def _answer(rpc: OmniRpc, item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        return _error(INVALID_REQUEST, "each request must be an object")
    request_id = item.get("id")
    method = item.get("method")
    if not isinstance(method, str):
        return _error(INVALID_REQUEST, "method must be a string", request_id)
    try:
        result = rpc.call(method, item.get("params"))
    except RpcError as exc:
        return _error(exc.code, exc.message, request_id)
    return {"result": result, "error": None, "id": request_id}


def parse_error() -> dict[str, Any]:
    return _error(PARSE_ERROR, "the body is not JSON")


def handle(state: Any, request: Request, parsed: Any, chain: Any) -> JSONResponse:
    """Answer one HTTP request: a single call or a batch, already parsed as JSON."""
    if not authorised(request, state.rpc_secret):
        return JSONResponse(_error(MISC_ERROR, f"unauthorised: send the contents of "
                                   f"{state.rpc_cookie_path} as HTTP basic auth"),
                            status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="dogecoinarcade"'})
    rpc = OmniRpc(state, chain)
    if isinstance(parsed, list):
        return JSONResponse([_answer(rpc, item) for item in parsed])
    return JSONResponse(_answer(rpc, parsed))
