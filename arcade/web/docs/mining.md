# Mining on this arcade

Mining is a module: **Admin → Edition & modules** switches it, per arcade. It is on in the full edition and off in
ARCADE Lite until you switch it on. Off, its pages, its console commands and the work this arcade gives other arcades'
miners all stop, and so do a running mining run and the stratum server.

**Admin → Mining** shows every chain this arcade has and how it can be mined here. `listmining` in the console says
the same.

## This machine's CPU, on test chains

A chain of real coins is never mined on a CPU: it finds nothing worth the power. A test chain is mined one of two ways.

- **By its own node**, when that node runs here. DigiByte testnet is mined this way, with odo, qubit and skein side by
  side, a thread each. Each DigiByte algorithm has its own difficulty, and a CPU wins the most blocks on the ones little
  else mines. Measured on a 4-core machine on 2026-11-04: about 80–90 blocks an hour.
- **By this arcade's own miner** (scrypt or SHA-256), with work from the chain's node here or from one it borrows. On a
  borrowed chain, a tenth of each block goes to the arcade that shares the node.

Start either from **Mine on this machine** in Admin → Mining, with an address, and hours or a number of blocks if you
like. From the console:

    mine dgbtest                       to your own account's DigiByte testnet address, until `mine stop`
    mine dgbtest 50 stz68...            50 blocks, to that address
    mining                             how it goes: blocks found by algorithm, hashes a second
    mine stop

A run keeps going across a restart of the arcade, such as an update: it picks up again once the chain's node answers.
Coins from a mined block can be spent once the chain is 100 blocks further.

## ASICs on your network

A stratum server on this machine hands local ASICs work from a chain's node here. This is solo mining: a block an ASIC
finds pays the address you give, whole.

1. In **Admin → Mining → ASICs on your network**, pick the chain, the algorithm and the address the blocks pay, and
   press Start. In the console: `stratum start <chain> <algo> <address> [port]`.
2. Point each ASIC at the address it shows, `stratum+tcp://<this machine>:3333`, with any worker name and any password.
3. Admin → Mining lists each miner with its difficulty, shares and hashrate, and every block found and what its node
   said.

**What an ASIC mines here:**
- a chain whose node runs on this machine, not a borrowed one;
- in **scrypt** (Litecoin, Litecoin testnet, DigiByte's scrypt) or **SHA-256** (DigiByte's sha256d);
- **not a merge-mined chain** (Dogecoin, Pepecoin, Bellscoin, Luckycoin, Flopcoin). Their blocks are mined beside their
  parent chain's work, which nothing here builds yet.

**How the server behaves:**
- **Difficulty:** each miner starts at a small ASIC's difficulty, and the server adjusts it to about one share every 8
  seconds.
- **Who may connect:** only machines on your own network, unless you tick "from outside this network too".
- **Version rolling** (BIP 310) is offered to SHA-256 ASICs that ask for it.
- **After a restart:** the server starts again with the arcade, as you left it.

Tested against a real DigiByte Core on regtest: a simulated ASIC mined a scrypt block and a SHA-256 block through it,
and the node took both (`tests/test_stratum_regtest.py`).
