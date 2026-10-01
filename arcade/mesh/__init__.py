"""The arcade mesh: arcade nodes talking to each other directly, for realtime rooms.

Everything else an arcade knows, it knows from the chain, and that is right for
anything that must be agreed on. It is wrong for a player walking across a
town five times a second: a transaction per step is seconds late and costs a
fee. The mesh is the other half -- the "small separate overlay" the messaging
notes (docs/p2p-messaging.md) said would be needed for anything that must be
free and ephemeral.

* **Peer to peer.** Nodes connect to each other by address (host and port) and
  prove who they are by key. There is no server in the middle, no website and
  no hub: a node that can be reached accepts connections, a node behind a
  router dials out, and every node forwards for the others.
* **Game- and data-agnostic.** A room is an opaque name and a message is
  opaque bytes. The mesh never reads either; it moves them, caps their size
  and rate, and says who is in a room.
* **Nothing kept.** Messages are delivered and forgotten. What must last
  belongs on the chain.

`link` is one encrypted, authenticated connection between two nodes; `node`
is a node's view of the whole mesh: its peers, its rooms, and the forwarding.
"""
