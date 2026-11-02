// A room in numbers: the shared sound through a speaker that distorts, a delay and a room's reverberation, into a microphone,
// with somebody talking over it. Run: node tests/echo_canceller_sim.js   (tests/test_echo_canceller.py runs it and reads the answer)
const fs = require('fs'), path = require('path'), vm = require('vm');
const box = {console, Math, Float32Array, Float64Array, Uint32Array};
vm.createContext(box);
vm.runInContext(fs.readFileSync(path.join(__dirname, '../arcade/web/templates/echo_canceller.js'), 'utf8') + '\nthis.Core = ArcadeEchoCore;', box);
for (const k of ['OVER', 'STEP', 'RISE', 'FLOOR', 'TAKE', 'TAKE_N', 'LEARNED', 'BOOST']) if (process.env[k]) box.Core[k] = +process.env[k];
const R = 48000; let seed = 12345;
const rnd = () => { seed = (seed * 1664525 + 1013904223) >>> 0; return seed / 4294967296 * 2 - 1; };
function music(n) {                 // a low steady bed, notes that change, and a beat: most of it in the voice's band
  const o = new Float64Array(n); let lp = 0, notes = [220, 277, 330];
  for (let i = 0; i < n; i++) {
    if (i % 12000 === 0) notes = [0, 1, 2].map(() => 110 * Math.pow(2, Math.floor(Math.abs(rnd()) * 36) / 12));
    lp += 0.15 * (rnd() - lp);
    let v = lp * 1.5 * (1 + Math.sin(2 * Math.PI * 2 * i / R));
    for (const f of notes) for (let h = 1; h < 6; h++) v += Math.sin(2 * Math.PI * f * h * i / R) / h * 0.12;
    o[i] = v * 0.35;
  }
  return o;
}
function speech(n, from, to) {      // a voice: buzz through moving resonances, in bursts
  const o = new Float64Array(n); let a = 0, b = 0;
  for (let i = from; i < to; i++) {
    const t = i / R, on = Math.sin(2 * Math.PI * 3 * t) > -0.2 ? 1 : 0, f = 120 + 30 * Math.sin(2 * Math.PI * 0.7 * t);
    const buzz = ((t * f) % 1) < 0.1 ? 1 : 0, c = 2 * 0.97 * Math.cos(2 * Math.PI * (500 + 300 * Math.sin(2 * Math.PI * 1.3 * t)) / R), v = buzz * on + c * a - 0.94 * b;
    b = a; a = v; o[i] = v * 0.012;
  }
  return o;
}
function room(delayS, gain, tail) {
  const n = Math.round(R * (delayS + tail)), h = new Float64Array(n), d = Math.round(delayS * R);
  h[d] = gain; for (let i = d + 1; i < n; i += 37) h[i] = gain * 0.5 * rnd() * Math.exp(-(i - d) / (R * tail / 5));
  return h;
}
function through(x, h, soft) {
  const out = new Float64Array(x.length), taps = []; for (let i = 0; i < h.length; i++) if (h[i]) taps.push([i, h[i]]);
  for (let i = 0; i < x.length; i++) { let v = 0; const s = soft ? Math.tanh(1.2 * x[Math.max(0, i)]) / 1.2 : x[i];
    out[i] = s; }
  const y = new Float64Array(x.length);
  for (const [t, g] of taps) for (let i = t; i < x.length; i++) y[i] += g * out[i - t];
  return y;
}
function run(ref, mic) {
  const core = new box.Core(R), n = ref.length, out = new Float64Array(n), a = new Float32Array(128), b = new Float32Array(128), c = new Float32Array(128);
  const start = Date.now();
  for (let i = 0; i + 128 <= n; i += 128) { for (let k = 0; k < 128; k++) { a[k] = mic[i + k]; b[k] = ref[i + k]; } core.process(a, b, c); out.set(c, i); }
  return {out, used: (Date.now() - start) / 1000 / (n / R), core};
}
const db = (s, from, to) => { let e = 0; for (let i = from; i < to; i++) e += s[i] * s[i]; return 10 * Math.log10(e / (to - from) + 1e-20); };
const LAG = (() => {                       // what the canceller adds, measured from a click and not assumed
  const n = R, m = new Float64Array(n); m[R / 2] = 1; const o = run(new Float64Array(n), m).out;
  let best = 0; for (let i = 0; i < n; i++) if (Math.abs(o[i]) > Math.abs(o[best])) best = i; return best - R / 2; })();
