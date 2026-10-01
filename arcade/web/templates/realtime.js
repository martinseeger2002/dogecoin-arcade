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
    this._on = {message: [], join: [], leave: [], closed: []};
    var self = this;
    (said.members || []).forEach(function (p) { self._members[p.id] = p; });
  }
  Room.prototype.on = function (type, fn) {
    if (!this._on[type]) throw new Error('events are: message, join, leave, closed');
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
  function join(name, options) {
    options = options || {};
    name = String(name || '');
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

  window.arcade = window.arcade || {};
  window.arcade.realtime = {join: join, MAX_BYTES: 512};
})();
