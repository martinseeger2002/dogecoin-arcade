"""The arcade's account API, as one T-Deck's session on it.

The gateway is an ordinary client of the arcade it sits next to: it signs in
with the device's Ed25519 login (the device signs the challenge, the gateway
only carries it) and keeps the `arcade_session` cookie for that device.
Loopback requests do not pass the public door, but every route used here is
in PUBLIC_POST anyway, so a gateway on another machine works the same.
"""
from __future__ import annotations

import httpx


class ArcadeError(Exception):
    """The arcade said no; the text is its own reason."""


class ArcadeSession:
    def __init__(self, base: str, timeout: float = 30.0):
        self.base = base.rstrip("/")
        self.http = httpx.Client(base_url=self.base, timeout=timeout, follow_redirects=False)

    def close(self):
        self.http.close()

    def _call(self, method: str, path: str, **kw) -> dict:
        r = self.http.request(method, path, **kw)
        try:
            said = r.json()
        except ValueError:
            said = {"detail": r.text[:200]}
        if r.status_code >= 400:
            detail = said.get("detail") or said.get("error") or f"HTTP {r.status_code}"
            raise ArcadeError(str(detail))
        return said

    # -- signing in ---------------------------------------------------------
    def challenge(self) -> dict:
        return self._call("GET", "/auth/challenge")

    def login(self, pubkey: bytes, nonce: str, signature: bytes, join: bool = False) -> dict:
        return self._call("POST", "/auth/login", json={
            "pubkey": pubkey.hex(), "nonce": nonce, "signature": signature.hex(), "join": join})

    @property
    def signed_in(self) -> bool:
        return "arcade_session" in self.http.cookies

    def cookie(self) -> str | None:
        return self.http.cookies.get("arcade_session")

    def restore(self, cookie: str):
        """Sign back in with a saved session (30-day sliding) after a restart."""
        host = httpx.URL(self.base).host
        self.http.cookies.set("arcade_session", cookie, domain=host)

    # -- account ------------------------------------------------------------
    def account(self) -> dict:
        return self._call("GET", "/account")

    def tokens(self) -> list[dict]:
        """Tokens this account holds, on every chain (the Wallet's own answer)."""
        return self._call("GET", "/account/tokens")

    def token20(self, network: str) -> list[dict]:
        """DRC-20 / PRC-20 balances on Dogecoin or Pepecoin main."""
        return self._call("GET", "/account/token20", params={"chain": network}).get("balances") or []

    def mesh_page(self, address: str, have: str = "") -> tuple[int, dict]:
        """A .mesh page (the arcade's /r/mesh/<name>[/path]): (HTTP status, answer). Public: no session needed."""
        r = self.http.get("/r/mesh/" + address.lstrip("/"), params={"have": have} if have else None)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {"detail": r.text[:200]}

    def mesh_picture(self, iid: str) -> tuple[int, bytes | dict]:
        """A picture on a .mesh page as a T-Deck draws it (the arcade's /r/mesh/picture/<id>): (HTTP status, its bytes,
        or the answer saying why not). Public."""
        r = self.http.get("/r/mesh/picture/" + iid)
        if r.status_code == 200:
            return 200, r.content
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {"detail": r.text[:200]}

    def coins(self, network: str, spendable: bool = True) -> list[dict]:
        """This account's coins on a chain; `spendable` (the default for a T-Deck, which builds its own transactions):
        only those a transaction may take -- none carrying an inscription, none the arcade could not check."""
        params = {"chain": network}
        if spendable:
            params["spendable"] = 1
        return self._call("GET", "/account/coins", params=params)["coins"]

    def broadcast(self, network: str, raw: bytes) -> dict:
        """A transaction the T-Deck built and signed (its own coins only; the arcade checks)."""
        return self._call("POST", "/account/broadcast", json={"chain": network, "raw": raw.hex()})

    def send(self, network: str, to: str, amount: int) -> dict:
        # The arcade parses amounts as coin text; send it exactly, no float.
        return self._call("POST", "/account/send", json={
            "chain": network, "to": to, "amount": f"{amount // 10**8}.{amount % 10**8:08d}"})

    def sign(self, offer: str, signatures: list[bytes], pubkey: bytes) -> dict:
        return self._call("POST", "/account/sign", json={
            "offer": offer, "signatures": [s.hex() for s in signatures], "pubkey": pubkey.hex()})

    def register(self, network: str, address: str, coin_pubkey: bytes, signature: bytes = b"") -> dict:
        # `signature`: the coin key's proof the address is this account's, which the arcade asks (2026-11-09).
        return self._call("POST", "/account/address", json={
            "chain": network, "address": address, "coin_pubkey": coin_pubkey.hex(),
            "address_signature": signature.hex()})

    def who(self, name: str) -> dict:
        return self._call("GET", f"/account/who/{name.lstrip('@')}")

    def write(self, to_address: str, sealed: bytes) -> dict:
        return self._call("POST", "/account/write", json={"to": to_address, "sealed": sealed.hex()})

    def messages(self, after: str | int | None, limit: int = 200) -> dict:
        params = {"limit": limit}
        if after not in (None, ""):
            params["after"] = after
        return self._call("GET", "/account/messages", params=params)

    def tag_of(self, address: str) -> str:
        """The @tag that holds an address on the tag chain, or ""."""
        try:
            return self._call("GET", f"/r/address/{address}").get("tag") or ""
        except ArcadeError:
            return ""

    def tx_status(self, txid: str) -> dict:
        return self._call("GET", f"/r/tx/{txid}")
