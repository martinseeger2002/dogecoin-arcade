# For AI coding agents working in this repository

Before you change anything, read `COLLAB-NOTES.md` in this folder. Read all of it, after every pull.

That file is how the two sides of this project talk to each other: the operator's DogecoinArcade and
SadFrog's PepecoinArcade, each built with an AI agent. Before you push or hand over a merge, add a
dated entry at the top of its Log. Say what you changed, which branch and commits, and what you
need from the other side. Never edit the other side's entries.

Before you change anything that signs, files or spends a pre-signed transaction or a lock -- listings,
Doginal listings, buy-order lots, bids, swap locks and their lock times, escrows, a chain's address
bytes, or anything that hands a transaction to a node's wallet to sign -- read
`arcade/web/docs/exchange-safety.md`. It lists the rules that keep coins from being taken (learned from
the Doginal Dogs theft, 2026-10-26), the code that holds each one and the test that proves it. Keep the
rule, or change that page and its test in the same commit and say why in `COLLAB-NOTES.md`.
