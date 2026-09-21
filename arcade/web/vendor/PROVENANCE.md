# What is in here, where it came from, and how it was checked

Three libraries, vendored rather than fetched, because a wallet whose
cryptography arrives from somebody else's CDN at page load is a wallet
that key theft is one compromised CDN away from. They are in the package,
they ship in the wheel, and they are served from the node the page came
from.

They are the only third-party JavaScript in this application. Everything
else the browser does is `crypto.subtle` (see `templates/signin.js`).
They are here because WebCrypto has none of what this needs:

    secp256k1       signing, and the point arithmetic BIP32 needs
    RIPEMD-160      half of what turns a public key into an address
    X25519,         the sealed box the messaging envelope already uses
    XSalsa20-
    Poly1305
    Blake2b         the nonce that sealed box is derived with

The last three are not a choice. What is on the chain is libsodium's
`crypto_box_seal`, and a browser that cannot open it is a wallet that can
read half the messages on one network. Changing the envelope for browser
accounts would have avoided the library and split the chain instead.

## @noble/secp256k1 2.1.0 → `noble-secp256k1.js`

    sha256  fb2f5bc4f05fca63bbeec3c28d1ed1e928e5d6790efb86aabe3edc21cc685a2d
    24,275 bytes, MIT, https://github.com/paulmillr/noble-secp256k1

Checked three ways on 2026-09-21, and all three had to agree:

1. Fetched from **jsdelivr** and from **unpkg** independently; the two
   files are byte-identical, so a single compromised mirror does not
   decide what is here.
2. The npm registry's own `dist.integrity` for 2.1.0
   (`sha512-XLEQQNdablO0XZOIniFQimiXsZDNwaYgL96dZwC54Q30imSbAOFf3NKtepc+cXyuZf5Q1HCgbqgZ2UFFuHVcEw==`)
   matches the sha512 of the published tarball, computed here.
3. The file inside that tarball is byte-identical to the one fetched from
   the CDNs.

## @noble/hashes 1.5.0 → `hashes/` (ripemd160 and what it imports)

    tarball sha512 matches the registry's integrity:
    1j6kQFb7QRru7eKN3ZDvRcP13rugwdxZqCjbiAVZfIJwgj2A65UmT4TgARXGlXgnRkORLTDTrO19ZErt7+QXgA==

Five files -- `ripemd160.js` and everything reachable from it -- taken
from the ESM build in that tarball and nothing else.

The set was worked out by FOLLOWING the imports, not by reading the file
list and choosing what looked relevant. The first attempt did the latter,
missed `_assert.js`, and the browser answered "error loading dynamically
imported module" with no clue which one. `tests/test_vendor.py` now
resolves every relative import and fails if it lands anywhere but here.

    ripemd160.js  cda421868b8afda4758185c3b8e18f2e92c9d8c58c64cd643b10196caa779f4e
    _md.js        a68964dae10647550e342326b431100c649387dbe23ec4d3799a1dc1f84fbe51
    crypto.js     9211d026c5d21e60e0126dd6f01150d87da5ba7261b8f468215c1264372ff5a5
    _assert.js    b32ab2fad690f26bccc440ee4251879fd327871ded8ee22e344e0b985f121f7d
    utils.js      a154f178738af5b790d54b8934831333b83264b4a6d36b307d8fb0a15a5fa3ed
                  (patched -- see below; upstream is
                   64cf850dde3aa9e5bd335904d3a326f971487159e43419a511240651c26f9444)

### The one line that was changed

`utils.js` line 8 imports a BARE specifier, which a browser cannot resolve
without an import map:

    -import { crypto } from '@noble/hashes/crypto';
    +import { crypto } from './crypto.js';

It names the file sitting beside it, which is the same file the bundler
would have resolved it to. The alternative was an import map in every page
that loads a coin key, which puts a rule about where cryptography comes
from into markup and out of this directory.

Both sums are written down above so the change is auditable rather than
merely declared: fetch the tarball, check the upstream sum, apply that one
substitution, and the patched sum follows.

## Keeping them honest

`tests/test_vendor.py` pins both sha256 sums. A change to either file is
a failing test rather than a quiet substitution, which is the failure mode
that matters: nobody reads a 24 KB file again once it is in a repository.

Upgrading is deliberate: fetch, check the three ways above, update the
sums in the test, and say in the commit what changed and why.


## tweetnacl 1.0.3 → `tweetnacl.js` (the `nacl-fast.js` build)

    sha256  6bcd37a3b20dce913f82d4b23e4e2b661058b4b953df8a3f8c45d56ac4f72447
    49,790-byte tarball, Public Domain, https://github.com/dchest/tweetnacl-js

Checked the same three ways on 2026-09-21: jsdelivr and unpkg
byte-identical to each other and to the file inside the published tarball,
whose sha512 matches the registry's
`sha512-6rt+RN7aOi1nGMyC4Xa5DdYiukl2UWCbcJft7YhxReBGQD7OAM8Pbxw6YMo4r2diNEA8FEmu32YOn9rhaiE5yw==`.

It is a UMD file, not an ES module: imported for its side effect, it sets
`globalThis.nacl`. Left as published rather than converted, because the
file is the thing that was audited.

**One caught mistake, recorded because it would be waved through a second
time.** The first check of this tarball reported a mismatch. It was not a
mismatch: the integrity string being compared against had been pasted from
the `@noble/hashes` check above. A false supply-chain alarm is worse than
none, because the next real one gets the same shrug.

## @noble/hashes 1.5.0 → `hashes/blake2b.js` and its imports

    blake2b.js    7fa3427c037c34e2f12de9b766465a51b3f1e5a41df5441bca7be506b2fecf70
    _blake.js     62f995fcf0a2c722b31443a5c9ad64c5cc61610b5d75db0626e655efabf2dcbc
    _u64.js       25a28c13f59b354b981f44c647a35e69df7775fbae5e549d966f7b1d519ea22e

The same tarball as `ripemd160.js` above, so the same integrity check
covers them. The set was again worked out by following the imports.

Extracted into its OWN directory this time: the first extraction of
tweetnacl went into the same folder as the hashes tarball, and for a few
minutes it was impossible to say which file had come from which archive.
Nothing wrong was copied, and only because the file in question exists in
one of the two.
