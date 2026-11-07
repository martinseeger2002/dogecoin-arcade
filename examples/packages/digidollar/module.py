"""DigiDollar, a node module for ARCADE Lite: DigiByte's stablecoin, read through the arcade's own DigiByte Cores, and
minted and redeemed from the caller's own wallet.

It holds no keys. It reads the Cores through the arcade (only the reads its manifest names), and a mint or a redeem
comes back to the arcade as a prepared transaction: the arcade checks every coin in it is the caller's, and the
caller's wallet shows it and signs it.

A mint spends ordinary DGB coins, which the wallet signs as any send. A redeem spends the vault by its script path and
the DigiDollars by their key path, both Taproot: the wallet signs those with Schnorr (BIP-340), checking first that each
is its own key's. `ddgather` puts many small coins into one first, as a mint may spend only a few.

Every command takes the chain, test (testnet26) unless main is said. Mainnet mints wait for the arcade's operator:
`ddmainnet on yes` (the README says what to check first).
"""
from __future__ import annotations

import html
import json
import time
from decimal import Decimal, InvalidOperation

from lib import taproot as tr
# DigiDollar's addresses, its record, the fee and a send: one file, the full Wallet's too (lib/ddtx.py).
from lib import ddtx
from lib.ddtx import COIN, DUST, MAIN, TEST, dd_address, dd_key, send

#: DigiByte mainnet and its test chain, testnet26, as this arcade names them (ddtx.MAIN, ddtx.TEST).
CHAINS = {"main": MAIN, "dgb": MAIN, "mainnet": MAIN, "test": TEST, "dgbtest": TEST, "testnet": TEST}
#: A DigiDollar transaction's version: the "DD" marker 0x0770 with its type in the top byte (primitives/transaction.h):
#: 1 a mint, 2 a send, 3 a redeem.
MINT_VERSION = (1 << 24) | 0x0770
REDEEM_VERSION = (3 << 24) | 0x0770
#: Blocks between building a mint and its lock (consensus/digidollar.h MINT_LOCK_CONFIRMATION_BUFFER_BLOCKS): a mint
#: must be in a block within this many blocks of being built.
BUFFER = 100
BLOCKS_PER_DAY = 5760
#: The sequence of an input that waits for the transaction's lock time (a vault's CHECKLOCKTIMEVERIFY).
WAITS = 0xFFFFFFFE
#: A gather is an ordinary send to yourself: twice DigiByte Core's relay fee (0.001 DGB a kB), and at most this many
#: coins (the arcade's own limit on a module's spend).
GATHER_FEE_PER_BYTE, GATHER_MOST = 200, 50
#: What DigiByte Core takes in one mint (consensus: $100 to $100,000), in cents.
LEAST_MINT, MOST_MINT = 10_000, 10_000_000


def _chain(name) -> str:
    try:
        return CHAINS[str(name or "test").lower()]
    except KeyError:
        raise ValueError(f"{name} is not a DigiByte chain here: main or test") from None


def _word(net: str) -> str:
    return "main" if net == MAIN else "test"


def _minting(reason) -> str:
    """Core's reason minting is held back, in words, or "open": it says "none" when nothing holds it back."""
    return "open" if str(reason or "none").lower() == "none" else "held back: " + str(reason).replace("_", " ")


def _cents(amount) -> int:
    try:
        dollars = Decimal(str(amount))
    except InvalidOperation:
        raise ValueError(f"{amount} is not an amount of dollars, like 100 or 12.50") from None
    if dollars <= 0 or dollars != dollars.quantize(Decimal("0.01")):
        raise ValueError("an amount of dollars and cents, more than zero")
    return int(dollars * 100)


def _sats(value) -> int:
    return int(Decimal(str(value)) * COIN)


def _active(ctx, chain: str) -> dict:
    """The Core's chain, refused in plain words while it has not checked DigiDollar's first block: a Core still
    syncing answers every DigiDollar read with "not yet active", whatever its headers say. The height is the one that
    Core gives with its answer: while it catches up, the arcade reads getblockchaininfo from an arcade sharing the
    chain, which is further on."""
    said = ctx.rpc(chain, "getdigidollardeploymentinfo")
    if not said.get("enabled"):
        start, tip = said.get("activation_height", 0), (said.get("thaw_day") or {}).get("tip_height")
        if tip is None:
            raise ValueError(f"DigiDollar is not active on this arcade's DigiByte Core yet (from block {start:,})")
        raise ValueError(f"DigiDollar starts at block {start:,}, and this arcade's DigiByte Core has checked blocks "
                         f"to {tip:,}" + (": it is still syncing" if tip < start else ""))
    return ctx.rpc(chain, "getblockchaininfo")


