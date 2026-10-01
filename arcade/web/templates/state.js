// arcade.state -- this game's own small record for each piece, public and ordered.
//
// A piece can carry state that belongs to a game -- a condition, a charge, a
// count -- which travels with it through every trade, sale and escrow, can be
// changed only by that game (its judge decides, its publisher writes), and is
// readable by anyone, on every arcade.
//
//   <script src="/r/state.js"></script>
//   arcade.state.get(swordTxid).then(function (games) { ... });
//   arcade.state.update([{piece: swordTxid, state: {c: 61}}], {replay: run})
//     .then(function (r) { console.log('published', r.txids); });
//
// `get(piece)`: [{family, game, name, state, seq, height, show}] -- one entry
//   per game that has written state for the piece.
// `get(piece, family)`: that family's entry, or null.
// `update(items, {replay, game})`: ask this game (or `game`) to set new state;
//   items are {piece, state} with state a small JSON object (96 bytes at most).
//   The game's judge reads `replay` and decides. Resolves {txids, updates}.
//   It is in the chain's pool in seconds and in a block within about a minute.
(function () {
  if (window.arcade && window.arcade.state) return;
  var seq = 0, waiting = {};

  function ask(message) {
    return new Promise(function (resolve, reject) {
      message.arcade = 'state';
      message.seq = ++seq;
      waiting[message.seq] = {resolve: resolve, reject: reject};
      setTimeout(function () {
        if (waiting[message.seq]) {
          delete waiting[message.seq];
          reject(new Error('no viewer is listening: open this page in a DogecoinArcade viewer'));
        }
      }, 120000);
      window.parent.postMessage(message, '*');
    });
  }

  window.addEventListener('message', function (e) {
    var m = e.data || {};
    if (m.arcade !== 'state' || !waiting[m.seq]) return;
    var w = waiting[m.seq];
    delete waiting[m.seq];
    if (m.ok) w.resolve(m); else w.reject(new Error(m.error || 'refused'));
  });

  window.arcade = window.arcade || {};
  window.arcade.state = {
    get: function (piece, family) {
      var path = '/r/state/' + (family ? encodeURIComponent(family) + '/' : '') + encodeURIComponent(piece);
      return fetch(path).then(function (r) {
        if (r.status === 404) return null;
        return r.json().then(function (j) { return family ? j : (j.games || []); });
      });
    },
    update: function (items, o) {
      o = o || {};
      return ask({op: 'update', items: items || [], replay: o.replay, game: o.game || ''});
    }
  };
})();
