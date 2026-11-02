// What a player's speakers play, taken out of their microphone (2026-11-02: "if somebody is streaming a video with audio on
// their computer and the audio is coming out of the speakers of their computer, it is picked up by the microphone and re-broadcast").
// A browser's own canceller only knows what it plays itself. A shared screen's sound is the very signal the speakers play, so it is
// the reference here: a partitioned frequency-domain adaptive filter learns the room from speakers to microphone and subtracts it.
//   - two filters: one always learns, the one that is used only takes it over when that has really done better. Somebody talking
//     over the video throws a learning filter off, and this way the voice that goes out is never cut by a filter thrown off;
//   - the microphone waits MIC_WAIT: the screen's sound can reach this page later than the speakers reach the microphone;
//   - what a filter cannot follow (a speaker's own distortion) is turned down bin by bin, never all the way;
//   - with no reference playing, the microphone comes through as it went in, MIC_WAIT and one frame later.
// Plain code with no page around it: it runs in an AudioWorklet, and tests run it directly.
class ArcadeEchoCore {
  constructor(rate) {
    const N = this.N = 512, M = this.M = N * 2, bins = this.bins = N + 1, K = this.K = Math.ceil(0.45 * rate / N);
    this.wait = Math.round((ArcadeEchoCore.MIC_WAIT) * rate);
    this.delay = new Float32Array(this.wait + 128); this.at = 0;
    this.re = new Float64Array(M); this.im = new Float64Array(M);
    this.cos = new Float64Array(N); this.sin = new Float64Array(N);
    for (let i = 0; i < N; i++) { this.cos[i] = Math.cos(2 * Math.PI * i / M); this.sin[i] = -Math.sin(2 * Math.PI * i / M); }
    this.rev = new Uint32Array(M); const bits = Math.log2(M);
    for (let i = 0; i < M; i++) { let r = 0; for (let b = 0; b < bits; b++) if (i & (1 << b)) r |= 1 << (bits - 1 - b); this.rev[i] = r; }
    const c = () => new Float64Array(bins * K);
    this.Xr = c(); this.Xi = c(); this.x0 = 0;
    this.Fr = c(); this.Fi = c(); this.Br = c(); this.Bi = c();
    this.P = new Float64Array(bins);
    this.xb = new Float64Array(M); this.db = new Float64Array(M);
    this.xq = new Float32Array(N); this.dq = new Float32Array(N); this.q = 0;
    this.out = new Float32Array(N * 4); this.oread = 0; this.owrite = N;
    this.win = new Float64Array(M); for (let i = 0; i < M; i++) this.win[i] = Math.sin(Math.PI * (i + 0.5) / M);
    this.eb = new Float64Array(M); this.yb = new Float64Array(M); this.ola = new Float64Array(M);
    this.Se = new Float64Array(bins); this.Sy = new Float64Array(bins); this.G = new Float64Array(bins).fill(1); this.L = new Float64Array(bins).fill(1);
    this.pd = 1e-9; this.pf = 1e-9; this.pb = 1e-9; this.py = 1e-9; this.better = 0; this.worse = 0; this.pc = 0;
    const r = () => new Float64Array(bins);
    this.Rf = r(); this.Yf = r(); this.Eh = r(); this.Yh = r(); this.step = r(); this.Er = r(); this.Ei = r();
    this.Pey = 0; this.Pyy = 1e-9; this.syy = 1e-9; this.adapted = false; this.sum = 0; this.xp = 0; this.yb2 = new Float64Array(N);
    this.ta = new Float64Array(N); this.tb = new Float64Array(N); this.Yr = new Float64Array(bins); this.Yi = new Float64Array(bins);
  }
  fft(re, im, back) {
    const M = this.M, rev = this.rev, cos = this.cos, sin = this.sin;
    for (let i = 0; i < M; i++) { const j = rev[i]; if (j > i) { let t = re[i]; re[i] = re[j]; re[j] = t; t = im[i]; im[i] = im[j]; im[j] = t; } }
    const sign = back ? -1 : 1;
    for (let size = 2; size <= M; size *= 2) {
      const half = size / 2, step = M / size;
      for (let s = 0; s < M; s += size) for (let k = 0, t = 0; k < half; k++, t += step) {
        const wr = cos[t], wi = sign * sin[t], a = s + k, b = a + half, xr = re[b] * wr - im[b] * wi, xi = re[b] * wi + im[b] * wr;
        re[b] = re[a] - xr; im[b] = im[a] - xi; re[a] += xr; im[a] += xi;
      }
    }
    if (back) for (let i = 0; i < M; i++) { re[i] /= M; im[i] /= M; }
  }
  // The filter's echo for this block: its time samples into `to`, the second half of the frame.
  echo(Wr, Wi, to) {
    const { K, bins, Xr, Xi, re, im, M, N, x0 } = this;
    re.fill(0); im.fill(0);
    for (let k = 0; k < K; k++) {
      const x = ((x0 + k) % K) * bins, w = k * bins;
      for (let b = 0; b < bins; b++) { const xr = Xr[x + b], xi = Xi[x + b], wr = Wr[w + b], wi = Wi[w + b]; re[b] += xr * wr - xi * wi; im[b] += xr * wi + xi * wr; }
    }
    for (let b = 1; b < N; b++) { re[M - b] = re[b]; im[M - b] = -im[b]; }
    this.fft(re, im, true);
    for (let i = 0; i < N; i++) to[i] = re[N + i];
  }
  // How fast to learn, bin by bin: the share of what is left that is really echo. The filter's error and its echo rise and fall together
  // only where it is echo, so how they move together says how much leaks through; a voice moves on its own and counts for nothing.
  // With headphones nothing moves together and nothing is learned. (The method is Valin's, as in Speex, "On adjusting the learning rate".)
  rates(e, y) {
    const { re, im, M, N, bins, Rf, Yf, Eh, Yh, step } = this;
    re.fill(0); im.fill(0);
    for (let i = 0; i < N; i++) re[N + i] = e[i];
    this.fft(re, im, false);
    for (let b = 0; b < bins; b++) { Rf[b] = re[b] * re[b] + im[b] * im[b]; this.Er[b] = re[b]; this.Ei[b] = im[b]; }
    re.fill(0); im.fill(0);
    for (let i = 0; i < N; i++) re[N + i] = y[i];
    this.fft(re, im, false);
    let pey = 0, pyy = 0, see = 1e-12, syy = 1e-12, sey = 0;
    for (let b = 0; b < bins; b++) {
      Yf[b] = re[b] * re[b] + im[b] * im[b];
      const a = Rf[b] - Eh[b], c = Yf[b] - Yh[b]; pey += a * c; pyy += c * c;
      Eh[b] += 0.35 * (Rf[b] - Eh[b]); Yh[b] += 0.35 * (Yf[b] - Yh[b]);
    }
    for (let i = 0; i < N; i++) { see += e[i] * e[i]; syy += y[i] * y[i]; sey += e[i] * y[i]; }
    pyy = Math.sqrt(pyy); pey = pey / (pyy + 1e-12);
    const f = Math.min(0.1 * Math.min(1, syy / (this.syy + 1e-12)), 0.25 * syy / (see + 1e-12) + 0.02);
    this.syy = Math.max(syy, this.syy * 0.99);
    this.Pey += f * (pey - this.Pey); this.Pyy += f * (pyy - this.Pyy);
    const leak = Math.min(0.5, Math.max(0.005, this.Pey / Math.max(this.Pyy, 1e-12)));
    let rer = 3 * leak * syy / see; rer = Math.max(rer, sey * sey / (see * syy)); rer = Math.min(0.5, rer);
    if (!this.adapted) {          // no echo estimate to go by yet: briskly, for as long as the first estimate takes
      const rate = Math.min(1, this.xp / see);
      this.sum += rate; if (this.sum > ArcadeEchoCore.LEARNED * this.K) this.adapted = true;
      step.fill(rate);
    } else for (let b = 0; b < bins; b++) {
      const e = Rf[b] + 1e-12, r = 0.7 * Math.min(leak * Yf[b], 0.5 * e) + 0.3 * rer * e;
      step[b] = Math.min(1, ArcadeEchoCore.BOOST * r / e);
    }
    this.leak = leak; this.rer = rer;
  }
  learn() {
    const { K, bins, Xr, Xi, Br, Bi, P, re, im, M, N, x0, Er, Ei, step } = this;
    for (let k = 0; k < K; k++) {
      const x = ((x0 + k) % K) * bins, w = k * bins;
      for (let b = 0; b < bins; b++) {
        const g = step[b] / (P[b] + 1e-6 * M), xr = Xr[x + b], xi = -Xi[x + b];
        Br[w + b] += g * (xr * Er[b] - xi * Ei[b]); Bi[w + b] += g * (xr * Ei[b] + xi * Er[b]);
      }
    }
    // One partition a block held to the frame's length: all of them in turn, at a small part of the cost.
    const w = this.pc * bins; this.pc = (this.pc + 1) % K;
    for (let b = 0; b < bins; b++) { re[b] = Br[w + b]; im[b] = Bi[w + b]; }
    for (let b = 1; b < N; b++) { re[M - b] = re[b]; im[M - b] = -im[b]; }
    this.fft(re, im, true);
    for (let i = N; i < M; i++) re[i] = 0;
    im.fill(0); this.fft(re, im, false);
    for (let b = 0; b < bins; b++) { Br[w + b] = re[b]; Bi[w + b] = im[b]; }
  }
  block(x, d) {
    const { N, M, K, bins, Xr, Xi, P, re, im, xb, db, ta, tb, eb, yb, win, Se, Sy, G, ola } = this;
    xb.copyWithin(0, N); db.copyWithin(0, N);
    xb.set(x, N); db.set(d, N);
    for (let i = 0; i < M; i++) { re[i] = xb[i]; im[i] = 0; }
    this.fft(re, im, false);
    this.x0 = (this.x0 + K - 1) % K;
    const at = this.x0 * bins; let xp = 0;
    for (let b = 0; b < bins; b++) {
      const old = Xr[at + b] * Xr[at + b] + Xi[at + b] * Xi[at + b], p = re[b] * re[b] + im[b] * im[b];
      Xr[at + b] = re[b]; Xi[at + b] = im[b]; xp += p;
      P[b] = Math.max(0, P[b] + p - old);               // the power in every partition the filter sees, exactly: a step can never overshoot on a sudden loud note
    }
    let pd = 0; for (let i = 0; i < N; i++) pd += d[i] * d[i];
    const playing = xp / M / bins > 1e-9;
    this.echo(this.Br, this.Bi, tb);
    this.yb2.set(tb);
    let pb = 0; for (let i = 0; i < N; i++) { tb[i] = d[i] - tb[i]; pb += tb[i] * tb[i]; }
    this.echo(this.Fr, this.Fi, ta);
    let pf = 0, py = 0; for (let i = 0; i < N; i++) { const e = d[i] - ta[i]; pf += e * e; py += ta[i] * ta[i]; }
    const s = 0.7; this.pd = s * this.pd + (1 - s) * pd; this.pb = s * this.pb + (1 - s) * pb; this.pf = s * this.pf + (1 - s) * pf;
    this.py = s * this.py + (1 - s) * py;
    if (playing) {
      this.xp = 0; for (let i = 0; i < N; i++) this.xp += x[i] * x[i]; this.rates(tb, this.yb2);
      // Taken over only if truly better and better than the bare microphone; thrown off, it starts again from the one in use.
      // Well under the bare microphone, and for a while: a filter finds a chance likeness between a voice and the music for a moment, and with
      // headphones on that is all there ever is to find. Only an echo that is really there stays cancelled.
      this.better = this.pb < 0.85 * this.pf && this.pb < ArcadeEchoCore.TAKE * this.pd ? this.better + 1 : 0;
      this.worse = this.pb > 2 * this.pd ? this.worse + 1 : 0;
      if (this.better >= ArcadeEchoCore.TAKE_N) { this.Fr.set(this.Br); this.Fi.set(this.Bi); this.pf = this.pb; this.better = 0; }
      else if (this.worse >= 6) { this.Br.set(this.Fr); this.Bi.set(this.Fi); this.pb = this.pf; this.worse = 0; }
      else this.learn();
    }
    // In use, and never more than the microphone: a filter that adds echo is left out of this block.
    const useless = this.pf > this.pd * 1.02;
    eb.copyWithin(0, N); yb.copyWithin(0, N);
    for (let i = 0; i < N; i++) { eb[N + i] = useless ? d[i] : d[i] - ta[i]; yb[N + i] = useless ? 0 : ta[i]; }
    // What is left, by bin. Two transforms of real signals in one complex one.
    for (let i = 0; i < M; i++) { re[i] = eb[i] * win[i]; im[i] = yb[i] * win[i]; }
    this.fft(re, im, false);
    const Yr = this.Yr, Yi = this.Yi;
    for (let b = 0; b < bins; b++) {
      const c = (M - b) % M, er = (re[b] + re[c]) / 2, ei = (im[b] - im[c]) / 2, yr = (im[b] + im[c]) / 2, yi = (re[c] - re[b]) / 2;
      Yr[b] = er; Yi[b] = ei;
      Se[b] = 0.6 * Se[b] + 0.4 * (er * er + ei * ei); Sy[b] = 0.6 * Sy[b] + 0.4 * (yr * yr + yi * yi);
    }
    const floor = ArcadeEchoCore.FLOOR, over = ArcadeEchoCore.OVER, L = this.L;
    for (let b = 0; b < bins; b++) {
      // L: how much of the filter's echo is still left after it. The lowest ratio seen, let rise slowly: a voice raises the ratio for a moment,
      // an echo the filter has not learned raises it for good. So a voice is left alone, and only what stays is turned down.
      const lo = Math.max(0, b - 1), hi = Math.min(bins - 1, b + 1), sy = Sy[lo] + Sy[b] + Sy[hi], se = Se[lo] + Se[b] + Se[hi] + 1e-12;
      if (sy > 1e-9) L[b] = Math.min(1, L[b] * ArcadeEchoCore.RISE, se / sy);
      const g = Math.max(floor, 1 - over * L[b] * sy / se);
      G[b] = g < G[b] ? g : 0.6 * G[b] + 0.4 * g;       // down at once, up gently
      re[b] = Yr[b] * G[b]; im[b] = Yi[b] * G[b];
    }
    for (let b = 1; b < N; b++) { re[M - b] = re[b]; im[M - b] = -im[b]; }
    this.fft(re, im, true);
    for (let i = 0; i < M; i++) ola[i] += re[i] * win[i];
    for (let i = 0; i < N; i++) { this.out[this.owrite] = ola[i]; this.owrite = (this.owrite + 1) % this.out.length; }
    ola.copyWithin(0, N); ola.fill(0, N);
  }
  // 128 samples at a time, as a worklet is given them. `ref` null: nothing is playing.
  process(mic, ref, out) {
    const { N, delay, wait, xq, dq } = this, n = mic.length;
    for (let i = 0; i < n; i++) {
      const d = delay[this.at]; delay[this.at] = mic[i]; this.at = (this.at + 1) % wait;
      dq[this.q] = d; xq[this.q] = ref ? ref[i] : 0;
      if (++this.q === N) { this.q = 0; this.block(xq, dq); }
      out[i] = this.out[this.oread]; this.out[this.oread] = 0; this.oread = (this.oread + 1) % this.out.length;
    }
  }
}
ArcadeEchoCore.MIC_WAIT = 0.04; ArcadeEchoCore.FLOOR = 0.08; ArcadeEchoCore.OVER = 1.5; ArcadeEchoCore.RISE = 1.02;
ArcadeEchoCore.TAKE = 0.5; ArcadeEchoCore.TAKE_N = 10; ArcadeEchoCore.LEARNED = 1; ArcadeEchoCore.BOOST = 4;
if (typeof registerProcessor === 'function') registerProcessor('arcade-echo', class extends AudioWorkletProcessor {
  constructor() { super(); this.core = new ArcadeEchoCore(sampleRate); }
  process(inputs, outputs) {
    const mic = inputs[0][0], ref = inputs[1] && inputs[1][0], out = outputs[0][0];
    if (mic) this.core.process(mic, ref && ref.length ? ref : null, out);
    return true;
  }
});
