"""The page a collection sells itself from.

A mintpad is an inscription like any other: an HTML page whose JSON names a
shop (`swap.py`) offering a random item of the creator's collection for a
price. The page draws the collection as a wall of tiles, spins a reel when
somebody mints, and stops on the piece that is now theirs.

It is built here rather than typed by hand because the interesting part is
not the page -- it is that the JSON beside it and the wallet behind it agree
about what is for sale. A collection inscribed through the wizard can offer
itself for sale the moment its last item is on its way, without anybody
writing a line of HTML or a line of JSON (D-036).

The page is the one the Goofball collection used, with the names taken out.
It talks to the wallet it is framed in through `/r/swap.js`; opened as a
bare `/content/` URL it says so rather than half-working.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: The page, with %%NAME%% placeholders. Package data, so an installed
#: release has it (pyproject: arcade.web package-data).
TEMPLATE = Path(__file__).parent / "web" / "templates" / "mintpad.html"


class MintpadError(Exception):
    """The mintpad cannot be built as asked."""


def plural(word: str) -> str:
    """"Goofball" -> "Goofballs". Crude on purpose.

    The alternative is a dictionary of English plurals in a wallet, to make
    one line of a page read slightly better. A collection called "Dice" gets
    "Dices left to mint" and its creator can inscribe their own page.
    """
    word = (word or "").strip()
    if not word:
        return ""
    return word if word.endswith(("s", "S")) else word + "s"


def page(creator: str, collection: str) -> bytes:
    """The mintpad for one collection, as the bytes that go on the chain."""
    name = (collection or "").strip()
    if not name:
        raise MintpadError("a mintpad needs the collection's name")
    if not (creator or "").strip():
        raise MintpadError("a mintpad needs the address that holds the collection")
    text = TEMPLATE.read_text(encoding="utf-8")
    for mark, value in (("%%CREATOR%%", creator.strip()),
                        ("%%COLLECTION%%", name),
                        ("%%TITLE%%", f"{name.upper()} MINTPAD"),
                        ("%%MANY%%", plural(name)),
                        ("%%ONE%%", name)):
        text = text.replace(mark, value)
    if "%%" in text:
        raise MintpadError("the mintpad template has a placeholder left in it")
    return text.encode("utf-8")


def take_of(kind: str, amount: str, property_id: int | None = None) -> dict[str, Any]:
    """What the pad asks for: coins, or an amount of one token.

    Checked here rather than where the form is read, so the rule is in one
    place and a pad built by anything else gets the same answer.
    """
    amount = (amount or "").strip()
    if not amount:
        raise MintpadError("say what one costs")
    try:
        if float(amount) <= 0:
            raise MintpadError("a price is more than nothing")
    except ValueError:
        raise MintpadError(f"{amount!r} is not a price") from None
    if kind == "coins":
        return {"coins": amount}
    if kind == "token":
        if not property_id:
            raise MintpadError("say which token the price is in")
        return {"token": int(property_id), "amount": amount}
    raise MintpadError("a price is in coins or in a token")


def shop_json(node: str, collection: str, take: dict[str, Any]) -> str:
    """The inscription's JSON field: the name, and the shop beside it."""
    name = (collection or "").strip()
    if not (node or "").strip():
        raise MintpadError("a mintpad needs this node's contact code")
    return json.dumps({
        "name": f"{name} Mintpad",
        "shop": {"node": node.strip(),
                 "listings": [{"give": {"collection": name, "pick": "random"},
                               "take": take}]},
    }, separators=(",", ":"))


# --- an account's mintpad, as an inscription (2026-09-27) -------------
#
# "Launchpad should also be a regular inscription so that it can be easily
# shared to the feed with a /content/ command", and "the content should be
# available on anyone who is running Dogecoin arcade". The page the wizard
# makes is drawn by one node; this is the same pad as bytes on the chain. It
# reads everything it shows through /r/ (what is left, the art, the counts),
# so whichever node serves it answers for itself, and it asks the page around
# it to buy (postMessage {arcade: "mint"}), which shows the ordinary Buy card
# signed in the reader's own tab. Only the chosen look's styling goes in, so
# the page is one transaction.

LOOKS = ("spotlight", "wall", "gallery", "arcade", "lottery",
         "neon", "polaroid", "terminal", "minimal")

