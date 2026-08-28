"""Transcriber engines (Architecture §2.2).

Each engine implements ``egregore.types.Transcriber``: given a chunk of PCM it
returns a text fragment or ``None``. Engines hold no history — the only place
transcript text accumulates is the zone's :class:`~egregore.scribe.ring.RingBuffer`.

The heavy models (NeMo/Parakeet, faster-whisper) are optional installs: they
are imported lazily inside ``__init__`` and raise ``RuntimeError`` with an
install hint when absent, so the core install works on a laptop with no GPU.

No engine here ever logs the text it produced — only lengths and durations.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import wave

from egregore.types import Transcriber

logger = logging.getLogger(__name__)

__all__ = [
    "FixtureTranscriber",
    "ParakeetTranscriber",
    "FasterWhisperTranscriber",
    "make_transcriber",
]

# Engine names as spelled in AsrConfig.engine.
ENGINES = ("parakeet", "faster-whisper", "fixture")

#: Where ParakeetTranscriber looks for a local ONNX export when
#: ``EGREGORE_PARAKEET_ONNX_DIR`` is unset. Overridable per deployment.
_DEFAULT_ONNX_DIR = os.path.expanduser("~/.egregore/models/parakeet-v2-int8")

#: onnx-asr's name for the Parakeet TDT transducer topology.
_ONNX_MODEL_TYPE = "nemo-conformer-tdt"


class FixtureTranscriber:
    """Demo-mode engine. **Not audio-driven.**

    The fixture mic source (``mic.type: fixture``) replays a scripted
    conversation file and hands each line to the scribe as UTF-8 *text* encoded
    to bytes, occupying the same ``bytes`` slot real PCM would. So this engine
    does not decode audio at all: it decodes UTF-8 and hands the line back.
    That keeps the demo path structurally identical to the live path — same
    ring buffer, same weaver, same validator — with the ASR's brain swapped
    out, and it needs no model, no GPU and no audio device in CI.

    ``sample_rate`` is accepted for protocol compatibility and ignored.
    """

    name = "fixture"

    def __init__(self, language: str = "en") -> None:
        self.language = language

    async def transcribe(self, pcm: bytes, sample_rate: int) -> str | None:
        try:
            text = bytes(pcm).decode("utf-8", errors="replace").strip()
        except Exception:  # pragma: no cover - decode with errors="replace" cannot raise
            return None
        return text or None


class ParakeetTranscriber:
    """NVIDIA Parakeet TDT via NeMo — the recommended live engine.

    Chosen because its transducer emits blank tokens on silence instead of
    looping the last phrase, which matters enormously for an always-on mic in
    a room full of music (Architecture §2.2).
    """

    name = "parakeet"

    #: "onnx" (local int8 export) or "nemo" (the CUDA-first reference stack).
    backend = "nemo"
    _onnx = None
    _model = None

    def __init__(
        self,
        language: str = "en",
        model_name: str = "nvidia/parakeet-tdt-0.6b-v3",
        device: str | None = None,
        onnx_dir: str | None = None,
    ) -> None:
        self.language = language
        self.model_name = model_name
        self._lock = asyncio.Lock()

        # Prefer a local ONNX export when one is present. NeMo is a CUDA-first
        # stack that installs poorly on Apple Silicon, while the same Parakeet
        # TDT weights exported to int8 ONNX run on CoreML/CPU at many times
        # real time. Same model, same transducer, far cheaper to deploy.
        onnx_dir = onnx_dir or os.environ.get("EGREGORE_PARAKEET_ONNX_DIR") or _DEFAULT_ONNX_DIR
        onnx_error: Exception | None = None
        if onnx_dir and os.path.isdir(onnx_dir):
            try:
                import onnx_asr  # type: ignore[import-not-found]

                self._onnx = onnx_asr.load_model(
                    _ONNX_MODEL_TYPE, onnx_dir, quantization="int8"
                )
                self.backend = "onnx"
                self.model_path = onnx_dir
                logger.info("parakeet: onnx backend from %s", onnx_dir)
                return
            except Exception as exc:  # fall through to NeMo
                onnx_error = exc
                logger.warning("parakeet: onnx backend unavailable (%s)", type(exc).__name__)

        try:
            from nemo.collections.asr.models import ASRModel  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - exercised only without nemo
            raise RuntimeError(
                "Parakeet ASR needs either a local ONNX export or NeMo, and found "
                "neither. Point EGREGORE_PARAKEET_ONNX_DIR at a Parakeet TDT ONNX "
                "directory (encoder-model.int8.onnx, decoder_joint-model.int8.onnx, "
                "nemo128.onnx, vocab.txt, config.json) and `pip install onnx-asr "
                "onnxruntime`, or install NeMo with: pip install 'nemo_toolkit[asr]' "
                "(or set asr.engine to 'faster-whisper' or 'fixture')."
                + (f" ONNX load failed with: {onnx_error!r}" if onnx_error else "")
            ) from exc

        self._model = ASRModel.from_pretrained(model_name=model_name)
        self._model.eval()
        if device:
            self._model = self._model.to(device)
        self.backend = "nemo"
        self.model_path = model_name

    async def transcribe(self, pcm: bytes, sample_rate: int) -> str | None:
        if not pcm:
            return None
        # NeMo's decoder is not re-entrant; serialize per engine instance.
        async with self._lock:
            text = await asyncio.to_thread(self._transcribe_sync, pcm, sample_rate)
        if text:
            logger.debug("parakeet: produced %d chars", len(text))
        return text

    def _transcribe_sync(self, pcm: bytes, sample_rate: int) -> str | None:
        path = _write_temp_wav(pcm, sample_rate)
        try:
            if self.backend == "onnx":
                text = self._onnx.recognize(path)
                return (text or "").strip() or None
            results = self._model.transcribe([path], batch_size=1, verbose=False)
        finally:
            _unlink(path)
        return _first_text(results)


class FasterWhisperTranscriber:
    """faster-whisper (CTranslate2) — the multilingual fallback.

    Whisper covers ~99 languages against Parakeet's ~25, at the cost of
    hallucinating on silence; the caller is expected to VAD-gate aggressively,
    and ``condition_on_previous_text`` is off here for the same reason.
    """

    name = "faster-whisper"

    def __init__(
        self,
        language: str = "en",
        model_name: str = "large-v3-turbo",
        device: str = "auto",
        compute_type: str = "int8",
    ) -> None:
        try:
            from faster_whisper import WhisperModel  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - exercised only without the dep
            raise RuntimeError(
                "faster-whisper ASR requires the faster-whisper package, which is not "
                "installed. Install it with: pip install 'egregore[asr]' "
                "(or set asr.engine to 'fixture')"
            ) from exc

        self.language = language
        self.model_name = model_name
        self._model = WhisperModel(model_name, device=device, compute_type=compute_type)
        self._lock = asyncio.Lock()

    async def transcribe(self, pcm: bytes, sample_rate: int) -> str | None:
        if not pcm:
            return None
        async with self._lock:
            text = await asyncio.to_thread(self._transcribe_sync, pcm, sample_rate)
        if text:
            logger.debug("faster-whisper: produced %d chars", len(text))
        return text

    def _transcribe_sync(self, pcm: bytes, sample_rate: int) -> str | None:
        import numpy as np

        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        if sample_rate != 16000:
            # faster-whisper resamples internally only for file input; for raw
            # arrays it expects 16 kHz, so do a cheap linear resample here.
            audio = _resample_linear(audio, sample_rate, 16000)
        segments, _info = self._model.transcribe(
            audio,
            language=self.language or None,
            beam_size=1,
            condition_on_previous_text=False,
            vad_filter=True,
        )
        text = " ".join(seg.text.strip() for seg in segments).strip()
        return text or None


def make_transcriber(engine: str, language: str = "en") -> Transcriber:
    """Build the engine named by ``AsrConfig.engine``.

    Raises ``ValueError`` for an unknown name and ``RuntimeError`` (from the
    engine's own constructor) when an optional dependency is missing.
    """
    key = engine.strip().lower()
    if key == "fixture":
        return FixtureTranscriber(language=language)
    if key == "parakeet":
        return ParakeetTranscriber(language=language)
    if key in ("faster-whisper", "faster_whisper"):
        return FasterWhisperTranscriber(language=language)
    raise ValueError(f"unknown asr engine {engine!r}; expected one of {', '.join(ENGINES)}")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _write_temp_wav(pcm: bytes, sample_rate: int, channels: int = 1) -> str:
    """Write 16-bit PCM to a temp wav file and return its path."""
    fd, path = tempfile.mkstemp(suffix=".wav", prefix="egregore-asr-")
    os.close(fd)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return path


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:  # pragma: no cover - best effort
        logger.debug("could not remove temp audio file")


def _first_text(results: object) -> str | None:
    """Pull the text out of NeMo's result list across its API versions.

    Older NeMo returns ``list[str]``; newer returns objects with a ``.text``.
    """
    if not results:
        return None
    try:
        item = results[0]  # type: ignore[index]
    except (TypeError, IndexError, KeyError):  # pragma: no cover - defensive
        return None
    if isinstance(item, list):  # some versions return (hypotheses, all_hyps)
        item = item[0] if item else None
    text = getattr(item, "text", item)
    if not isinstance(text, str):  # pragma: no cover - defensive
        return None
    text = text.strip()
    return text or None


def _resample_linear(audio: object, src_rate: int, dst_rate: int) -> object:
    import numpy as np

    arr = np.asarray(audio)
    if src_rate == dst_rate or arr.size == 0:
        return arr
    n_out = max(1, int(round(arr.size * dst_rate / src_rate)))
    idx = np.linspace(0.0, arr.size - 1, n_out, dtype=np.float64)
    return np.interp(idx, np.arange(arr.size), arr).astype(np.float32)
