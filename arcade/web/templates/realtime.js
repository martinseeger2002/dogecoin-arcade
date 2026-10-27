// arcade.realtime -- rooms where players on any arcade node meet, live.
//
// An inscribed page runs in a sandbox that can reach nothing but the page
// showing it, so this asks that page (the viewer) over postMessage, the same
// way arcade.storage does, and the viewer carries it over the arcade mesh:
// nodes talking directly to each other, with no server and no website.
//
// The mesh never reads what you send. A room is a name and a message is a
// string (or any JSON-able value, sent as JSON) of at most 512 bytes; send
// about five a second per player at most. Nothing is kept: what has to be true
// later belongs on the chain.
//
//   <script src="/r/realtime.js"></script>
//   arcade.realtime.join('town').then(function (room) {
//     if (!room.online) return playSolo();
//     room.on('join',    function (p) { addPlayer(p); });
//     room.on('leave',   function (p) { removePlayer(p); });
//     room.on('message', function (data, p) { movePlayer(p, data); });
//     room.members().forEach(addPlayer);
//     room.send({x: 10, y: 4, facing: 'n'});
//   });
//
// A player is {id, address, tag, guest, node}. `id` is unique in the room and
// is what to key your state by; `address` and `tag` are the player's chain
// address and @tag, proved by their own key, or null for a guest.
//
// BUSY PLACES (2026-10-12: tens of thousands of players). One room for a whole
// town means everybody hears everybody: a thousand players at five messages a
// second is five thousand a second for each of them. Two helpers:
//
//   join('town', {cap: 50})      a full room sends you on: town, town#2, town#3 ...
//                                (room.name says which you got)
//
//   arcade.realtime.area('world', {cell: 64, view: 48, x: 10, y: 20}).then(function (a) {
//     a.on('message', function (data, p) { movePlayer(p, data); });   // anyone within sight
//     a.on('join', addPlayer); a.on('leave', removePlayer);
//     a.move(x, y);              // as you walk: cells come and go by themselves
//     a.send({x: x, y: y});      // to the cell you stand in; whoever can see it hears
//   });
//
// The world is cut into square cells `cell` units wide; you are in the cells
// within `view` of where you stand (the rooms 'world@3,-1' and so on) and hear
// those, and you say things only in your own. Somebody on the far side of town
// costs you nothing. Keep `view` below `cell` and you are in at most four cells.
//
// THE FAST LANE (2026-10-18), for action games of up to about 8 players (a room connects up to 20):
//
//   arcade.realtime.join('match-1', {fast: true}).then(function (room) {
//     room.fast.on('message', function (data, p) { moveTank(p, data); });
//     room.fast.on('peer', function (p, state) { ... });  // 'connecting', 'direct', 'room' or 'gone'
//     setInterval(function () { room.fast.send({x: x, y: y, yaw: yaw}); }, 33);  // 30 a second
//     room.fast.send({kill: victimId}, {reliable: true});   // in order, never lost
//   });
//
// Players are connected to each other directly, browser to browser (WebRTC), so nothing waits on a node.
// fast.send() goes to every other player in the room. Plain sends are unordered and never resent: a lost one is
// skipped, which is what a position wants. {reliable: true} sends arrive, in order. Up to 16 KB a message.
// A pair of players who cannot connect directly (both behind strict routers, and the game's builder has not
// leased a relay) is in state 'room': their sends go through the room at about three a second, reliable ones in
// order and of the rest only the latest, so the game keeps working, slower, for those two. `room.fast` is null
// where the viewer has no WebRTC: use room.send then.
//
// A HOST AND ITS PLAYERS (2026-10-27): for more players than everybody-to-everybody can carry, link a star.
//
//   arcade.realtime.join('zone-1', {fast: true, link: []}).then(function (room) {   // link with nobody yet
//     room.fast.link([hostId]);                         // a player: one connection, to the host
//     // the host instead: room.fast.link(null, {max: 60})   -- everybody, up to 60
//     room.fast.send(bytes, {to: playerId});            // to that player only (an id or an array of ids)
//     room.fast.on('message', function (data, p) {       // data: an ArrayBuffer when bytes were sent
//       if (data instanceof ArrayBuffer) readPacket(new DataView(data));
//     });
//   });
//
// A pair links only when both want to; a player who will not is 'gone' to the other. Bytes (an ArrayBuffer or a
// typed array) arrive as an ArrayBuffer, up to 16 KB; to a player with no direct link (state 'room') only up to
// 300 bytes go, as the room carries them.
//
// CAMERA & MICROPHONE (2026-10-22), for a video call or a game with voice. The card says "media": true and the
// player turns on Camera & microphone on it. A page cannot open the camera itself; the viewer does, and draws:
//
//   arcade.realtime.join('call-x7', {fast: true}).then(function (room) {
//     room.media.start({video: true, audio: true}).then(function (r) {   // {ok, video, audio} or {ok: false, error}
//       if (r.error === 'off') return say('Turn on Camera & microphone on this game card');   // the viewer says so too
//     });
//     room.media.place('me', {x: 10, y: 10, w: 160, h: 120}, {fit: 'cover', mirror: true});
//     room.fast.on('peer', function (p, state) {
//       if (state === 'direct' && p.address === friend) {    // ONLY the player you mean: a room's name can be guessed
//         room.media.share(p.id, true);                      // your picture and sound go to them
//         room.media.hear(p.id, true);                       // their sound plays (nobody's does until you say)
//         room.media.place(p.id, {x: 0, y: 0, w: 640, h: 480}, {fit: 'contain'});
//       }
//     });
//     room.media.on('media', function (p, has) { ... });    // has = {video, audio}: they are sending
//     room.media.on('media-off', function (p) { ... });
//     room.media.on('state', function (s) { ... });         // 'on', 'off' (the player pressed Stop too), 'denied'
//     room.media.share(p.id, {video: false, audio: true});  // one kind only: your voice, not your picture
//     room.media.start({video: true, audio: true});          // again, mid-call: the camera joins the microphone
//     room.media.mute({video: false, audio: true});           // true = muted
//     room.media.stop();                                      // camera and microphone off, every share ends
//   });
//
// Rectangles are in this page's CSS pixels (getBoundingClientRect), placed again when your layout moves; null hides
// one. A picture can only be shared over a direct link ('not direct' otherwise: media never goes through the room).
// The viewer always shows that the camera or microphone is on, with a Stop button, over your page.
(function () {
  if (window.arcade && window.arcade.realtime) return;
  var seq = 0, waiting = {}, rooms = {};

  function ask(message, wait) {
    return new Promise(function (resolve, reject) {
      message.arcade = 'realtime';
      message.seq = ++seq;
      waiting[message.seq] = {resolve: resolve, reject: reject};
      setTimeout(function () {
        if (waiting[message.seq]) {
          delete waiting[message.seq];
          reject(new Error('no viewer is listening: open this page in a DogecoinArcade viewer'));
        }
      }, wait || 20000);
      window.parent.postMessage(message, '*');
    });
  }

  window.addEventListener('message', function (e) {
    var m = e.data || {};
    if (m.arcade !== 'realtime') return;
    if (m.event) {                                  // pushed: something happened in a room
      var room = rooms[m.event.room];
      if (room) room._hear(m.event);
      return;
    }
    var w = waiting[m.seq];
    if (!w) return;
    delete waiting[m.seq];
    if (m.ok) w.resolve(m); else w.reject(new Error(m.error || 'refused'));
  });

  function Room(name, said) {
    this.name = name;
    this.online = !!said.online;
    this.me = said.me || null;
    this.why = said.why || '';
    this._members = {};
    this._on = {message: [], join: [], leave: [], closed: [], rejoined: [], reconnecting: []};
    var self = this;
    (said.members || []).forEach(function (p) { self._members[p.id] = p; });
    this.fast = said.fast ? new Fast(this) : null;
    this.media = said.fast && said.media ? new Media(this) : null;
  }

  // CAMERA & MICROPHONE (2026-10-22), on a fast room. The viewer opens them (a sandboxed page cannot), only while the
  // game's card has Camera & microphone on, sends them to the players you name and draws the pictures over your page
  // where you say. See the guide at the top of this file.
  function Media(room) {
    this.room = room;
    this._on = {media: [], 'media-off': [], state: []};
  }
  Media.prototype.on = function (type, fn) {
    if (!this._on[type]) throw new Error('media events are: media, media-off, state');
    this._on[type].push(fn);
    return this;
  };
  Media.prototype._ask = function (what, extra, wait) {
    var m = {op: 'media', room: this.room.name, do: what};
    for (var k in extra || {}) m[k] = extra[k];
    return ask(m, wait);
  };
  Media.prototype.start = function (options) {
    options = options || {video: true, audio: true};
    return this._ask('start', {video: !!options.video, audio: !!options.audio}, 600000)
      .then(function (said) { return {ok: true, video: !!said.video, audio: !!said.audio}; },
            function (err) { return {ok: false, error: err.message}; });
  };
  Media.prototype.stop = function () { return this._ask('stop'); };
  Media.prototype.share = function (id, on) {
    // on: true / false, or {video, audio} for one kind only (your voice to everybody, your picture to a few)
    var said = on && typeof on === 'object' ? {video: !!on.video, audio: !!on.audio} : on !== false;
    return this._ask('share', {peer: String(id), on: said})
      .then(function () { return {ok: true}; }, function (err) { return {ok: false, error: err.message}; });
  };
  Media.prototype.hear = function (id, on) { return this._ask('hear', {peer: String(id), on: on !== false}); };
  // place(id | 'me', {x, y, w, h} in this page's CSS pixels, or null to hide, {fit: 'cover' | 'contain', mirror})
  Media.prototype.place = function (who, rect, options) {
    options = options || {};
    return this._ask('place', {who: who === 'me' ? 'me' : String(who),
                               rect: rect ? {x: rect.x, y: rect.y, w: rect.w != null ? rect.w : rect.width,
                                             h: rect.h != null ? rect.h : rect.height} : null,
                               fit: options.fit, mirror: !!options.mirror});
  };
  Media.prototype.mute = function (which) {
    which = which || {};
    var m = {};
    if (typeof which.video === 'boolean') m.video = which.video;
    if (typeof which.audio === 'boolean') m.audio = which.audio;
    return this._ask('mute', m);
  };
  Media.prototype._emit = function (type, a, b) { Room.prototype._emit.call(this, type, a, b); };

  function Fast(room) {
    this.room = room;
    this._peers = {};
    this._on = {message: [], peer: []};
  }
  Fast.prototype.on = function (type, fn) {
    if (!this._on[type]) throw new Error('fast lane events are: message, peer');
    this._on[type].push(fn);
    return this;
  };
  Fast.prototype.send = function (value, options) {
    options = options || {};
    var data;
    if (value instanceof ArrayBuffer) data = value.slice(0);
    else if (ArrayBuffer.isView(value)) data = value.buffer.slice(value.byteOffset, value.byteOffset + value.byteLength);
    else data = typeof value === 'string' ? value : JSON.stringify(value);
    var m = {arcade: 'realtime', seq: 0, op: 'fast', room: this.room.name, data: data, reliable: !!options.reliable};
    if (options.to != null) m.to = [].concat(options.to).map(String);
    window.parent.postMessage(m, '*');
  };
  // With whom this player links directly: an array of player ids, or null for everybody (the default). max: how
  // many links at most (19 unless a host asks for more, up to 60). Answers {ok, fast}.
  Fast.prototype.link = function (ids, options) {
    return ask({op: 'fastlink', room: this.room.name, link: ids == null ? null : [].concat(ids).map(String),
                max: (options && options.max) | 0}).then(function (said) { return {ok: true, fast: !!said.fast}; },
                                                         function (err) { return {ok: false, error: err.message}; });
  };
  // {player id: 'connecting' | 'direct' | 'room'}
  Fast.prototype.peers = function () {
    var out = {};
    for (var k in this._peers) out[k] = this._peers[k];
    return out;
  };
  Fast.prototype._emit = function (type, a, b) { Room.prototype._emit.call(this, type, a, b); };
  Room.prototype.on = function (type, fn) {
    if (!this._on[type]) throw new Error('events are: message, join, leave, closed, rejoined, reconnecting');
    this._on[type].push(fn);
    return this;
  };
  Room.prototype.members = function () {
    var out = [];
    for (var k in this._members) out.push(this._members[k]);
    return out;
  };
  Room.prototype.send = function (value) {
    if (!this.online) return Promise.reject(new Error('this room is not online'));
    var text = typeof value === 'string' ? value : JSON.stringify(value);
    return ask({op: 'send', room: this.name, data: text});
  };
  Room.prototype.leave = function () {
    delete rooms[this.name];
    this.online = false;
    return ask({op: 'leave', room: this.name}).catch(function () {});
  };
  Room.prototype._emit = function (type, a, b) {
    this._on[type].forEach(function (fn) { try { fn(a, b); } catch (err) { setTimeout(function () { throw err; }); } });
  };
  Room.prototype._hear = function (ev) {
    var p = ev.member;
    if (ev.type === 'join') { this._members[p.id] = p; this._emit('join', p); }
    else if (ev.type === 'leave') { delete this._members[p.id]; this._emit('leave', p); }
    else if (ev.type === 'closed') { this.online = false; this._emit('closed', ev.why || ''); }
    else if (ev.type === 'me') { this.me = p; }    // back after the node restarted, under a new name
    // This page's own seat was taken again (its connection dropped for longer than the
    // node waits): the others saw it leave and come back, so send your state again.
    else if (ev.type === 'rejoined') { this.me = p || this.me; this.online = true; this._emit('rejoined', this.me); }
    // The line dropped and the page is taking the seat again by itself, for as long as it takes (2026-10-18):
    // a game can say "reconnecting"; 'rejoined' follows when it is back.
    else if (ev.type === 'reconnecting') { this.online = false; this._emit('reconnecting'); }
    else if (ev.type === 'fast' && this.fast) {
      var said = ev.data;
      if (typeof said === 'string') { try { said = JSON.parse(said); } catch (err) {} }
      this.fast._emit('message', said, p);
    }
    else if (ev.type === 'media' && this.media) this.media._emit('media', p, {video: !!ev.video, audio: !!ev.audio});
    else if (ev.type === 'media-off' && this.media) this.media._emit('media-off', p);
    else if (ev.type === 'media-state' && this.media) this.media._emit('state', ev.state);
    else if (ev.type === 'peer' && this.fast) {
      if (ev.state === 'gone') delete this.fast._peers[p.id]; else this.fast._peers[p.id] = ev.state;
      this.fast._emit('peer', p, ev.state);
    }
    else if (ev.type === 'message') {
      var data = ev.data;
      if (typeof data === 'string') { try { data = JSON.parse(data); } catch (err) {} }
      else if (ev.b64) data = ev.b64;
      this._emit('message', data, p);
    }
  };

  // RINGING (arcade/ring.py, 2026-10-23): ring somebody while your app is closed on their phone. It wakes them only
  // if they turned on Notifications on your app's card and you are in their address book.
  //   arcade.realtime.ring(address, {line: 'Video call', link: 'call=x7'}) -> {ok, rung, why}
  //   arcade.realtime.ringed() -> {from, tag, link, line, at} when a tapped notification opened your app, else null
  function ring(address, options) {
    options = options || {};
    return ask({op: 'ring', room: '', to: String(address || ''), line: String(options.line || '').slice(0, 80),
                link: String(options.link || '').slice(0, 200)}, 60000)
      .then(function (said) { return {ok: true, rung: !!said.rung, why: said.why || ''}; },
            function (err) { return {ok: false, error: err.message}; });
  }
  function ringed() {
    return ask({op: 'ringed', room: ''}).then(function (said) { return said.ring || null; }, function () { return null; });
  }

  // join(room, {game}): `game` defaults to this inscription, so only copies of
  // the same page meet. A game whose versions are separate inscriptions names
  // its family instead (its registry's id, say) and every version shares rooms.
  function joinOne(name, options) {
    if (rooms[name]) return Promise.resolve(rooms[name]);
    var m = {op: 'join', room: name, game: options.game || '', fast: !!options.fast};
    if (Array.isArray(options.link)) m.link = options.link.map(String);
    if (options.max) m.max = options.max | 0;
    return ask(m).then(function (said) {
      var room = new Room(name, said);
      if (room.online) rooms[name] = room;
      return room;
    }, function (err) {
      // Not being able to play together is never a reason not to play.
      return new Room(name, {online: false, why: String(err && err.message || err)});
    });
  }

  function join(name, options) {
    options = options || {};
    name = String(name || '');
    var cap = options.cap | 0;
    if (!cap) return joinOne(name, options);
    // A full room sends you on to the next of its name (2026-10-12): town, town#2, ...
    function tryN(n) {
      var label = n === 1 ? name : name + '#' + n;
      return joinOne(label, options).then(function (room) {
        if (!room.online || n >= 50) return room;
        var others = room.members().filter(function (p) { return !room.me || p.id !== room.me.id; });
        if (others.length < cap) return room;
        return room.leave().then(function () { return tryN(n + 1); });
      });
    }
    return tryN(1);
  }

  // area(base, {game, cell, view, x, y}): see BUSY PLACES above.
  function Area(base, options) {
    this.base = base; this.options = options;
    this.cell = Math.max(1, +options.cell || 64);
    this.view = Math.max(0, options.view == null ? this.cell / 2 : +options.view);
    this.rooms = {};            // cell key -> Room
    this.where = {};            // player id -> {cell key: true}
    this.players = {};          // player id -> player
    this.online = false; this.me = null; this.here = null;
    this._on = {message: [], join: [], leave: [], closed: [], rejoined: [], reconnecting: []};
  }
  Area.prototype.on = Room.prototype.on;
  Area.prototype._emit = Room.prototype._emit;
  Area.prototype.key = function (cx, cy) { return this.base + '@' + cx + ',' + cy; };
  Area.prototype.cellOf = function (x, y) { return [Math.floor(x / this.cell), Math.floor(y / this.cell)]; };
  Area.prototype.wanted = function (x, y) {
    var out = {}, v = this.view, c = this.cell;
    for (var cx = Math.floor((x - v) / c); cx <= Math.floor((x + v) / c); cx++)
      for (var cy = Math.floor((y - v) / c); cy <= Math.floor((y + v) / c); cy++) out[this.key(cx, cy)] = true;
    return out;
  };
  Area.prototype._seen = function (key, p, present) {
    if (this.me && p.id === this.me.id) return;
    var w = this.where[p.id] || (this.where[p.id] = {});
    var before = Object.keys(w).length;
    if (present) w[key] = true; else delete w[key];
    var after = Object.keys(w).length;
    if (!before && after) { this.players[p.id] = p; this._emit('join', p); }
    else if (before && !after) { delete this.where[p.id]; delete this.players[p.id]; this._emit('leave', p); }
  };
  Area.prototype._attach = function (key, room) {
    var self = this;
    this.rooms[key] = room;
    if (room.me) this.me = room.me;
    room.members().forEach(function (p) { self._seen(key, p, true); });
    room.on('join', function (p) { if (self.rooms[key] === room) self._seen(key, p, true); });
    room.on('leave', function (p) { if (self.rooms[key] === room) self._seen(key, p, false); });
    room.on('message', function (data, p) { if (self.rooms[key] === room) self._emit('message', data, p, key); });
    room.on('closed', function (why) { if (self.rooms[key] === room) { delete self.rooms[key]; self._emit('closed', why); } });
    room.on('reconnecting', function () { if (self.rooms[key] === room) self._emit('reconnecting', key); });
    room.on('rejoined', function (me) { if (self.rooms[key] === room) self._emit('rejoined', me, key); });
  };
  Area.prototype.move = function (x, y) {
    var self = this, want = this.wanted(x, y), jobs = [];
    var c = this.cellOf(x, y); this.here = this.key(c[0], c[1]); want[this.here] = true;
    this._want = want;                              // where we are NOW: a join that ends late checks this
    Object.keys(this.rooms).forEach(function (key) {
      if (want[key]) return;
      var room = self.rooms[key]; delete self.rooms[key];
      if (room === 'joining') return;               // its join will see it is no longer wanted
      Object.keys(self.where).forEach(function (id) { if (self.where[id][key]) self._seen(key, self.players[id], false); });
      jobs.push(room.leave());
    });
    Object.keys(want).forEach(function (key) {
      if (self.rooms[key]) return;
      self.rooms[key] = 'joining';
      jobs.push(joinOne(key, self.options).then(function (room) {
        if (!room.online) { if (self.rooms[key] === 'joining') delete self.rooms[key]; return; }
        self.online = true;
        if (self._want[key] && self.rooms[key] === 'joining') self._attach(key, room);
        else { if (self.rooms[key] === 'joining') delete self.rooms[key]; return room.leave(); }
      }));
    });
    return Promise.all(jobs).then(function () { return self; });
  };
  Area.prototype.send = function (value) {
    var room = this.rooms[this.here];
    if (!room || room === 'joining') return Promise.reject(new Error('not in a cell yet'));
    return room.send(value);
  };
  Area.prototype.members = function () {
    var out = [];
    for (var k in this.players) out.push(this.players[k]);
    return out;
  };
  Area.prototype.leave = function () {
    var self = this;
    return Promise.all(Object.keys(this.rooms).map(function (key) {
      var room = self.rooms[key]; delete self.rooms[key];
      return room && room.leave ? room.leave() : null;
    }));
  };
  function area(base, options) {
    options = options || {};
    var a = new Area(String(base || ''), options);
    return a.move(+options.x || 0, +options.y || 0);
  }

  window.arcade = window.arcade || {};
  window.arcade.realtime = {join: join, area: area, ring: ring, ringed: ringed, MAX_BYTES: 512};
})();