_BASE_CSS = """*{box-sizing:border-box}html,body{margin:0;min-height:100%}
body{background:#141310;color:#ece8e0;font:16px/1.45 system-ui,sans-serif;padding:18px;text-align:center}
.card{position:relative;z-index:1;max-width:520px;margin:0 auto;background:#1c1a16;border:1px solid #2e2b25;border-radius:16px;padding:20px}
h1{margin:10px 0 4px;font-size:1.6rem}.m{color:#9b948a}.by{margin:0 0 6px;font-size:.9rem}
.left{font-size:1.1rem;margin:12px 0 4px}.note{font-size:.8rem;margin-top:10px}
button{font:inherit;font-weight:700;cursor:pointer;border:0;border-radius:12px;padding:13px 20px;margin-top:8px;background:#d9a520;color:#1a1408;width:100%}
button:disabled{opacity:.6}.cover{width:200px;height:200px;object-fit:cover;border-radius:14px}
#say{margin:10px 0 0;font-size:.9rem}#say.bad{color:#ff8a80}#say.ok{color:#8fdcae}"""

_LOOK_CSS = {
    "spotlight": "",
    "wall": """#bg{position:fixed;inset:0;display:grid;gap:4px;padding:4px;opacity:.45;pointer-events:none;
grid-template-columns:repeat(auto-fill,minmax(90px,1fr));overflow:hidden}
#bg img{width:100%;aspect-ratio:1;object-fit:cover;border-radius:6px}.card{box-shadow:0 20px 60px #0009}""",
    "gallery": """.strip{display:flex;gap:8px;overflow-x:auto;padding-bottom:6px}
.strip img{flex:none;width:140px;height:140px;object-fit:cover;border-radius:12px}.card{max-width:760px}""",
    "arcade": """.card{background:#120f1c;border:4px solid #c9a227;border-radius:18px;box-shadow:0 0 30px #c9a22773}
h1{font-family:monospace;color:#ffd84a;text-shadow:0 0 8px #ffd84a99}.cover{image-rendering:pixelated;border:3px solid #ffd84a;border-radius:6px}
button{background:#e0282e;color:#fff;font-family:monospace;animation:b 1.1s steps(2) infinite}@keyframes b{50%{opacity:.55}}""",
    "lottery": """body{background:#07060c}#bg{position:fixed;inset:-4vmin;display:grid;gap:6px;padding:6px;pointer-events:none;
grid-template-columns:repeat(auto-fill,minmax(88px,1fr));grid-auto-rows:88px;transform:rotate(-6deg) scale(1.12);overflow:hidden}
#bg div{border-radius:12px;background:#1a1530 center/cover;image-rendering:pixelated;transition:transform .9s}
#bg div.o{transform:rotateY(180deg)}#veil{position:fixed;inset:0;pointer-events:none;
background:radial-gradient(ellipse at 50% 45%,#07060c40,#07060cd1 62%,#07060cf7)}
.card{background:#0c0a16cc;border-color:#ffffff1f;border-radius:26px;backdrop-filter:blur(10px);box-shadow:0 0 60px #ff5fa22e}
h1{background:linear-gradient(90deg,#ffc83d,#ff5fa2,#43e8ff,#ffc83d);background-size:300% 100%;-webkit-background-clip:text;
background-clip:text;color:transparent;animation:s 6s linear infinite}@keyframes s{to{background-position:300% 0}}
.reel{position:relative;width:200px;height:200px;margin:0 auto 12px;border-radius:22px;overflow:hidden;background:#110d22;
box-shadow:0 0 0 3px #ffc83d,0 0 40px #ffc83d59}#strip{position:absolute;left:0;top:0;width:100%}
#strip div{width:200px;height:200px;background:#1a1530 center/cover;image-rendering:pixelated}
.reel.won{animation:w 1.1s 3}@keyframes w{50%{box-shadow:0 0 0 3px #43e8ff,0 0 80px #43e8ffb3}}
button{background:linear-gradient(90deg,#ffc83d,#ff5fa2);color:#1a1024}""",
}