def command_ddstats(ctx, chain="test"):
    """DigiDollar on this arcade's DigiByte: the oracle's price, the supply and how well it is backed.

    ddstats [main|test]   the test chain (testnet26) unless you say main"""
    net = _chain(chain)
    info = _active(ctx, net)
    price = ctx.rpc(net, "getoracleprice")
    stats = ctx.rpc(net, "getdigidollarstats")
    usd = price.get("price_usd")
    return {"chain": "DigiByte mainnet" if net == MAIN else "DigiByte testnet",
            "block": f"{int(info.get('blocks') or 0):,}",
            "price": f"${float(usd):,.6f} a DGB" if usd is not None else "no price yet",
            "oracle": str(price.get("status") or "unknown") + (", stale" if price.get("is_stale") else ""),
            "supply": f"${(stats.get('total_dd_supply') or 0) / 100:,.2f} in DigiDollars",
            "locked": f"{float(stats.get('total_collateral_dgb') or 0):,.2f} DGB in vaults",
            "health": f"{stats.get('health_percentage')}%, {stats.get('health_status')}",
            "vaults_open": f"{int(stats.get('active_positions') or 0):,}",
            "minting": _minting(stats.get("minting_restricted_reason"))}


def command_ddcollateral(ctx, amount, tier=0, chain="test"):
    """The DGB a mint of this many dollars locks, for this long."""
    net = _chain(chain)
    _active(ctx, net)
    said = ctx.rpc(net, "estimatecollateral", _cents(amount), int(tier))
    return {"dollars": str(amount), "tier": int(tier), "lock_days": said["lock_days"],
            "locks_dgb": said["wallet_collateral_dgb"], "ratio_percent": said["effective_ratio"],
            "minting": _minting(said.get("minting_restricted_reason"))}


def _leaves(owner: bytes, cents: int, lock: int) -> tuple[bytes, bytes]:
    """A vault's two script paths (scripts.cpp CreateNormalRedemptionPath, CreateERRPath): both wait for the lock, and
    both are signed by the owner alone. The second is the one for when the system is under 100% backed."""
    dd = tr.push(tr.num(cents))
    normal = tr.push_int(lock) + bytes([tr.OP_CLTV, tr.OP_DROP, tr.OP_DIGIDOLLAR]) + dd + bytes([tr.OP_DDVERIFY]) \
        + tr.push(owner) + bytes([tr.OP_CHECKSIG])
    emergency = tr.push_int(lock) + bytes([tr.OP_CLTV, tr.OP_DROP]) + tr.push(tr.num(100)) \
        + bytes([tr.OP_CHECKCOLLATERAL, tr.OP_NOT, tr.OP_VERIFY, tr.OP_DIGIDOLLAR]) + dd + bytes([tr.OP_DDVERIFY]) \
        + tr.push(owner) + bytes([tr.OP_CHECKSIG])
    return normal, emergency


def _fresh(ctx, net: str):
    """Whether a coin is still unspent counting transactions waiting for a block (gettxout with the mempool): a coin
    the last Gather spent is passed over, so Gather, Mint and the rest can be pressed again without waiting."""
    return lambda coin: ctx.rpc(net, "gettxout", coin["txid"], coin["vout"], True) is not None


def _lock(height: int, lock_days: int) -> int:
    """A vault's lock height: tier 0's 240 blocks on the test chain, or the days, and the mint's buffer."""
    return height + (240 if lock_days == 0 else lock_days * BLOCKS_PER_DAY) + BUFFER


