/* A node module's command forms and how they run: one copy for every look (2026-11-08, SadFrog: "make sure every
 * feature that is available in full is available in core and vice versa", "chose the lites version of things"). Moved
 * here unchanged from the ArcadeCore window (lite.html), which draws a module's tab with it; the website's module page
 * (module_page.html, /apps/m/<name>) draws the same forms, and the operator's forms are among them. Served at
 * /lite/modforms.js. A module's spend comes back as an offer this browser's wallet reads from its own bytes, lists to
 * the person and signs only on a yes (`signOffer`, which was _wallet.html's liteSign). */

let modList = null;
/** The commands this caller may run, with each module command's form (/lite/api/commands), asked once a page. */
export function moduleCommands() {
  if (!modList) {
    modList = fetch('/lite/api/commands', {credentials: 'same-origin'}).then(function (r) { return r.json(); })
      .catch(function () { modList = null; return []; });
  }
  return modList;
}
function moduleWords() {
  return moduleCommands().then(function (list) {
    var out = {}; list.forEach(function (c) { if (c.package) out[c.name] = 1; }); return out;
  });
}

/** One console line, typed or from a module's form: a node module's command is told the keys this browser's wallet
 * holds (only their public halves, `hooks.pubkeys()`), so a mint can name its owner; what it prepares to spend comes
 * back as an offer this wallet reviews and signs (`hooks.sign(offer)`). `shown` gets the answer before the wallet asks;
 * the promise ends with what the wallet said ("Sent. Transaction ..."), or nothing when there was nothing to sign. */
export function runLine(text, shown, hooks) {
  var word = text.split(/\s+/)[0], h = hooks || {};
  return moduleWords().then(function (words) {
    return words[word] && h.pubkeys ? h.pubkeys().catch(function () { return {}; }) : {};
  }).then(function (pubkeys) {
    return fetch('/lite/api/console', {method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'X-Arcade-Lite': '1', 'X-Arcade-Admin': '1'},
      body: JSON.stringify({line: text, pubkeys: pubkeys})});
  }, function () { throw new Error('The node did not answer.'); })
    .then(function (r) { return r.json(); })
    .then(function (j) {
      if (!j.ok) throw new Error(j.error || j.detail || 'refused');
      var sign = j.result && j.result.sign;
      if (sign) delete j.result.sign;
      shown(j.result);
      if (sign && h.sign) return h.sign(sign);
    });
}

/** The console, in any look (2026-11-08, SadFrog: "there shouldnt be a differentiation between wallets now"): `out` is
 * where lines are written, `input` the box a line is typed in. Lines run as runLine runs them, with `hooks` (the
 * browser wallet's keys and signing); a word in `local` ({word: (rest) => answer}) is run by the page itself, as the
 * ArcadeCore window runs an account's message commands (_messages.html), unless only its help is asked. Up and Down
 * walk back through what was typed. Returns {send(line)}, for a menu item or a guide's button to run one. */
export function startConsole(out, input, hooks, opts) {
  var o = opts || {}, past = [], back = 0;
  function say(text, cls) {
    var div = document.createElement('div'); if (cls) div.className = cls;
    div.textContent = text; out.appendChild(div); out.scrollTop = out.scrollHeight;
  }
  function shown(r) { say(typeof r === 'string' ? r : JSON.stringify(r, null, 2)); }
  function fail(e) { say(String((e && e.message) || e), 'err'); }
  say(o.welcome || 'Welcome to the console.\nType help for the list of commands, or help <command> for one.');
  function send(text) {
    say('> ' + text, 'in');
    var word = text.split(/\s+/)[0], rest = text.slice(word.length), local = o.local ? (o.local() || {})[word] : null;
    if (local && !/(^|\s)(-h|-help|--help)(\s|$)/.test(rest)) {
      return Promise.resolve().then(function () { return local(rest); }).then(shown, fail);
    }
    return runLine(text, shown, typeof hooks === 'function' ? hooks() : hooks)
      .then(function (signed) { if (signed) say(signed); }, fail);
  }
  input.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && input.value.trim()) {
      past.push(input.value); back = past.length; send(input.value.trim()); input.value = '';
    } else if (e.key === 'ArrowUp' && back > 0) { back--; input.value = past[back]; e.preventDefault(); }
    else if (e.key === 'ArrowDown') { back = Math.min(past.length, back + 1); input.value = past[back] || ''; e.preventDefault(); }
  });
  return {send: send, say: say};
}