const res = {}, N = R * 24, x = music(N);
const noise = n => { const o = new Float64Array(n); for (let i = 0; i < n; i++) o[i] = rnd() * 0.0005; return o; };
const add = (...s) => { const o = new Float64Array(s[0].length); for (const v of s) for (let i = 0; i < o.length; i++) o[i] += v[i]; return o; };
// How much an echo drops, at several delays between the screen's sound reaching us and the speakers reaching the microphone
for (const ms of [-30, 0, 20, 60, 150, 300]) {
  const h = room(Math.max(ms, 0) / 1000, 0.5, 0.2), echo = through(x, h, true), r = new Float64Array(N);
  if (ms < 0) for (let i = 0; i + 1440 < N; i++) r[i + 1440] = x[i]; else r.set(x);       // the screen's sound arrives late
  const e = ms < 0 ? add(echo, noise(N)) : add(echo, noise(N)), o = run(r, e).out;
  res['drop_' + ms + 'ms'] = +(db(e, R * 12, R * 22) - db(o, R * 12 + LAG, R * 22 + LAG)).toFixed(1);
}
// How fast
{ const h = room(0.06, 0.5, 0.2), e = add(through(x, h, true), noise(N)), o = run(x, e);
  res.after_2s = +(db(e, R * 2, R * 3) - db(o.out, R * 2 + LAG, R * 3 + LAG)).toFixed(1);
  res.after_5s = +(db(e, R * 5, R * 6) - db(o.out, R * 5 + LAG, R * 6 + LAG)).toFixed(1);
  res.realtime_share = +o.used.toFixed(3); }
// Somebody talking over it, after the room is learned: how much of the voice goes out, and how much echo
{ const h = room(0.06, 0.5, 0.2), echo = through(x, h, true), v = speech(N, R * 10, R * 20), o = run(x, add(echo, v, noise(N))).out;
  let sv = 0, vv = 0; for (let i = R * 11; i < R * 19; i++) { sv += o[i + LAG] * v[i]; vv += v[i] * v[i]; }
  res.voice_kept = +(sv / vv).toFixed(2);
  const c = run(x, add(echo, noise(N))).out, e = new Float64Array(N); for (let i = 0; i < N - LAG; i++) e[i] = o[i + LAG] - sv / vv * v[i];
  res.voice_to_echo_in = +(db(v, R * 11, R * 19) - db(echo, R * 11, R * 19)).toFixed(1);
  res.voice_to_echo_out = +(db(v, R * 11, R * 19) + 20 * Math.log10(sv / vv) - db(e, R * 11, R * 19)).toFixed(1); }
// The microphone moved
{ const a = through(x, room(0.06, 0.5, 0.2), true), b = through(x, room(0.09, 0.4, 0.2), true), e = new Float64Array(N);
  for (let i = 0; i < N; i++) e[i] = (i < R * 12 ? a[i] : b[i]); const o = run(x, add(e, noise(N))).out;
  res.moved_after_3s = +(db(e, R * 15, R * 16) - db(o, R * 15 + LAG, R * 16 + LAG)).toFixed(1); }
// Nothing playing: the voice comes through untouched
{ const v = speech(N, R, R * 8), o = run(new Float64Array(N), add(v)).out; let d = 0, s = 0; for (let i = R * 2; i < R * 7; i++) { d += (o[i + LAG] - v[i]) ** 2; s += v[i] * v[i]; }
  res.quiet_difference_db = +(10 * Math.log10(d / s + 1e-20)).toFixed(1); }
// Headphones: the screen plays, the microphone hears only the voice
{ const v = speech(N, R, R * 20), o = run(x, add(v, noise(N))).out; let sv = 0, vv = 0; for (let i = R * 5; i < R * 19; i++) { sv += o[i + LAG] * v[i]; vv += v[i] * v[i]; }
  res.headphones_voice_kept = +(sv / vv).toFixed(2); }
// Somebody already talking when the share starts: the filter has to learn under the voice
{ const echo = through(x, room(0.06, 0.5, 0.2), true), v = speech(N, 0, N), m = add(echo, v, noise(N)), o = run(x, m).out; const e = new Float64Array(N); let sv = 0, vv = 0;
  for (let i = R * 10; i < R * 22; i++) { sv += o[i + LAG] * v[i]; vv += v[i] * v[i]; }
  for (let i = 0; i < N - LAG; i++) e[i] = o[i + LAG] - sv / vv * v[i];
  res.talking_from_start_kept = +(sv / vv).toFixed(2); res.talking_from_start_drop = +(db(echo, R * 10, R * 22) - db(e, R * 10, R * 22)).toFixed(1); }
// A steady tone over it, as the browser test has
{ const echo = through(x, room(0.06, 0.5, 0.2), false), t = new Float64Array(N); for (let i = 0; i < N; i++) t[i] = 0.05 * Math.sin(2 * Math.PI * 3000 * i / R);
  const o = run(x, add(echo, t)).out, e = new Float64Array(N); for (let i = 0; i < N - LAG; i++) e[i] = o[i + LAG] - t[i];
  res.steady_tone_drop_5s = +(db(echo, R * 5, R * 6) - db(e, R * 5, R * 6)).toFixed(1); res.steady_tone_drop_20s = +(db(echo, R * 20, R * 22) - db(e, R * 20, R * 22)).toFixed(1); }
// Glitches: the page falls behind and one input loses 10 ms every 3 s
{ const echo = through(x, room(0.06, 0.5, 0.2), true), r = new Float64Array(N); let skip = 0; for (let i = 0; i < N; i++) { if (i % (R * 3) === 0) skip += 480; r[i] = x[Math.min(N - 1, i + skip - 480)]; }
  const o = run(r, add(echo, noise(N))).out; res.glitching_drop = +(db(echo, R * 12, R * 22) - db(o, R * 12 + LAG, R * 22 + LAG)).toFixed(1); }
res.lag_ms = +(LAG / R * 1000).toFixed(1);
console.log(JSON.stringify(res));
