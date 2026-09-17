"""Whether a release is one of ours: the signature over what was published.

The installer and the updater have always checked the archive's SHA-256
against a sum published beside it, and the code that does it says plainly what
that is worth: "it is not a signature, and it cannot be -- whoever could
replace the archive could replace the sum next to it". The sum catches a
truncated download. It catches nothing else, because both halves come from the
same place.

That was a defensible position while a person typed `dogecoinarcade-update` and
could look at what they were installing. It stops being defensible the moment
the machine does it on its own: an automatic updater that trusts the website is
a website that can run code on every machine that ever installed this, with
nobody watching (D-065).

So a release carries a signature now. The manifest -- revision, archive hash,
and when it was published -- is signed with an Ed25519 key that lives on the
publishing machine and nowhere else, and every installation carries the public
half in its own code. Replacing the archive on the site is no longer enough;
the key is needed, and the key is not on the site.

Ed25519 through PyNaCl, which is already a dependency and is libsodium: no
primitive is invented here, and the one construction used is the one that
signs 32 bytes of hash and a number.
"""

from __future__ import annotations

import json
import time
from typing import Any

#: The public half of the release signing key, pinned in the code that checks
#: it. Changing it takes a release signed with the OLD key, which is what makes
#: it a pin rather than a suggestion: an attacker who can write to the website
#: cannot hand out a new public key, because every installed copy already has
#: this one.
PUBLIC_KEY = "9c955cd5c945d72775f2fa8592b13f5098285d1205edd0be2cca11591b2a008a"

#: What a signed manifest is called beside the archive it describes.
MANIFEST = "source.manifest.json"


class ReleaseError(Exception):
    """A release that is not ours, or not readable."""


def sign(secret_key_hex: str, revision: str, sha256: str,
         published: float | None = None) -> dict[str, Any]:
    """The signed manifest for one release. Run on the publishing machine."""
    from nacl.signing import SigningKey

    body = _body(revision, sha256, float(published if published is not None else time.time()))
    signer = SigningKey(bytes.fromhex(secret_key_hex))
    signature = signer.sign(_canonical(body)).signature
    return {**body, "signature": signature.hex(),
            "public_key": signer.verify_key.encode().hex()}


def verify(manifest: str | bytes | dict, public_key: str | None = None) -> dict[str, Any]:
    """The manifest's contents if it is signed by the key we pin, else raise.

    Fails closed on everything: no manifest, no signature, a signature by
    another key, a body that does not match. An update that cannot tell whose
    code it is about to install must not install it.
    """
    from nacl.exceptions import BadSignatureError
    from nacl.signing import VerifyKey

    key = (public_key if public_key is not None else PUBLIC_KEY) or ""
    if not key:
        raise ReleaseError(
            "this installation has no release signing key pinned, so it cannot "
            "tell whose code it is about to install")
    if isinstance(manifest, (str, bytes)):
        try:
            manifest = json.loads(manifest)
        except ValueError as exc:
            raise ReleaseError(f"the release manifest is not readable: {exc}") from None
    if not isinstance(manifest, dict):
        raise ReleaseError("the release manifest is not an object")

    try:
        body = _body(str(manifest["revision"]), str(manifest["sha256"]),
                     float(manifest["published"]))
        signature = bytes.fromhex(str(manifest["signature"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ReleaseError(f"the release manifest is incomplete: {exc}") from None

    try:
        VerifyKey(bytes.fromhex(key)).verify(_canonical(body), signature)
    except BadSignatureError:
        raise ReleaseError(
            "the release manifest is not signed by the key this installation "
            "trusts. Either it was not published by DogecoinArcade, or the "
            "signing key has changed and this copy is too old to know it. "
            "Install again from https://dogecoinarcade.com and read what it "
            "says before you do") from None
    except (ValueError, TypeError) as exc:
        raise ReleaseError(f"the release signature is not readable: {exc}") from None
    return body


def is_newer(manifest: dict[str, Any], installed_published: float | None) -> bool:
    """Whether this manifest is newer than the one already installed.

    An attacker who can serve files but cannot sign can still serve an OLD
    signed release -- one whose bug they know. Refusing to go backwards is
    what stops that, and `published` is inside the signature, so the timestamp
    cannot be edited to get around it.
    """
    if installed_published is None:
        return True
    return float(manifest["published"]) > float(installed_published)


def _body(revision: str, sha256: str, published: float) -> dict[str, Any]:
    return {"revision": revision, "sha256": sha256, "published": published}


def _canonical(body: dict[str, Any]) -> bytes:
    """The exact bytes that are signed. Sorted keys, no spaces, no float
    surprises: a signature over "whatever json.dumps did today" is a signature
    over nothing."""
    return json.dumps({"revision": str(body["revision"]),
                       "sha256": str(body["sha256"]),
                       "published": f"{float(body['published']):.3f}"},
                      sort_keys=True, separators=(",", ":")).encode("utf-8")
