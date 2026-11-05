/* Every chain the arcade runs, as the Arcade Wallet needs it: the version bytes an address carries, whether its coins
 * are real, and the words its Core signs a message under.
 *
 * The bytes are config.py NETWORKS' (tests/test_extension.py holds this table to it, so a chain added there fails the
 * test until it is placed here). The keys are coins.js's own, the arcade web wallet's file copied byte for byte:
 * m/44'/<COIN_TYPE>'/0'/0/0, so the same words show the same addresses here and on every arcade.
 */
import * as coins from "./coins.js";

//: `magic`: what `signmessage` hashes in front of the text on that chain's Core. Each was checked 2026-11-03 against
//: the chain's own Core (`signmessagewithprivkey` with a throwaway key gave the very signature coins.js makes):
//: Pepecoin main and test, Dogecoin, Litecoin main and test, Luckycoin and Bellscoin ("Bells", not "Bellscoin").
//: No Core of Kotia, Wojakcoin or DigiByte was at hand, so those sign no messages here until one is checked (null).
export const CHAINS = {
  main: {label: "Pepecoin", coin: "PEP", pubkeyhash: 56, scripthash: 22, real: true, magic: "Pepecoin Signed Message:\n"},
  test: {label: "Pepecoin test", coin: "PEP TEST", pubkeyhash: 113, scripthash: 196, real: false,
         magic: "Pepecoin Signed Message:\n"},
  regtest: {label: "Pepecoin regtest", coin: "PEP TEST", pubkeyhash: 111, scripthash: 196, real: false,
            magic: "Pepecoin Signed Message:\n"},
  doge: {label: "Dogecoin", coin: "DOGE", pubkeyhash: 30, scripthash: 22, real: true, magic: "Dogecoin Signed Message:\n"},
  ltc: {label: "Litecoin", coin: "LTC", pubkeyhash: 48, scripthash: 50, real: true, hrp: "ltc",
        magic: "Litecoin Signed Message:\n"},
  ltctest: {label: "Litecoin testnet", coin: "LTC TEST", pubkeyhash: 111, scripthash: 58, real: false, hrp: "tltc",
            magic: "Litecoin Signed Message:\n"},
  bel: {label: "Bellscoin", coin: "BEL", pubkeyhash: 25, scripthash: 30, real: true, hrp: "bel",
        magic: "Bells Signed Message:\n"},
  lky: {label: "Luckycoin", coin: "LKY", pubkeyhash: 47, scripthash: 5, real: true, magic: "Luckycoin Signed Message:\n"},
  kot: {label: "Kotia", coin: "KOT", pubkeyhash: 46, scripthash: 45, real: true, magic: null},
  wjk: {label: "Wojakcoin", coin: "WJK", pubkeyhash: 73, scripthash: 5, real: true, magic: null},
  dgb: {label: "DigiByte", coin: "DGB", pubkeyhash: 30, scripthash: 63, real: true, hrp: "dgb", magic: null},
  dgbtest: {label: "DigiByte testnet", coin: "DGB TEST", pubkeyhash: 126, scripthash: 140, real: false, hrp: "dgbt",
            magic: null},
  flop: {label: "Flopcoin", coin: "FLOP", pubkeyhash: 35, scripthash: 22, real: true, magic: null},
};

/** The chain named, or a refusal: a key is never guessed for a chain this wallet does not know. */
export function chain(network) {
  const found = Object.prototype.hasOwnProperty.call(CHAINS, network) ? CHAINS[network] : null;
  if (!found) throw new Error(`the Arcade Wallet does not know the chain ${JSON.stringify(String(network))}`);
  return found;
}

/** This wallet's key and address on one chain: {network, key, pubkey, address, version}. */
export async function keysOn(seed, network) {
  const c = chain(network);
  const {key, pubkey} = await coins.coinKey(seed, network, 0);
  return {network, key, pubkey, version: c.pubkeyhash, address: await coins.address(pubkey, c.pubkeyhash)};
}

/** Every chain's address, public only: {network: {address, pubkey}}. */
export async function addresses(seed) {
  const out = {};
  for (const network of Object.keys(CHAINS)) {
    const k = await keysOn(seed, network);
    out[network] = {address: k.address, pubkey: coins.hex(k.pubkey)};
  }
  return out;
}

/** Satoshis as coins, the way the arcade writes them. */
export function coinsOf(sats) {
  const n = BigInt(sats);
  const whole = n / 100000000n, part = (n < 0n ? -n : n) % 100000000n;
  const frac = part.toString().padStart(8, "0").replace(/0+$/, "");
  return frac ? `${whole}.${frac}` : `${whole}`;
}
