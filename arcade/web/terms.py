"""The terms a public node's visitors accept, or leave (SadFrog's side, 2026-11-02).

Off by default. With `terms_gate` on (Admin, Safety), a browser opening a page from outside is sent to /terms until it
has accepted the text as it stands now. The answer lives in a cookie holding the text's fingerprint, so changing one
word asks everybody again.

**Pages, not requests.** Only a browser opening a page is stopped. Other arcades, the mesh, `git`, wallets and a page's
own fetches ask the same routes and cannot click anything, so they pass (door.public_visitor says who is a visitor).

**What this is.** A node's way of showing its terms and hearing a yes. It is a stated agreement and not an access
control: anybody can set a cookie. It also records nothing about who accepted, only the cookie in their browser.
"""

from __future__ import annotations

import hashlib

from .door import PAGES_DOOR

#: The settings: the switch, and the operator's own words (nothing kept means DEFAULT, so a better default reaches them).
GATE_SETTING = "terms_gate"
TEXT_SETTING = "terms_text"
#: Holds the fingerprint of the terms this browser accepted.
COOKIE = "arcade_terms"
COOKIE_DAYS = 365
#: Longest text Admin keeps. Every gated page view hashes it.
TEXT_MAX = 64_000
#: Never stopped: the terms themselves, and the panel, which keeps its own password, so that whoever runs the node
#: reaches it whatever the terms cookie says.
EXEMPT = ("/terms", "/admin")

#: The template. One paragraph to a line; a line starting with "# " is a heading (blocks). Generic on purpose: it names
#: "the operator" and no one real, and it fits every face, so it says "this site" and never a brand.
DEFAULT = """\
# Terms of use

Please read these terms before you use this site. By choosing Accept you agree to them. If you do not agree, choose \
Leave and do not use this site.

"This site" means this node and everything it serves, including its browsers, explorers, indexes, messages, exchanges, \
wallet tools, apps, games and AI tools. "The operator" means whoever runs this node. "The developers" means everyone \
who has written or contributed to the software it runs.

# Built with care

The developers put real care and effort into making this software work properly and keeping the people who use it \
safe. Its code is open for anyone to read and check, changes are tested and reviewed before they are released, and \
safety checks are built into its wallet, payment and trade tools. The operator runs this node in good faith and works \
to keep it running and secure. These terms take nothing away from that work. They exist because no software is \
perfect, and because this site is an open, shared tool that everyone uses in their own way. Please read them in that \
spirit.

# 1. Experimental software

The developers test this software carefully and keep improving it. However, it is in perpetual testing: it is \
experimental, it will stay that way, and you should treat it that way. Anything here may change, break, give wrong \
results or disappear without notice. Do not rely on it for anything you cannot afford to lose.

# 2. Open source, as is, no warranty

Being open source means anyone can read the code, see how it works, and report or fix a problem, and that openness is \
one of its strengths. However, this site is provided "as is" and "as available". The operator and the developers give \
no warranty of any kind, express or implied, including any warranty of accuracy, reliability, security, \
merchantability, fitness for a particular purpose or non-infringement. What these terms say about the care put into \
this site describes effort, not a promise or a warranty of any result.

# 3. Always cross-check and verify

The developers work to show information that is accurate and up to date. However, indexes, balances, inscriptions, \
tokens, names, prices, listings, transactions, messages and AI output shown here may be wrong, late or incomplete. \
Always cross-check them against other sources, and verify them on the chain yourself, before you rely on them. That \
is good practice with any blockchain tool.

# 4. Your own risk

The operator and the developers want every visit here to go well. However, you use this site entirely at your own \
risk. The operator and the developers claim no responsibility for any result of using it, including any result from \
its browsers, explorers, indexes, messages, exchanges, wallet tools, apps, games or AI tools.

# 5. Freedom of expression and peer-to-peer speech

This site is offered as a tool for freedom of expression and peer-to-peer speech, built so that people can publish and \
talk to each other directly. Messages, posts, inscriptions, pages, apps and other content come from the people who \
publish them and are their own speech. They are not the speech of the operator or the developers, who do not write, \
approve or endorse them, even where this node screens or hides content. You are responsible for what you publish. \
Anything written to a blockchain or a peer-to-peer network may be public and permanent, and neither the operator nor \
the developers can remove it from there.

# 6. Not advice

The tools here are made to help you see clearly and decide for yourself. However, nothing here is financial, \
investment, tax or legal advice, or an offer to buy or sell anything. Make your own decisions and do your own research.

# 7. Your keys, wallets and transactions

The developers design the wallet and trade tools with checks that help you avoid mistakes. However, you alone are \
responsible for your keys, seed words, passwords, wallets and backups. Do not expect the operator or the developers to \
recover them. Blockchain transactions are irreversible: a mistake, a wrong address or a signature you did not \
understand cannot be undone by anyone. Trades and payments between users are between those users.

# 8. Following the law

This software is made for lawful use and free expression. You must follow the laws that apply to you, wherever you \
are, and you are responsible for any taxes you owe. Do not use this site to break the law or to harm anyone. If using \
this site is not lawful where you are, do not use it.

# 9. Age

You must be at least 18 years old, or the age of adulthood where you live if that is higher, to use this site. By \
choosing Accept you confirm that you are.

# 10. Other people's content and links

This site connects you with content, apps, games, nodes and services from many other people, which is part of what \
makes it useful. However, anything from anyone else is not endorsed by the operator or the developers, and they are \
not responsible for it. You open it at your own risk.

# 11. Uptime, data and privacy

The operator works to keep this site available and its data safe. However, there is no guarantee that this site will \
be available, fast or secure, or that any data will be kept. It may stop, lose data or be reset at any time, so keep \
your own backups. This node keeps what it needs to run, which may include technical logs such as network addresses, \
and a cookie in your browser that remembers you accepted these terms.

# 12. What the operator may do

The operator may refuse service to anyone, limit or block access, hide or remove content from this node, and change \
or end this site at any time, for any reason and without notice. These choices help keep this site safe and working \
for everyone.

# 13. Changes to these terms

The operator may change these terms at any time, for example to keep them clear and up to date. When they change you \
will be asked to accept them again before you continue.

# 14. Limit of liability

The operator and the developers take the safety of this site seriously. However, to the fullest extent the law \
allows, they are not liable for any loss or damage of any kind that comes from using this site or from not being able \
to use it. That includes lost coins, tokens, data, profits or opportunities, and direct, indirect, incidental, special \
or consequential damages, even if they were told such a loss was possible. Where the law does not allow a limit, their \
liability is limited to the smallest amount it allows.

# 15. Covering the operator and the developers

You agree to defend and cover the operator and the developers against any claim, loss or cost, including reasonable \
legal fees, that comes from how you used this site, what you published, or your breaking these terms or the law.

# 16. The rest

These terms are the whole agreement between you and the operator about this site, and they are governed by the law \
of the place where the operator is based. If part of them cannot be enforced, the rest still applies. Not enforcing \
a term is not giving it up.

Thank you for reading, and welcome. The operator and the developers hope you enjoy this site.
"""