# More looks (2026-09-28: "add the multiple styles of mint pads for both
# tokens and [NFTs]"). Shared with the token pad, which draws the same card.
_SHARED_CSS = {
    "neon": """body{background:#05010f}.card{background:#0b0418;border:2px solid #ff2bd6;border-radius:18px;
box-shadow:0 0 24px #ff2bd6aa,inset 0 0 24px #22e1ff33}h1{color:#22e1ff;text-shadow:0 0 10px #22e1ff}
.cover{border:2px solid #22e1ff;box-shadow:0 0 18px #22e1ffaa}.m{color:#b9a8d9}
button{background:#ff2bd6;color:#fff;box-shadow:0 0 16px #ff2bd6}""",
    "terminal": """body{background:#000;color:#39ff6a;font-family:ui-monospace,Menlo,monospace}
body:after{content:'';position:fixed;inset:0;pointer-events:none;background:repeating-linear-gradient(0deg,#0000 0 2px,#0006 2px 3px)}
.card{background:#000;border:1px solid #39ff6a;border-radius:4px;text-align:left}.m{color:#1f9a3f}
h1{font-family:inherit;font-size:1.3rem}h1:before{content:'> '}
.cover{border-radius:0;border:1px solid #39ff6a;filter:grayscale(1) sepia(1) hue-rotate(70deg) saturate(3)}
button{background:#000;color:#39ff6a;border:1px solid #39ff6a;border-radius:0;font-family:inherit}
button:hover{background:#39ff6a;color:#000}""",
    "minimal": """body{background:#fafaf7;color:#111}.card{background:#fff;border:1px solid #e7e5df;box-shadow:0 1px 3px #0001}
.m{color:#777}.cover{width:260px;height:260px;border-radius:4px}button{background:#111;color:#fff;border-radius:999px}
#say.ok{color:#1c7a45}#say.bad{color:#b3261e}""",
}
_LOOK_CSS.update(_SHARED_CSS)
_LOOK_CSS["polaroid"] = """body{background:#efe6d6;color:#2b2620}.card{background:transparent;border:0}
.cover{width:250px;height:280px;padding:12px 12px 48px;border-radius:2px;background:#fff;box-shadow:0 12px 30px #0003;
transform:rotate(-3deg);object-fit:cover}h1{font-family:'Brush Script MT','Segoe Script',cursive;font-size:2.1rem}
.m{color:#6b6257}button{background:#2b2620;color:#efe6d6;border-radius:4px}#say.ok{color:#1c7a45}#say.bad{color:#b3261e}"""

_BUTTON = {"arcade": "INSERT COIN", "lottery": "Spin to mint", "terminal": "run mint",
           "polaroid": "Develop one"}

