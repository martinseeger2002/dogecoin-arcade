/* Edition & modules, drawn once for both places it is shown: Admin's tab (_modules_panel.html) and the Lite window's
 * Settings -> Options (arcade/lite/templates/_options.html). Served at /admin/modules.js; a page puts
 * <div data-modules-ui></div> where it goes and loads this.
 *
 * The modules come in categories, one tab each, from the server (modules.describe: a module's `kind`, titled by
 * KIND_TITLES), so a new kind -- chains, or modules brought from the mesh -- is a new tab with nothing changed here.
 * "All" lists every one; the filter box narrows whichever tab is open, by name, id or what it does. Each tab says how
 * many of its modules are on. A category with groups (the Apps: social, inscriptions and tokens, markets, node
 * modules; modules.APP_GROUPS) draws each group under a heading of its own, and All heads each category too. A switch
 * asks first when the module carries a caution, or when it turns others on or off with it. The tab picked last is kept in this browser. "Who may use it" says who gets a module's tab and pages:
 * only the operator, signed-in users too, or everyone (modules.access; 2026-11-04). "Add a node module" lists the
 * node modules that come with the arcade's source, each with Add, or Update when a newer one came with an update; an
 * added one shows up under Apps -> Node modules, off, to be ticked on (2026-11-06: step 4 of the DigiDollar guide "in
 * the ui"). It is shown with the Apps and All tabs, where node modules are. */
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
    + '.mui tr.mui-head th{padding-top:12px;font-weight:600;border-bottom:1px solid var(--line)}'
    + '.mui tr.mui-head small{font-weight:400;color:var(--muted);margin-left:8px}'
    + '.mui .why{color:var(--muted)}.mui .ok{font-weight:600}.mui .mui-add{margin-top:18px}.mui select{font:inherit}';
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
    // The look it opens in (arcade/modules.py LOOKS): ArcadeCore, a desktop, or the website. Each person can wear
    // another one in their own browser; this is what a newcomer gets.
    var lookRow = el('p'), lookPick = el('select');
    lookPick.setAttribute('aria-label', 'The look this arcade opens in');
    lookPick.onchange = function () {
      call({look: lookPick.value}).then(draw).catch(function (e) { alert(e.message); if (data) lookPick.value = data.look; });
    };
    lookRow.append('Opens in ', lookPick, el('span', ' (each person can switch looks for themselves)', 'why'));
    restart.hidden = true;
    ['full', 'lite'].forEach(function (name) {
      var b = el('button', name === 'lite' ? 'Core modules only' : 'Every module');
      b.type = 'button'; b.dataset.edition = name;
      b.onclick = function () {
        var lite = name === 'lite';
        if (!confirm(lite ? 'Switch to the core modules only, opening in ArcadeCore? Every module but the core is switched off. Nothing is deleted.'
                          : 'Switch every module on? The arcade keeps the look it opens in.')) return;
        if (!lite) return call({edition: name, look: data && data.look}).then(function () { location.reload(); })
          .catch(function (e) { alert(e.message); });
        call({edition: name}).then(function () { location.reload(); }).catch(function (e) { alert(e.message); });
      };
      acts.append(b, ' ');
    });
    var tabs = el('div', null, 'mui-tabs'); tabs.setAttribute('role', 'tablist');
    var tools = el('div', null, 'mui-tools'), filter = el('input');
    filter.type = 'search'; filter.placeholder = 'Filter modules'; filter.setAttribute('aria-label', 'Filter modules');
    tools.append(filter);
    var blurb = el('p', '', 'why'), table = el('table'), body = el('tbody');
    table.innerHTML = '<thead><tr><th class="c">On</th><th>Module</th><th>What it does</th><th>Who may use it</th><th class="c">Core only</th></tr></thead>';
    table.append(body);
    var addHead = el('h2', 'Add a node module', 'mui-add'), addWhy = el('p', 'These come with this arcade. Adding one puts'
      + ' it under Apps \u2192 Node modules above, switched off; then tick it on there and say who may use it.', 'why');
    var addSaid = el('p', '', 'ok'), addTable = el('table'), addBody = el('tbody');
    addTable.innerHTML = '<thead><tr><th>Module</th><th>What it does</th><th></th></tr></thead>';
    addTable.append(addBody);
    addSaid.hidden = true;
    root.append(el('h2', 'Edition'), edition, acts, lookRow, restart, el('h2', 'Modules'), tabs, tools, blurb, table,
                addHead, addWhy, addSaid, addTable);
    filter.oninput = rows;

    function draw(d) {
      data = d;
      edition.textContent = 'This arcade runs ' + (d.edition === 'lite' ? 'the core modules only (ArcadeCore).' : 'every module.')
        + (d.asking ? ' (Not chosen yet: it runs every module until you choose.)' : '')
        + ' The two are the same arcade: changing it sets every module to that choice\'s default; nothing is deleted.';
      lookPick.textContent = '';
      (d.looks || []).forEach(function (l) {
        var o = el('option', l.title); o.value = l.id; o.selected = l.id === d.look; lookPick.append(o);
      });
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
      shipped(d.shipped || []);
    }
    function shipped(list) {
      addHead.hidden = addWhy.hidden = addTable.hidden = !list.length || (tab !== 'all' && tab !== 'app');
      addBody.textContent = '';
      list.forEach(function (p) {
        var tr = addBody.insertRow();
        tr.insertCell().textContent = p.title + ' ' + p.version;
        tr.insertCell().textContent = p.summary;
        var act = tr.insertCell(), b = el('button', p.same ? 'Added' : p.added ? 'Update from ' + p.added : 'Add');
        b.type = 'button'; b.disabled = p.same; b.setAttribute('aria-label', b.textContent + ' ' + p.title);
        b.onclick = function () {
          b.disabled = true;
          call({add: p.name}).then(function (d) {
            tab = 'app'; keep(tab); filter.value = '';
            addSaid.hidden = false;
            addSaid.textContent = 'Added ' + p.title + ' ' + p.version + '. Tick it on under Apps \u2192 Node modules above.';
            draw(d);
          }).catch(function (e) { alert(e.message); b.disabled = false; });
        };
        act.append(b);
      });
    }
    // A heading row over each category (in All) and each group (in a category that has groups).
    function head(title, summary) {
      var tr = body.insertRow(), th = el('th', title); tr.className = 'mui-head'; th.colSpan = 5;
      if (summary) th.append(el('small', summary));
      tr.append(th);
    }
    function rows() {
      if (!data) return;
      var words = filter.value.trim().toLowerCase();
      body.textContent = '';
      (data.categories || []).filter(function (c) { return tab === 'all' || c.id === tab; }).forEach(function (c) {
        var mine = data.modules.filter(function (m) {
          return m.kind === c.id
            && (!words || (m.title + ' ' + m.id + ' ' + m.summary).toLowerCase().indexOf(words) >= 0);
        });
        var groups = c.groups || [];
        if (tab === 'all' && mine.length) head(c.title, groups.length ? '' : c.summary);
        if (!groups.length) { mine.forEach(row); return; }
        var ids = groups.map(function (g) { return g.id; });
        groups.concat([{id: null, title: 'Other', summary: ''}]).forEach(function (g) {
          var these = mine.filter(function (m) { return g.id === null ? ids.indexOf(m.group) < 0 : m.group === g.id; });
          if (!these.length && (words || g.id === null || tab === 'all')) return;
          head(g.title, g.summary);
          these.forEach(row);
          if (!these.length) note('None yet.' + (g.id === 'node-modules' && (data.shipped || []).length
            ? ' Add one under Add a node module below.' : ''));
        });
      });
      if (!body.querySelector('tr:not(.mui-head)')) note('No module matches.');
    }
    function note(text) {
      var td = body.insertRow().insertCell(); td.colSpan = 5; td.append(el('span', text, 'why'));
    }
    function row(m) {
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
      // A node module's own page, its forms the operator's switches among them (2026-11-08): the website's /module/<id>,
      // or its tab when this is drawn inside the ArcadeCore window.
      if (m.package && m.on) {
        var go = el('a', 'Open ' + m.title + ' \u2192'); go.href = '/module/' + encodeURIComponent(m.id);
        go.style.cssText = 'display:block;margin-top:2px';
        go.onclick = function (e) {
          var w = null;
          try { w = window.liteWindow || (window.parent !== window && window.parent.liteWindow) || null; } catch (x) {}
          // Its tab, when the window has one; else the page itself, framed where it was clicked (a tab hidden under
          // Your tabs, or a module switched on since the window opened: 2026-11-08, it was a click that did nothing).
          var tab = w && w.route ? w.route('/module/' + m.id) : null;
          if (tab && w.open) { e.preventDefault(); w.open(tab); }
        };
        what.append(go);
      }
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
    }
    function names(ids) {
      return ids.map(function (id) {
        return (data.modules.filter(function (m) { return m.id === id; })[0] || {title: id}).title;
      }).join(', ');
    }
    function flip(m, box) {
      // only what the switch changes (2026-11-08): the needs that are off now, the modules that need it that are on
      var off = !box.checked, also = (off ? m.needed_by : m.needs).filter(function (id) {
        var other = data.modules.filter(function (o) { return o.id === id; })[0];
        return !other || other.on === off;
      });
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