def mint(owner: bytes, cents: int, tier: int, height: int, lock_days: int, collateral: int,
         coins: list[dict], change_script: bytes, chain: str = TEST, fresh=None) -> dict:
    """The prepared mint: the vault, the DigiDollar output, the "DD" record and the change, paid from `coins`.

    `owner` is the caller's x-only key: it alone can redeem the vault and spend the DigiDollars."""
    lock = _lock(height, lock_days)
    normal, emergency = _leaves(owner, cents, lock)
    vault = tr.p2tr(tr.tweak(tr.NUMS, tr.branch(tr.leaf(normal), tr.leaf(emergency))))
    record = bytes([tr.OP_RETURN]) + tr.push(b"DD") + tr.push(tr.num(1)) + tr.push(tr.num(cents)) \
        + tr.push(tr.num(lock)) + tr.push(tr.num(tier)) + tr.push(owner)
    outputs = [(collateral, vault), (0, tr.p2tr(tr.tweak(owner))), (0, record)]
    chosen, fee, change = ddtx.pay(coins, outputs, collateral, 148, change_script, fresh=fresh)
    if change:
        outputs.append((change, change_script))
    return {"chain": chain, "version": MINT_VERSION, "inputs": [{"txid": c["txid"], "vout": c["vout"]} for c in chosen],
            "outputs": [(v, s.hex()) for v, s in outputs],
            "what": f"mint ${cents / 100:,.2f} DigiDollar, locking {collateral / COIN:,.8f} DGB until block {lock:,}"}


def _holdings(ctx, net: str, owner: bytes) -> list[dict]:
    """The caller's DigiDollar coins: each unspent zero-value Taproot output of their key, with its amount in cents
    read from the record of the transaction that made it."""
    script = tr.p2tr(tr.tweak(owner))
    found = ctx.rpc(net, "scantxoutset", "start", [f"raw({script.hex()})"])
    out = []
    for u in found.get("unspents", []):
        tx = ctx.rpc(net, "getrawtransaction", u["txid"], True)
        rec = ddtx.record(tx)
        if rec is None or u["vout"] not in rec[2]:
            continue
        kind, items, dd = rec
        amounts = ddtx.amounts(kind, items)
        at = dd.index(u["vout"])
        if at < len(amounts):
            out.append({"txid": u["txid"], "vout": u["vout"], "cents": amounts[at], "kind": kind,
                        "record": items, "height": u.get("height")})
    return out


def _vaults(ctx, net: str, owner: bytes, held: list[dict]) -> list[dict]:
    """The caller's vaults still locked: each mint whose own DigiDollar output is still theirs, and each vault this
    arcade prepared for their key (kept in the module's data, as a vault's script can't be worked out from the key
    alone), whose coin is unspent on the chain. So a vault whose dollars were sent on is still found here."""
    out, seen = [], set()
    for coin in held:
        if coin["kind"] != 1 or len(coin["record"]) < 4 or coin["record"][3] != owner:
            continue
        vault = ctx.rpc(net, "gettxout", coin["txid"], 0, True)
        if not vault:
            continue
        seen.add(coin["txid"])
        out.append({"txid": coin["txid"], "cents": tr.read_num(coin["record"][0]),
                    "lock": tr.read_num(coin["record"][1]), "tier": tr.read_num(coin["record"][2]),
                    "collateral": round(float(vault["value"]) * COIN)})
    kept = _kept(ctx).get(net, {}).get(owner.hex(), [])
    if kept:
        found = ctx.rpc(net, "scantxoutset", "start", [f"raw({v['script']})" for v in kept])
        for u in found.get("unspents", []):
            v = next((k for k in kept if k["script"] == u.get("scriptPubKey")), None)
            if v and u["vout"] == 0 and u["txid"] not in seen:
                seen.add(u["txid"])
                out.append({"txid": u["txid"], "cents": v["cents"], "lock": v["lock"], "tier": v["tier"],
                            "collateral": _sats(u["amount"])})
    return out


def _kept(ctx) -> dict:
    try:
        return json.loads((ctx.data / "vaults.json").read_text())
    except (OSError, ValueError, AttributeError):
        return {}


def _keep(ctx, net: str, owner: bytes, vault: dict) -> None:
    """Remember a vault prepared for `owner`, so it is found once its dollars have moved (`_vaults`)."""
    kept = _kept(ctx)
    mine = kept.setdefault(net, {}).setdefault(owner.hex(), [])
    if not any(v["script"] == vault["script"] for v in mine):
        mine.append(vault)
        try:
            (ctx.data / "vaults.json").write_text(json.dumps(kept))
        except (OSError, AttributeError):
            pass


def _owner(ctx, net: str) -> tuple[str, bytes]:
    address = ctx.caller.get("address", {}).get(net)
    key = ctx.caller.get("pubkey", {}).get(net)
    if not address:
        raise ValueError("sign in: DigiDollars are paid from and to your own DigiByte wallet")
    if not key:
        raise ValueError("open your wallet in this arcade's window first: a DigiDollar names your key as its owner")
    return address, bytes.fromhex(key)[1:]


