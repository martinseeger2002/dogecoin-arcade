const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };

/* Scanning a QR code with the phone's camera (2026-10-17: "phone camera support for QR
   scanning on the send in the Wallet tab"). Used by the Wallet's Send; no imports.

   The phone's camera where the browser allows it (it needs HTTPS, which an arcade's public
   address is), read by the browser's own BarcodeDetector where there is one and otherwise by
   jsQR (vendor/, checked like the rest -- iPhones have no BarcodeDetector). A photo works too,
   for a browser that will not open the camera. What is read only fills the boxes: nothing is
   sent until Review, as with anything typed. */
let jsqrLoading = null;
export function loadJsQR() {
  if (window.jsQR) return Promise.resolve(window.jsQR);
  return jsqrLoading || (jsqrLoading = new Promise((ok, bad) => {
    const tag = document.createElement("script");
    tag.src = "/vendor/jsQR.js"; tag.onload = () => ok(window.jsQR); tag.onerror = () => bad(new Error("the QR reader did not load"));
    document.head.append(tag);
  }));
}
export async function qrReader() {
  try {
    if ("BarcodeDetector" in window && (await BarcodeDetector.getSupportedFormats()).includes("qr_code")) {
      const det = new BarcodeDetector({formats: ["qr_code"]});
      return async (source) => ((await det.detect(source))[0] || {}).rawValue || null;
    }
  } catch (e) { /* the vendored reader below */ }
  const jsQR = await loadJsQR();
  const canvas = document.createElement("canvas"), ctx = canvas.getContext("2d", {willReadFrequently: true});
  return async (source) => {
    const w = source.videoWidth || source.width, h = source.videoHeight || source.height;
    if (!w || !h) return null;
    const k = Math.min(1, 720 / Math.max(w, h));
    canvas.width = Math.round(w * k); canvas.height = Math.round(h * k);
    ctx.drawImage(source, 0, 0, canvas.width, canvas.height);
    const img = ctx.getImageData(0, 0, canvas.width, canvas.height);
    const hit = jsQR(img.data, img.width, img.height, {inversionAttempts: "attemptBoth"});
    return hit ? hit.data : null;
  };
}
/** Open the camera (or take a photo) and resolve with the first QR code's text, or null. */
export function scanQR() {
  return new Promise((resolve) => {
    const box = el("div", "wl-scanbox");
    const video = document.createElement("video");
    video.setAttribute("playsinline", ""); video.muted = true;
    const say = el("p", "", "Point the camera at a QR code.");
    const photo = el("button", "wl-scan", "Use a photo"); photo.type = "button";
    const close = el("button", "wl-scan", "Cancel"); close.type = "button";
    const file = document.createElement("input"); file.type = "file"; file.accept = "image/*";
    file.setAttribute("capture", "environment"); file.hidden = true;
    const row = el("div", "row"); row.append(photo, close);
    box.append(video, say, row, file);
    document.body.append(box);
    let stream = null, done = false, read = null;
    const finish = (text) => {
      if (done) return; done = true;
      if (stream) stream.getTracks().forEach((t) => t.stop());
      box.remove(); resolve(text || null);
    };
    close.onclick = () => finish(null);
    photo.onclick = () => file.click();
    file.onchange = async () => {
      const f = file.files && file.files[0]; if (!f) return;
      say.textContent = "Reading the photo\u2026";
      try {
        read = read || await qrReader();
        const bmp = await createImageBitmap(f);
        const text = await read(bmp);
        if (text) finish(text); else say.textContent = "No QR code in that photo. Try again, closer and in focus.";
      } catch (e) { say.textContent = String(e.message || e); }
    };
    (async () => {
      try {
        read = await qrReader();
        if (!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia)) throw new Error("no camera here");
        stream = await navigator.mediaDevices.getUserMedia({video: {facingMode: {ideal: "environment"}}, audio: false});
        if (done) { stream.getTracks().forEach((t) => t.stop()); return; }
        video.srcObject = stream; await video.play();
        const tick = async () => {
          if (done) return;
          try { const text = await read(video); if (text) return finish(text); } catch (e) { /* the next frame */ }
          setTimeout(tick, 120);
        };
        tick();
      } catch (e) {
        video.hidden = true;
        say.textContent = (e && e.name === "NotAllowedError")
          ? "The camera is not allowed for this site. Allow it in the browser's settings, or use a photo."
          : "The camera cannot be opened here. Use a photo of the QR code instead.";
      }
    })();
  });
}
/** What a scanned code says to Send: an address, an @name, or a payment link with an amount. */
export function parseQR(text) {
  const t = String(text || "").trim();
  const link = t.match(/^([a-z]+):(?:\/\/)?([^?]+)(?:\?(.*))?$/i);
  if (link && /^(pepecoin|pep|dogecoin|doge)$/i.test(link[1])) {
    const q = new URLSearchParams(link[3] || "");
    return {to: decodeURIComponent(link[2]), amount: q.get("amount") || "", coin: link[1].toLowerCase()};
  }
  return {to: t.replace(/^arcade:/i, ""), amount: "", coin: ""};
}
