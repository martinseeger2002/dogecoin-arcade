"""Restoring without an arcade, on the web (arcade/restore_kit.py, 2026-11-07, SadFrog's side).

    GET /recover          the offline recovery page: one self-contained file to save and open with no network
    GET /restore-kit.js   the renderer the backup page draws each chain's Core commands with (and /recover inlines)

Both are public: neither holds anything of anybody's. The words are typed into the page, offline, and never sent.
"""
from __future__ import annotations

from fastapi.responses import HTMLResponse, Response

from .. import restore_kit

NO_STORE = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}


def register(app, state, versioned=lambda text: text) -> None:
    """`versioned` is app.py's: restore-kit.js imports /coins.js at the fingerprinted address the page itself uses, so a
    page never holds two copies of the module that derives keys."""
    @app.get("/recover", response_class=HTMLResponse)
    def recover_page():
        """The page to save: everything it runs is inside it, and its policy lets it fetch nothing, served or saved."""
        html, policy = restore_kit.build()
        return HTMLResponse(html, headers={**NO_STORE, "Content-Security-Policy": policy + "; frame-ancestors 'none'"})

    @app.get("/restore-kit.js")
    def restore_kit_js():
        return Response(versioned((restore_kit.TEMPLATES / "restore_kit.js").read_text()),
                        media_type="application/javascript", headers=NO_STORE)