#: A mined block's coins can be spent once the chain is this many blocks further (consensus.h COINBASE_MATURITY_2).
RIPE = 100


def _coins(ctx, net: str, address: str) -> tuple[list[dict], bytes]:
    """The caller's coins that can be spent now (a mined block's only once ripe), and the script they pay."""
    found = ctx.rpc(net, "scantxoutset", "start", [f"addr({address})"])
    tip = found.get("height", 0)
    ripe = [u for u in found.get("unspents", []) if not u.get("coinbase") or tip - u.get("height", tip) + 1 >= RIPE]
    coins = [{"txid": u["txid"], "vout": u["vout"], "value": _sats(u["amount"])} for u in ripe]
    return coins, (bytes.fromhex(ripe[0]["scriptPubKey"]) if coins else b"")


def _mainnet_on(ctx) -> bool:
    try:
        return bool(json.loads((ctx.data / "mainnet.json").read_text()).get("on"))
    except (OSError, ValueError, AttributeError):
        return False


def _ready(ctx, net: str) -> dict:
    """The chain's Core, checked to be the chain it is said to be, and DigiDollar active on it."""
    info = _active(ctx, net)
    if (info.get("chain") == "main") != (net == MAIN):
        raise ValueError(f"this arcade's {'DigiByte' if net == MAIN else 'DigiByte testnet'} answers as "
                         f"{info.get('chain')}: refused")
    return info


def command_ddmint(ctx, amount, tier=None, chain="test"):
    """Mint DigiDollars from your own DGB: the DGB stays locked in a vault until the tier's time is up.

    ddmint <dollars> [tier] [main|test]   tier 0 is one hour (test chain only), 1 thirty days, up to 9 for ten years
    Your wallet shows the transaction and signs it. On mainnet only once this arcade's operator has turned it on."""
    net = _chain(chain)
    tier = int(0 if tier is None and net == TEST else 1 if tier is None else tier)
    if net == MAIN and not _mainnet_on(ctx):
        raise ValueError("mainnet mints are off on this arcade: its operator turns them on (ddmainnet), once they "
                         "have read what the module's README asks them to check")
    if net == MAIN and tier == 0:
        raise ValueError("tier 0, one hour, is for the test chain: a mainnet vault locks 30 days (tier 1) or longer")
    if not 0 <= tier <= 9:
        raise ValueError("a tier is 0 to 9")
    cents = _cents(amount)
    if not LEAST_MINT <= cents <= MOST_MINT:
        raise ValueError("a mint is $100 to $100,000")
    _ready(ctx, net)
    address, owner = _owner(ctx, net)
    said = ctx.rpc(net, "estimatecollateral", cents, tier)
    if said.get("minting_restricted"):
        raise ValueError(f"minting is closed now: {said.get('minting_restricted_reason')}")
    coins, script = _coins(ctx, net, address)
    height = ctx.rpc(net, "getblockcount")
    made = mint(owner, cents, tier, height, int(said["lock_days"]), _sats(said["wallet_collateral_dgb"]), coins,
                script, net, _fresh(ctx, net))
    _keep(ctx, net, owner, {"script": made["outputs"][0][1], "cents": cents, "tier": tier,
                            "lock": _lock(height, int(said["lock_days"]))})
    return {"prepared": made, "actions": [{"label": "My vaults", "line": f"ddvaults {_word(net)}"}]}


def gather(coins: list[dict], script: bytes, chain: str = TEST, fresh=None) -> dict:
    """The prepared gather: up to GATHER_MOST of the caller's smallest coins into one, back to their own address. With
    `fresh`, coins a gather still waiting for its block has spent are passed over, so it can be pressed again at once."""
    chosen, waiting = [], 0
    for coin in sorted(coins, key=lambda c: c["value"]):
        if len(chosen) == GATHER_MOST:
            break
        if fresh is not None and not fresh(coin):
            waiting += 1
            continue
        chosen.append(coin)
    if len(chosen) < 2:
        raise ValueError("your DGB is in one coin already: nothing to gather" if not waiting else
                         "the rest of your coins are in gathers waiting for a block: once it comes, they are one each")
    total = sum(c["value"] for c in chosen)
    fee = (10 + 148 * len(chosen) + 9 + len(script)) * GATHER_FEE_PER_BYTE
    if total - fee < DUST:
        raise ValueError("those coins are worth less than gathering them costs")
    left = len(coins) - len(chosen) - waiting
    return {"chain": chain, "version": 2, "inputs": [{"txid": c["txid"], "vout": c["vout"]} for c in chosen],
            "outputs": [(total - fee, script.hex())],
            "what": f"gather {len(chosen)} of your coins into one ({(total - fee) / COIN:,.8f} DGB) at your own "
                    f"address" + (f"; about {left} more after this: press Gather again, no need to wait" if left > 0
                                  else "")}


