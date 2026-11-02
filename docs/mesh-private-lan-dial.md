# Mesh UDP was dialing other networks' home addresses

## What was observed

On a Windows host the web app was started dial-out only:

`arcade-web.exe --mesh-port 0 --mesh-core off --stun-port 0`

The host firewall blocks the app from the local LAN. The app's single mesh UDP socket still sent UDP, over and over, to private addresses that are not on that host's network. Two of them were `192.168.1.20:32805` and `192.168.1.20:41658`. Nothing answered ARP for either. The traffic looks like a scan of the LAN, and it tells every peer this machine's internal address.

`--mesh-core off` does not stop it. That flag only turns off Core-to-Core peering. The UDP path is a separate extension and starts whenever the mesh does.

## Cause

A linked peer is reached over UDP by hole punching (`arcade/mesh/punch.py`, `arcade/mesh/udp.py`). Each side asks STUN what address the internet sees for its one UDP socket (other mesh nodes' STUN first; the punch probe then tries Google and Cloudflare). It sends that list to the peer. `Session.offer` then always appended `local_ipv4()`, this machine's home-network address, so two arcades in one house could reach each other there.

The receiver did not look at what kind of address that was. `punch._addrs` keeps every well-formed IPv4 address (up to four), and these all aim at the whole list:

- `UdpShare._try` / `UdpShare._answer` (`arcade/mesh/udp.py`), on the node's one UDP socket, for every link. This is the repeated traffic. It runs with mesh core off.
- `PunchShare.run` / `PunchShare._answer` (`arcade/mesh/punch.py`), the admin punch probe, which uses its own socket.
- `Introducer._punch` (`arcade/mesh/introduce.py`), which also sets `spray_host` from the first address and, for a NAT that picks a new port per destination, sends to random high ports on that host.

A peer on another network advertising `192.168.1.20` therefore made this node transmit to `192.168.1.20` on the LAN here. The introduction frame is also relayed, so the home address was visible to nodes along the way.

## Fix

`punch.dialable` is what a node may send to. A public IPv4 address is always dialed. A private address (RFC 1918), link-local (`169.254.0.0/16`), loopback, or CGNAT (`100.64.0.0/10`, and the rest of the non-global space) is dialed only when that is safe:

- **`auto` (default).** Only when the peer's offer contains a public address that STUN observed for us. That is the same NAT, so their home address is on this LAN. A shared private address does not count: `192.168.1.10` is a common address on someone else's network. Loopback is dialed only when this node's own STUN answer was that loopback address, so a peer cannot point the socket at a service on this machine.
- **`on`.** Dial advertised home-network addresses as well, for several nodes on one LAN when STUN cannot show they share a NAT. Loopback stays under the rule above.
- **`off`.** Never dial a non-public address, even on the same NAT.

`Session.offer` no longer puts a home-network address in what a remote peer is told. The side that has already seen the other's public address includes it, and only then (`punch.include_lan`). The direct UDP answer does this. A relayed introduction does not, unless the setting is `on`, because every hop would otherwise see the address. Port guesses (`punch.spray_at`) go to a public address, or to loopback when that is our own mapped address, never to a home-network address.

Public-address hole punching is unchanged: the outside address STUN saw is still offered and still dialed.

Two nodes in one house still get a direct UDP path, whether or not their NAT hairpins. The node that answers the punch has seen the asker's public address and includes its home address. The asker then knows they share a NAT, so it sends its own home address over the link (`op: "home"`). Both sides must aim: a host firewall drops a LAN packet until its own side has sent to that address. A home address that answers replaces an outside one on a path that is already up, because a router that hairpins answers about as soon and costs far more (measured on two machines in one house: 1 to 2 ms on the LAN, 50 to 65 ms through the hairpin). A node with no TCP link, met through a relayed introduction, does not get this. That UDP exchange is unchanged.

## TCP dials the same way

The link is still found over TCP, and TCP stays up as the backup when the UDP path goes quiet. That dialer (`MeshNode._dial`) connects to addresses in the peer book. A node that shares a LAN with `192.168.1.20` learns that address from the connection itself (`link.host` plus the peer's listen port, 8421) and, once it has reached it, tells its other links. Those links are not on that LAN. They were opening TCP to `192.168.1.20:8421` over and over.

`punch.tcp_targets` asks `punch.dialable` and is the list the dialer, the IPv6 meeter and the tuner may connect to. It keeps every public IPv4 address and every tunnel name. A private, link-local or CGNAT address, and the same address written as `::ffff:a.b.c.d` or as the host of a `ws://` URL, is kept only when `dialable` says so. Unique-local and link-local IPv6 follow that same LAN rule. The whole set of addresses known for that peer is judged together, so the public one is visible next to the home one.

The same filter runs on the way in and the way out of the address gossip (`addrs` frames). A home-network address is not stored from a neighbour, and not sent to a neighbour, unless that neighbour is on this NAT (or `mesh_lan_dial` is `on`).

An address the operator configured (`--mesh-peer`) is still dialed. That is how a machine is pointed at a node on its own LAN before STUN has shown they share one. `mesh_lan_dial` `off` dials no home-network address, including those. Loopback (`127.0.0.1`, `::1`) stays dialable until STUN has reported a public address, so two nodes on one machine still meet. The reachability dial-back is unchanged: it connects only to the address the asker's own connection came from.

A shared carrier-grade NAT can give two unrelated nodes the same public address. `auto` will then try the home address, and those packets stay on the LAN here. `off` is the switch if that matters.

## Setting

In the node's `settings.json`:

```json
"mesh_lan_dial": "auto"
```

`auto`, `on`, or `off`, as above. Unset is `auto`. `ARCADE_MESH_LAN_DIAL`, when set, overrides the file. The mesh reads it when it builds an offer or chooses targets. `state.settings()` notices a changed file, so the next punch uses the new value.

The same note is in the node setup guide under the mesh options.

## How to test

Unit tests, from the repository root:

```bash
python -m pytest tests/test_punch.py tests/test_mesh.py tests/test_mesh_udp.py tests/test_mesh_introduce.py tests/test_stun.py
```

`test_a_peer_on_another_network_is_not_dialed_at_its_home_address` is the observed UDP case: our outside address `129.222.44.64`, the peer offering `156.67.28.112` plus `192.168.1.20:32805` and `192.168.1.20:41658`, and only the public address is dialed. The following test is the same public address, where the home addresses are dialed. `test_tcp_targets_follow_the_same_rule_as_udp` is that rule for the TCP dialer, including `192.168.1.20:8421`. `test_the_dialer_does_not_open_tcp_to_another_nats_home_address` runs a node and checks `open_connection` is called for the public address and not for `192.168.1.20:8421`. `test_the_probe_punches_the_mapped_address_and_not_the_home_network` runs two local nodes and checks the offers on the wire contain no `192.168.1.20`. The UDP and introduction tests check those call paths.

On a machine, with another arcade whose public address is different: run as before (`--mesh-port 0 --mesh-core off --stun-port 0`) and confirm the UDP socket is not sending to `192.168.0.0/16`, `10.0.0.0/8`, `172.16.0.0/12`, `169.254.0.0/16`, `127.0.0.0/8`, or `100.64.0.0/10`. Packets to the peer's public address should still appear once STUN has mapped the socket. With two nodes behind one NAT, `auto` should still open a UDP path. With `"mesh_lan_dial": "on"`, a home address is offered and dialed even when the public addresses differ.
