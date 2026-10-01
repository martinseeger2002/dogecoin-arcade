// arcade.escrow -- a player's NFTs and tokens held for this game, released by
// the game's own judge.
//
// A game names a referee and a judge in its inscription's JSON:
//   {"game": {...}, "escrow": {"referee": {"pubkey": "02…", "node": "…"},
//                              "judge": "<inscription>", "unlock_hours": 24}}
// A player puts things in (`deposit`); while they are there, the game's referee
// releases them to whoever the judge says (`release`); after the unlock time the
// player can take everything back from their wallet, whatever the game says.
// The page never touches a key: the player confirms a deposit in their own
// viewer, and signs each move there.
//
//   <script src="/r/escrow.js"></script>
//   arcade.escrow.open({hours: 6}).then(function (e) {
//     return arcade.escrow.deposit(e.escrow, [{inscription: swordTxid},
//                                             {token: 26, amount: '50'}]);
//   });
//   arcade.escrow.release({escrow: e.escrow, owner: e.owner, owner_pubkey: e.owner_pubkey,
//                          unlock: e.unlock, to: winner, items: [{inscription: swordTxid}],
//                          replay: {...what your judge reads...}});
//
// `open({hours, game})`: this player's escrow in this game (or `game`, an
//   inscription id); unlocks on the hour, no later than the game allows.
//   Resolves {escrow, unlock, owner, owner_pubkey, referee, game}. Sends nothing.
// `deposit(escrow, items)`: one card for all of them, then one transaction each.
//   An item is {inscription: id} or {token: id, amount: "50"}. Resolves {txids}.
// `status(escrow)`: {inscriptions, tokens, coins, owner, unlock, open}.
// `release({...})`: anybody may ask; the game's judge decides. Needs the escrow's
//   facts (escrow, owner, owner_pubkey, unlock -- what `open` returned; share them
//   with other players your own way) and, for a referee on another node, `coins`
//   (from `status`). Resolves {txids, verdict}.
// `mine()`: this player's escrows in this game that still hold something.
(function () {
  if (window.arcade && window.arcade.escrow) return;
  var seq = 0, waiting = {};

  function ask(message, wait) {
    return new Promise(function (resolve, reject) {
      message.arcade = 'escrow';
      message.seq = ++seq;
      waiting[message.seq] = {resolve: resolve, reject: reject};
      setTimeout(function () {
        if (waiting[message.seq]) {
          delete waiting[message.seq];
          reject(new Error('no viewer is listening: open this page in a DogecoinArcade viewer'));
        }
      }, wait || 60000);
      window.parent.postMessage(message, '*');
    });
  }

  window.addEventListener('message', function (e) {
    var m = e.data || {};
    if (m.arcade !== 'escrow' || !waiting[m.seq]) return;
    var w = waiting[m.seq];
    delete waiting[m.seq];
    if (m.ok) w.resolve(m); else w.reject(new Error(m.error || 'refused'));
  });

  window.arcade = window.arcade || {};
  window.arcade.escrow = {
    open: function (o) { o = o || {}; return ask({op: 'open', hours: o.hours, game: o.game || ''}); },
    deposit: function (escrow, items) {
      return ask({op: 'deposit', escrow: escrow, items: items || []}, 900000);
    },
    status: function (escrow) { return ask({op: 'status', escrow: escrow}); },
    release: function (r) { return ask(Object.assign({op: 'release'}, r || {}), 180000); },
    mine: function () { return ask({op: 'mine'}); }
  };
})();