def command_ddgather(ctx, chain="test"):
    """Put many small coins of yours into one, so a mint or a redeem can pay from it.

    ddgather [main|test]   up to 50 coins at a time, back to your own address; your wallet signs it"""
    net = _chain(chain)
    _ready(ctx, net)
    address, _ = _owner(ctx, net)
    coins, script = _coins(ctx, net, address)
    made = gather(coins, script, net, _fresh(ctx, net))
    more = "press Gather again" in made["what"]
    return {"prepared": made, **({"actions": [{"label": "Gather again", "line": f"ddgather {_word(net)}"}]}
                                 if more else {})}


#: Each tier's lock, as the Mint form names it.
TIERS = {0: "1 hour", 1: "30 days", 2: "90 days", 3: "180 days", 4: "1 year", 5: "2 years", 6: "3 years",
         7: "5 years", 8: "7 years", 9: "10 years"}


def command_ddvaults(ctx, chain="test"):
    """Your vaults and what you hold: each vault's dollars, the DGB it locks, when it unlocks, and Redeem once it has.

    ddvaults [main|test]   a vault's id (its first 12 characters) is what ddredeem takes"""
    net = _chain(chain)
    _ready(ctx, net)
    _, owner = _owner(ctx, net)
    height = ctx.rpc(net, "getblockcount")
    held = _holdings(ctx, net, owner)
    have = sum(c["cents"] for c in held)
    vaults = []
    for v in sorted(_vaults(ctx, net, owner, held), key=lambda v: v["lock"]):
        left = v["lock"] - height
        short = v["txid"][:12]
        if left > 0:
            status = f"locked: {left} blocks to go, {_about(left)}"
        elif have < v["cents"]:
            status = (f"unlocked, but its redeem burns ${v['cents'] / 100:,.2f} of DigiDollars and you hold "
                      f"${have / 100:,.2f}: get the rest back first")
        else:
            status = "unlocked: ready to redeem"
        vaults.append({"vault": short, "dollars": f"${v['cents'] / 100:,.2f}",
                       "locked": f"{v['collateral'] / COIN:,.2f} DGB", "lock": TIERS.get(v["tier"], f"tier {v['tier']}"),
                       "unlocks": f"block {v['lock']:,}", "status": status,
                       **({"actions": [{"label": "Redeem", "line": f"ddredeem {short} {_word(net)}"}]}
                          if left <= 0 and have >= v["cents"] else {})})
    return {"you_hold": f"${have / 100:,.2f} in DigiDollars", "chain": "DigiByte mainnet" if net == MAIN else
            "DigiByte testnet", "block": f"{height:,}",
            **({"vaults": vaults} if vaults else {"vaults": "none yet: Mint makes one"})}


def _about(blocks: int) -> str:
    minutes = blocks * 15 // 60
    return f"about {minutes} minutes" if minutes < 120 else f"about {minutes // 60} hours" if minutes < 2880 \
        else f"about {minutes // 1440} days"