def clean(words: str) -> str:
    """Text as kept and hashed: the same words from any browser or editor are the same terms."""
    return str(words or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def on(state) -> bool:
    return bool(state.setting(GATE_SETTING, False))


def text(state) -> str:
    """The terms in force: the operator's own, or the template."""
    return clean(state.setting(TEXT_SETTING, "")) or clean(DEFAULT)


def version(words: str) -> str:
    """The fingerprint a browser's yes is kept under. Any change to the words is a new version."""
    return hashlib.sha256(clean(words).encode("utf-8")).hexdigest()[:16]


def blocks(words: str) -> list[tuple[bool, str]]:
    """The text as (is a heading, words), split at blank lines. The page escapes it, so no text can add markup."""
    parts = []
    for block in clean(words).split("\n\n"):
        block = block.strip()
        if block:
            heading = block.startswith("# ") and "\n" not in block
            parts.append((heading, block[2:].strip() if heading else block))
    return parts


def opens_a_page(method: str, path: str, headers) -> bool:
    """Is this a browser opening a page? `Sec-Fetch-Dest` when the browser says (no page can set it, and a frame or a
    fetch says so too). A browser that sends none (plain http, an old Safari) is known by asking for HTML, except for
    what runs in a sandboxed frame: that has no cookie, so it could never get past the terms, and the page around it
    already has. Everything that cannot click Accept is left through: a program, another arcade, a link preview, a
    page's own fetch, which a redirect would break (a game reading /account as an empty save)."""
    if method.upper() != "GET":
        return False
    dest = (headers.get("sec-fetch-dest") or "").strip().lower()
    if dest:
        return dest == "document"
    return "text/html" in (headers.get("accept") or "").lower() and not path.startswith(PAGES_DOOR)


def exempt(path: str) -> bool:
    path = (path or "/").rstrip("/") or "/"
    return any(path == root or path.startswith(root + "/") for root in EXEMPT)


def safe_back(where: str) -> str:
    """Where Accept returns to: a path on this site, never another site and never the terms again."""
    where = str(where or "")
    if (not where.startswith("/") or where.startswith(("//", "/\\")) or len(where) > 2048
            or any(ord(c) < 32 or c == "\\" for c in where) or exempt(where.split("?", 1)[0])):
        return "/"
    return where
