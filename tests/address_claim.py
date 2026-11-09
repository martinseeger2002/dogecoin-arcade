"""Filing an address as the browser does since 2026-11-09 (security review): the first claim of an address carries its
own key's proof that it is this account's on that chain (wallet_imports.check_proof, the proof /signup asks; wallet_js
`claimProof` signs it). For tests that post /account/address or /account/segwit/register themselves."""
from arcade import wallet_imports
from arcade.config import NETWORKS

from signup_proof import coin_key, coin_sign

#: Every address `keyed` made, with its key: file_address and register_segwit find the key here when not given one.
KNOWN: dict = {}


def keyed(network: str, secret: int | None = None, kind: str = "p2pkh", params=None):
    """A key and its address of that kind on `network`: (address, coin), coin being (private key, public key) as
    signup_proof.coin_key makes it. The same secret gives test_funding._pubkey's key."""
    coin = coin_key(secret)
    address = wallet_imports.address_of(params or NETWORKS[network], coin[1], kind)
    KNOWN[address] = coin
    return address, coin


#: The account addresses most tests file, each with a key: a Pepecoin regtest one (version 111) and a mainnet one (56).
TEST, TEST_COIN = keyed("regtest", 0x7e57)
MAIN, MAIN_COIN = keyed("main", 0x3a1)


def proof(account: str, network: str, address: str, coin, *, field: str = "coin_pubkey") -> dict:
    """The fields that prove `address` is `account`'s on `network`: the key (under `field`) and its signature. `coin`
    is (private key, public key), or the secret (an int) to make it from."""
    if isinstance(coin, int):
        coin = coin_key(coin)
    digest = wallet_imports.proof_digest(account, network, address, coin[1].hex())
    return {field: coin[1].hex(), "address_signature": coin_sign(coin[0], digest).hex()}


def _asked(app, chain, headers):
    said = app.get("/account?only=addresses", headers=headers or {}).json()
    return said["pubkey"], chain or next(c["network"] for c in said["chains"] if c["tags"])


def file_address(app, address: str, coin=None, *, chain: str | None = None, headers=None, **extra):
    """POST /account/address for the signed-in account, proved by `coin`. `chain` names the network; without it the
    request names none, as the browser's first sign-up chain does, and the tag chain's network is signed. Without
    `coin`, the key `keyed` made the address with."""
    account, network = _asked(app, chain, headers)
    body = {**extra, "address": address, **({"chain": chain} if chain else {}),
            **proof(account, network, address, KNOWN[address] if coin is None else coin)}
    return app.post("/account/address", json=body, headers=headers or {})


def register_segwit(app, address: str, coin=None, *, chain: str, headers=None, **extra):
    """POST /account/segwit/register for the signed-in account, proved by `coin` (its key travels as `pubkey`)."""
    account, network = _asked(app, chain, headers)
    body = {**extra, "address": address, "chain": chain,
            **proof(account, network, address, KNOWN[address] if coin is None else coin, field="pubkey")}
    return app.post("/account/segwit/register", json=body, headers=headers or {})