const coinsOf = (n) => (Number(n || 0) / 1e8).toLocaleString(undefined, {maximumFractionDigits: 8});
const unitOf = (c) => (c.coin || c.label || "") + (c.mainnet === false && !/test/i.test(c.coin || "") ? " test" : "");

/** The public keys the open wallet holds, by chain ({network: hex}); none while it is shut. */
export async function pubkeys(wallet, tagchain, chains) {
  const k = await wallet.opened(tagchain);
  const out = {};
  const hexOf = (u) => Array.from(u, (b) => b.toString(16).padStart(2, "0")).join("");
  if (k) for (const c of chains) { try { out[c.network] = hexOf(wallet.keysOn(k, c.network).pubkey); } catch (e) {} }
  return out;
}

/** An offer a node module prepared, reviewed and signed only on a yes: every payment as this browser read it from the
 * transaction itself (coins.verifyOffer), never the module's own words alone (2026-11-05 review: the box said only the
 * module's "what", the fee and the change, so a module could pay itself the wallet's coins under any description). */
export async function signOffer(wallet, tagchain, chains, o) {
  const k = await wallet.opened(tagchain);
  if (!k) throw new Error("Open your wallet first (Send asks for its password), then run the command again. Nothing was signed.");
  const read = await wallet.checked(o, k);
  const c = chains.find((x) => x.network === o.chain) || {};
  const paid = (read.pays || []).filter((x) => !x.mine);
  const lines = [`The module says: ${o.what || "a transaction"}`, "", "This transaction pays:"];
  for (const x of paid) {
    lines.push(x.dd !== undefined ? `  $${(x.dd / 100).toFixed(2)} in DigiDollars to ${x.to}`
               : x.bytes !== undefined ? `  ${x.bytes} bytes written to the chain${x.value ? ` (${coinsOf(x.value)} ${unitOf(c)})` : ""}`
                                       : `  ${coinsOf(x.value)} ${unitOf(c)} to ${x.to}`);
  }
  if (!paid.length) lines.push("  nobody but you");
  lines.push(`Network fee: ${coinsOf(read.fee)} ${unitOf(c)}`);
  if (read.change) lines.push(`Comes back to you: ${coinsOf(read.change)} ${unitOf(c)}`);
  // A module's spend on a chain of real coins says so, as Send's review does (w-warn).
  if (c.mainnet) lines.push("", `Real ${unitOf(c)}. Once it is in a block it can't be taken back.`);
  if (!window.confirm(lines.join("\n") + "\n\nSign it with your wallet and send it?")) return "Not sent. Nothing was signed.";
  const out = await wallet.confirm(k, o, {reviewed: true});
  return `Sent. Transaction ${out.txid}`;
}

