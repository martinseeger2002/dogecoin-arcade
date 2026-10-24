// arcade.llm -- the LLM service, for a game or app (arcade/llm.py; 2026-10-23: apps and players on every
// arcade buy model tokens, prepaid, from the sellers on the Exchange's LLM tab).
//
// Asked of the viewer the page is running in, the way arcade.contacts is. The signed-in player pays, from the credit
// they hold with a seller; the first time a page asks, the player is asked whether this game may use it.
//
//   <script src="/r/llm.js"></script>
//   arcade.llm.offers().then(function (offers) { ... });   // [{seller, name, tag, models: [{id}], prices: [...]}]
//   arcade.llm.chat({messages: [{role: 'user', content: 'Name a dragon'}],
//                    onToken: function (text) { ... }})     // optional: each piece as it is written
//     .then(function (r) { console.log(r.content, r.cost); },
//           function (e) { console.log(e.message); });      // "declined", "sign in first", "not enough credit" ...
//
// chat() takes OpenAI's chat fields (messages, max_tokens, temperature, tools ...) plus `seller` and `leg` (which
// seller and which coin pays; left out, the viewer picks one the player has credit with). It resolves {content,
// reasoning, usage: {tokens_in, tokens_out}, cost, left}.
//
//   arcade.llm.image({prompt: 'a red dragon', size: '1024x1024'}).then(function (r) { img.src = r.url; });
(function () {
  var seq = 0, waiting = {};

  window.addEventListener('message', function (e) {
    var m = e.data || {};
    if (m.arcade !== 'llm' || !waiting[m.seq]) return;
    var w = waiting[m.seq];
    if (m.part) { if (w.onToken && m.delta) w.onToken(m.delta, m); return; }
    delete waiting[m.seq];
    if (m.ok) w.resolve(m); else w.reject(new Error(m.error || 'refused'));
  });

  function ask(body, onToken) {
    return new Promise(function (resolve, reject) {
      if (window.parent === window) { reject(new Error('no viewer: open this page in the arcade')); return; }
      var n = ++seq;
      waiting[n] = {resolve: resolve, reject: reject, onToken: onToken};
      body.arcade = 'llm';
      body.seq = n;
      window.parent.postMessage(body, '*');
    });
  }

  function offers() {
    return ask({op: 'offers'}).then(function (m) { return m.offers || []; });
  }

  function chat(req) {
    req = req || {};
    var onToken = typeof req.onToken === 'function' ? req.onToken : null;
    var body = {op: 'chat', request: {}};
    Object.keys(req).forEach(function (k) { if (k !== 'onToken') body.request[k] = req[k]; });
    return ask(body, onToken).then(function (m) {
      return {content: m.content || '', reasoning: m.reasoning || '', usage: m.usage || {}, cost: m.cost || '',
              left: m.left || '', seller: m.seller, leg: m.leg};
    });
  }

  // image({prompt, size: '1024x1024', image: dataURL to edit, seller, leg}) -> {png: base64, seed, cost, left}: a picture
  // from a seller's image model, paid per picture (2026-10-23).
  function image(req) {
    var body = {op: 'image', request: {}};
    Object.keys(req || {}).forEach(function (k) { body.request[k] = req[k]; });
    return ask(body).then(function (m) {
      return {png: m.png || '', url: m.png ? 'data:image/png;base64,' + m.png : '', seed: m.seed, cost: m.cost || '',
              left: m.left || '', seller: m.seller, leg: m.leg};
    });
  }

  window.arcade = window.arcade || {};
  window.arcade.llm = {offers: offers, chat: chat, image: image};
})();