_PAD_JS = r"""const P=JSON.parse(document.getElementById('pad').textContent),$=i=>document.getElementById(i);
const E=encodeURIComponent,C='/content/',W='/r/mintpad/'+E(P.creator)+'/'+E(P.collection);
const say=(t,k)=>{$('say').textContent=t;$('say').className=k||''};
const j=u=>fetch(u).then(r=>r.ok?r.json():null).catch(()=>null);
const img=(id,t)=>{const e=document.createElement(t||'img');if(t)e.style.backgroundImage='url('+C+id+')';else{e.src=C+id;e.loading='lazy';e.alt=''}return e};
let art=[];
(async()=>{
 const c=await j('/r/collection/'+E(P.creator)+'/'+E(P.collection)+'?limit=60');
 if(!c){say('This collection is not on this node yet.','bad');return}
 art=(c.items||[]).filter(i=>String(i.contenttype||'').startsWith('image/'));
 $('by').textContent='by '+(c.items&&c.items[0]&&c.items[0].creatortag?'@'+c.items[0].creatortag:P.creator.slice(0,10)+'…')
  +' · '+c.count+' of '+(c.supply||'∞');
 const bg=$('bg');if(bg&&art.length){const L=P.look==='lottery';for(let k=0;k<(L?3:1);k++)for(const a of art)bg.append(img(a.id,L?'div':''));
  if(L){const t=[...bg.children];setInterval(()=>t[Math.random()*t.length|0].classList.toggle('o'),700)}}
 const st=$('strip');if(st)for(const a of art){const e=img(a.id,P.look==='lottery'?'div':'');e.dataset.id=a.id;st.append(e)}
 const cv=$('cover');if(cv&&c.cover)cv.src=C+c.cover;else if(cv)cv.remove();
 load();
})();
async function load(){const p=await j(W);const n=p?p.left:0;
 $('left').innerHTML=n?'<b>'+n+'</b> left · '+(p.prices||[]).map(x=>x/1e8).join(' / ')+' coins each':'Nothing left on this mintpad right now.';
 $('go').hidden=!n;return p}
function spin(id){const r=$('reel'),s=$('strip');
 if(!r||!s){const cv=$('cover');if(cv){cv.src=C+id;cv.style.boxShadow='0 0 0 3px #43e8ff,0 0 40px #43e8ffb3'}return}const c=[...s.children];let a=c.findIndex(e=>e.dataset.id===id);if(a<0)a=0;
 const x=[];for(let i=0;i<3;i++)for(const e of c)x.push(e.cloneNode(true));s.prepend(...x);s.style.transition='none';s.style.transform='none';
 requestAnimationFrame(()=>{s.style.transition='transform 3.2s cubic-bezier(.12,.8,.2,1)';s.style.transform='translateY(-'+(x.length+a)*200+'px)'});
 setTimeout(()=>r.classList.add('won'),3300)}
let seq=0;const wait={},heard={};
addEventListener('message',e=>{const m=e.data||{};if(m.arcade!=='mint')return;if(m.heard){heard[m.seq]=1;return}
 if(wait[m.seq]){wait[m.seq](m);delete wait[m.seq]}});
const tall=()=>{const c=document.querySelector('.card');parent.postMessage({arcade:'size',height:Math.ceil((c?c.getBoundingClientRect().bottom+scrollY:document.body.scrollHeight)+24)},'*')};
addEventListener('load',tall);setTimeout(tall,800);setTimeout(tall,2500);
if(window.ResizeObserver)new ResizeObserver(tall).observe(document.body);
$('go').onclick=async()=>{say('');const p=await load();if(!p||!p.next)return;
 const n=++seq;$('go').disabled=true;
 const got=await new Promise(ok=>{wait[n]=ok;parent.postMessage({arcade:'mint',seq:n,listing:p.next.listing,
  piece:p.next.piece,name:P.collection+' #'+(p.next.edition||p.next.number)},'*');
  setTimeout(()=>{if(wait[n]&&!heard[n]){delete wait[n];ok({error:'Open this mintpad on DogecoinArcade to mint from it.'})}},4000)});
 $('go').disabled=false;
 if(got.error)say(got.error,'bad');else if(got.ok){spin(p.next.piece);say('Minted #'+p.next.number+'! It is yours when its block lands.','ok');setTimeout(load,4000)}};"""


def account_page(creator: str, collection: str, look: str = "spotlight",
                 blurb: str = "") -> bytes:
    """An account's mintpad for one of its collections, as bytes for the chain.

    Everything the page shows is read at view time through /r/, so the bytes
    name only the collection, the seller and the look; the seller, collection
    and blurb go in as JSON inside a script tag, never as markup, so a name
    with angle brackets in it is a name and not HTML.
    """
    name = (collection or "").strip()
    seller = (creator or "").strip()
    if not name or not seller:
        raise MintpadError("a mintpad needs a collection and the address selling it")
    look = look if look in LOOKS else "spotlight"
    import html as _html
    data = json.dumps({"creator": seller, "collection": name, "look": look},
                      ensure_ascii=False).replace("</", "<\\/")
    top = ""
    if look in ("wall", "lottery"):
        top = '<div id="bg"></div>' + ('<div id="veil"></div>' if look == "lottery" else "")
    show = {"lottery": '<div class="reel" id="reel"><div id="strip"></div></div>',
            "gallery": '<div class="strip" id="strip"></div>'}.get(
        look, '' if look == "wall" else '<img class="cover" id="cover" alt="">')
    words = _html.escape(" ".join((blurb or "").split())[:300])
    text = (
        "<!doctype html><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{_html.escape(name)} mintpad</title>"
        f"<style>{_BASE_CSS}{_LOOK_CSS[look]}</style>{top}"
        f"<div class=card>{show}<h1>{_html.escape(name)}</h1>"
        "<p class='m by' id=by></p>"
        + (f"<p>{words}</p>" if words else "")
        + "<p class=left id=left>…</p>"
        f"<button id=go hidden>{_BUTTON.get(look, 'Mint one')}</button><p id=say></p>"
        "<p class='m note'>You get a random piece of the set: the creator signed each "
        "piece over at this price, and your key pays in one transaction.</p></div>"
        f"<script type=application/json id=pad>{data}</script>"
        f"<script>{_PAD_JS}</script>")
    return text.encode("utf-8")


