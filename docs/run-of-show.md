# Run of show + physical kit checklist

## Kit checklist (pack the day before)

- [ ] Core node + power supply (and GPU box if split-role)
- [ ] Dedicated router / AP + ethernet cables
- [ ] 1 USB mic per zone + stands/mounts + extension cables
- [ ] Physical mute switches (wired, per zone)
- [ ] Screens/projectors + HDMI + power for each
- [ ] Printed signage — **the variant matching the mode you will run**
- [ ] Gaff tape, power strips, spare cables
- [ ] Party config YAML edited and validated (`uv run egregore check <cfg>`)
- [ ] If cloud tier: `GEMINI_API_KEY` exported and billing console checked
- [ ] If local tier: ComfyUI running, one manual LTX-2 generation verified

## T-minus

| When | What |
|---|---|
| T-45 min | Power network, core node. Start ComfyUI / Ollama if local. |
| T-40 | `uv run egregore run <config>`. Watch first procedural clip land. |
| T-35 | Place mics away from speakers; wire mute switches; test each (status shows the zone muted). |
| T-30 | Join each screen, fullscreen, verify it breathes with room sound. |
| T-20 | Tape signage at each mic. Walk every sightline. |
| T-10 | Check `/api/status`: all zones listening, spend $0, screens connected. |
| T-0 | Say the verbal framing (docs/signage.md). Go be at the party. |

## During the night

You should not need to touch anything. If you must intervene:

- A screen died → reopen the URL; it rejoins and back-fills from cache.
- Imagery must stop NOW (distressed guest) → hit the zone's mute switch;
  the buffer zeroes and no new themes originate there.
- Check spend at a glance: `/api/status` — the ceiling cannot be exceeded
  regardless, but the curve should track the night.

## After

- Ctrl-C the core node.
- Unless `export_dream` was set, wipe clips: `uv run egregore wipe <cfg>`.
- Nothing else was retained. That is the point.
