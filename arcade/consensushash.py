"""The consensus hash.

A single SHA-256 over pipe-delimited records in a strictly defined order. Two
independent implementations that agree on state produce the same hash; if they
disagree, the hash tells you *that* they disagree, and the per-section digests
below help narrow down *where*.

This matters more for Arcade than it does for Omni. Omni had a reference node to
compare against from day one. Arcade has none (docs/DECISIONS.md D-002), so this
hash is the mechanism by which a future second implementation -- ours or someone
else's, which is why the spec is public (D-006) -- can ever prove us right or
wrong.

Layout mirrors omnicore/src/omnicore/consensushash.cpp:148-270 exactly, including
the orderings and the skip rules, which are the easiest part to get subtly wrong.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from .db import Database

ECOSYSTEM_MAIN = 1
ECOSYSTEM_TEST = 2


@dataclass
class ConsensusBreakdown:
    """Per-section digests, for locating a disagreement rather than just detecting one."""

    balances: str = ""
    dex_offers: str = ""
    dex_accepts: str = ""
    metadex_trades: str = ""
    crowdsales: str = ""
    properties: str = ""
    total: str = ""
    records: list[str] = field(default_factory=list)


def _section_digest(records: list[str]) -> str:
    hasher = hashlib.sha256()
    for record in records:
        hasher.update(record.encode("ascii"))
    return hasher.hexdigest()


def balance_records(db: Database) -> list[str]:
    """`address|propertyid|balance|selloffer_reserve|accept_reserve|metadex_reserve`

    Ordered by address lexicographically, then property id ascending -- Omni
    copies its unordered_map into a std::map before iterating
    (consensushash.cpp:159-166), which is precisely a lexicographic sort.

    Rows whose four buckets are ALL zero are skipped (consensushash.cpp:55). This
    is load-bearing: a balance that has been fully spent must hash identically to
    one that never existed, or two implementations that prune differently will
    disagree.

    Note PENDING is excluded -- it is a wallet concept, not consensus state.
    """
    rows = db.conn.execute(
        "SELECT address, property_id, balance, selloffer_reserve, accept_reserve, "
        "metadex_reserve FROM balance ORDER BY address, property_id"
    ).fetchall()
    records = []
    for row in rows:
        if not (
            row["balance"]
            or row["selloffer_reserve"]
            or row["accept_reserve"]
            or row["metadex_reserve"]
        ):
            continue
        records.append(
            f"{row['address']}|{row['property_id']}|{row['balance']}|"
            f"{row['selloffer_reserve']}|{row['accept_reserve']}|{row['metadex_reserve']}"
        )
    return records


def dex_offer_records(db: Database) -> list[str]:
    """`txid|address|propertyid|offeramount|btcdesired|minfee|timelimit`, ordered by txid.

    Empty until M3. The section exists now so that adding it later cannot change
    the hash of state that contains no offers.
    """
    if not _table_exists(db, "dex_offer"):
        return []
    rows = db.conn.execute(
        "SELECT txid, seller, property_id, offer_amount, native_desired, min_fee, "
        "time_limit FROM dex_offer ORDER BY txid"
    ).fetchall()
    return [
        f"{r['txid']}|{r['seller']}|{r['property_id']}|{r['offer_amount']}|"
        f"{r['native_desired']}|{r['min_fee']}|{r['time_limit']}"
        for r in rows
    ]


def dex_accept_records(db: Database) -> list[str]:
    """`matchedselloffertxid|buyer|acceptamount|acceptamountremaining|acceptblock`.

    Ordered by matched txid, then buyer. Empty until M3.
    """
    if not _table_exists(db, "dex_accept"):
        return []
    rows = db.conn.execute(
        "SELECT matched_txid, buyer, amount, amount_remaining, accept_block "
        "FROM dex_accept ORDER BY matched_txid, buyer"
    ).fetchall()
    return [
        f"{r['matched_txid']}|{r['buyer']}|{r['amount']}|{r['amount_remaining']}|"
        f"{r['accept_block']}"
        for r in rows
    ]


def metadex_records(db: Database) -> list[str]:
    """`txid|address|propertyidforsale|amountforsale|propertyiddesired|amountdesired|amountremaining`.

    Ordered by txid. Empty until M3.
    """
    if not _table_exists(db, "metadex_trade"):
        return []
    rows = db.conn.execute(
        "SELECT txid, address, property_id_for_sale, amount_for_sale, "
        "property_id_desired, amount_desired, amount_remaining "
        "FROM metadex_trade WHERE amount_remaining > 0 ORDER BY txid"
    ).fetchall()
    return [
        f"{r['txid']}|{r['address']}|{r['property_id_for_sale']}|{r['amount_for_sale']}|"
        f"{r['property_id_desired']}|{r['amount_desired']}|{r['amount_remaining']}"
        for r in rows
    ]


def crowdsale_records(db: Database) -> list[str]:
    """Always empty in Arcade.

    Omni hashes open crowdsales here (consensushash.cpp:232-248). Arcade dropped
    crowdsales (D-008), so this section contributes nothing -- and because an
    empty section writes no bytes, re-adding crowdsales later would not change
    the hash of any state that has none. Kept deliberately for that reason.
    """
    return []


def property_records(db: Database) -> list[str]:
    """`propertyid|issueraddress`, ecosystem 1 then 2, property id ascending.

    Only the issuer is hashed, not the metadata: the point is to capture issuer
    changes (type 70), which are consensus state, while a property's name and URL
    are immutable after creation.
    """
    records = []
    for ecosystem in (ECOSYSTEM_MAIN, ECOSYSTEM_TEST):
        rows = db.conn.execute(
            "SELECT property_id, issuer FROM property WHERE ecosystem = ? "
            "ORDER BY property_id",
            (ecosystem,),
        ).fetchall()
        records.extend(f"{r['property_id']}|{r['issuer']}" for r in rows)
    return records


def _table_exists(db: Database, name: str) -> bool:
    return (
        db.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


SECTIONS = (
    ("balances", balance_records),
    ("dex_offers", dex_offer_records),
    ("dex_accepts", dex_accept_records),
    ("metadex_trades", metadex_records),
    ("crowdsales", crowdsale_records),
    ("properties", property_records),
)


def consensus_hash(db: Database) -> str:
    """The consensus hash of current state, as lowercase hex.

    Single SHA-256 over the concatenation of every record, in section order, with
    no separator between records -- each record already ends where the next
    begins because the fields are pipe-delimited and the record set is ordered.
    """
    hasher = hashlib.sha256()
    for _, producer in SECTIONS:
        for record in producer(db):
            hasher.update(record.encode("ascii"))
    return hasher.hexdigest()


def consensus_breakdown(db: Database) -> ConsensusBreakdown:
    """Per-section digests plus every record, for diagnosing a mismatch.

    When two implementations disagree, the total hash tells you only that they
    do. Comparing section digests narrows it to one section, and the record list
    then shows the offending row.
    """
    breakdown = ConsensusBreakdown()
    hasher = hashlib.sha256()
    for name, producer in SECTIONS:
        records = producer(db)
        setattr(breakdown, name, _section_digest(records))
        breakdown.records.extend(records)
        for record in records:
            hasher.update(record.encode("ascii"))
    breakdown.total = hasher.hexdigest()
    return breakdown
