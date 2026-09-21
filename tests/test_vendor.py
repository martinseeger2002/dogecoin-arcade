"""The two vendored libraries: that they are the ones that were checked.

Nobody reads a 24 KB file twice. A substitution would be invisible from
then on, which is exactly the failure this pins shut -- and it is the only
third-party JavaScript in the application, so the list is short enough to
name file by file.
"""

import hashlib
import pathlib

VENDOR = pathlib.Path("arcade/web/vendor")

#: Checked on 2026-09-21 three ways that all had to agree: two independent
#: CDNs byte-identical, npm's own dist.integrity matching the tarball, and
#: the file inside that tarball identical to what was fetched. See
#: vendor/PROVENANCE.md.
PINNED = {
    "noble-secp256k1.js":
        "fb2f5bc4f05fca63bbeec3c28d1ed1e928e5d6790efb86aabe3edc21cc685a2d",
    "hashes/ripemd160.js":
        "cda421868b8afda4758185c3b8e18f2e92c9d8c58c64cd643b10196caa779f4e",
    "hashes/_md.js":
        "a68964dae10647550e342326b431100c649387dbe23ec4d3799a1dc1f84fbe51",
    # Patched: one bare import specifier made relative, so a browser can
    # resolve it without an import map. PROVENANCE.md carries the upstream
    # sum, the diff, and why. Everything else in this file is untouched.
    "hashes/utils.js":
        "a154f178738af5b790d54b8934831333b83264b4a6d36b307d8fb0a15a5fa3ed",
    "hashes/crypto.js":
        "9211d026c5d21e60e0126dd6f01150d87da5ba7261b8f468215c1264372ff5a5",
    "hashes/_assert.js":
        "b32ab2fad690f26bccc440ee4251879fd327871ded8ee22e344e0b985f121f7d",
    # Blake2b, for the sealed box's nonce. Same tarball as ripemd160.
    "hashes/blake2b.js":
        "7fa3427c037c34e2f12de9b766465a51b3f1e5a41df5441bca7be506b2fecf70",
    "hashes/_blake.js":
        "62f995fcf0a2c722b31443a5c9ad64c5cc61610b5d75db0626e655efabf2dcbc",
    "hashes/_u64.js":
        "25a28c13f59b354b981f44c647a35e69df7775fbae5e549d966f7b1d519ea22e",
    # X25519 and XSalsa20-Poly1305, for the box itself.
    "tweetnacl.js":
        "6bcd37a3b20dce913f82d4b23e4e2b661058b4b953df8a3f8c45d56ac4f72447",
}


def test_every_vendored_file_is_the_one_that_was_checked():
    for name, digest in PINNED.items():
        path = VENDOR / name
        assert path.exists(), f"{name} is gone"
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        assert actual == digest, (
            f"{name} is not the file that was checked. If this is a "
            f"deliberate upgrade: fetch it, check it the three ways in "
            f"vendor/PROVENANCE.md, update the sum here, and say in the "
            f"commit what changed. If it is not, something replaced the "
            f"cryptography in this wallet.")


def test_nothing_else_crept_in():
    """The list is the whole of it. A third library appearing without a
    line here is a dependency nobody decided to take."""
    found = {str(p.relative_to(VENDOR)) for p in VENDOR.rglob("*.js")}
    assert found == set(PINNED), f"unpinned: {found - set(PINNED)}"


def test_the_vendored_set_is_the_whole_import_graph():
    """Every relative import resolves to a file that is here.

    The first cut of this directory was chosen by listing the files that
    looked relevant, and it missed `_assert.js` -- so the browser failed at
    load with "error loading dynamically imported module" and nothing else.
    A set chosen by reading is a set with something missing in it; this
    follows the imports instead.
    """
    import re

    missing = []
    for name in PINNED:
        text = (VENDOR / name).read_text()
        here = pathlib.Path(name).parent
        for spec in re.findall(r"""from\s+['"]([^'"]+)['"]""", text):
            if not spec.startswith("."):
                missing.append(f"{name} imports the bare specifier {spec}")
                continue
            target = (VENDOR / here / spec).resolve()
            if not target.is_file():
                missing.append(f"{name} imports {spec}, which is not vendored")
    assert missing == [], missing


def test_the_only_patch_is_the_one_that_is_documented():
    """One line, and it is named in PROVENANCE.md. A vendored file that
    quietly differs from upstream is the thing the sums exist to prevent,
    so the difference is written down rather than merely allowed."""
    utils = (VENDOR / "hashes/utils.js").read_text()
    assert "'@noble/hashes/crypto'" not in utils, "the bare specifier is gone"
    assert "'./crypto.js'" in utils, "and names the file beside it"
    said = (VENDOR / "PROVENANCE.md").read_text()
    assert "64cf850dde3aa9e5bd335904d3a326f971487159e43419a511240651c26f9444" in said, \
        "the upstream sum has to stay written down, or the patch is unverifiable"


def test_the_provenance_is_written_down():
    said = (VENDOR / "PROVENANCE.md").read_text()
    for digest in PINNED.values():
        assert digest in said, "a file is pinned in the test and not explained"
    assert "jsdelivr" in said and "unpkg" in said, "how it was checked"
