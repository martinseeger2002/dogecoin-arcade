"""Restore with the twelve words on any arcade (2026-11-07, SadFrog: "from any core if arcade goes down as well as any
arcade"). wallet.js `_restore` files an account it has never seated only after /auth/login refused it for having no
seat, which login says only once the signature and the arcade's address have checked out; then only when the words'
own address holds an @name on the chain (/r/address)."""
import pathlib
import re

from arcade import accounts

ROOT = pathlib.Path(__file__).resolve().parent.parent
WALLET_JS = (ROOT / "arcade" / "web" / "templates" / "wallet_js.js").read_text()


def _restore_source() -> str:
    return re.search(r"async function _restore\(.*?\n}\n", WALLET_JS, re.S).group(0)


def test_restore_files_only_after_the_no_seat_refusal():
    source = _restore_source()
    said = re.search(r"/(does not hold a seat)/\.test", source)
    assert said, "the restore matches login's no-seat refusal"
    login = pathlib.Path(accounts.__file__).read_text()
    assert said.group(1) in login, "accounts.login still says it in those words"
    no_seat = login.index(said.group(1))
    assert login.index("verify(") < no_seat and login.index("accept(want)") < no_seat, \
        "the no-seat refusal comes after the signature and the address are checked"


def test_restore_files_only_words_whose_address_holds_a_name():
    source = _restore_source()
    lookup, filing = source.index("/r/address/"), source.index("fileHere(")
    assert lookup < filing and "if (!named)" in source[lookup:filing]
    assert "seatWith(wallet, {join: false})" in source, "a seat is never taken by the sign-in itself"
