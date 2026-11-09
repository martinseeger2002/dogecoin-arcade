"""The .mesh web's routes (arcade/mesh_web.py; 2026-10-26: a simple "web" browser for the T-Deck, pages a
.mesh name's holder inscribes, "just like original web like in 1990").

* GET /r/mesh                       the directory: every .mesh name with a page that counts, newest first
* GET /r/mesh/<name>[/path]         one page: its MeshText and where it comes from. `?have=<page id>` answers
                                    {"unchanged": true} when the asker's copy is the newest (the T-Deck's cache over LoRa)
* GET /meshweb[/<name>[/path]]      the same pages drawn in a browser at the T-Deck's size, to look at without one
* GET /r/mesh/picture/<id>          a picture on a page, as a T-Deck draws it (arcade/mesh_picture.py)

The Browser (2026-10-26: "Instead of mesh browser call the app browser ... on the PWA or website it should be able
to explore all p:dns names"), every name's site on every chain this arcade reads:

* GET /r/sites                      every site, newest first, each with its chain
* GET /r/site/<name>[/path]         one page, from whichever chain has the name's site (`?chain=` to say which); a name
                                    with a site on more than one chain answers 300 with the `choices`
* GET /site/<chain>/<name>/[path]   an HTML page of a site, sandboxed like an inscription; an inscription page goes to
                                    /content/<id>; a MeshText page is drawn
* GET /browser[/<address>]          the Browser in a web page: the same file the App Store's Browser card is

Answered by the most complete walk of each chain (DoginalIndex.lookup): this arcade's, or a linked arcade's.
"""
from __future__ import annotations

import html
import pathlib
import time
from collections import OrderedDict
from typing import Any
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .. import mesh_picture as MP
from .. import mesh_web as M
from . import content as contentlib

#: How long the directory is kept before it is asked again (every page of it is read off the chain once, then kept).
DIRECTORY_KEEP = 60.0
#: Mesh pictures kept made (an inscription never changes, so neither does its picture).
PICTURES_KEPT = 200
#: The Browser: one file, served at /browser and inscribed as the App Store's Browser card.
BROWSER_FILE = pathlib.Path(__file__).with_name("browser.html")


