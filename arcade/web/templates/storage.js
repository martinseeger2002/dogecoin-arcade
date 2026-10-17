// arcade.storage -- what the wallet remembers for this page.
//
// An inscribed page runs in a sandbox with no origin of its own, so the
// browser gives it no localStorage. This is the same shape -- getItem,
// setItem, removeItem, clear, key, length -- kept by the wallet the page is
// running in, under this inscription's id, and the same on every device that
// wallet is used from. Reads are synchronous once `ready` has resolved;
// writes return a promise that rejects if the wallet refuses (too big, too
// many) or if no wallet is listening (the page was opened outside a viewer).
//
//   <script src="/r/storage.js"></script>
//   arcade.storage.ready.then(function (s) {
//     var best = Number(s.getItem('best') || 0);
//     s.setItem('best', String(Math.max(best, score)));
//   });
(function () {
  var items = {}, seq = 0, waiting = {};

  function ask(message) {
    return new Promise(function (resolve, reject) {
      message.arcade = 'storage';
      message.seq = ++seq;
      waiting[message.seq] = {resolve: resolve, reject: reject};
      setTimeout(function () {
        if (waiting[message.seq]) {
          delete waiting[message.seq];
          reject(new Error('no wallet is listening: open this page in a DogecoinArcade viewer'));
        }
      }, 5000);
      // '*' because the wallet's origin is not this page's business, and the
      // only window that can be `parent` is the one that framed us.
      window.parent.postMessage(message, '*');
    });
  }

  window.addEventListener('message', function (e) {
    var m = e.data || {};
    if (m.arcade !== 'storage' || !waiting[m.seq]) return;
    var w = waiting[m.seq];
    delete waiting[m.seq];
    if (m.ok) {
      if (m.items) items = m.items;
      w.resolve(storage);
    } else {
      w.reject(new Error(m.error || 'refused'));
    }
  });

  var storage = {
    getItem: function (key) {
      key = String(key);
      return Object.prototype.hasOwnProperty.call(items, key) ? items[key] : null;
    },
    setItem: function (key, value) {
      key = String(key); value = String(value);
      var had = Object.prototype.hasOwnProperty.call(items, key), before = items[key];
      items[key] = value;
      return ask({op: 'set', key: key, value: value}).catch(function (err) {
        if (had) items[key] = before; else delete items[key];
        throw err;
      });
    },
    removeItem: function (key) {
      key = String(key);
      delete items[key];
      return ask({op: 'remove', key: key});
    },
    clear: function () {
      items = {};
      return ask({op: 'clear'});
    },
    key: function (i) { return Object.keys(items)[i] || null; },
    get length() { return Object.keys(items).length; },
    // Everything, as a plain object -- a copy, so writing to it changes nothing.
    all: function () { var out = {}; for (var k in items) out[k] = items[k]; return out; }
  };
  storage.ready = ask({op: 'load'});

  window.arcade = window.arcade || {};
  window.arcade.storage = storage;
})();