def redeem(vault: dict, owner: bytes, held: list[dict], coins: list[dict], pay_script: bytes,
           chain: str = TEST, fresh=None) -> dict:
    """The prepared redeem of `vault`: the vault by its normal path, DigiDollars of the owner's to burn at least what
    it minted, and a coin of theirs for the fee. Every DGB in the vault comes back to `pay_script`, and DigiDollars
    over the vault's amount come back as DigiDollars (validation.cpp ValidateRedemptionTransaction)."""
    normal, emergency = _leaves(owner, vault["cents"], vault["lock"])
    key, parity = tr.tweaked(tr.NUMS, tr.branch(tr.leaf(normal), tr.leaf(emergency)))
    control = bytes([0xC0 | parity]) + tr.NUMS + tr.leaf(emergency)
    burn, burned = [], 0
    for coin in sorted(held, key=lambda c: (c["txid"] != vault["txid"], -c["cents"])):
        if burned >= vault["cents"]:
            break
        burn.append(coin)
        burned += coin["cents"]
    if burned < vault["cents"]:
        raise ValueError(f"this vault minted ${vault['cents'] / 100:,.2f} and you hold ${burned / 100:,.2f} in "
                         f"DigiDollars: a redeem burns at least what the vault minted")
    outputs = [(vault["collateral"], pay_script)]
    if burned > vault["cents"]:
        outputs += [(0, tr.p2tr(tr.tweak(owner))),
                    (0, bytes([tr.OP_RETURN]) + tr.push(b"DD") + tr.push(tr.num(3))
                     + tr.push(tr.num(burned - vault["cents"])))]
    witness = (1 + 65 + 1 + len(normal) + 1 + len(control)) + (1 + 65) * len(burn)
    chosen, fee, change = ddtx.pay(coins, outputs, 0, 148, pay_script, extra=41 * (1 + len(burn)) + witness,
                                   fresh=fresh)
    if change:
        outputs.append((change, pay_script))
    inputs = [{"txid": vault["txid"], "vout": 0, "sequence": WAITS,
               "taproot": {"leaf": normal.hex(), "control": control.hex()}}]
    inputs += [{"txid": c["txid"], "vout": c["vout"], "sequence": WAITS, "taproot": {"key": True}} for c in burn]
    inputs += [{"txid": c["txid"], "vout": c["vout"]} for c in chosen]
    return {"chain": chain, "version": REDEEM_VERSION, "locktime": vault["lock"], "inputs": inputs,
            "outputs": [(v, s.hex()) for v, s in outputs],
            "what": f"redeem ${vault['cents'] / 100:,.2f} DigiDollar: burn ${burned / 100:,.2f} and take back "
                    f"{vault['collateral'] / COIN:,.8f} DGB from its vault"}


def command_ddredeem(ctx, vault, chain="test"):
    """Redeem a vault once it unlocks: burn the DigiDollars it minted and take back all its DGB.

    ddredeem <vault> [main|test]   the vault's transaction id, from ddvaults
    Your wallet shows the transaction and signs it (with Schnorr, as DigiDollar's Taproot coins need)."""
    net = _chain(chain)
    _ready(ctx, net)
    address, owner = _owner(ctx, net)
    held = _holdings(ctx, net, owner)
    found = [v for v in _vaults(ctx, net, owner, held) if v["txid"].startswith(str(vault).lower())]
    if len(found) != 1:
        raise ValueError("that is not one of your locked vaults: ddvaults lists them" if not found else
                         "more than one of your vaults starts like that: give more of its id")
    height = ctx.rpc(net, "getblockcount")
    if height < found[0]["lock"]:
        raise ValueError(f"that vault unlocks at block {found[0]['lock']:,} and the chain is at {height:,}: "
                         f"{found[0]['lock'] - height} blocks more, {_about(found[0]['lock'] - height)}")
    coins, script = _coins(ctx, net, address)
    if not coins:
        raise ValueError("a redeem pays its fee (about 0.15 DGB) from your own DGB, and your wallet has none here")
    return {"prepared": redeem(found[0], owner, held, coins, script, net, _fresh(ctx, net)),
            "actions": [{"label": "My vaults", "line": f"ddvaults {_word(net)}"}]}


def command_ddreceive(ctx, chain="test"):
    """Your DigiDollar address, to be paid at, and the DigiDollars you hold.

    ddreceive [main|test]   anyone may send DigiDollars to it; it is your wallet's key, on that chain"""
    net = _chain(chain)
    _ready(ctx, net)
    _, owner = _owner(ctx, net)
    held = _holdings(ctx, net, owner)
    return {"chain": _word(net), "address": dd_address(net, owner),
            "digidollars": sum(c["cents"] for c in held) / 100,
            "coins": [{"txid": c["txid"], "vout": c["vout"], "dollars": c["cents"] / 100,
                       "from": {1: "a mint", 2: "a send", 3: "a redeem's change"}.get(c["kind"], "")} for c in held]}


