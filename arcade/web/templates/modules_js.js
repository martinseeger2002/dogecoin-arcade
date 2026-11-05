/* Edition & modules, drawn once for both places it is shown: Admin's tab (_modules_panel.html) and the Lite window's
 * Settings -> Options (arcade/lite/templates/_options.html). Served at /admin/modules.js; a page puts
 * <div data-modules-ui></div> where it goes and loads this.
 *
 * The modules come in categories, one tab each, from the server (modules.describe: a module's `kind`, titled by
 * KIND_TITLES), so a new kind -- chains, or modules brought from the mesh -- is a new tab with nothing changed here.
 * "All" lists every one; the filter box narrows whichever tab is open, by name, id or what it does. Each tab says how
 * many of its modules are on. A switch asks first when the module carries a caution, or when it turns others on or
 * off with it. The tab picked last is kept in this browser. "Who may use it" says who gets a module's tab and pages:
 * only the operator, signed-in users too, or everyone (modules.access; 2026-11-04). */
(function () {
  var KEPT = 'arcade.modules.tab';
  var CSS = '.mui-tabs{display:flex;flex-wrap:wrap;gap:2px;border-bottom:1px solid var(--line);margin:8px 0 6px}'
    + '.mui-tabs button{background:none;border:1px solid transparent;border-bottom:0;color:var(--fg);padding:4px 10px;'
    + 'margin-bottom:-1px;cursor:pointer;font:inherit}'
    + '.mui-tabs button[aria-selected=true]{background:var(--panel);border-color:var(--line);font-weight:600}'
    + '.mui-tabs small{color:var(--muted);margin-left:4px}'
    + '.mui-tools{display:flex;gap:8px;align-items:center;margin:0 0 6px}.mui-tools input{flex:1;min-width:0}'
    + '.mui table{width:100%;border-collapse:collapse}.mui td,.mui th{text-align:left;vertical-align:top;'
    + 'padding:3px 6px;border-bottom:1px solid var(--line)}.mui td.c{text-align:center;width:3em}'
    + '.mui .why{color:var(--muted)}.mui select{font:inherit}';
  var WHO = [['operator', 'Only me'], ['user', 'Signed-in'], ['anyone', 'Everyone']];

  function call(body) {
    return fetch('/admin/api/modules', {method: body ? 'POST' : 'GET', credentials: 'same-origin', cache: 'no-store',
      headers: {'Content-Type': 'application/json', 'X-Arcade-Admin': '1'}, body: body ? JSON.stringify(body) : undefined})
      .then(function (r) { return r.json().then(function (j) { if (!r.ok) throw new Error(j.detail || r.statusText); return j; }); });
  }
  function el(tag, text, cls) {
    var e = document.createElement(tag);
    if (text != null) e.textContent = text;
    if (cls) e.className = cls;
    return e;
  }
  function kept() { try { return localStorage.getItem(KEPT) || 'all'; } catch (e) { return 'all'; } }
  function keep(tab) { try { localStorage.setItem(KEPT, tab); } catch (e) { /* the next visit opens on All */ } }

  function mount(root) {
    var tab = kept(), said = null, data = null;
    root.classList.add('mui');
    root.innerHTML = '';
    var edition = el('p', 'Loading…'), acts = el('p'), restart = el('p', '', 'warn');
    restart.hidden = true;
    ['full', 'lite'].forEach(function (name) {
      var b = el('button', name === 'lite' ? 'Use ARCADE Lite' : 'Use Full');
      b.type = 'button'; b.dataset.edition = name;
      b.onclick = function () {
        var lite = name === 'lite';
        if (!confirm(lite ? 'Switch to ARCADE Lite? Every module but the core is switched off. Nothing is deleted.'
                          : 'Switch to the full edition? Every module is switched on.')) return;
        call({edition: name}).then(function () { location.reload(); }).catch(function (e) { alert(e.message); });
      };
      acts.append(b, ' ');
    });
    var tabs = el('div', null, 'mui-tabs'); tabs.setAttribute('role', 'tablist');
    var tools = el('div', null, 'mui-tools'), filter = el('input');
    filter.type = 'search'; filter.placeholder = 'Filter modules'; filter.setAttribute('aria-label', 'Filter modules');
    tools.append(filter);
    var blurb = el('p', '', 'why'), table = el('table'), body = el('tbody');
    table.innerHTML = '<thead><tr><th class="c">On</th><th>Module</th><th>What it does</th><th>Who may use it</th><th class="c">In Lite</th></tr></thead>';
    table.append(body);
    root.append(el('h2', 'Edition'), edition, acts, restart, el('h2', 'Modules'), tabs, tools, blurb, table);
    filter.oninput = rows;

    function draw(d) {
      data = d;
      edition.textContent = 'This arcade runs ' + (d.edition === 'lite' ? 'ARCADE Lite.' : 'the full edition.')
        + (d.asking ? ' (Not chosen yet: it runs as Full until you choose.)' : '')
        + ' Changing it sets every module to that edition\'s default; nothing is deleted.';
      acts.querySelectorAll('button').forEach(function (b) { b.disabled = !d.asking && b.dataset.edition === d.edition; });
      restart.hidden = !d.restart_for.length;
      restart.textContent = 'Pages follow at once. The background work of ' + d.restart_for.join(', ')
        + ' follows when the arcade restarts.';
      var cats = [{id: 'all', title: 'All', summary: 'Every module this arcade has.'}].concat(d.categories || []);
      if (!cats.some(function (c) { return c.id === tab; })) tab = 'all';
      tabs.textContent = '';
      cats.forEach(function (c) {
        var mine = d.modules.filter(function (m) { return c.id === 'all' || m.kind === c.id; });
        var b = el('button', c.title); b.type = 'button'; b.setAttribute('role', 'tab'); b.dataset.cat = c.id;
        b.setAttribute('aria-selected', c.id === tab);
        b.append(' ', el('small', mine.filter(function (m) { return m.on; }).length + '/' + mine.length));
        b.onclick = function () { tab = c.id; keep(tab); draw(data); };
        tabs.append(b);
      });
      blurb.textContent = (cats.filter(function (c) { return c.id === tab; })[0] || {}).summary || '';
      rows();
    }
    function rows() {
      if (!data) return;
      var words = filter.value.trim().toLowerCase();
      body.textContent = '';
      data.modules.filter(function (m) {
        return (tab === 'all' || m.kind === tab)
          && (!words || (m.title + ' ' + m.id + ' ' + m.summary).toLowerCase().indexOf(words) >= 0);
      }).forEach(function (m) {
        var tr = body.insertRow(), on = tr.insertCell();
        on.className = 'c';
        if (m.core) on.append(el('span', 'core', 'why'));
        else {
          var box = el('input'); box.type = 'checkbox'; box.checked = m.on; box.setAttribute('aria-label', m.title);
          box.onchange = function () { flip(m, box); };
          on.append(box);
        }
        tr.insertCell().textContent = m.title;
        var what = tr.insertCell(); what.textContent = m.summary;
        if (m.needs.length) what.append(el('div', 'Needs: ' + names(m.needs), 'why'));
        var who = tr.insertCell();
        if (m.core) who.append(el('span', 'everyone', 'why'));
        else {
          var pick = el('select'); pick.setAttribute('aria-label', 'Who may use ' + m.title);
          WHO.forEach(function (w) { var o = el('option', w[1]); o.value = w[0]; pick.append(o); });
          pick.value = m.access || 'anyone';
          pick.onchange = function () {
            call({module: m.id, access: pick.value}).then(draw).catch(function (e) { alert(e.message); pick.value = m.access; });
          };
          who.append(pick);
        }
        var lite = tr.insertCell(); lite.className = 'c why'; lite.textContent = m.lite ? 'on' : 'off';
      });
      if (!body.rows.length) body.insertRow().insertCell().textContent = 'No module matches.';
    }
    function names(ids) {
      return ids.map(function (id) {
        return (data.modules.filter(function (m) { return m.id === id; })[0] || {title: id}).title;
      }).join(', ');
    }
    function flip(m, box) {
      var off = !box.checked, also = off ? m.needed_by : m.needs;
      var ask = off && m.caution ? m.caution + '\n\n' : '';
      if (also.length) ask += (off ? 'Also stops: ' : 'Also turns on: ') + names(also) + '.\n\n';
      if (ask && !confirm(ask + (off ? 'Switch ' + m.title + ' off?' : 'Switch ' + m.title + ' on?'))) {
        box.checked = !box.checked; return;
      }
      call({module: m.id, on: box.checked}).then(draw).catch(function (e) { alert(e.message); box.checked = !box.checked; });
    }
    root.refresh = function () { return call(null).then(draw).catch(function (e) { edition.textContent = e.message; }); };
    root.refresh();
  }

  if (!document.getElementById('mui-css')) {
    var style = document.createElement('style'); style.id = 'mui-css'; style.textContent = CSS;
    document.head.append(style);
  }
  document.querySelectorAll('[data-modules-ui]').forEach(mount);
})();
