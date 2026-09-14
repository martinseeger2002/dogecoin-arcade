/* arcade-lib.js -- a small library, inscribed once, reused by anything after it.
 *
 * This is the point of /content/<id> being a plain URL: a page inscribes a
 * <script src="/content/<the number this gets>"> and the code arrives from the
 * chain rather than from a CDN that may or may not still exist. Nothing here
 * reaches outside the machine it runs on, because nothing can.
 */
(function (global) {
  'use strict';

  // Which inscription is running. A page is always served at /content/<id>,
  // so it can find out what it is without being told.
  function self() {
    return location.pathname.split('/').pop();
  }

  async function get(path) {
    const response = await fetch(path);
    const body = await response.text();
    try { return { ok: response.ok, status: response.status, json: JSON.parse(body) }; }
    catch (e) { return { ok: response.ok, status: response.status, text: body }; }
  }

  const api = {
    self,
    height:   () => get('/r/blockheight'),
    time:     () => get('/r/blocktime'),
    about:    (id) => get('/r/inscription/' + id),
    metadata: (id) => get('/r/metadata/' + id),
    list:     (limit) => get('/r/inscriptions?limit=' + (limit || 20)),
    owned:    (address) => get('/r/inscriptions/' + address),
    balances: (address) => get('/r/balances/' + address),
    tag:      (name) => get('/r/tag/' + name),
    address:  (addr) => get('/r/address/' + addr),
    wallet:   () => get('/r/wallet'),
    contentUrl: (id) => '/content/' + id,
  };

  // A tiny drawing helper, so a page inscribed after this one can be a few
  // lines rather than a canvas tutorial.
  api.plaque = function (canvas, lines, background) {
    const ctx = canvas.getContext('2d');
    ctx.fillStyle = background || '#12131a';
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = '#d9a520';
    ctx.font = '600 20px system-ui, sans-serif';
    ctx.fillText('inscribed on chain', 24, 44);
    ctx.fillStyle = '#e8e8ea';
    ctx.font = '15px ui-monospace, monospace';
    lines.forEach(function (line, n) { ctx.fillText(line, 24, 84 + n * 26); });
    return canvas;
  };

  api.VERSION = '1.0.0';
  global.arcade = api;
})(this);
