// arcade.swap -- this page selling, or buying from, the shop it is.
//
// A shop is an inscription whose JSON lists what it sells (see the guide,
// "Shops"). The page inscribed with that JSON loads this and gets a
// storefront's whole back end: the listings as this node's ledger reads
// them, an offer from the shop's node, the buyer's approval, and the
// finished swap -- one transaction both sides signed, so both legs move or
// neither does. The page never holds a key, never sees an address it did
// not ask for, and cannot change the price: the terms are in the
// inscription, and both wallets read them from there.
//
//   <script src="/r/swap.js"></script>
//   arcade.swap.shop().then(function (s) { draw(s.listings); });
//   arcade.swap.buy(0, {step: function (what) { status.textContent = what; }})
//     .then(function (r) { console.log('swapped in', r.txid); })
//     .catch(function (e) { alert(e.message); });
//
// `shop()`: {shop, node, seller, listings, mine, open, ready, height, from}.
//   Each listing: {n, give, take, text, available} -- `available` is null or
//   why the shop cannot give it right now. `mine` says this page is being
//   looked at by the wallet that keeps it; `open` that its creator still
//   holds it; `ready` that this chain reads swaps at its current height.
// `offer(n)`: asks the shop's node for an offer on listing n. Resolves to
//   {txid, buyer} -- the message it went in, and the address that will pay
//   and receive. The answer comes back on the chain: see `awaitOffer`.
// `awaitOffer(txid, opts)`: resolves to the offer once the shop answers,
//   rejects with the shop's reason when it refuses. Polls every 10 s, for up
//   to 30 minutes by default (opts.timeout, ms).
// `accept(offer)`: puts the offer in front of the buyer -- the approvals
//   pop-up, with the transaction as built. Resolves to a request id.
// `status(request)`: the request, as /r/send/<id> describes one.
// `awaitDecision(request, opts)`: resolves once approved, rejects when
//   refused, expired, or failed.
// `awaitSwap(offer, opts)`: resolves to {txid} once the shop's node has
//   signed and sent; rejects with its reason if it would not.
// `buy(n, opts)`: all of the above in order. opts.step(text) is told what
//   is happening; opts.timeout as for awaitOffer.
//
// Timing: every step but the buyer's own yes travels as a node-to-node
// message, and a message is in a block or it is nowhere. On testnet that
// is a minute or several, twice, and then the swap itself. A storefront
// should say so rather than spin.
(function () {
  var seq = 0, waiting = {};

  function ask(message) {
    return new Promise(function (resolve, reject) {
      message.arcade = 'swap';
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
    if (m.arcade !== 'swap' || !waiting[m.seq]) return;
    var w = waiting[m.seq];
    delete waiting[m.seq];
    if (m.ok) w.resolve(m); else w.reject(new Error(m.error || 'refused'));
  });

  function until(check, opts, what) {
    // Poll `check` (a promise of a result or null) until it gives one.
    opts = opts || {};
    var every = opts.every || 10000, deadline = Date.now() + (opts.timeout || 1800000);
    return new Promise(function (resolve, reject) {
      function tick() {
        check().then(function (found) {
          if (found !== null && found !== undefined) return resolve(found);
          if (Date.now() > deadline) return reject(new Error('gave up waiting for ' + what));
          setTimeout(tick, every);
        }).catch(function (err) {
          // A wallet that did not answer once is asked again; a refusal is final.
          if (err && /not listening|did not answer/.test(err.message)) setTimeout(tick, every);
          else reject(err);
        });
      }
      tick();
    });
  }

  function replySaying(test) {
    // The newest reply from the shop's node that passes `test`.
    return function () {
      return ask({op: 'replies', after: 0, limit: 100}).then(function (r) {
        var rs = r.replies || [], hit = null;
        rs.forEach(function (x) { if (x.json && test(x.json)) hit = x.json; });
        return hit;
      });
    };
  }

  var swap = {
    shop: function () { return ask({op: 'shop'}); },
    offer: function (n) { return ask({op: 'offer', listing: n}); },
    accept: function (offer) { return ask({op: 'accept', offer: offer}); },
    status: function (request) {
      return ask({op: 'status', request: request}).then(function (r) { return r.request; });
    },
    awaitOffer: function (txid, opts) {
      return until(replySaying(function (j) { return j.swap === 'offer' && j.re === txid; }),
                   opts, "the shop's offer")
        .then(function (j) {
          if (!j.ok) throw new Error(j.error || 'the shop refused');
          return j.offer;
        });
    },
    awaitDecision: function (request, opts) {
      return until(function () {
        return swap.status(request).then(function (r) {
          return r.status === 'pending' ? null : r;
        });
      }, {every: (opts && opts.every) || 3000, timeout: (opts && opts.timeout) || 3600000},
         'the decision')
        .then(function (r) {
          if (r.status !== 'sent') throw new Error(r.error || ('the request was ' + r.status));
          return r;
        });
    },
    awaitSwap: function (offer, opts) {
      var id = typeof offer === 'string' ? offer : offer.id;
      return until(replySaying(function (j) { return j.swap === 'sign' && j.offer === id; }),
                   opts, "the shop's signature")
        .then(function (j) {
          if (!j.ok) throw new Error(j.error || 'the shop would not sign');
          return {txid: j.txid, offer: id};
        });
    },
    buy: function (n, opts) {
      opts = opts || {};
      var step = opts.step || function () {}, offer;
      return swap.shop().then(function (s) {
        if (!s.ready) throw new Error('this chain does not read swaps yet (from block ' + s.from + ')');
        if (s.mine) throw new Error('this is your own shop');
        var listing = s.listings[n];
        if (!listing) throw new Error('no listing ' + n);
        if (listing.available) throw new Error(listing.available);
        step('asking the shop for an offer');
        return swap.offer(n);
      }).then(function (sent) {
        step('offer asked for; waiting for the shop to answer (a block or two)');
        return swap.awaitOffer(sent.txid, opts);
      }).then(function (o) {
        offer = o;
        step('offered ' + describe(o.give) + ' for ' + describe(o.take) + '; look at the approval');
        return swap.accept(o);
      }).then(function (r) {
        return swap.awaitDecision(r.request, opts);
      }).then(function () {
        step('signed; waiting for the shop to sign and send (a block or two)');
        return swap.awaitSwap(offer, opts);
      }).then(function (done) {
        step('swapped in ' + done.txid);
        done.give = offer.give; done.take = offer.take;
        return done;
      });
    }
  };

  function describe(leg) {
    if (!leg) return '?';
    if (leg.kind === 'random') return 'a random ' + leg.collection;
    if (leg.kind === 'inscription') {
      return 'inscription #' + leg.number + (leg.collection ? ' (' + leg.collection +
        (leg.edition != null ? ' #' + leg.edition : '') + ')' : '');
    }
    if (leg.kind === 'token') return leg.amount + ' ' + (leg.name || 'token ' + leg.propertyid);
    return leg.amount + ' coins';
  }
  swap.describe = describe;

  window.arcade = window.arcade || {};
  window.arcade.swap = swap;
})();
