// arcade.contacts -- the signed-in player's address book.
//
// Asked of the viewer the page is running in, the way arcade.storage is.
//
// list(): the person is asked once per game whether it may see their
// contacts; until they say yes, and for a guest, the answer is an empty list.
// What comes back is only names and addresses: [{address, tag}].
//
//   <script src="/r/contacts.js"></script>
//   arcade.contacts.list().then(function (people) {
//     people.forEach(function (p) { console.log('@' + p.tag, p.address); });
//   });
//
// add({address, name}) and remove(address) (2026-10-21): change the book, one
// person at a time, each only after the person taps yes on a card the viewer
// shows (naming this game and what will change). `address` is an address or
// an @tag; only names claimed on the chain go in the book. `name` is optional,
// at most 60 characters, kept as the person's private note on them. Both
// resolve {ok: true, status}: "added" or "already" (add), "removed" or
// "absent" (remove); "already" and "absent" show no card. They reject with an
// Error saying why: "declined", "sign in first ...", "not an address ...",
// "busy ..." (a contacts card from this page is already open: ask again after
// it is answered), "no viewer ...", or what the chain said ("nobody holds
// @x ..."). Neither reads the book, and a yes does not let list() read it.
//
//   arcade.contacts.add({address: '@alice', name: 'Alice'})
//     .then(function (r) { console.log(r.status); },          // "added" | "already"
//           function (e) { console.log(e.message); });        // "declined", ...
(function () {
  var seq = 0, waiting = {};

  window.addEventListener('message', function (e) {
    var m = e.data || {};
    if (m.arcade !== 'contacts' || !waiting[m.seq]) return;
    var w = waiting[m.seq];
    delete waiting[m.seq];
    if (m.ok) w.resolve(m);
    else w.reject(new Error(m.error || 'refused'));
  });

  function ask(body, wait, resolve, reject, late) {
    var n = ++seq;
    waiting[n] = {resolve: resolve, reject: reject};
    setTimeout(function () {
      if (waiting[n]) { delete waiting[n]; late(); }
    }, wait);
    body.arcade = 'contacts';
    body.seq = n;
    window.parent.postMessage(body, '*');
  }

  // list() -> the contacts, an array. Empty, it says why on `.why` (2026-10-23): 'off' (Contacts is off on this
  // app's card: say "turn on Contacts on its card"), 'guest' (not signed in), 'operator' (the node's own wallet
  // view), 'no' (a page with no card, told no), 'viewer' (no arcade viewer around this page).
  function list() {
    function said(contacts, why) {
      var out = contacts || [];
      if (!out.length && why) out.why = why;
      return out;
    }
    return new Promise(function (resolve) {
      // The person may take a while to answer the question, so the wait is long;
      // no viewer at all (the page opened on its own) answers [] when it ends.
      ask({op: 'list'}, 120000, function (m) { resolve(said(m.contacts, m.why)); },
          function () { resolve(said([], 'viewer')); }, function () { resolve(said([], 'viewer')); });
    });
  }

  function change(op, body) {
    return new Promise(function (resolve, reject) {
      if (window.parent === window) {
        reject(new Error('no viewer: open this page in the arcade to change your address book'));
        return;
      }
      body.op = op;
      // Long, for the same reason as list(): the person is reading a card.
      ask(body, 300000, function (m) { resolve({ok: true, status: m.status}); }, reject,
          function () { reject(new Error('no viewer: nothing answered; open this page in the arcade')); });
    });
  }

  function add(who) {
    who = who || {};
    var body = {address: typeof who === 'string' ? who : who.address};
    if (typeof who === 'object' && who.name != null) body.name = who.name;
    return change('add', body);
  }

  function remove(address) {
    if (address && typeof address === 'object') address = address.address;
    return change('remove', {address: address});
  }

  window.arcade = window.arcade || {};
  window.arcade.contacts = {list: list, add: add, remove: remove};
})();
