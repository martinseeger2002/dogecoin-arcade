# arcade/lora: the LoRa (Meshtastic) gateway

A copy of `gateway/arcade_lora` from https://github.com/dogecoinarcade/arcade-lora (commit 92f88e3, 2026-10-26: no radio acks, RESEND, zlib pages, airtime pacing, replay gap, hops as needed, coin types, T-Deck-built letters via BROADCAST, pictures on .mesh pages via PIC_REQ),
unchanged, so every arcade runs the same gateway (Admin -> Meshtastic, `arcade/web/lora.py`). Its protocol is that
repository's PROTOCOL.md; its tests are `tests/test_lora_{frames,gateway,keys,txcheck}.py` here (the C++ T-Deck client
tests stay in arcade-lora: they need its hosttest build).

To take a newer gateway: copy `gateway/arcade_lora/*.py` over these files and the four Python test files over
`tests/test_lora_*.py` (imports `arcade_lora` -> `arcade.lora`), run them, and say so in COLLAB-NOTES.

Needs the `[lora]` extra (meshtastic, coincurve); Admin -> Meshtastic installs it at the node machine.

Changed here since that commit (2026-11-09, security review): the arcade files no address without its coin key's proof
that it is the account's (`wallet_imports.check_proof`). So ADDR carries, after the coin key, one length byte and that
key's DER signature over `wallet_imports.proof_message(account login key, network, address, coin key)` hashed twice
with SHA-256 (`device.py` `register`); `gateway.py` passes it on, and a device that sends none is refused by the
arcade for that reason. The upstream gateway and the C++ T-Deck client need the same change.
