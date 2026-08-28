# Signage — printable disclosure copy

Per PRD P-5, the copy must be **literally true in the mode being run**. Print
the variant matching your configuration and place one at each microphone.
`privacy.signage_required: true` is the default for all real presets.

---

## Cloud mode (any preset where `budget.total_usd > 0` and a Veo backend is enabled)

> **This room is listening.**
>
> Microphones here transcribe conversation on a computer in this building.
> The recording is never saved, and the words are destroyed within five
> minutes. Nobody — including us — can read back what was said.
>
> From those words a computer here writes a short, abstract description of
> the *mood and themes* in the room. That description, and nothing else, is
> sent out to a video service that renders what you see on the screens. It
> contains no names, no quotes, and nothing that could identify anyone.
>
> The switch below silences this microphone.

---

## Local mode (`budget.total_usd: 0`, local/procedural backends only)

> **This room is listening.**
>
> Everything happens on a computer in this building. No audio, no words,
> and no data of any kind leave this room. Recordings are never saved and
> words are destroyed within five minutes.
>
> The switch below silences this microphone.

---

## Verbal framing script (spoken at the door or at opening)

> "The screens tonight are dreaming about what the room talks about. The
> mics don't record anything — words live for five minutes in memory and
> are destroyed, and nothing anyone says can ever be read back or shown.
> What reaches the screens is only mood and theme, made abstract. If you'd
> rather not be listened to near a particular mic, hit its switch — that's
> what it's for."

Per PRD P-6 the switch must be real: wired so it zeroes that zone's ring
buffer, not decorative.


## When phones are microphones

A party using `presets/party.yaml` takes its audio from guests' own devices.
The posted notice needs one more sentence:

> Phones acting as microphones send audio over this building's wifi to the
> machine running Egregore, where it is turned into text and then discarded.
> Audio is only sent while someone is speaking. Nothing is recorded, nothing
> is kept, and nothing leaves the building.

That is a stronger claim than the software can make on its own, so it is worth
checking before a party that `budget.total_usd` is `0` — which makes any cloud
backend structurally unreachable — or that the operator understands an
abstracted prompt will cross the network if it is not.
