"""A referee: the node a prize pool names to co-sign a claim only for a
verified win (2026-09-29, the operator: "a game-agnostic referee ... so a claim pays
only for a verified win, in any game").

Why a key and not a check. A pool's lots are signed ahead of time and sealed
with the phrase, and the phrase is in the game's own code, so anybody who
reads it can build a claim by hand and send it to the network through no
arcade node at all. A check that runs on nodes stops nobody who skips the
nodes. So a refereed pool keeps its coins and tokens at a two-key address:

    OP_IF   <pool key> OP_CHECKSIGVERIFY <referee key> OP_CHECKSIG
    OP_ELSE <creator key> OP_CHECKSIG
    OP_ENDIF

A claim takes the IF branch: the pool key's signature made ahead of time
(SINGLE|ANYONECANPAY, as every lot), and the referee's, made over the WHOLE
claim (ALL) and only after the replay it was handed wins under the pool's own
judge. ALL is what binds the prize to the wallet that played: the referee
signs a transaction whose every other output pays that wallet.

Closing takes the ELSE branch, with the creator's own wallet key. Not the
pool key: its lot signatures are sealed with a phrase anybody can read, and a
branch the pool key opened alone would open with them.

The referee is one node, named by the pool. Every other node takes a claim and
forwards the replay to it, so only the referee runs judges, and there is no
second engine to disagree with. When it is down, claims wait; the prizes stay
where they are.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from .script import OP_CHECKSIG, b58check_encode, hash160
from .txbuild import push

OP_0, OP_1 = 0x00, 0x51
OP_IF, OP_ELSE, OP_ENDIF = 0x63, 0x67, 0x68
OP_CHECKSIGVERIFY = 0xAD
OP_HASH160, OP_EQUAL = 0xA9, 0x87

#: The engine judges run in. Pinned, and said on /r/referee, so a game's author
#: can run their judge in exactly this before inscribing it.
ENGINE = "quickjs==1.19.4"
#: Caps. A judge that needs more is a game that cannot be refereed here.
CPU_SECONDS = 5
MEMORY_BYTES = 64 << 20
INPUTS_BYTES = 1 << 20
JUDGE_BYTES = 256 << 10
#: How long a seed can wait for its run and its claim, unless the pool says
#: (`seed_hours`, 2026-09-29: "each pool set its own"), and the longest a pool
#: may say. How many one wallet may hold open for one pool at once.
SEED_SECONDS = 2 * 3600
SEED_HOURS_MOST = 720
SEEDS_OPEN = 20


def seed_seconds(ref: dict) -> int:
    """How long a pool's seeds last: its own `seed_hours`, or two hours."""
    try:
        hours = float(ref.get("seed_hours") or 0)
    except (TypeError, ValueError):
        hours = 0
    if not hours > 0:
        return SEED_SECONDS
    return int(min(hours, SEED_HOURS_MOST) * 3600)
#: Replays one wallet may have judged in a minute.
JUDGED_PER_MINUTE = 3


class RefereeError(Exception):
    """A claim the referee will not sign. The message is shown to the player."""


# --- the script -------------------------------------------------------------------

def redeem_script(pool_pub: bytes, referee_pub: bytes, creator_pub: bytes) -> bytes:
    for name, key in (("pool", pool_pub), ("referee", referee_pub),
                      ("creator", creator_pub)):
        if len(key) != 33 or key[0] not in (2, 3):
            raise ValueError(f"the {name} key is not a compressed public key")
    return (bytes([OP_IF]) + push(pool_pub) + bytes([OP_CHECKSIGVERIFY])
            + push(referee_pub) + bytes([OP_CHECKSIG, OP_ELSE]) + push(creator_pub)
            + bytes([OP_CHECKSIG, OP_ENDIF]))


def keys_of(redeem: bytes) -> tuple[bytes, bytes, bytes] | None:
    """(pool, referee, creator) out of a redeem script of exactly that shape."""
    if (len(redeem) != 3 * 34 + 6 or redeem[0] != OP_IF or redeem[1] != 33
            or redeem[35] != OP_CHECKSIGVERIFY or redeem[36] != 33
            or redeem[70:72] != bytes([OP_CHECKSIG, OP_ELSE]) or redeem[72] != 33
            or redeem[106:] != bytes([OP_CHECKSIG, OP_ENDIF])):
        return None
    return redeem[2:35], redeem[37:70], redeem[73:106]


def address_of(redeem: bytes, params: Any) -> str:
    return b58check_encode(params.scripthash_version, hash160(redeem))


def claim_script_sig(pool_sig: bytes, referee_sig: bytes, redeem: bytes) -> bytes:
    """The IF branch: the referee's signature, the pool's, OP_1, the script."""
    return push(referee_sig) + push(pool_sig) + bytes([OP_1]) + push(redeem)


def close_script_sig(creator_sig: bytes, redeem: bytes) -> bytes:
    """The ELSE branch: the creator's signature, OP_0, the script."""
    return push(creator_sig) + bytes([OP_0]) + push(redeem)


# --- the referee's own key ----------------------------------------------------------