// A node module's tab: a form for each of its commands that has one (packages._check_form), drawn here by the
// window from the manifest's words. Pressing one runs the very line the console would, shown under the answer, and
// a spend still goes to the wallet to review and sign. An answer's "actions" ([{label, line}]) become buttons only
// when the line is one of this module's own commands.
export function drawModule(box, runner) {
  var id = box.dataset.modpanel;
  moduleCommands().then(function (list) {
    var mine = list.filter(function (c) { return c.module === id && c.form; })
      .sort(function (a, b) { return (a.order || 0) - (b.order || 0); });
    var own = {}; list.forEach(function (c) { if (c.module === id) own[c.name] = 1; });
    box.hidden = !mine.length;
    if (!mine.length) return;
    var forms = document.createElement('div'), out = document.createElement('div');
    forms.className = 'modforms'; out.className = 'modout'; out.setAttribute('aria-live', 'polite');
    box.replaceChildren(forms, out);
    function run(line) {
      var block = document.createElement('div'), said = document.createElement('div');
      said.className = 'line'; said.textContent = 'Console: ' + line;
      block.appendChild(said); out.replaceChildren(block);
      block.scrollIntoView({block: 'nearest'});
      runner(line, function (result) { block.appendChild(answer(result)); block.scrollIntoView({block: 'nearest'}); })
        .then(function (signed) { if (signed) block.appendChild(note(signed)); },
              function (e) { block.appendChild(note(String((e && e.message) || e), 'err')); });
    }
    function note(text, cls) { var p = document.createElement('pre'); if (cls) p.className = cls; p.textContent = text; return p; }
    function actions(list) {
      var span = document.createElement('span');
      (list || []).forEach(function (a) {
        if (!a || !own[String(a.line || '').split(/\s+/)[0]]) return;
        var b = document.createElement('button'); b.type = 'button'; b.textContent = String(a.label || a.line);
        b.onclick = function () { run(String(a.line)); }; span.appendChild(b);
      });
      return span;
    }
    // An answer's field names, as words: "you_hold" says "You hold".
    function said(k) { k = String(k).replace(/_/g, ' '); return k.charAt(0).toUpperCase() + k.slice(1); }
    function cell(v) {
      return v === null || v === undefined ? '' : typeof v === 'object' ? JSON.stringify(v) : String(v);
    }
    function answer(result) {
      if (typeof result === 'string' || result === null || typeof result !== 'object') return note(cell(result));
      var wrap = document.createElement('div'), dl = document.createElement('dl');
      Object.keys(result).forEach(function (k) {
        var v = result[k];
        if (k === 'actions') { wrap.appendChild(actions(v)); return; }
        if (Array.isArray(v) && v.length && v.every(function (x) { return x && typeof x === 'object' && !Array.isArray(x); })) {
          var cols = []; v.forEach(function (x) { Object.keys(x).forEach(function (c) { if (c !== 'actions' && cols.indexOf(c) < 0) cols.push(c); }); });
          var t = document.createElement('table'), head = t.createTHead().insertRow(), body = t.createTBody();
          var cap = t.createCaption(); cap.textContent = said(k); cap.style.textAlign = 'left';
          cols.concat(v.some(function (x) { return x.actions; }) ? [''] : []).forEach(function (c) {
            var th = document.createElement('th'); th.textContent = c && said(c); head.appendChild(th);
          });
          v.forEach(function (x) {
            var tr = body.insertRow();
            cols.forEach(function (c) { tr.insertCell().textContent = cell(x[c]); });
            if (x.actions) tr.insertCell().appendChild(actions(x.actions));
          });
          wrap.appendChild(t); return;
        }
        var dt = document.createElement('dt'), dd = document.createElement('dd');
        dt.textContent = said(k); dd.textContent = Array.isArray(v) && !v.length ? 'none' : cell(v);
        dl.append(dt, dd);
      });
      if (dl.children.length) wrap.insertBefore(dl, wrap.firstChild);
      return wrap;
    }
    mine.forEach(function (c) {
      var f = c.form || {}, fs = document.createElement('fieldset'), lg = document.createElement('legend');
      var form = document.createElement('form'), fields = f.fields || {}, inputs = {};
      lg.textContent = f.title || c.name; fs.appendChild(lg);
      if (f.note) { var n = document.createElement('p'); n.className = 'note'; n.textContent = f.note; fs.appendChild(n); }
      Object.keys(fields).forEach(function (arg) {
        var d = fields[arg] || {}, label = document.createElement('label'), input;
        label.textContent = d.label || arg;
        if (d.type === 'choice') {
          input = document.createElement('select');
          (d.choices || []).forEach(function (ch) { var o = document.createElement('option'); o.value = ch[0]; o.textContent = ch[1]; input.appendChild(o); });
        } else {
          input = document.createElement('input'); input.autocomplete = 'off';
          if (d.type === 'number') input.inputMode = 'decimal';
        }
        input.id = 'mod-' + c.name + '-' + arg; input.name = arg;
        if (d.value) input.value = d.value;
        label.appendChild(input); form.appendChild(label); inputs[arg] = input;
      });
      var go = document.createElement('button'); go.type = 'submit'; go.textContent = f.button || c.name;
      form.appendChild(go); fs.appendChild(form); forms.appendChild(fs);
      form.onsubmit = function (e) {
        e.preventDefault();
        var words = [c.name], gap = false;
        (c.args || []).forEach(function (arg) {
          var v = inputs[arg] ? inputs[arg].value.trim() : '';
          if (!v) { gap = true; return; }
          if (gap || /\s/.test(v)) { words = null; }
          if (words) words.push(v);
        });
        if (!words) { out.replaceChildren(note('Fill in each box, one word each.', 'err')); return; }
        run(words.join(' '));
      };
    });
  });
}
