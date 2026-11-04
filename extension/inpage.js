/* window.arcade: what a page sees of the Arcade Wallet.
 *
 *   const said = await arcade.request({method: "arcade_signIn", params: {nonce}});
 *
 * It also announces itself ("arcade:announceProvider", again on "arcade:requestProvider"), so a page can find it
 * even when another script took the name first. */
(() => {
  if (window.arcade && window.arcade.isArcadeWallet) return;
  let n = 0;
  const waiting = new Map(), listeners = {};

  window.addEventListener("message", (e) => {
    if (e.source !== window || !e.data || e.data.target !== "arcade-wallet-inpage") return;
    const d = e.data;
    if (d.event) { (listeners[d.event] || []).slice().forEach((fn) => { try { fn(d.data); } catch (x) {} }); return; }
    const w = waiting.get(d.id);
    if (!w) return;
    waiting.delete(d.id);
    if (d.ok) w.ok(d.result); else w.no(new Error(d.error || "refused"));
  });

  const provider = Object.freeze({
    isArcadeWallet: true,
    version: "0.2.0",
    request({method, params} = {}) {
      return new Promise((ok, no) => {
        const id = ++n;
        waiting.set(id, {ok, no});
        window.postMessage({target: "arcade-wallet-content", id, method, params}, "*");
      });
    },
    on(event, fn) { (listeners[event] = listeners[event] || []).push(fn); },
    removeListener(event, fn) { listeners[event] = (listeners[event] || []).filter((f) => f !== fn); },
  });

  try { Object.defineProperty(window, "arcade", {value: provider, configurable: false, writable: false}); }
  catch (e) { /* a page took the name: it can still find us by the announcement */ }
  const info = Object.freeze({name: "Arcade Wallet", rdns: "com.arcade-nodes.wallet", version: provider.version});
  const announce = () => window.dispatchEvent(new CustomEvent("arcade:announceProvider",
                                                              {detail: Object.freeze({info, provider})}));
  window.addEventListener("arcade:requestProvider", announce);
  announce();
})();
