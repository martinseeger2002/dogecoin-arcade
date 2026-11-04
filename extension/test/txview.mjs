// For tests/test_extension.py: what txview.js reads from outputs and inputs, as JSON on stdout.
//   node --import ./extension/test/register.mjs extension/test/txview.mjs '{"scripts": [{network, hex}], ...}'
import * as coins from "../coins.js";
import * as txview from "../txview.js";

const asked = JSON.parse(process.argv[2]);
const out = {addresses: [], payloads: [], references: [], landing: asked.landing.map((l) => txview.landing(l.inputs, l.outputs))};
for (const s of asked.scripts) {
  out.addresses.push(await txview.addressOf(coins.unhex(s.hex), s.network));
  out.payloads.push(txview.payload(coins.unhex(s.hex)));
}
for (const r of asked.references) out.references.push(txview.reference(r.addresses, r.sender));
console.log(JSON.stringify(out));
