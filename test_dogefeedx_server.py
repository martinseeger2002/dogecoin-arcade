#!/usr/bin/env python3
"""Minimal server for testing DogeFeedX pages.

Run with: python3 test_dogefeedx_server.py
Access at: http://localhost:8080/dogefeedx/feed

This server proxies API requests to the real dogefeed.app API.
NO MOCK DATA - all data comes from the live dogefeed.app API.
"""
from pathlib import Path
import uvicorn
import json
from markupsafe import Markup
from jinja2 import Environment, FileSystemLoader, select_autoescape, TemplateNotFound
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.templating import Jinja2Templates

from arcade.web.dogefeedx.apps import router as dogefeedx_router

app = FastAPI(title="DogeFeedX Test Server")
# Same read-only dogefeed.app proxies the arcade serves. No local fixtures.
app.include_router(dogefeedx_router)

# Template directory
TEMPLATE_DIR = Path(__file__).parent / "arcade" / "web" / "templates"

# Create a simple base.html for testing 
TEST_BASE_HTML = '''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>{% block title %}DogeFeedX{% endblock %}</title>
<style>* { margin: 0; padding: 0; box-sizing: border-box; } body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }</style>
</head>
<body>
{% block body %}{% endblock %}
</body>
</html>'''

class TestTemplateLoader(FileSystemLoader):
    """Custom loader that substitutes base.html with a simple test version."""
    def get_source(self, environment, template):
        if template == "base.html":
            return TEST_BASE_HTML, "base.html", lambda: True
        return super().get_source(environment, template)

env = Environment(
    loader=TestTemplateLoader(str(TEMPLATE_DIR)),
    autoescape=select_autoescape(['html', 'xml'])
)
env.filters["tojson"] = lambda value: Markup(json.dumps(value))

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
templates.env = env

# Mock brand object
class MockBrand:
    name = "Dogecoin Arcade"
    light_bg = "#ffffff"
    dark_bg = "#1a1a1a"
    
brand = MockBrand()

# Mock state object with all required attributes
class MockState:
    generation = "test"
    public = False
    
    @staticmethod
    def setting(key, default=None):
        return default
    
    @staticmethod
    def _signed_in(request):
        return None
    
state = MockState()

def render(request: Request, template: str, fullscreen=False, **kwargs):
    """Render a template with standard context."""
    return templates.TemplateResponse(
        template,
        {
            "request": request,
            "brand": brand,
            "state": state,
            "fullscreen": fullscreen,
            "embed": False,
            **kwargs
        }
    )

# Static files for DogeFeedX
@app.get("/dogefeedx/aqua.css")
async def aqua_css():
    path = TEMPLATE_DIR / "dogefeedx" / "aqua.css"
    if path.is_file():
        return FileResponse(path, media_type="text/css")
    return HTMLResponse("/* no aqua.css */", media_type="text/css")

@app.get("/dogefeedx/shell.js")
async def shell_js():
    path = TEMPLATE_DIR / "dogefeedx" / "shell.js"
    if path.is_file():
        return FileResponse(path, media_type="application/javascript")
    return HTMLResponse("// no shell.js", media_type="application/javascript")

# DogeFeedX page routes. JSON comes from dogefeedx.router above (live dogefeed.app).
@app.get("/dogefeedx/feed", response_class=HTMLResponse)
def dogefeedx_feed(request: Request):
    return render(request, "dogefeedx/feed.html")

@app.get("/dogefeedx/explore", response_class=HTMLResponse)
def dogefeedx_explore(request: Request):
    return render(request, "dogefeedx/explore.html")

@app.get("/dogefeedx/market", response_class=HTMLResponse)
def dogefeedx_market(request: Request):
    return render(request, "dogefeedx/market.html")

@app.get("/dogefeedx/profile", response_class=HTMLResponse)
def dogefeedx_profile(request: Request):
    return render(request, "dogefeedx/profile.html")

