// Node harness for arcade/web/templates/realtime.js: a stand-in viewer in memory.
// Rooms hold members; the test adds "other players" by pushing events, and checks what the page saw.
const path = require('path'), fs = require('fs'), assert = require('assert');
const listeners = [], log = [];
const viewer = { rooms: {} };               // room -> {id: player}
const me = { id: 'me', tag: 'me' };
function deliver(msg) { for (const fn of listeners) fn({ data: msg }); }
global.window = {
  addEventListener: (t, fn) => { if (t === 'message') listeners.push(fn); },
  parent: { postMessage(m) {
    log.push(m.op + ' ' + m.room);
    const R = viewer.rooms[m.room] = viewer.rooms[m.room] || {};
    let reply = { arcade: 'realtime', seq: m.seq, ok: true };
    if (m.op === 'join') { reply = Object.assign(reply, { online: true, me, members: Object.values(R) }); R.me = me; }
    if (m.op === 'leave') delete R.me;
    if (m.op === 'send') (viewer.sent = viewer.sent || []).push([m.room, m.data]);
    setTimeout(() => deliver(reply), 1);
  } },
};
eval(fs.readFileSync(path.join(__dirname, '../../arcade/web/templates/realtime.js'), 'utf8'));
const rt = window.arcade.realtime;
const other = (room, id) => { const p = { id, tag: id }; (viewer.rooms[room] = viewer.rooms[room] || {})[id] = p;
  deliver({ arcade: 'realtime', event: { room, type: 'join', member: p } }); };
(async () => {
  // 1. cap: a full room sends you on
  viewer.rooms['town'] = { a: { id: 'a' }, b: { id: 'b' } };
  const t = await rt.join('town', { cap: 2 });
  assert.strictEqual(t.name, 'town#2', 'town was full (2 others): on to town#2');
  const t2 = await rt.join('plaza', { cap: 2 });
  assert.strictEqual(t2.name, 'plaza', 'an empty room is yours');
  // 2. area: cells within sight, send only to your own
  const a = await rt.area('w', { cell: 64, view: 20, x: 10, y: 10 });
  assert.deepStrictEqual(Object.keys(a.rooms).sort(), ['w@-1,-1', 'w@-1,0', 'w@0,-1', 'w@0,0'], 'near a corner: four cells');
  const seen = [];
  a.on('join', p => seen.push('+' + p.id)); a.on('leave', p => seen.push('-' + p.id));
  other('w@0,0', 'near'); other('w@-1,0', 'west');
  await new Promise(r => setTimeout(r, 10));
  assert.deepStrictEqual(seen, ['+near', '+west']);
  await a.send({ x: 10 });
  assert.deepStrictEqual(viewer.sent.slice(-1)[0], ['w@0,0', '{"x":10}'], 'said only in the cell you stand in');
  // walk east into the middle of cell 1,0: the western cells and their players go
  await a.move(96, 32);
  assert.deepStrictEqual(Object.keys(a.rooms).sort(), ['w@1,0'], 'in the middle of a cell: just that one');
  assert.ok(seen.includes('-near') && seen.includes('-west'), 'players in cells left behind leave');
  // a player in two of your cells is one player
  const b = await rt.area('v', { cell: 64, view: 20, x: 64, y: 32 });
  const bs = []; b.on('join', p => bs.push(p.id)); b.on('leave', p => bs.push('-' + p.id));
  other('v@0,0', 'twin'); other('v@1,0', 'twin');
  await new Promise(r => setTimeout(r, 10));
  assert.deepStrictEqual(bs, ['twin'], 'seen in two cells, joined once');
  deliver({ arcade: 'realtime', event: { room: 'v@0,0', type: 'leave', member: { id: 'twin' } } });
  assert.deepStrictEqual(bs, ['twin'], 'still in the other cell: not gone');
  deliver({ arcade: 'realtime', event: { room: 'v@1,0', type: 'leave', member: { id: 'twin' } } });
  assert.deepStrictEqual(bs, ['twin', '-twin'], 'gone from both: gone');
  console.log('all ok');
})().catch(e => { console.error('FAIL', e && e.message); process.exit(1); });
