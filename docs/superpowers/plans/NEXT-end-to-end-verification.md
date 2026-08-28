# Next batch: prove the whole flow, end to end

Raised 2026-08-28. Nothing here is a new feature. Every item is "we built it,
we have not watched it work on real conversation", which is the only thing
that matters now.

## The claim to be proved

> Someone speaks in a room. That speech becomes a transcript. That transcript
> becomes an abstracted prompt. That prompt becomes video. That video appears
> on a screen. With more than one zone, each gets its own.

No part of that has been observed whole. The pieces are each tested; the seam
between them is not.

## 1. Real speech to real video, procedurally

The shortest complete loop, and the one to establish first because it needs no
GPU and no key.

- Run `presets/live-mic.yaml`, speak, confirm in order: `buffer_tokens` rises,
  `prompts_sent` rises, `validator_rejections` stays 0, a clip lands, the clip
  appears in the manifest, the Lens plays it.
- Capture the actual prompt that was synthesised from what was said. Confirm
  by eye that it relates to the conversation and carries none of its words.
- Screenshot the screen showing a clip that came from that sentence.

## 2. Local diffusion from real speech

- Same, with `presets/local-demo.yaml` and ComfyUI up, so LTX renders from a
  transcript rather than from a canned prompt.
- Confirm the clip on screen was generated after the sentence was spoken
  (compare clip mtime against the log line for the prompt).

## 3. fal.ai from real speech

- `FAL_KEY` is set. Run `scripts/verify_fal.py --generate` first to prove the
  key bills, then `presets/fal-demo.yaml` end to end.
- Confirm the ladder actually chose fal, not the procedural fallback: the clip
  record should read `backend=fal tier=minimax-h3-max`.
- Watch the ledger: reserved, then settled at the real price.

## 4. Multiple zones, each with its own video

- Two zones, `topology: independent`, both generating.
- Confirm the two manifests differ, the two screens show different clips, and
  each zone's clips derive from its own room's speech.
- Then `commons` and `mirror`, confirming the documented difference is what
  actually happens on screen.

## 5. The input source is not trustworthy yet

Reported: tapping the microphone does not move the live meter, and the zones
panel says `usb — system default input` on a Mac with no USB microphone
attached.

Two separate problems, at least:

- **The label is wrong.** `usb` is the schema's name for "a local audio
  device", not "a device on the USB bus". On a laptop it is the built-in
  microphone. The UI should say which device it actually opened, by name, and
  say `built-in` where that is what it is. `MicConfig.type` is a frozen
  Literal, so the fix is in how it is presented, not in what it is called on
  disk.
- **The meter may genuinely not be moving.** Note that `presets/party.yaml`
  uses `mic.type: network`, which opens no local microphone at all — tapping
  the laptop would correctly do nothing there. Establish which preset was
  running before concluding anything. Then verify, for a `usb` zone, that
  `MicSource` reaches `on_features` at all: put a level readout next to the
  device name in the zones panel and watch it while tapping.

Also worth checking while in there: which device `sounddevice` actually opens
when `device: null`, and whether it is the one the operator expects. Offer a
device picker if it is not.

## How this gets closed

Not by a green test run. By a screenshot of a screen playing a clip, next to
the log line showing the sentence that produced it, for each of the four paths
above.
