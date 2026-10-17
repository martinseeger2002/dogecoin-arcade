"""Mesh scale bench: N in-process mesh nodes on loopback, P players each spread over R rooms, kept
alive like real pages; after SETTLE seconds it measures for 20 s what each node SENDS (KB/s, by
message kind), how many arrivals were duplicate copies, and whether every node with a player in a
room sees every member of it (presence).

    ~/arcade-work/venv/bin/python tools/mesh_scale.py NODES PLAYERS_PER_NODE ROOMS [SETTLE]
    ulimit -n 8192 first. One Python process carries every node: past ~60 nodes the
    process itself is the bottleneck (numbers stop meaning anything; run it on a big box).

Results 2026-10-12 (30 players/node, every node in ~30 rooms -- harsher than any real game):
    mesh v1 (flooded member lists):  10 nodes 82 KB/s, 20 nodes 206 KB/s, 40 nodes broke down
    mesh v2 (this branch):           20 nodes 27 KB/s, 40 nodes 15.7 KB/s, 0% duplicates, presence 1200/1200
    + binary room messages:          40 nodes 9.1 KB/s; send -> heard median 1.0 ms, 95% 1.4 ms, worst 1.7 ms
                                     (the mesh's own cost across several hops; real latency adds the network)
Gate for releases that touch the mesh: 40 30 80 40 must show presence N/N, under 15 KB/s per node and a
median latency under 5 ms.
"""
import asyncio, random, sys, time, collections
sys.path.insert(0, ".")
import nacl.signing
from arcade.mesh import node as M
from arcade.mesh import link as L
N, PLAYERS, ROOMS, SECS = int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]), 20.0
SETTLE = float(sys.argv[4]) if len(sys.argv) > 4 else 25.0
sent = collections.Counter(); kinds = collections.Counter()
orig_send = L.Link.send
async def send(self, header, blob=b""):
    n = len(L.Link.pack(header, blob)) + 4 + 16
    sent[id(self)] += n; kinds[header.get("t")] += n
    return await orig_send(self, header, blob)
L.Link.send = send
dups = collections.Counter()
orig_fresh = M.MeshNode._fresh
def fresh(self, header):
    ok = orig_fresh(self, header)
    dups["fresh" if ok else "dup"] += 1
    return ok
M.MeshNode._fresh = fresh
async def main():
    nodes = [M.MeshNode(nacl.signing.SigningKey.generate(), "scale", listen_host="127.0.0.1", listen_port=0, outbound_target=4) for _ in range(N)]
    for n in nodes: await n.start()
    for i, n in enumerate(nodes):                     # each knows a few others: a random mesh
        for j in random.sample(range(N), min(6, N - 1)):
            if j != i: n.add_address("127.0.0.1", nodes[j].listen_port, nodes[j].node_id)
    t0 = time.time()
    while time.time() - t0 < 40 and sum(1 for n in nodes if n.links) < N: await asyncio.sleep(0.2)
    cred = {"a": "n" + "x" * 33, "x": int(time.time()) + 80000, "s": "H" + "y" * 87}   # a real certificate's size
    for i, n in enumerate(nodes):
        for p in range(PLAYERS):
            room = "game%d/room%d" % (p % max(1, ROOMS // 4), (i * PLAYERS + p) % ROOMS)
            await n.join(room, "n%033d" % (i * PLAYERS + p), dict(cred, a="n%033d" % (i * PLAYERS + p)))
    async def pages():                             # each player's page says "still here", as a browser does
        while True:
            for n in nodes:
                for (room, member) in list(n.local): n.touch(room, member)
            await asyncio.sleep(2)
    keep = asyncio.create_task(pages())
    await asyncio.sleep(SETTLE); sent.clear(); kinds.clear(); dups.clear()
    await asyncio.sleep(SECS)
    per = sum(sent.values()) / N / SECS
    links = sum(len(n.links) for n in nodes) / N
    print(f"nodes {N}, players/node {PLAYERS}, rooms {ROOMS}, links/node {links:.1f}: sent per node {per/1024:.1f} KB/s; by kind (KB/s/node):",
          {k: round(v / N / SECS / 1024, 1) for k, v in kinds.most_common(5)}, flush=True)
    print(f"   arrivals: {dups['fresh']} new, {dups['dup']} duplicate ({100*dups['dup']/max(1,dups['fresh']+dups['dup']):.0f}% wasted)", flush=True)
    # latency: a message from one player, timed to every other player in the room (under the same load)
    lat, hopsn = [], []
    rooms = sorted({r for n in nodes for r, _ in n.local})
    for room in random.sample(rooms, min(40, len(rooms))):
        holders = [(n, m) for n in nodes for (r, m) in n.local if r == room]
        if len(holders) < 2: continue
        sn, sm = holders[0]
        listeners = [(n, n.local[(room, m)].queue) for n, m in holders[1:] if n is not sn]
        for _, q in listeners:
            while not q.empty(): q.get_nowait()
        tag = ("t%f" % time.monotonic()).encode()
        t0 = time.monotonic(); await sn.send(room, sm, tag)
        async def arrive(q):
            while True:
                ev = await q.get()
                if ev.type == "message" and ev.data == tag: return time.monotonic() - t0
        got = await asyncio.gather(*[asyncio.wait_for(arrive(q), 5) for _, q in listeners], return_exceptions=True)
        lat += [g for g in got if isinstance(g, float)]
    if lat:
        lat.sort()
        print(f"   latency (send -> heard, {len(lat)} deliveries, all in one process): median {1000*lat[len(lat)//2]:.1f} ms, "
              f"95% {1000*lat[int(len(lat)*0.95)]:.1f} ms, worst {1000*lat[-1]:.1f} ms", flush=True)
    # correctness: for every room, does every node with a player there see all its members?
    want = collections.defaultdict(set)
    for i, n in enumerate(nodes):
        for (room, member) in n.local: want[room].add(member)
    checked = ok = 0; why = []
    for n in nodes:
        for room in {r for r, _ in n.local}:
            checked += 1
            seen = {m["member"] for m in n.members(room)}
            ok += seen == want[room]
            if seen != want[room] and len(why) < 12:
                for miss in list(want[room] - seen)[:1]:
                    org = next(x for x in nodes if (room, miss) in x.local)
                    rt = M.room_id(room) in n.routes and org.node_id in n.routes[M.room_id(room)]
                    rem = n.remote.get(room, {}).get(org.node_id)
                    back = M.room_id(room) in org.routes and n.node_id in org.routes[M.room_id(room)]
                    why.append(f"route n->org {rt}, org->n {back}, remote entry {'none' if rem is None else ('member there' if miss in rem['members'] else 'member absent')}, cred {None if rem is None else (rem['members'].get(miss) is not None)}")
    print("   why missing:", *collections.Counter(why).most_common(6), sep="\n      ")
    print(f"   presence: {ok}/{checked} (node, room) pairs see every member; members per room ~{sum(map(len, want.values()))/len(want):.0f}", flush=True)
    for n in nodes: await n.stop()
asyncio.run(main())
