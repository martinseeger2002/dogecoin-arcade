// arcade.access -- what a game may ask of the viewer about access and nodes (2026-10-23).
//
// Asked of the viewer the page is running in, the way arcade.contacts is. Every card is the viewer's, drawn
// outside the game's frame where the game cannot draw; the game only ever gets a status back.
//
// status(): {wallet, signedIn, node, game, name} -- whether this game has wallet access here, whether the player
//   is signed in on this arcade, which arcade this is. No card.
// wallet(): asks for wallet access, over the game: the card names the game, its @publisher and address, and warns
//   about lookalike copies -- the same card as the Wallet access switch on the game's card, which it ticks.
//   Resolves {ok, wallet, status}: status "allowed", "already" or "declined". Rejects "sign in first ...".
// nodes(): the arcades and relays this arcade knows, each timed from this browser: [{base, host, ms, here,
//   arcade}], this arcade first, then by ms (null: no answer). `arcade` true: a place to play (a domain announced
//   on the chain); false: a relay that carries rooms only.
// move(base): offers to play this game on another arcade from nodes(). The card says what signing in there means;
//   yes leaves for its /play page (resolves {status: "moving"} just before). Also "declined" or "here". Only an
//   arcade this one lists, never an address the game makes up.
//
//   <script src="/r/access.js"></script>
//   arcade.access.status().then(function (s) { if (s.signedIn && !s.wallet) arcade.access.wallet(); });
(function () {
  var seq = 0, waiting = {};

  window.addEventListener('message', function (e) {
    var m = e.data || {};
    if (m.arcade !== 'access' || !waiting[m.seq]) return;
    var w = waiting[m.seq];
    delete waiting[m.seq];
    if (m.ok) w.resolve(m);
    else w.reject(new Error(m.error || 'refused'));
  });

  function ask(body, wait) {
    return new Promise(function (resolve, reject) {
      if (window.parent === window) { reject(new Error('no viewer: open this page in the arcade')); return; }
      var n = ++seq;
      waiting[n] = {resolve: resolve, reject: reject};
      // Long where the person reads a card; short where nobody is asked.
      setTimeout(function () {
        if (waiting[n]) { delete waiting[n]; reject(new Error('no viewer: nothing answered; open this page in the arcade')); }
      }, wait);
      body.arcade = 'access';
      body.seq = n;
      window.parent.postMessage(body, '*');
    });
  }

  function strip(m) { var o = {}; for (var k in m) if (k !== 'arcade' && k !== 'seq') o[k] = m[k]; return o; }
  window.arcade = window.arcade || {};
  window.arcade.access = {
    status: function () { return ask({op: 'status'}, 15000).then(strip); },
    wallet: function () { return ask({op: 'wallet'}, 300000).then(strip); },
    nodes: function () { return ask({op: 'nodes'}, 30000).then(function (m) { return m.nodes || []; }); },
    move: function (base) {
      if (base && typeof base === 'object') base = base.base;
      return ask({op: 'move', to: String(base || '')}, 300000).then(strip);
    }
  };
})();
