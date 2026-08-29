#!/usr/bin/env python3
"""Soak test: a scripted conversation, end to end, with a written report.

Plays a multi-scene conversation into a loopback audio device (BlackHole)
so the *real* microphone → ASR → ring buffer → selection → prompt → render →
manifest → screen path is exercised without a sound in the room. Meanwhile
it samples every seam the pipeline has and, at scene boundaries, changes a
setting live — grammar, abstraction, selection weights, local steps — and
records how long each took to show up in the next prompt or render.

Run against a party already started on ``presets/soak-local.yaml``:

    uv run egregore run presets/soak-local.yaml --ignore-settings &
    python3 tools/soak.py --log <party log> --out docs/reports

Stdlib only. Never reads or writes transcript text except through the
operator monitor, which is what it is for; the report quotes prompts and
validated motifs, never the transcript.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import statistics
import subprocess
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# The conversation. Six scenes, each a distinct world, so a theme that
# belongs to one is recognisable against another. Written to be spoken.
# ---------------------------------------------------------------------------

SCENES = [
    {
        "name": "coast",
        "seam": None,
        "lines": [
            "We drove out to the coast before sunrise and the tide had gone all the way out.",
            "There were pools left behind in the rock and the water in them was glowing this pale green.",
            "Something living in it, bioluminescence, every time you moved your hand the light followed.",
            "The whole cove was breathing light and we just stood there for an hour.",
            "My grandmother used to keep shells on the windowsill above the sink.",
            "I never asked her where any of them came from and now I can't.",
            "The kelp was heavy and dark and moved like slow hair under the surface.",
            "By the time the sun came up the glow was gone and it was just grey water again.",
        ],
    },
    {
        "name": "workshop",
        "seam": None,
        "lines": [
            "The scheduler keeps timing out because the latency on that board is terrible.",
            "It's the voltage, look at this electrical hazard, it works here but not there.",
            "I put a stronger higher amperage supply on it and the whole lattice of signals settled.",
            "Gears and pressure, everything in that room is gears and pressure and copper.",
            "The soldering iron tip is oxidised again so nothing wets properly.",
            "We should mount the maker stuff on the roof so the sensors get clean air.",
            "There's a recursive structure to the failure, it fails the same way at every scale.",
            "Iron filings on the bench, grey steel dust in every drawer.",
        ],
    },
    {
        "name": "forest-rain",
        "seam": {"kind": "grammar", "payload": {"aesthetic": {"grammar": (
            "Painterly, oil on canvas, thick impasto and visible brushwork. "
            "Wet-into-wet colour, soft edges, lamplight warmth. Suggest a place "
            "without showing one: no readable text, no recognizable faces."
        )}}, "expect": "impasto"},
        "lines": [
            "Then the rain came in under the canopy and everything went green and quiet.",
            "Moss on the fallen trunks, that deep wet green that almost hums.",
            "Water running down the bark in braids, little rivers on every tree.",
            "Ferns unrolling, the whole floor of the forest was ferns and rot and mushrooms.",
            "We sheltered under a cedar and the smell was overwhelming, resin and earth.",
            "A deer stood in the clearing for a full minute before it noticed us.",
            "Mist hanging in the branches, light coming through in long soft columns.",
            "You could hear the creek getting louder as the rain filled it.",
        ],
    },
    {
        "name": "city-night",
        "seam": {"kind": "abstraction", "payload": {"aesthetic": {"abstraction": 0.15}},
                 "expect": "Depict these subjects directly"},
        "lines": [
            "The city at three in the morning, neon reflecting in the wet street.",
            "Steam coming up from the grates and a taxi idling with its light off.",
            "Every window in that tower was dark except one on the fortieth floor.",
            "Pink and cyan signs, a noodle shop still open, the cook smoking outside.",
            "Rain on the taxi roof, the windshield wipers keeping time.",
            "The subway grate breathed warm air up through the sidewalk.",
            "Glass and chrome and the long red smear of brake lights down the avenue.",
            "A single crow on the traffic light, watching the empty crossing.",
        ],
    },
    {
        "name": "kitchen",
        "seam": {"kind": "selection", "payload": {"weaver": {"selection": {
            "salience": 0.1, "novelty": 0.1, "recency": 0.8}}}, "expect": None},
        "lines": [
            "Onions going soft in butter, that sweet smell filling the whole apartment.",
            "Bread rising under a towel on the radiator, the dough warm and alive.",
            "My mother's recipe for the stew, written on a card gone brown with oil.",
            "Steam from the pot fogging the window, the garden outside going blue with evening.",
            "Copper pans hanging over the stove, catching the last of the light.",
            "Cinnamon, cardamom, the coffee grinder rattling on the counter.",
            "Everyone arrives at once and the kitchen is suddenly too small and perfect.",
            "Candles on the table, wine, the dog under everyone's feet.",
        ],
    },
    {
        "name": "high-desert",
        "seam": {"kind": "local_steps", "payload": {"generation": {"local_steps": 12}},
                 "expect": 12},
        "lines": [
            "Out in the high desert the silence has a weight to it, a pressure on your ears.",
            "Red rock, ochre dust, a sky so wide it bends at the edges.",
            "Heat shimmer over the road, the horizon dissolving into liquid.",
            "At night the stars came down to the ground, the whole Milky Way lying on the mesa.",
            "Juniper and sage, that dry resin smell after the sun goes down.",
            "A coyote called from somewhere behind the ridge and another answered.",
            "The cold came fast once the light went, we sat close to the fire.",
            "Sparks going up into all those stars, you couldn't tell which was which.",
        ],
    },
]

# ---------------------------------------------------------------------------


def get(base: str, path: str, timeout: float = 5.0):
    with urllib.request.urlopen(base + path, timeout=timeout) as r:
        return json.load(r)


def post(base: str, path: str, payload: dict):
    req = urllib.request.Request(
        base + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


class Soak:
    def __init__(self, args):
        self.base = args.base
        self.device = args.device
        self.log_path = Path(args.log)
        self.out = Path(args.out)
        self.out.mkdir(parents=True, exist_ok=True)
        self.stamp = datetime.now().strftime("%Y-%m-%d-%H%M")
        self.samples_path = self.out / f"soak-{self.stamp}.samples.jsonl"
        self.rng = random.Random(7)
        self.t0 = time.time()
        self.stop = threading.Event()
        self.spoken_words = 0
        self.scene_log: list[dict] = []     # {name, started, ended, seam, applied_at, applied}
        self.prompts: list[dict] = []       # {t, scene, prompt, candidates, listened, age}
        self.clips: list[dict] = []         # {t, id, backend, duration}
        self.playing: list[dict] = []       # {t, clip_id}
        self.samples: list[dict] = []
        self.seen_clips: set[str] = set()
        self.last_prompt_at = 0.0
        self.pause_s = args.pause
        self.scene_gap_s = args.scene_gap
        self.rate = args.rate

    # -- helpers -----------------------------------------------------------

    def now(self) -> float:
        return time.time() - self.t0

    def scene_at(self, t: float) -> str:
        cur = "-"
        for s in self.scene_log:
            if s["started"] <= t:
                cur = s["name"]
        return cur

    def say(self, text: str) -> None:
        self.spoken_words += len(text.split())
        subprocess.run(
            ["say", "-a", self.device, "-r", str(self.rate), "-v", "Samantha", text],
            check=False,
        )

    # -- threads -----------------------------------------------------------

    def speaker(self) -> None:
        for scene in SCENES:
            if self.stop.is_set():
                return
            entry = {"name": scene["name"], "started": self.now(), "seam": scene["seam"],
                     "applied_at": None, "applied": None}
            if scene["seam"]:
                try:
                    res = post(self.base, "/api/settings", scene["seam"]["payload"])
                    entry["applied_at"] = self.now()
                    entry["applied"] = res.get("applied_live")
                except Exception as exc:                       # noqa: BLE001
                    entry["applied"] = f"error: {exc}"
            self.scene_log.append(entry)
            print(f"[{self.now():6.0f}s] scene {scene['name']}"
                  + (f"  seam={scene['seam']['kind']} -> {entry['applied']}" if scene["seam"] else ""))
            for line in scene["lines"]:
                if self.stop.is_set():
                    return
                self.say(line)
                time.sleep(self.rng.uniform(*self.pause_s))
            entry["ended"] = self.now()
            time.sleep(self.scene_gap_s)
        # A long tail of silence at the end so the last renders land.
        print(f"[{self.now():6.0f}s] conversation over; listening for the tail")

    def sampler(self, interval: float) -> None:
        with self.samples_path.open("a") as fh:
            while not self.stop.is_set():
                try:
                    self.sample(fh)
                except Exception as exc:                       # noqa: BLE001
                    print(f"[{self.now():6.0f}s] sample error: {exc}")
                self.stop.wait(interval)

    def sample(self, fh) -> None:
        t = self.now()
        st = get(self.base, "/api/status")
        z = st["zones"]["main"]
        mon = get(self.base, "/api/monitor").get("zones", {}).get("main", {})
        man = get(self.base, "/api/manifest?zone=main")
        sel = z.get("last_selection") or {}
        row = {
            "t": round(t, 1), "scene": self.scene_at(t),
            "in_flight": z.get("in_flight"), "queue": z.get("queue_depth"),
            "waited": z.get("waited_for_slot"), "held": z.get("held_for_speech"),
            "tokens": z.get("buffer_tokens"), "fragments": z.get("buffer_fragments"),
            "prompts_sent": z.get("prompts_sent"), "rejections": z.get("validator_rejections"),
            "purges": z.get("purges"), "bleeds": z.get("bleeds"),
            "lag": z.get("lag_s"), "age": sel.get("age_s"), "cands": sel.get("candidates"),
            "listened": sel.get("listened_s"), "winner_score": sel.get("winner_score"),
            "chain": z.get("current_chain_length"), "movements": z.get("movement_count"),
            "pool": z.get("active_clip_count"), "last_frame": z.get("has_last_frame"),
            "manifest_n": len(man.get("entries", [])), "manifest_rev": man.get("revision"),
            "playing": {s: v.get("clip_id") for s, v in (st.get("now_playing", {}).get("main", {}) or {}).items()},
        }
        fh.write(json.dumps(row) + "\n")
        fh.flush()
        self.samples.append(row)
        # prompt events
        at = z.get("last_prompt_at") or 0.0
        if at and at != self.last_prompt_at and z.get("last_prompt"):
            self.last_prompt_at = at
            key = (sel.get("listened_s"), sel.get("age_s"), sel.get("candidates"))
            same = bool(self.prompts) and self.prompts[-1].get("key") == key
            self.prompts.append({
                "key": key, "fill": same,
                "t": round(t, 1), "scene": row["scene"], "prompt": z["last_prompt"],
                "candidates": mon.get("candidates", []), "listened": sel.get("listened_s"),
                "age": sel.get("age_s"), "n": sel.get("candidates"),
            })
        # now playing events
        for s, cid in row["playing"].items():
            if cid and (not self.playing or self.playing[-1]["clip_id"] != cid):
                self.playing.append({"t": round(t, 1), "screen": s, "clip_id": cid})

    # -- after the run -------------------------------------------------------

    def harvest_log(self) -> list[dict]:
        """Render events from the party log: content-blind lines only."""
        out = []
        if not self.log_path.is_file():
            return out
        pat_local = re.compile(
            r"^(\S+ \S+) .*local clip backend=(\S+) zone=(\S+) duration=(\d+)s wall=([\d.]+)s "
            r"latency_est=([\d.]+)s seeded=(\w+)")
        pat_store = re.compile(
            r"^(\S+ \S+) .*clip stored id=(\S+) zone=(\S+) backend=(\S+) tier=(\S+) duration=([\d.]+)s")
        for line in self.log_path.read_text(errors="replace").splitlines():
            m = pat_local.match(line)
            if m:
                out.append({"kind": "render", "when": m.group(1), "backend": m.group(2),
                            "duration": int(m.group(4)), "wall": float(m.group(5)),
                            "seeded": m.group(7) == "True"})
                continue
            m = pat_store.match(line)
            if m:
                out.append({"kind": "stored", "when": m.group(1), "id": m.group(2),
                            "backend": m.group(4), "duration": float(m.group(6))})
        return out

    def comfy_steps_after(self, when_epoch: float) -> list[int]:
        """Steps ComfyUI was asked for in prompts queued after ``when_epoch``."""
        try:
            with urllib.request.urlopen("http://127.0.0.1:8188/history?max_items=60", timeout=5) as r:
                hist = json.load(r)
        except Exception:                                       # noqa: BLE001
            return []
        steps = []
        for _pid, rec in hist.items():
            nodes = rec.get("prompt", [None, None, {}])[2]
            st = None
            for n in nodes.values():
                if isinstance(n, dict) and "steps" in n.get("inputs", {}):
                    st = n["inputs"]["steps"]
            if st is not None:
                steps.append(int(st))
        return steps

    def report(self) -> Path:
        events = self.harvest_log()
        renders = [e for e in events if e["kind"] == "render"]
        stored = [e for e in events if e["kind"] == "stored"]
        by_backend = {}
        for e in stored:
            by_backend[e["backend"]] = by_backend.get(e["backend"], 0) + 1
        lags = [r["lag"] for r in self.samples if isinstance(r.get("lag"), (int, float))]
        lags = sorted(set(lags))
        ages = sorted({r["age"] for r in self.samples if isinstance(r.get("age"), (int, float))})
        cands = [r["cands"] for r in self.samples if isinstance(r.get("cands"), int)]
        walls = [r["wall"] for r in renders]
        queues = [r["queue"] for r in self.samples if isinstance(r.get("queue"), int)]
        tokens_max = max((r["tokens"] or 0) for r in self.samples) if self.samples else 0
        held_max = max((r["held"] or 0) for r in self.samples) if self.samples else 0
        waited_max = max((r["waited"] or 0) for r in self.samples) if self.samples else 0
        elapsed_min = self.now() / 60.0

        # which clips were on screen, by backend
        backend_of = {e["id"]: e["backend"] for e in stored}
        play_by = {}
        for p in self.playing:
            b = backend_of.get(p["clip_id"], "unknown")
            play_by[b] = play_by.get(b, 0) + 1

        def med(xs):
            return f"{statistics.median(xs):.1f}" if xs else "—"

        def rng(xs):
            return f"{min(xs):.1f}–{max(xs):.1f}" if xs else "—"

        L = []
        L.append(f"# Soak report — {self.stamp}\n")
        L.append(f"Preset `presets/soak-local.yaml`, scripted conversation through `{self.device}`, "
                 f"{elapsed_min:.0f} min, {self.spoken_words} words spoken across {len(self.scene_log)} scenes.\n")
        L.append("## Headline numbers\n")
        L.append("| metric | value |\n|---|---|")
        L.append(f"| prompts produced | {len(self.prompts)} ({sum(1 for p in self.prompts if not p.get('fill'))} paid, {sum(1 for p in self.prompts if p.get('fill'))} fill) |")
        fb = sum(1 for p in self.prompts if not p.get('fill') and 'formless drift; soft accumulation; quiet dispersal' in p['prompt'])
        L.append(f"| paid prompts rendered from the no-match fallback theme | {fb} |")
        L.append(f"| clips stored | {sum(by_backend.values())} — " + ", ".join(f"{b} {n}" for b, n in sorted(by_backend.items())) + " |")
        L.append(f"| local renders | {len(renders)} ({sum(1 for r in renders if r['seeded'])} seeded) |")
        L.append(f"| render wall (s) | median {med(walls)}, range {rng(walls)} |")
        L.append(f"| lag, last word → clip on disk (s) | median {med(lags)}, range {rng(lags)} over {len(lags)} clips |")
        L.append(f"| age of the chosen thought when picked (s) | median {med(ages)}, range {rng(ages)} |")
        L.append(f"| candidates per selection | median {med(cands)}, range {rng(cands)} |")
        L.append(f"| paid queue depth | max {max(queues) if queues else '—'} (pull scheduling: must be ≤ 1) |")
        L.append(f"| ticks waited for the GPU | {waited_max} |")
        L.append(f"| paid cycles held for speech | {held_max} |")
        L.append(f"| ring buffer peak | {tokens_max} tokens |")
        L.append(f"| validator rejections / purges | {self.samples[-1]['rejections'] if self.samples else '—'} / {self.samples[-1]['purges'] if self.samples else '—'} |")
        L.append(f"| chain / movements at end | {self.samples[-1]['chain'] if self.samples else '—'} / {self.samples[-1]['movements'] if self.samples else '—'} |")
        if play_by:
            tot = sum(play_by.values())
            L.append("| what the screen showed (beacon samples) | " + ", ".join(f"{b} {n/tot:.0%}" for b, n in sorted(play_by.items())) + " |")
        L.append("")

        L.append("## Seams — a setting changed live, and when the pipeline showed it\n")
        L.append("| scene | change | applied live | first prompt/render reflecting it | latency |\n|---|---|---|---|---|")
        for s in self.scene_log:
            if not s.get("seam"):
                continue
            kind = s["seam"]["kind"]
            expect = s["seam"].get("expect")
            hit = None
            if kind in ("grammar", "abstraction"):
                for p in self.prompts:
                    if p["t"] >= (s["applied_at"] or 0) and expect and expect in p["prompt"]:
                        hit = p["t"]
                        break
                what = f"prompt contains “{expect}”"
            elif kind == "local_steps":
                steps = self.comfy_steps_after(self.t0 + (s["applied_at"] or 0))
                hit = "yes" if expect in steps else None
                what = f"ComfyUI asked for steps={expect} (seen: {sorted(set(steps))})"
            else:
                what = "selection weights (no textual trace; see candidate scores below)"
                hit = "n/a"
            if isinstance(hit, float):
                lat = f"{hit - s['applied_at']:.0f}s"
                hit_s = f"{hit:.0f}s"
            else:
                lat = "—"
                hit_s = str(hit)
            L.append(f"| {s['name']} | {kind} | {s['applied']} @ {s['applied_at']:.0f}s | {what}: **{hit_s}** | {lat} |")
        L.append("")

        L.append("## Scenes → prompts\n")
        L.append("Validated motifs only — never transcript text. The room said something in each "
                 "scene; the question is whether the prompt for that stretch belongs to it.\n")
        for s in self.scene_log:
            ps = [p for p in self.prompts if s["started"] <= p["t"] < (s.get("ended", 1e9) + self.scene_gap_s)]
            L.append(f"### {s['name']} ({s['started']:.0f}s–{s.get('ended', 0):.0f}s), {len(ps)} prompt(s)\n")
            for p in ps:
                themes = re.search(r"(Depict|Show|Suggest|Render) these[^\n]*", p["prompt"])
                pal = re.search(r"Elemental palette[^\n]*", p["prompt"])
                tag = " *(fill — same selection as above)*" if p.get("fill") else ""
                L.append(f"- **{p['t']:.0f}s**{tag} — {themes.group(0) if themes else '(fallback prompt)'}")
                if pal:
                    L.append(f"  - {pal.group(0)}")
                if p.get("n"):
                    L.append(f"  - chose from {p['n']} candidate(s), listened {p.get('listened')}s, thought was {p.get('age')}s old")
                for c in (p.get("candidates") or [])[:4]:
                    mark = "▶" if c.get("winner") else "·"
                    L.append(f"  - {mark} {'; '.join(c.get('motifs', []))} "
                             f"(sal {c.get('salience')} nov {c.get('novelty')} rec {c.get('recency')} → {c.get('score')})")
            L.append("")

        L.append("## Renders\n")
        L.append("| # | backend | duration | wall (s) | seeded |\n|---|---|---|---|---|")
        for i, r in enumerate(renders, 1):
            L.append(f"| {i} | {r['backend']} | {r['duration']}s | {r['wall']:.1f} | {'yes' if r['seeded'] else 'no'} |")
        L.append("")

        L.append("## Timeline (every ~5 min)\n")
        L.append("| t | scene | tokens | in flight | held | lag | cands | chain | pool | playing |\n|---|---|---|---|---|---|---|---|---|---|")
        last = -999
        for r in self.samples:
            if r["t"] - last < 300 and r is not self.samples[-1]:
                continue
            last = r["t"]
            playing = ", ".join(f"{s}:{(backend_of.get(c, '?') if c else '-')}" for s, c in (r["playing"] or {}).items()) or "—"
            L.append(f"| {r['t']:.0f}s | {r['scene']} | {r['tokens']} | {r['in_flight']} | {r['held']} | {r['lag']} | {r['cands']} | {r['chain']} | {r['pool']} | {playing} |")
        L.append("")
        L.append(f"Raw samples: `{self.samples_path.name}`.\n")

        path = self.out / f"soak-{self.stamp}.md"
        path.write_text("\n".join(L))
        return path

    # -- run ---------------------------------------------------------------

    def run(self, tail_s: float, interval: float) -> Path:
        get(self.base, "/api/status")   # fail fast if the party is not up
        t_sampler = threading.Thread(target=self.sampler, args=(interval,), daemon=True)
        t_speaker = threading.Thread(target=self.speaker, daemon=True)
        t_sampler.start()
        t_speaker.start()
        t_speaker.join()
        time.sleep(tail_s)
        self.stop.set()
        t_sampler.join(timeout=interval + 5)
        return self.report()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8420")
    ap.add_argument("--device", default="BlackHole 2ch")
    ap.add_argument("--log", required=True, help="the party's log file")
    ap.add_argument("--out", default="docs/reports")
    ap.add_argument("--interval", type=float, default=10.0)
    ap.add_argument("--pause", type=float, nargs=2, default=(2.0, 5.0), help="seconds between lines")
    ap.add_argument("--scene-gap", type=float, default=45.0, help="silence between scenes")
    ap.add_argument("--tail", type=float, default=300.0, help="listen after the last line")
    ap.add_argument("--rate", type=int, default=170, help="say words per minute")
    args = ap.parse_args()
    path = Soak(args).run(args.tail, args.interval)
    print(f"report: {path}")


if __name__ == "__main__":
    main()
