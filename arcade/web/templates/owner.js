// arcade.owner -- a page sending from the wallet that made it and keeps it.
//
// A page asks the wallet to send with arcade.send (see /r/send) and the
// person is asked. A page this wallet CREATED and still HOLDS is that
// person's own words, so it is not asked: `arcade.owner.send` builds,
// signs and broadcasts at once, and the send is written in the approvals
// list as one that came from a page of your own. In anybody else's wallet
// the same call is refused with a message, and the page can fall back to
// asking. Testnet only -- nothing leaves mainnet without a person looking.
//
//   <script src="/r/owner.js"></script>
//   arcade.owner.identity().then(function (me) {
//     if (me.owner) return arcade.owner.send({kind: 'token', propertyid: 3,
//                                            amount: '5', to: winner});
//   });
//
// `identity()`: {owner, creator, holder, network} -- `owner` is true when
//   this page is running in the wallet that created it and holds it.
// `send(request)`: the same shape as arcade.send -- {kind: 'coins' |
//   'token' | 'inscription', to, amount, propertyid, inscription, from,
//   label, note}. Resolves to {txid, request, what, fee}; rejects when the
//   wallet is not the page's owner or the send cannot be built.
(function () {
  var seq = 0, waiting = {};

  function ask(message) {
    return new Promise(function (resolve, reject) {
      message.arcade = 'owner';
      message.seq = ++seq;
      waiting[message.seq] = {resolve: resolve, reject: reject};
      setTimeout(function () {
        if (waiting[message.seq]) {
          delete waiting[message.seq];
          reject(new Error('no wallet is listening: open this page in a DogecoinArcade viewer'));
        }
      }, 60000);
      window.parent.postMessage(message, '*');
    });
  }

  window.addEventListener('message', function (e) {
    var m = e.data || {};
    if (m.arcade !== 'owner' || !waiting[m.seq]) return;
    var w = waiting[m.seq];
    delete waiting[m.seq];
    if (m.ok) w.resolve(m); else w.reject(new Error(m.error || 'refused'));
  });

  window.arcade = window.arcade || {};
  window.arcade.owner = {
    identity: function () { return ask({op: 'identity'}); },
    send: function (request) {
      var m = {op: 'send'};
      for (var k in (request || {})) m[k] = request[k];
      return ask(m);
    }
  };
})();