class Referee:
    """This node as a referee: its key, the seeds it issued, its judge runs."""

    def __init__(self, home: Path):
        self.home = Path(home)
        self._lock = threading.Lock()
        self._judged: dict[str, list[float]] = {}
        self._key = None

    # The key is made on first use and kept beside the node's other files. It
    # is the one private key a node holds, and it signs nothing but claims its
    # own judge passed.
    def _private(self):
        from cryptography.hazmat.primitives.asymmetric import ec
        if self._key is None:
            path = self.home / "referee.key"
            with self._lock:
                if not path.exists():
                    secret = secrets.randbelow(
                        0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364140) + 1
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "w") as f:
                        f.write(f"{secret:064x}\n")
                secret = int(path.read_text().strip(), 16)
            self._key = ec.derive_private_key(secret, ec.SECP256K1())
        return self._key

    @property
    def pubkey(self) -> bytes:
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        return self._private().public_key().public_bytes(Encoding.X962,
                                                         PublicFormat.CompressedPoint)

    def sign(self, digest: bytes, sighash_type: int = 1) -> bytes:
        """DER, low-S, and the sighash byte: what a scriptSig carries."""
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec, utils
        der = self._private().sign(bytes(digest), ec.ECDSA(utils.Prehashed(hashes.SHA256())))
        r, s = utils.decode_dss_signature(der)
        n = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
        if s > n // 2:
            s = n - s
        return utils.encode_dss_signature(r, s) + bytes([sighash_type])

    # --- seeds ----------------------------------------------------------------------

    def _db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.home / "referee.sqlite", timeout=10)
        conn.execute("CREATE TABLE IF NOT EXISTS seed (seed TEXT PRIMARY KEY, pool TEXT, "
                     "address TEXT, issued REAL, expires REAL, used TEXT DEFAULT '')")
        return conn

    def issue_seed(self, pool: str, address: str, seconds: int = SEED_SECONDS) -> dict:
        now = time.time()
        with self._lock, closing(self._db()) as db, db:
            db.execute("DELETE FROM seed WHERE expires < ? AND used = ''", (now - 86400,))
            open_ = db.execute("SELECT COUNT(*) FROM seed WHERE pool=? AND address=? "
                               "AND used='' AND expires > ?", (pool, address, now)).fetchone()[0]
            if open_ >= SEEDS_OPEN:
                raise RefereeError(f"this wallet already holds {SEEDS_OPEN} unused seeds "
                                   "for this pool; play one of them")
            seed = secrets.token_hex(32)
            db.execute("INSERT INTO seed (seed, pool, address, issued, expires) "
                       "VALUES (?,?,?,?,?)", (seed, pool, address, now, now + seconds))
        return {"seed": seed, "expires": int(now + seconds)}

    def seed_row(self, seed: str) -> dict | None:
        with closing(self._db()) as db:
            row = db.execute("SELECT seed, pool, address, issued, expires, used FROM seed "
                             "WHERE seed=?", (str(seed),)).fetchone()
        if row is None:
            return None
        return dict(zip(("seed", "pool", "address", "issued", "expires", "used"), row))

    def use_seed(self, seed: str, txid: str) -> bool:
        """Spend a seed on the claim it won. False if something got there first."""
        with self._lock, closing(self._db()) as db, db:
            return db.execute("UPDATE seed SET used=? WHERE seed=? AND used=''",
                              (txid or "signed", seed)).rowcount == 1

    def unuse_seed(self, seed: str) -> None:
        """A signed claim the network refused: the seed was never really spent."""
        with self._lock, closing(self._db()) as db, db:
            db.execute("UPDATE seed SET used='' WHERE seed=?", (seed,))

    def pace(self, address: str) -> None:
        now = time.time()
        with self._lock:
            recent = [t for t in self._judged.get(address, []) if now - t < 60]
            if len(recent) >= JUDGED_PER_MINUTE:
                raise RefereeError("that wallet has had its limit of replays judged this "
                                   "minute; try again in a moment")
            self._judged[address] = recent + [now]

    # --- the judge --------------------------------------------------------------------

    def judge(self, source: str, seed: str, inputs: Any, params: Any) -> dict:
        """Run a judge in its own process, and read what it says."""
        return run_judge(source, seed, inputs, params)


def run_judge(source: str, seed: str, inputs: Any, params: Any,
              seconds: float = CPU_SECONDS) -> dict:
    if len(source.encode()) > JUDGE_BYTES:
        raise RefereeError("that judge is larger than a referee runs")
    given = json.dumps(inputs, separators=(",", ":"))
    if len(given) > INPUTS_BYTES:
        raise RefereeError("that replay is larger than a referee reads (1 MB)")
    job = json.dumps({"source": source, "seed": seed, "inputs": given,
                      "params": json.dumps(params if params is not None else {})})
    try:
        done = subprocess.run([sys.executable, "-m", "arcade.refjudge"], input=job,
                              capture_output=True, text=True, timeout=seconds + 5,
                              cwd=str(Path(__file__).resolve().parent.parent))
    except subprocess.TimeoutExpired:
        raise RefereeError("the judge ran too long") from None
    try:
        said = json.loads(done.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise RefereeError("the judge could not be run") from None
    if "error" in said:
        raise RefereeError(said["error"])
    return said


def meets(verdict: dict, require: dict) -> str:
    """Why a verdict falls short of a pool's requirement, or nothing."""
    if require.get("won") and verdict.get("won") is not True:
        return "the replay did not win"
    if "score_min" in require:
        if float(verdict.get("score") or 0) < float(require["score_min"]):
            return f"the replay scored {verdict.get('score')}, under {require['score_min']}"
    return ""


def fingerprint(source: str) -> str:
    return hashlib.sha256(source.encode()).hexdigest()
