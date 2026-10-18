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
(function () {
  if (window.arcade && window.arcade.realtime) return;
  var seq = 0, waiting = {}, rooms = {};

  function ask(message) {
    return new Promise(function (resolve, reject) {
      message.arcade = 'realtime';
      message.seq = ++seq;
      waiting[message.seq] = {resolve: resolve, reject: reject};
      setTimeout(function () {
        if (waiting[message.seq]) {
          delete waiting[message.seq];
          reject(new Error('no viewer is listening: open this page in a DogecoinArcade viewer'));
        }
      }, 20000);
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
  }
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
    else if (ev.type === 'message') {
      var data = ev.data;
      if (typeof data === 'string') { try { data = JSON.parse(data); } catch (err) {} }
      else if (ev.b64) data = ev.b64;
      this._emit('message', data, p);
    }
  };

  // join(room, {game}): `game` defaults to this inscription, so only copies of
  // the same page meet. A game whose versions are separate inscriptions names
  // its family instead (its registry's id, say) and every version shares rooms.
  function joinOne(name, options) {
    if (rooms[name]) return Promise.resolve(rooms[name]);
    return ask({op: 'join', room: name, game: options.game || ''}).then(function (said) {
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
  window.arcade.realtime = {join: join, area: area, MAX_BYTES: 512};
})();
