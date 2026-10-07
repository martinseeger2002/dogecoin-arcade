// arcade.contacts -- the signed-in player's address book, read-only.
//
// Asked of the viewer the page is running in, the way arcade.storage is. The
// person is asked once per game whether it may see their contacts; until they
// say yes, and for a guest, the answer is an empty list. What comes back is
// only names and addresses: [{address, tag}].
//
//   <script src="/r/contacts.js"></script>
//   arcade.contacts.list().then(function (people) {
//     people.forEach(function (p) { console.log('@' + p.tag, p.address); });
//   });
(function () {
  var seq = 0, waiting = {};

  window.addEventListener('message', function (e) {
    var m = e.data || {};
    if (m.arcade !== 'contacts' || !waiting[m.seq]) return;
    var w = waiting[m.seq];
    delete waiting[m.seq];
    if (m.ok) w.resolve(m.contacts || []);
    else w.reject(new Error(m.error || 'refused'));
  });

  function list() {
    return new Promise(function (resolve) {
      var n = ++seq;
      waiting[n] = {resolve: resolve, reject: function () { resolve([]); }};
      // The person may take a while to answer the question, so the wait is long;
      // no viewer at all (the page opened on its own) answers [] when it ends.
      setTimeout(function () {
        if (waiting[n]) { delete waiting[n]; resolve([]); }
      }, 120000);
      window.parent.postMessage({arcade: 'contacts', op: 'list', seq: n}, '*');
    });
  }

  window.arcade = window.arcade || {};
  window.arcade.contacts = {list: list};
})();