def register(app, state, content=None) -> None:
    kept: dict[str, Any] = {}
    pictures: OrderedDict[str, bytes] = OrderedDict()

    def index():
        idx = state.ord_index(M.NET)
        if idx is None:
            raise LookupError("this arcade does not read Pepecoin's ords")
        return idx

    def ask(question: dict) -> dict:
        return index().lookup(question)

    def directory() -> list[dict]:
        at, names = kept.get("dir", (0.0, None))
        if names is None or time.time() - at > DIRECTORY_KEEP:
            names = ask({"what": "mesh_directory"}).get("mesh_directory") or []
            kept["dir"] = (time.time(), names)
        return names

    def directory_page() -> dict:
        """The directory as a page of its own, so the T-Deck's browser opens it like any site (its homepage until one
        is set): MeshText, its id the sha256 of what it lists, so an unchanged directory is not sent again."""
        names = directory()
        lines = ["# The .mesh web", "Every .mesh site, newest first.", ""]
        lines += [f"=> {n['name']} {n['name']}" + (f" - {n['title']}" if n.get("title") else "") for n in names[:60]]
        if not names:
            lines.append("Nobody has published a page yet. Claim a .mesh name and publish one from the arcade's "
                         "Creator tab.")
        body = "\n".join(lines)
        return {"name": "directory", "path": "/", "page": "directory-" + M.digest(body.encode())[:16],
                "title": "The .mesh web", "body": body, "paths": ["/"], "size": len(body.encode()), "number": None,
                "holder": None, "claim": None}

    def site(address: str) -> tuple[int, dict]:
        if address.strip("/ ").lower() in ("", "directory", "mesh://directory"):
            return 200, directory_page()
        name, path = M.split_address(address)
        if not name:
            return 400, {"detail": "not a .mesh name", "address": address}
        said = ask({"what": "mesh", "name": name}).get("mesh") or {}
        out: dict[str, Any] = {"name": name, "path": path, "holder": said.get("holder"), "claim": said.get("claim")}
        page = said.get("page")
        if not said.get("claim"):
            return 404, {**out, "detail": f"Nobody has claimed {name}."}
        if not page:
            return 404, {**out, "detail": f"{name} has no page yet."}
        pages = page.get("pages") or {}
        out.update({"page": page["id"], "number": page["number"], "size": page["size"], "sha256": page["sha256"],
                    "paths": sorted(pages), "title": M.title_of(pages.get("/", ""))})
        if path not in pages:
            return 404, {**out, "detail": f"{name} has no page {path}."}
        out["body"] = pages[path]
        out["pictures"] = [iid for iid, _alt in M.pictures_of(pages[path])]
        return 200, out

    # --- every name, every chain (the Browser) --------------------------------------------------------------------

    def indexes(chain: str = "", every: bool = False) -> dict:
        """The indexes the Browser reads (lazy ord indexes, 2026-10-27): the .mesh chain's always, a chain named in the
        address opened for it, and every chain opened only to look up a name given without one (`every`); the
        directory lists what is open."""
        from ..doginal_index import STARTS
        wanted = [chain] if chain else ([c.network for c in getattr(state, "all_chains", ()) if c.network in STARTS]
                                        if every else [])
        for net in wanted:
            state.ord_index(net, wait=0)
        state.ord_index(M.NET)
        for net in wanted:
            state.ord_index(net)
        return dict(state.ord_indexes() or {})

    def all_sites() -> list[dict]:
        at, names = kept.get("all", (0.0, None))
        if names is None or time.time() - at > DIRECTORY_KEEP:
            names = []
            for net, idx in indexes().items():
                try:
                    found = idx.lookup({"what": "mesh_directory", "all": True}).get("mesh_directory") or []
                except Exception:                        # noqa: BLE001 -- a chain not read just now: the others
                    continue
                names += [{**n, "chain": net} for n in found]
            names.sort(key=lambda n: -(n.get("height") or 0))
            kept["all"] = (time.time(), names)
        return names

    def any_site(address: str, chain: str = "") -> tuple[int, dict]:
        name, path = M.split_address(address, any_name=True)
        if not name:
            return 400, {"detail": "not a name: label.ending, like yours.mesh or yours.pepe", "address": address}
        found = []
        nets = indexes(chain, every=not chain and not M.is_mesh(name))
        for net, idx in nets.items():
            if chain and net != chain:
                continue
            try:
                said = idx.lookup({"what": "mesh", "name": name}).get("mesh") or {}
            except Exception:                            # noqa: BLE001
                continue
            if said.get("claim"):
                found.append((net, said))
        with_page = [(n, s) for n, s in found if s.get("page")]
        out: dict[str, Any] = {"name": name, "path": path}
        if len(with_page) > 1:
            return 300, {**out, "detail": f"{name} has a site on more than one chain",
                         "choices": [{"chain": n, "page": s["page"]["id"], "holder": s.get("holder")}
                                     for n, s in with_page]}
        if not found:
            # a mistyped name (2026-10-26: "mee-seeks.mesh" for me-seeks.mesh) is offered the sites it is closest to
            import difflib
            try:
                close = difflib.get_close_matches(name, sorted({n["name"] for n in all_sites()}), n=3, cutoff=0.75)
            except Exception:                            # noqa: BLE001 -- no directory now: no suggestions
                close = []
            # said in the words too: the Browser card already inscribed shows `detail` and nothing else
            return 404, {**out, "detail": f"Nobody has claimed {name}" + (f" on {chain}" if chain else "") + "."
                         + (f" Did you mean {' or '.join(close)}?" if close else ""), "suggest": close}
        net, said = (with_page or found)[0]
        page = said.get("page")
        out.update({"chain": net, "holder": said.get("holder"), "claim": said.get("claim")})
        if not page:
            return 404, {**out, "detail": f"{name} has no site yet."}
        pages = page.get("pages") or {}
        out.update({"page": page["id"], "number": page["number"], "size": page["size"], "sha256": page["sha256"],
                    "paths": sorted(pages), "title": M.title_of(pages.get("/", ""))})
        if path not in pages:
            return 404, {**out, "detail": f"{name} has no page {path}."}
        kind, value = M.page_kind(pages[path])
        out["kind"] = kind
        if kind == "text":
            out["body"] = value
            out["pictures"] = [iid for iid, _alt in M.pictures_of(value)]
        else:
            out["url"] = f"/site/{quote(net)}/{quote(name)}{path if path != '/' else '/'}"
            if kind == "id":
                out["inscription"] = value
        return 200, out

    # before /r/mesh/{address:path}, which would take the path for a page
    @app.get("/r/mesh/picture/{iid}")
    def mesh_picture(iid: str):
        """A picture on a page as a T-Deck draws it: a mesh picture made from the inscription (as /content serves it,
        screened the same way), kept once made."""
        iid = str(iid or "").strip().lower()
        if not M.INSCRIPTION.match(iid):
            return JSONResponse({"detail": "not an inscription id"}, status_code=400)
        body = pictures.get(iid)
        if body is None:
            if content is None:
                return JSONResponse({"detail": "this arcade serves no inscriptions"}, status_code=503)
            answer = content(iid)
            kind = str(answer.headers.get("content-type", ""))
            if getattr(answer, "status_code", 0) != 200 or not kind.startswith("image/"):
                return JSONResponse({"detail": "that inscription is not a picture this arcade can show"},
                                    status_code=404)
            try:
                body = MP.make(bytes(answer.body))
            except MP.PictureError as exc:
                return JSONResponse({"detail": str(exc)}, status_code=422)
            pictures[iid] = body
            while len(pictures) > PICTURES_KEPT:
                pictures.popitem(last=False)
        return Response(body, media_type="application/octet-stream",
                        headers={**contentlib.CORS, "Cache-Control": "public, max-age=86400"})

    @app.get("/r/mesh")
    def mesh_directory():
        try:
            return JSONResponse({"names": directory()}, headers={"Cache-Control": "no-store"})
        except (LookupError, ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=503)

    @app.get("/r/mesh/{address:path}")
    def mesh_page(address: str, have: str = ""):
        try:
            code, out = site(address)
        except (LookupError, ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=503)
        if code == 200 and have and have == out.get("page"):
            out = {k: v for k, v in out.items() if k != "body"}
            out["unchanged"] = True
        return JSONResponse(out, status_code=code, headers={"Cache-Control": "no-store"})

    @app.get("/r/sites")
    def sites_directory(page: int = 0, per: int = 50):
        """Every name with a site, newest first, `per` a page (2026-10-27: "The directory at some point will be
        millions of pages long"): from this arcade's sites search where it hosts one (every site, a page at a time),
        else from the ord indexes (the newest records they scan). Readable by inscribed pages, as /r/site is
        (2026-10-27, the me-seeks.pep search engine is an inscription)."""
        page, per = max(0, int(page or 0)), max(1, min(int(per or 50), 200))
        headers = {**contentlib.CORS, "Cache-Control": "no-store"}
        try:
            hosted = list(state.sites_working().values()) if hasattr(state, "sites_working") else []
            if hosted:
                lists = [s.directory(0, (page + 1) * per) for s in hosted]
                names = sorted((n for d in lists for n in d["names"]), key=lambda n: -(n.get("height") or 0))
                total = sum(d["total"] for d in lists)
                return JSONResponse({"names": names[page * per:(page + 1) * per], "total": total, "page": page,
                                     "pages": (total + per - 1) // per}, headers=headers)
            names = all_sites()
            return JSONResponse({"names": names[page * per:(page + 1) * per], "total": len(names), "page": page,
                                 "pages": (len(names) + per - 1) // per}, headers=headers)
        except (LookupError, ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=503, headers=headers)

    @app.get("/r/search")
    def sites_search(q: str = "", page: int = 0, chain: str = ""):
        """The sites search (arcade/sites_index.py; Me-Seeks, me-seeks.pep, asks this): 10 results a page, every site
        on every chain this arcade reads (or `chain`), from this arcade's index or a linked arcade's. A result's
        snippet marks its matching words with \\x02 and \\x03: draw it as text, never as markup."""
        headers = {**contentlib.CORS, "Cache-Control": "no-store"}
        if not q.strip():
            return JSONResponse({"detail": "search for some words: ?q=frog"}, status_code=400, headers=headers)
        try:
            return JSONResponse(state.sites_search(q[:200], page, chain), headers=headers)
        except (LookupError, ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=503, headers=headers)

    @app.get("/r/site/{address:path}")
    def site_page(address: str, chain: str = "", have: str = ""):
        try:
            code, out = any_site(address, chain)
        except (LookupError, ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=503)
        if code == 200 and have and have == out.get("page") and "body" in out:
            out = {k: v for k, v in out.items() if k != "body"}
            out["unchanged"] = True
        return JSONResponse(out, status_code=code, headers={**contentlib.CORS, "Cache-Control": "no-store"})

    @app.get("/site/{chain}/{address:path}")
    def site_served(chain: str, address: str):
        """A site's page on its own: HTML sandboxed as an inscription is (no cookies, nothing of the arcade's to reach);
        an inscription page is that inscription; MeshText is drawn."""
        code, out = any_site(address, chain)
        if code != 200:
            return HTMLResponse(f"<!doctype html><meta charset=utf-8><p>{html.escape(out.get('detail', ''))}</p>",
                                status_code=code, headers=contentlib.CONTENT_HEADERS)
        if out.get("inscription"):
            return RedirectResponse(f"/content/{quote(out['inscription'])}", status_code=307)
        if out["kind"] == "html":
            idx = indexes(chain).get(chain)
            said = idx.lookup({"what": "mesh", "name": out["name"]}).get("mesh") or {}
            value = M.page_kind((said.get("page") or {}).get("pages", {}).get(out["path"]))[1]
            return HTMLResponse(value, headers={**contentlib.CONTENT_HEADERS, "Cache-Control": "no-store"})
        page = (f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
                f"<title>{html.escape(out.get('title') or out['name'])}</title>{draw(out['body'], out['name'], '/browser/')}")
        return HTMLResponse(page, headers={**contentlib.CONTENT_HEADERS, "Cache-Control": "no-store"})

    @app.get("/browser", response_class=HTMLResponse)
    @app.get("/browser/{address:path}", response_class=HTMLResponse)
    def browser(address: str = ""):
        # Its script carries this request's nonce (app.nonced, 2026-11-09, security review): the same file is a card.
        from .app import nonced
        return HTMLResponse(nonced(BROWSER_FILE.read_text()), headers={"Cache-Control": "no-cache"})

    # --- in a browser ---------------------------------------------------------------------------------------------

    def draw(text: str, here: str, base: str = "/meshweb/") -> str:
        """MeshText as HTML: what the T-Deck shows, line by line."""
        out, pre = [], False
        for line in str(text).splitlines():
            if line.startswith("```"):
                out.append("</pre>" if pre else "<pre>")
                pre = not pre
                continue
            if pre:
                out.append(html.escape(line))
                continue
            s = line.rstrip()
            if s.startswith("=>"):
                parts = s[2:].strip().split(None, 1)
                if parts:
                    target = parts[0]
                    href = base + (here + target if target.startswith("/") else target.removeprefix("mesh://"))
                    label = parts[1] if len(parts) > 1 else target
                    out.append(f'<p class="l"><a href="{html.escape(href)}">{html.escape(label)}</a></p>')
            elif M.PICTURE_LINE.match(s.strip()):
                got = M.PICTURE_LINE.match(s.strip())
                out.append(f'<p class="pic"><img src="/content/{html.escape(got.group(2).lower())}" '
                           f'alt="{html.escape(got.group(1))}" style="max-width:100%;image-rendering:pixelated"></p>')
            elif s.startswith("### "):
                out.append(f"<h3>{html.escape(s[4:])}</h3>")
            elif s.startswith("## "):
                out.append(f"<h2>{html.escape(s[3:])}</h2>")
            elif s.startswith("# "):
                out.append(f"<h1>{html.escape(s[2:])}</h1>")
            elif s.startswith("* "):
                out.append(f"<p class=\"li\">&bull; {html.escape(s[2:])}</p>")
            elif s.startswith(">"):
                out.append(f"<blockquote>{html.escape(s[1:].strip())}</blockquote>")
            elif s:
                out.append(f"<p>{html.escape(s)}</p>")
        if pre:
            out.append("</pre>")
        return "\n".join(out)

    def frame(title: str, address: str, inner: str, status: str) -> HTMLResponse:
        doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)} · .mesh</title>
<style>
:root{{--bg:#141310;--fg:#ece8e0;--muted:#9b948a;--line:#2c2a25;--accent:#d9a520;--panel:#1c1b17}}
body{{margin:0;min-height:100vh;background:#0b0b0a;color:var(--fg);font:14px/1.45 Roboto,system-ui,sans-serif;
  display:flex;flex-direction:column;align-items:center;gap:10px;padding:16px;box-sizing:border-box}}
.tdeck{{width:320px;height:240px;background:var(--bg);border:1px solid var(--line);border-radius:10px;display:flex;
  flex-direction:column;overflow:hidden;box-shadow:0 8px 30px rgba(0,0,0,.5)}}
.bar{{display:flex;gap:6px;align-items:center;padding:4px 6px;background:var(--panel);border-bottom:1px solid var(--line);
  font-size:11px}}
.bar a{{color:var(--accent);text-decoration:none}}
.bar .addr{{flex:1;background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:2px 7px;overflow:hidden;
  white-space:nowrap;text-overflow:ellipsis;color:var(--fg)}}
.page{{flex:1;overflow:auto;padding:6px 8px;font-size:12px}}
.page h1{{font-size:16px;margin:2px 0 4px}} .page h2{{font-size:14px;margin:6px 0 3px}} .page h3{{font-size:12px;margin:6px 0 2px}}
.page p{{margin:0 0 4px}} .page .li{{margin-left:4px}} .page a{{color:#6fb0ff}}
.page blockquote{{margin:0 0 4px;padding-left:6px;border-left:2px solid var(--line);color:var(--muted)}}
.page pre{{margin:2px 0 6px;font:11px/1.2 ui-monospace,monospace;white-space:pre;overflow:auto}}
.status{{padding:2px 6px;border-top:1px solid var(--line);font-size:10px;color:var(--muted)}}
.note{{max-width:320px;color:var(--muted);font-size:12px}}
.note a{{color:var(--accent)}}
</style></head><body>
<div class="tdeck">
  <div class="bar"><a href="/meshweb">Home</a><span class="addr">{html.escape(address)}</span></div>
  <div class="page">{inner}</div>
  <div class="status">{html.escape(status)}</div>
</div>
<p class="note">A .mesh page, drawn at the size of a DogecoinArcade T-Deck (320 &times; 240). On the T-Deck it opens in
the Browser from the App Store, over WiFi or LoRa. <a href="/browser">The Browser</a> opens every name's site.</p>
</body></html>"""
        return HTMLResponse(doc, headers={"Cache-Control": "no-store"})

    @app.get("/meshweb", response_class=HTMLResponse)
    def meshweb_home():
        try:
            names = directory()
        except (LookupError, ValueError) as exc:
            return frame("The .mesh web", "directory", f"<p>{html.escape(str(exc))}</p>", "Document: not found")
        lines = ["# The .mesh web", "Every .mesh name with a page, newest first.", ""]
        lines += [f"=> {n['name']} {n['name']}" + (f" - {n['title']}" if n.get("title") else "") for n in names]
        if not names:
            lines.append("Nobody has published a page yet.")
        return frame("The .mesh web", "directory", draw("\n".join(lines), ""), f"Document: Done ({len(names)} sites)")

    @app.get("/meshweb/{address:path}", response_class=HTMLResponse)
    def meshweb_page(address: str):
        try:
            code, out = site(address)
        except (LookupError, ValueError) as exc:
            return frame(".mesh", address, f"<p>{html.escape(str(exc))}</p>", "Document: not found")
        name, path = out.get("name") or address, out.get("path") or "/"
        shown = name + ("" if path == "/" else path)
        if code != 200:
            return frame(shown, shown, f"<h1>Not found</h1><p>{html.escape(out.get('detail', ''))}</p>",
                         "Document: not found")
        return frame(out.get("title") or shown, shown, draw(out["body"], name),
                     f"Document: Done ({out['size']:,} bytes, inscription #{out['number']:,})")
