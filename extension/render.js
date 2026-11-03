/* A decoded transaction (txview.describe) drawn for a person: every output, what each moves, the fee. Text only:
 * nothing from a transaction or a site is ever put into the page as markup. */
const el = (tag, cls, text) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
};

export function drawTx(box, view, tokens = {}) {
  box.textContent = "";
  box.append(el("p", view.real ? "real" : "muted", view.real ? `Real coins, on ${view.label}.` : `Test coins, on ${view.label}.`));
  const list = el("ul", "outputs");
  for (const o of view.outputs) {
    const li = el("li", o.mine ? "mine" : "");
    if (o.data && o.data.kind === "token") {
      const t = tokens[o.data.propertyid] || {};
      const amount = t.divisible ? (Number(o.data.units) / 1e8).toFixed(8) : o.data.units;
      li.append(el("div", "what", `Moves ${amount} of token #${o.data.propertyid}${t.name ? ` (${t.name}, as the arcade names it)` : ""}`),
                el("code", "", `to ${o.data.to || "nobody"}${o.data.toOutput >= 0 ? ` (output ${o.data.toOutput})` : ""}`));
    } else if (o.data && o.data.kind === "nft") {
      li.append(el("div", "what", "Moves the NFT"), el("code", "", o.data.piece),
                el("code", "", `to ${o.data.to || "nobody"}${o.data.toOutput >= 0 ? ` (output ${o.data.toOutput})` : ""}`));
    } else if (o.data) {
      li.append(el("div", "what", `Writes ${o.data.bytes} bytes to the chain`));
    } else {
      li.append(el("div", "what", `${o.coins} ${view.coin}${o.mine ? " back to you (change)" : ""}`),
                el("code", "", o.address || "a script that is not an address"));
    }
    for (const id of o.receives) li.append(el("div", "ins", `Inscription ${id} lands here`));
    list.append(li);
  }
  box.append(el("p", "muted", `Output by output, read from the transaction itself:`), list);
  for (const l of view.lands) {
    if (l.output === null) box.append(el("p", "err", `Inscription ${l.id} would be burned to the fee.`));
    if (l.output === undefined) box.append(el("p", "err", `Where inscription ${l.id} goes cannot be worked out.`));
  }
  const mine = view.inputs.filter((i) => i.mine).length;
  box.append(el("p", "", `Spends ${mine} coin${mine === 1 ? "" : "s"} of yours`
    + (view.inputs.length > mine ? ` and ${view.inputs.length - mine} of somebody else's` : "") + "."));
  box.append(el("p", "fee", view.feeCoins == null ? "Fee: cannot be worked out here (not every coin it spends is yours)."
    : `Fee: ${view.feeCoins} ${view.coin}`));
}