def command_ddsend(ctx, address, amount, chain="test"):
    """Send DigiDollars to a DigiDollar address (DD... on mainnet, TD... on the test chain).

    ddsend <address> <dollars> [main|test]   your wallet shows the transaction and signs it"""
    net = _chain(chain)
    if net == MAIN and not _mainnet_on(ctx):
        raise ValueError("mainnet DigiDollar sends are off on this arcade: its operator turns them on (ddmainnet)")
    key = dd_key(net, address)
    cents = _cents(amount)
    _ready(ctx, net)
    pay_address, owner = _owner(ctx, net)
    held = _holdings(ctx, net, owner)
    coins, script = _coins(ctx, net, pay_address)
    if not coins:
        raise ValueError("a DigiDollar send pays its fee (about 0.15 DGB) from your own DGB, and your wallet has none here")
    return {"prepared": send(owner, held, coins, script, key, cents, net, str(address).strip(), _fresh(ctx, net))}


def command_ddmainnet(ctx, state="status", confirm=""):
    """Whether this arcade's people may mint and send DigiDollars on DigiByte mainnet, with real DGB (operator only).

    ddmainnet [status|on|off] [yes]   on needs yes; off at once. Off until turned on."""
    if ctx.caller.get("audience") != "operator":
        raise ValueError("only this arcade's operator turns mainnet mints on or off")
    state = str(state or "status").lower()
    path = ctx.data / "mainnet.json"
    if state == "on":
        if str(confirm).lower() != "yes":
            raise ValueError("mainnet mints lock real DGB until their tier's time is up (30 days or more), and sends "
                             "move real dollars. Read the module's README first, then: ddmainnet on yes")
        path.write_text(json.dumps({"on": True, "since": int(time.time())}))
    elif state == "off":
        path.unlink(missing_ok=True)
    elif state != "status":
        raise ValueError("ddmainnet status, ddmainnet on yes, or ddmainnet off")
    on = _mainnet_on(ctx)
    return {"mainnet_mints": "on" if on else "off",
            "means": "people here may mint and send DigiDollars on DigiByte mainnet with their own DGB" if on else
                     "mints and sends on DigiByte mainnet are refused here; the test chain is open"}


def page(ctx, method, path, query, body):
    """/m/digidollar/: both chains' numbers, side by side, in the window's own look (its theme too: /lite/theme.css),
    and how the controls above it go."""
    cards = ""
    for chain in ("test", "main"):
        try:
            stats = command_ddstats(ctx, chain)
            rows = "".join(f"<dt>{html.escape(k.replace('_', ' ').capitalize())}</dt><dd>{html.escape(str(v))}</dd>"
                           for k, v in stats.items() if k != "chain")
            title = stats["chain"]
        except Exception as exc:                           # noqa: BLE001 -- the page says what the Core said
            title = "DigiByte mainnet" if chain == "main" else "DigiByte testnet"
            rows = f"<dt>Its Core</dt><dd>did not answer: {html.escape(str(exc))}</dd>"
        cards += f"<fieldset><legend>{html.escape(title)}</legend><dl>{rows}</dl></fieldset>"
    mainnet = "open on this arcade" if _mainnet_on(ctx) else "off on this arcade until its operator turns them on"
    return {"type": "text/html", "body": PAGE.format(cards=cards, mainnet=mainnet)}


PAGE = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>DigiDollar</title><link rel="stylesheet" href="/m/digidollar/files/page.css">
<link rel="stylesheet" href="/lite/theme.css">
<body data-lite>
<div class="cards">{cards}</div>
<fieldset><legend>How it goes</legend>
<p class="lead">Lock DGB in a vault and get DigiDollars for it; burn them later to take every DGB back.</p>
<ol><li><b>Cost:</b> what a mint locks, for each lock time.</li>
<li><b>Gather:</b> many small coins into one; a mint spends only a few. Press it again at once for the rest.</li>
<li><b>Mint:</b> your wallet shows the transaction and signs it.</li>
<li><b>My vaults:</b> each vault, and when it unlocks; Redeem appears once it has.</li>
<li><b>Receive</b> and <b>Send:</b> your DigiDollar address (TD... on the test chain, DD... on mainnet), and paying
someone's.</li></ol>
<p class="muted">The same in the console: <code>ddcollateral 100 0</code>, <code>ddgather</code>,
<code>ddmint 100 0</code>, <code>ddvaults</code>, <code>ddredeem &lt;vault&gt;</code>, <code>ddreceive</code>,
<code>ddsend &lt;address&gt; &lt;dollars&gt;</code>. Each takes <code>main</code> or <code>test</code> last. Mainnet mints and
sends are {mainnet}.</p></fieldset>
</body>"""
