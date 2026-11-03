/* Runs in every https page: puts window.arcade into the page (inpage.js) and carries its requests to the service
 * worker, one port per request, so the worker knows the page's origin from the browser itself. */
const s = document.createElement("script");
s.src = chrome.runtime.getURL("inpage.js");
(document.head || document.documentElement).appendChild(s);
s.remove();

window.addEventListener("message", (e) => {
  if (e.source !== window || !e.data || e.data.target !== "arcade-wallet-content") return;
  const {id, method, params} = e.data;
  const port = chrome.runtime.connect({name: "request"});
  port.onMessage.addListener((answer) => {
    window.postMessage({target: "arcade-wallet-inpage", id, ...answer}, "*");
    port.disconnect();
  });
  port.onDisconnect.addListener(() => {
    if (chrome.runtime.lastError) window.postMessage({target: "arcade-wallet-inpage", id, ok: false,
                                                      error: "the Arcade Wallet stopped"}, "*");
  });
  port.postMessage({method, params});
});

chrome.runtime.onMessage.addListener((msg) => {
  if (msg && msg.event) window.postMessage({target: "arcade-wallet-inpage", event: msg.event, data: msg.data}, "*");
});
