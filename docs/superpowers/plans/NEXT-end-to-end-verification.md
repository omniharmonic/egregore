# Open work: prove it, then make it adaptive

Updated 2026-08-28. Ordered: finish the verification run first, then the
tuning and interoperability work.

---

## A. Verification still open (finish this first)

### A1. Local diffusion from real speech — NOT YET SEEN
Procedural and fal are both proved end to end (speech -> transcript ->
abstracted prompt -> clip -> screen, with screenshots). ComfyUI/LTX is not.
Run `presets/local-demo.yaml` with ComfyUI up, speak, and confirm a clip whose
mtime is after the sentence that produced it. The 5-minute render makes this
slow to observe, not hard.

### A1b. ComfyUI jobs outlive the party that queued them — BUG
Found while proving A1. ComfyUI's queue lives in the server, so every restart
of Egregore leaves its in-flight and pending renders running. Four orphaned
12-minute jobs from earlier runs were ahead of the live one, which looked
exactly like "local video never appears". `ComfyUIBackend` should cancel its
outstanding prompt ids on close (ComfyUI takes `POST /queue {"delete": [id]}`
and `/interrupt`), and the dashboard should show the backend's queue depth so
a stale backlog is visible rather than mysterious. `FalBackend` has the same
shape of problem and fal hands back a `cancel_url` for it.

### A2. Multiple zones, each with its own video
Topology tests pass at the unit level. Nobody has watched two screens showing
different clips derived from two rooms' speech. Do `independent`, then
`commons`, then `mirror`, and screenshot each.

### A3. Microphone selection is not in the UI
The zones panel says "set in the preset" and names the device `usb`, which is
the schema's word for "a local audio device" and reads as "a USB device". On a
laptop it is the built-in microphone.

Two things to build:
- Name the device that was actually opened, from `sounddevice`, rather than
  the config word for its kind.
- Let the operator pick an input device from the dashboard. The device is
  opened once at start-up, so either mark it restart-only and persist the
  choice, or make `MicSource` able to reopen on a new device — the latter is
  better and is what "adaptive" asks for.

### A4. Reaching it from a phone
Reported: `localhost:8420` did not open on a phone. `localhost` on a phone is
the phone, so that is expected — the LAN address is what to open. But the
system should not depend on someone knowing that:
- Print the LAN address in the banner, not `<this-host>`.
- Show it on the join page and dashboard (already partly done).
- Check for the failure modes that actually bite: macOS firewall prompts,
  wifi client isolation on guest networks, and a `serving.bind` that is not
  `0.0.0.0`. Add a preflight to `egregore setup` that reports the reachable
  address and warns when bound to loopback.

---

## A6. Aesthetic grammar should be editable inline
The dashboard shows each zone's prompt preamble, which is the right place for
it — but it is read-only. Make it editable per zone and applied live: it is
the single strongest lever over how everything looks, and changing it
currently means editing a preset and restarting a party.

## A7. Abstract is a choice, not a law
The grammar hard-codes "abstract, symbolic, non-representational". Offer named
registers — abstract, semi-representational, figurative — as a starting point
someone can then edit, rather than one aesthetic baked into every preset.

## A8. Continuity mode is unproven on both real backends
Everything so far has run in mosaic. Continuity chains clips with last-frame
seeding, and neither fal nor LTX has been watched doing it. Worth knowing
before a party whether it looks better than mosaic — and whether MiniMax,
which cannot continue its own clips, degrades gracefully.

## A9. Local models driving the whole thing, at quality
Local LTX is proved to produce a clip from real speech (76s wall, 512x320,
8 steps). What has not been seen is a *good* local party: enough clips, at a
resolution worth looking at, sustaining a loop. This is the highest-value
remaining verification.

## B. Procedural art: make it tunable, then make it listen

The audio-reactive shader stack is the part that most wants knobs.

### B0. Make the art more alive
The direction asked for: smoke, liquid, light, refraction, diffusion,
psychedelic. The existing flow/feedback/liquid/bloom lenses are the seed of
that and the parts to push. Worth adding: domain-warped flow, chromatic
refraction that separates on movement, a smoke/curl-noise advection pass, and
light-bloom that blooms from the video's own bright regions rather than
uniformly.

### B1. Expose shader parameters
Each lens has constants baked into its `.frag`. Lift the ones worth playing
with into uniforms driven by config: feedback decay and zoom, flow speed and
scale, glitch density and block size, chroma separation, CRT curvature and
scanline weight, kaleidoscope segments, pixelsort threshold. Per zone, live,
on the same push channel the lens stack already uses.

### B2. Presets for the look, not just the party
Named looks ("deep", "brittle", "liquid", "broadcast") that set a stack plus
its parameters together, so an operator changes the feel in one move.

### B3. Drive parameters from the transcript
The weaver already produces a ThemeObject with valence, intensity, movement
and an elemental palette, and it currently only reaches the video prompt. The
same object could set shader parameters, so the *compositing* responds to what
the room is talking about, not only to how loud it is. This is the piece that
would make the art feel authored by the room rather than decorated by it.

### B4. Confirm the composite, visibly
The shaders run over the generated video, not instead of it. That is easy to
lose track of and worth a side-by-side screenshot in the docs: same clip, no
lenses; same clip, full stack.

---

## C. Split the pipeline across machines (DGX Spark)

Goal: default to one machine, but let any stage be pointed at another box on
the LAN, so a Spark can do diffusion while this laptop keeps its GPU for WebGL
and Parakeet.

Already remote-capable today, by URL:
- **Video (local diffusion)** — `generation.comfyui_url`. Point it at the
  Spark and it already works. Verify over the LAN and document it.
- **Prompt abstraction** — `weaver.llm.base_url` speaks an OpenAI-compatible
  API. An LLM on the Spark would replace the deterministic heuristic.

Not remote-capable yet:
- **Transcription** — Parakeet is in-process. Wants an ASR service interface
  so the Scribe can call a remote endpoint, mirroring how the forge calls a
  remote ComfyUI.
- **Health and discovery** — the dashboard should show each stage's endpoint
  and whether it is reachable, so a split rig is diagnosable from one page.

Design note: every one of these is already an interface, so this is mostly
about adding a URL to config and a health row to the dashboard rather than
restructuring anything. Keep the default as one machine, with every URL
defaulting to localhost.

---

### A5. Shader passes are dropped under software rendering
Not a bug, but it confuses every screenshot taken in a headless browser: the
Lens drops lens passes when frame time exceeds budget (VIS-7), and SwiftShader
is always over budget, so automated captures often show `lens 0/4` and look
like the stack is not running. Real GPUs do not hit this. Any visual check of
the shaders needs either a real GPU or the adaptive drop pinned off for the
duration of the test.

## D. Answered, for the record

**What turns a transcript into a video prompt?** No model at all, by default.
`weaver.engine: auto` uses `HeuristicAbstractor`, a deterministic
keyword/lexicon mapper, because `weaver.llm.base_url` is unset. That is what
produced "vast blue depth; inherited memory; a shape passed down" from talk of
tide pools and a grandmother's shells. An `LLMAbstractor` exists and takes any
OpenAI-compatible endpoint — which is the natural first thing to move to the
Spark.

**Did the shaders change during testing?** Yes, and not by design: the live
lens-stack control was exercised against the running party, which left zones
on whatever stack the test last set. Presets are unaffected; a restart returns
to them.