@app.get("/dogefeedx/wallet", response_class=HTMLResponse)
def dogefeedx_wallet(request: Request):
    return render(request, "dogefeedx/wallet.html")

@app.get("/dogefeedx/collections", response_class=HTMLResponse)
def dogefeedx_collections(request: Request):
    return render(request, "dogefeedx/collections.html")

@app.get("/dogefeedx/nfts", response_class=HTMLResponse)
def dogefeedx_nfts(request: Request):
    return render(request, "dogefeedx/nfts.html")

@app.get("/dogefeedx/launchpad", response_class=HTMLResponse)
def dogefeedx_launchpad(request: Request):
    return render(request, "dogefeedx/launchpad.html")

@app.get("/dogefeedx/leaderboard", response_class=HTMLResponse)
def dogefeedx_leaderboard(request: Request):
    return render(request, "dogefeedx/leaderboard.html")

@app.get("/dogefeedx/notifications", response_class=HTMLResponse)
def dogefeedx_notifications(request: Request):
    return render(request, "dogefeedx/notifications.html")

@app.get("/dogefeedx/messages", response_class=HTMLResponse)
def dogefeedx_messages(request: Request):
    return render(request, "dogefeedx/messages.html")

@app.get("/dogefeedx/bookmarks", response_class=HTMLResponse)
def dogefeedx_bookmarks(request: Request):
    return render(request, "dogefeedx/bookmarks.html")

@app.get("/dogefeedx/search", response_class=HTMLResponse)
def dogefeedx_search(request: Request):
    return render(request, "dogefeedx/search.html")

@app.get("/dogefeedx/inscription", response_class=HTMLResponse)
def dogefeedx_inscription(request: Request):
    return render(request, "dogefeedx/inscription.html")

@app.get("/dogefeedx/post", response_class=HTMLResponse)
def dogefeedx_post(request: Request):
    return render(request, "dogefeedx/post.html")

@app.get("/dogefeedx/explorer", response_class=HTMLResponse)
def dogefeedx_explorer(request: Request):
    return render(request, "dogefeedx/explorer.html")

@app.get("/dogefeedx/terminal", response_class=HTMLResponse)
def dogefeedx_terminal(request: Request):
    return render(request, "dogefeedx/terminal.html")

@app.get("/dogefeedx/network", response_class=HTMLResponse)
def dogefeedx_network(request: Request):
    return render(request, "dogefeedx/network.html")

@app.get("/desktop", response_class=HTMLResponse)
def desktop_view(request: Request):
    """DogeFeedX desktop theme, so pages can be checked inside a window."""
    nav = [
        ("/me", "You", None, True),
        ("/", "Home", None, True),
        ("/feed", "Feed", None, True),
        ("/explore", "Explore", None, True),
        ("/me/wallet", "Wallet", None, True),
        ("/me/messages", "Messages", None, True),
    ]
    return render(
        request, "desktop.html",
        nav=nav,
        desk_default="dogefeedx",
        apps=[],
        ord_nets=[],
        coins=[],
        system={"version": "test", "node": "", "chains": []},
        operator_view=False,
        account_name=None,
    )

if __name__ == "__main__":
    print("Starting DogeFeedX test server at http://localhost:8080")
    print("Pages:")
    print("  http://localhost:8080/dogefeedx/feed")
    print("  http://localhost:8080/dogefeedx/explore")
    print("  http://localhost:8080/dogefeedx/market")
    print("  http://localhost:8080/dogefeedx/profile")
    print("  http://localhost:8080/dogefeedx/wallet")
    print("  http://localhost:8080/dogefeedx/collections")
    print("  http://localhost:8080/dogefeedx/nfts")
    print("  http://localhost:8080/dogefeedx/launchpad")
    print("  http://localhost:8080/dogefeedx/leaderboard")
    print("  http://localhost:8080/dogefeedx/search")
    print("  http://localhost:8080/dogefeedx/post")
    uvicorn.run(app, host="0.0.0.0", port=8080)