# --- a token mintpad: an account's own token, sold a lot at a time ------------
#
# 2026-09-27: "we need token mint pads as well in the mint pad wizard".
# The seller puts one resting ask on the token's book (type 25) for the whole
# supply they want to sell; this page sells it a LOT at a time by asking the
# page around it to take that much of the ask (type 29, D-189) -- the buyer
# signs, the seller can be away, and every node that serves the page reads the
# same book through /r/book. One transaction, like the NFT pad.

TOKEN_LOOKS = ("counter", "vending", "progress", "coin", "ticker",
               "neon", "terminal", "minimal")

_TOKEN_CSS = {
    "counter": "",
    "vending": """body{background:#1a1030}.card{background:#2a1850;border:6px solid #ff4fa3;border-radius:22px;
box-shadow:0 0 0 6px #1a1030,0 0 40px #ff4fa366}h1{font-family:monospace;color:#ffe45e;letter-spacing:.06em}
.slot{margin:14px auto;width:140px;height:90px;border-radius:12px;background:#0d0820;border:3px solid #ff4fa3;
display:grid;place-items:center;font:700 1.6rem monospace;color:#7dffb0}
button{background:#7dffb0;color:#0d0820;font-family:monospace}""",
    "progress": """.bar{height:18px;border-radius:999px;background:#2e2b25;overflow:hidden;margin:12px 0 4px}
.bar i{display:block;height:100%;width:0;background:linear-gradient(90deg,#d9a520,#ffe45e);transition:width 1s}
.sold{font-size:.85rem;color:#9b948a}""",
    "coin": """.coin{width:150px;height:150px;margin:6px auto 14px;border-radius:50%;background:#d9a520 center/cover;
border:6px solid #f5c542;box-shadow:0 0 0 4px #8a6410,0 10px 30px #0008;display:grid;place-items:center;
font:800 3rem system-ui;color:#5a3e05;animation:c 6s linear infinite}@keyframes c{to{transform:rotateY(360deg)}}
body.won .coin{animation:c .5s linear 5}button{background:#f5c542}""",
    "ticker": """.tick{overflow:hidden;white-space:nowrap;background:#000;color:#7dffb0;font:600 .9rem ui-monospace,monospace;
padding:7px 0;margin:-20px -20px 16px;border-radius:16px 16px 0 0}
#tape{display:inline-block;padding-left:100%;animation:t 16s linear infinite}@keyframes t{to{transform:translateX(-100%)}}
.card{border-color:#7dffb055}button{background:#7dffb0;color:#04160b}""",
}
_TOKEN_CSS.update(_SHARED_CSS)

