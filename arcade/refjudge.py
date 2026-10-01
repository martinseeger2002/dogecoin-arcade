"""Run one judge, in its own process: `python -m arcade.refjudge` < job.json.

The harness a game's author can reproduce exactly (docs/prize-pools.md):

    ctx = quickjs.Context()             # quickjs==1.19.4
    ctx.set_memory_limit(64 << 20); ctx.set_time_limit(5)
    ctx.eval("delete globalThis.Date; Math.random = undefined;")
    ctx.eval(judge_source)
    judge(seed, inputs, params)         # seed a hex string, the rest JSON

No DOM, no network, no clock, no randomness but the seed: the same replay
gives the same verdict every time it is run. Answers one line of JSON,
{"won": ..., "score": ...} or {"error": "..."}; an escrow's judge may also say
{"release": true|false, "to": address, "why": "..."}.
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
        print(json.dumps({"error": "the judge did not answer {won, score}"}))
        return
    score = verdict.get("score", 0)
    said = {"won": verdict.get("won") is True,
            "score": score if isinstance(score, (int, float)) else 0}
    # An escrow's judge answers a different question -- may these things go to
    # that address -- in the same three words a person can check: `release`,
    # who `to` (optional, and only ever compared), and `why` when it says no.
    if "release" in verdict:
        said["release"] = verdict.get("release") is True
    # A game-state judge's yes or no to the states a page asked for.
    if "update" in verdict:
        said["update"] = verdict.get("update") is True
    if isinstance(verdict.get("to"), str):
        said["to"] = verdict["to"][:100]
    if isinstance(verdict.get("why"), str):
        said["why"] = verdict["why"][:200]
    print(json.dumps(said))


if __name__ == "__main__":
    main()
