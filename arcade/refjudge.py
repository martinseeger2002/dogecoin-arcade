"""Run one judge, in its own process: `python -m arcade.refjudge` < job.json.

The harness a game's author can reproduce exactly (docs/prize-pools.md):

    ctx = quickjs.Context()             # quickjs==1.19.4
    ctx.set_memory_limit(64 << 20); ctx.set_time_limit(5)
    ctx.eval("delete globalThis.Date; Math.random = undefined;")
    ctx.eval(judge_source)
    judge(seed, inputs, params)         # seed a hex string, the rest JSON

No DOM, no network, no clock, no randomness but the seed: the same replay
gives the same verdict every time it is run. Answers one line of JSON,
{"won": ..., "score": ...} or {"error": "..."}.
"""

import json
import sys


def main() -> None:
    job = json.loads(sys.stdin.read())
    try:
        import quickjs
    except ImportError:
        print(json.dumps({"error": "this node cannot referee: quickjs is not installed"}))
        return
    ctx = quickjs.Context()
    ctx.set_memory_limit(64 << 20)
    ctx.set_time_limit(5)
    try:
        ctx.eval("delete globalThis.Date; Math.random = undefined;")
        ctx.eval(job["source"])
        if ctx.eval("typeof judge") != "function":
            print(json.dumps({"error": "that judge defines no function judge"}))
            return
        ctx.set("__seed", str(job["seed"]))
        ctx.set("__inputs", job["inputs"])
        ctx.set("__params", job["params"])
        out = ctx.eval("JSON.stringify(judge(__seed, JSON.parse(__inputs), JSON.parse(__params)))")
    except Exception as exc:                                 # noqa: BLE001
        text = str(exc)
        if "interrupted" in text:
            print(json.dumps({"error": "the judge ran too long"}))
        elif text.strip() in ("null", "") or "out of memory" in text:
            print(json.dumps({"error": "the judge used too much memory"}))
        else:
            print(json.dumps({"error": "the judge failed: " + text.splitlines()[0][:200]}))
        return
    try:
        verdict = json.loads(out) if isinstance(out, str) else None
    except ValueError:
        verdict = None
    if not isinstance(verdict, dict):
        print(json.dumps({"error": "the judge did not answer with an object"}))
        return
    # The result exactly as the judge returned it (2026-09-30, the operator: "take
    # out the hard coded won and score entries ... leave them optional"): any
    # object, up to 2 KB. A pool that requires a win or a score reads those
    # fields when the judge gives them, and counts them missing when it does not.
    if len(json.dumps(verdict)) > 2048:
        print(json.dumps({"error": "the judge's result is larger than 2 KB"}))
        return
    print(json.dumps(verdict))


if __name__ == "__main__":
    main()