_TOKEN_JS = r"""const P=JSON.parse(document.getElementById('pad').textContent),$=i=>document.getElementById(i);
const say=(t,k)=>{$('say').textContent=t;$('say').className=k||''};
const j=u=>fetch(u).then(r=>r.ok?r.json():null).catch(()=>null);
const fmt=(u,d)=>d?(u/1e8).toLocaleString(undefined,{maximumFractionDigits:8}):u.toLocaleString();
let pick=null;
async function load(){const b=await j('/r/book/'+P.property+'?address='+encodeURIComponent(P.creator));
 if(!b){say('This token is not on this node yet.','bad');return}
 const lot=P.lot,asks=(b.asks||[]).filter(a=>!a.pending);
 const left=asks.reduce((n,a)=>n+Math.max(0,a.tokens-a.taking),0);
 pick=asks.find(a=>a.tokens-a.taking>=lot)||null;
 const lots=Math.floor(left/lot);
 $('left').innerHTML=lots?'<b>'+lots.toLocaleString()+'</b> left':'Nothing left on this mintpad right now.';
 if(pick){const per=Math.floor(pick.coins*lot/pick.tokens)/1e8;$('price').textContent=per+' '+(per===1?'coin':'coins')+' for '+fmt(lot,b.divisible)+' '+b.name}
 $('go').hidden=!pick;
 const bar=document.querySelector('.bar i');if(bar&&P.total){const sold=Math.max(0,P.total-left);bar.style.width=Math.min(100,100*sold/P.total)+'%';
  $('sold').textContent=fmt(sold,b.divisible)+' of '+fmt(P.total,b.divisible)+' '+b.name+' sold'}
 const slot=$('slot');if(slot)slot.textContent=lots?String(lots).padStart(3,'0'):'000';
 const tp=$('tape');if(tp)tp.textContent=(b.name+'  ·  '+($('price').textContent||'')+'  ·  '+lots.toLocaleString()+' left  ·  ').repeat(3);
 return b}
let seq=0;const wait={},heard={};
addEventListener('message',e=>{const m=e.data||{};if(m.arcade!=='take')return;if(m.heard){heard[m.seq]=1;return}
 if(wait[m.seq]){wait[m.seq](m);delete wait[m.seq]}});
const tall=()=>{const c=document.querySelector('.card');parent.postMessage({arcade:'size',height:Math.ceil((c?c.getBoundingClientRect().bottom+scrollY:document.body.scrollHeight)+24)},'*')};
addEventListener('load',tall);setTimeout(tall,800);if(window.ResizeObserver)new ResizeObserver(tall).observe(document.body);
$('go').onclick=async()=>{say('');await load();if(!pick)return;const n=++seq;$('go').disabled=true;
 const got=await new Promise(ok=>{wait[n]=ok;parent.postMessage({arcade:'take',seq:n,order:pick.order,units:P.lot},'*');
  setTimeout(()=>{if(wait[n]&&!heard[n]){delete wait[n];ok({error:'Open this mintpad on DogecoinArcade to mint from it.'})}},4000)});
 $('go').disabled=false;
 if(got.error)say(got.error,'bad');else if(got.ok){say('Minted! Yours when its block lands.','ok');
  const d=document.body;d.classList.remove('won');void d.offsetWidth;d.classList.add('won');setTimeout(load,4000)}};
load();"""


def account_token_page(creator: str, property_id: int, name: str, lot_units: int,
                       total_units: int = 0, look: str = "counter",
                       blurb: str = "", icon: str = "") -> bytes:
    """A token mintpad: `lot_units` of token `property_id` per mint, from the
    resting ask(s) of `creator`, in one of TOKEN_LOOKS. Everything shown is read
    live from /r/book; the seller, the token and the lot are fixed in JSON."""
    import html as _html
    if not creator or not property_id or lot_units <= 0:
        raise MintpadError("a token mintpad needs the seller, the token and a lot")
    look = look if look in TOKEN_LOOKS else "counter"
    data = json.dumps({"creator": creator, "property": int(property_id),
                       "lot": int(lot_units), "total": int(total_units or 0),
                       "look": look}).replace("</", "<\\/")
    top = ""
    face = f'<img class="cover" src="/content/{_html.escape(icon)}" alt="">' if icon else ""
    if look == "vending":
        top = '<div class="slot" id="slot">000</div>'
    elif look == "coin":
        top = (f'<div class="coin" style="background-image:url(/content/{_html.escape(icon)})"></div>'
               if icon else f'<div class="coin">{_html.escape((name or "?")[:1].upper())}</div>')
    elif look == "ticker":
        top = '<div class="tick"><span id="tape"></span></div>' + face
    else:
        top = face
    bar = ('<div class="bar"><i></i></div><div class="sold" id="sold"></div>'
           if look == "progress" else "")
    words = _html.escape(" ".join((blurb or "").split())[:300])
    button = {"vending": "INSERT COIN", "terminal": "run mint", "coin": "Mint a stack"}.get(look, "Mint")
    text = (
        "<!doctype html><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{_html.escape(name)} mintpad</title>"
        f"<style>{_BASE_CSS}{_TOKEN_CSS[look]}</style>"
        f"<div class=card>{top}<h1>{_html.escape(name)}</h1>"
        + (f"<p>{words}</p>" if words else "")
        + f"{bar}<p class=left id=left>…</p><p class='m' id=price></p>"
        f"<button id=go hidden>{button}</button><p id=say></p>"
        "<p class='m note'>Each mint is one transaction you sign: the tokens come out of "
        "the seller's standing order at its own price, and the seller does not need to "
        "be online.</p></div>"
        f"<script type=application/json id=pad>{data}</script>"
        f"<script>{_TOKEN_JS}</script>")
    return text.encode("utf-8")
