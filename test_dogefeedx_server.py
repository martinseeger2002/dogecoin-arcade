#!/usr/bin/env python3
"""Minimal server for testing DogeFeedX pages.

Run with: python3 test_dogefeedx_server.py
Access at: http://localhost:8080/dogefeedx/feed
"""
from pathlib import Path
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.templating import Jinja2Templates

app = FastAPI(title="DogeFeedX Test Server")

# Template directory
TEMPLATE_DIR = Path(__file__).parent / "arcade" / "web" / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

# Mock brand object
class MockBrand:
    name = "Dogecoin Arcade"
    
brand = MockBrand()

# Mock state object
class MockState:
    generation = "test"
    public = False
    
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

# DogeFeedX API routes (mock data)
@app.get("/dogefeedx/api/feed")
async def api_feed(limit: int = 20):
    """Mock feed data for testing."""
    return {
        "items": [
            {
                "id": "post1",
                "author": {
                    "handle": "dogelord",
                    "displayName": "Doge Lord",
                    "avatarUrl": "https://dogefeed.app/doginal-img/sample1",
                    "verifiedPfp": True
                },
                "body": "Much wow, very Dogecoin! The future is here. #dogecoin",
                "createdAt": "2026-11-03T00:00:00Z",
                "counts": {"likes": 42, "branches": 5}
            },
            {
                "id": "post2",
                "author": {
                    "handle": "shibamaster",
                    "displayName": "Shiba Master",
                    "avatarUrl": "https://dogefeed.app/doginal-img/sample2",
                    "verifiedPfp": True
                },
                "body": "Building the future of digital collectibles on Dogecoin.",
                "createdAt": "2026-11-02T23:30:00Z",
                "counts": {"likes": 28, "branches": 3}
            },
            {
                "id": "post3",
                "author": {
                    "handle": "cryptodoge",
                    "displayName": "Crypto Doge",
                    "avatarUrl": "",
                    "verifiedPfp": False
                },
                "body": "GM everyone! Ready for another great day in the Doge ecosystem!",
                "createdAt": "2026-11-02T23:00:00Z",
                "counts": {"likes": 15, "branches": 1}
            }
        ],
        "source": "mock"
    }

@app.get("/dogefeedx/api/collections")
async def api_collections(limit: int = 50, sort: str = "name", q: str = ""):
    """Mock collections data."""
    return {
        "collections": [
            {"id": "doginal-dogs", "name": "Doginal Dogs", "item_count": 10000, "holder_count": 2500, "sample_item_ids": []},
            {"id": "doge-punks", "name": "Doge Punks", "item_count": 5000, "holder_count": 1200, "sample_item_ids": []},
            {"id": "shiba-society", "name": "Shiba Society", "item_count": 3000, "holder_count": 800, "sample_item_ids": []},
        ],
        "total": 3,
        "source": "mock"
    }

@app.get("/dogefeedx/api/search")
async def api_search(q: str = "", type: str = "all"):
    """Mock search results."""
    return {
        "query": q,
        "users": [
            {"handle": "dogelord", "display_name": "Doge Lord", "verified_pfp": True, "avatar_url": ""},
        ],
        "posts": [],
        "source": "mock"
    }

@app.get("/dogefeedx/api/inscription")
async def api_inscription(id: str = ""):
    """Mock inscription data."""
    return {
        "inscription": {
            "id": id or "sample123",
            "number": 12345,
            "content_type": "image/png",
            "collection": "Doginal Dogs",
            "owner": "D1234567890abcdef"
        },
        "source": "mock"
    }

# DogeFeedX page routes
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
