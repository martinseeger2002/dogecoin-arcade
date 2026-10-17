// arcade.node -- this page talking to another DogecoinArcade node.
//
// An inscribed page can load nothing from outside the machine it runs on.
// What it can do is send a node-to-node message -- sealed to the other
// node's key, signed as from the wallet it is running in, carried on the
// chain -- and read what that node sends back. The wallet does the sealing
// and the sending; the page never holds a key. Testnet only, always: that
// is the chain node-to-node messages live on, and it is why nobody is asked
// to approve one. A page may send thirty an hour.
//
//   <script src="/r/node.js"></script>
//   arcade.node.send(SHOP_NODE, {order: 'hat', size: 'L'})
//     .then(function (r) { console.log('sent as', r.txid); });
//   var stop = arcade.node.listen(function (reply) {
//     console.log(reply.json || reply.body);
//   });
//
// `send(to, body)`: `to` is the other node's public key (64 hex characters)
// or its contact code; `body` is a string or anything JSON-shaped. Resolves
// to {txid, fee, total, size}; rejects with a message when the wallet
// refuses (over the hourly cap, too big, unfunded) or when no wallet is
// listening (the page was opened outside a viewer).
// `replies({after, limit})`: what the nodes this page wrote to have said
// since it wrote, oldest first; each has id, txid, block, frompubkey, body
// and json. Only replies to THIS page's messages -- never the rest of the
// wallet's inbox.
// `listen(handler, {after, every})`: polls replies, calls handler once per
// new one, returns a function that stops it. `after` defaults to "from now".
// `identity()`: this node's own pubkey and contact code.
// `sent()`: what this page has sent, newest first.
(function () {
  var seq = 0, waiting = {};

  function ask(message) {
    return new Promise(function (resolve, reject) {
      message.arcade = 'node';
      message.seq = ++seq;
      waiting[message.seq] = {resolve: resolve, reject: reject};
      setTimeout(function () {
        if (waiting[message.seq]) {
          delete waiting[message.seq];
          reject(new Error('no wallet is listening: open this page in a DogecoinArcade viewer'));
        }
      }, 60000);   // a send waits for the node to build and broadcast
      window.parent.postMessage(message, '*');
    });
  }

  window.addEventListener('message', function (e) {
    var m = e.data || {};
    if (m.arcade !== 'node' || !waiting[m.seq]) return;
    var w = waiting[m.seq];
    delete waiting[m.seq];
    if (m.ok) w.resolve(m); else w.reject(new Error(m.error || 'refused'));
  });

  var node = {
    identity: function () { return ask({op: 'identity'}); },
    send: function (to, body) { return ask({op: 'send', to: String(to), body: body}); },
    sent: function () { return ask({op: 'sent'}).then(function (r) { return r.sent; }); },
    replies: function (opts) {
      opts = opts || {};
      return ask({op: 'replies', after: opts.after || 0, limit: opts.limit || 100})
        .then(function (r) { return r.replies; });
    },
    listen: function (handler, opts) {
      opts = opts || {};
      var after = opts.after, stopped = false, timer = null;
      function tick() {
        node.replies({after: after || 0}).then(function (rs) {
          if (stopped) return;
          if (after === undefined) {
            // From now: what was said before this listener started is not
            // news, and a page that wants it asks replies() itself.
            after = rs.length ? rs[rs.length - 1].id : 0;
          } else {
            rs.forEach(function (r) { after = r.id; handler(r); });
          }
        }).catch(function () {}).then(function () {
          if (!stopped) timer = setTimeout(tick, opts.every || 10000);
        });
      }
      tick();
      return function () { stopped = true; if (timer) clearTimeout(timer); };
    }
  };

  window.arcade = window.arcade || {};
  window.arcade.node = node;
})();
