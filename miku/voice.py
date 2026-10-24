from __future__ import annotations

import array
import json
import re
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True, slots=True)
class VoiceCommand:
    action: str
    query: str | None = None


class VoiceCommandParser:
    """Parse only explicit wake-word-prefixed supported commands."""

    def __init__(self, wake_word: str = "miku"):
        self.wake_word = wake_word.casefold()

    @staticmethod
    def _clean(text: str) -> str:
        return " ".join(re.sub(r"[^\w\s]", " ", text.casefold()).split())

    def parse(self, text: str) -> VoiceCommand | None:
        words = self._clean(text).split()
        if len(words) < 2 or words[0] != self.wake_word:
            return None
        command = words[1:]
        rest = " ".join(command)
        if rest in {"pause", "ruk jao", "ruko"}:
            return VoiceCommand("pause")
        if rest in {"resume", "continue", "chalao"}:
            return VoiceCommand("resume")
        if rest in {"skip", "next", "agla", "agla gana"}:
            return VoiceCommand("skip")
        if rest in {"queue", "show queue", "queue dikhao"}:
            return VoiceCommand("queue")
        if rest in {"leave", "disconnect", "bahar aao"}:
            return VoiceCommand("leave")
        for prefix in ("search ", "play "):
            if rest.startswith(prefix):
                query = rest[len(prefix):].strip()
                if prefix == "search " and query.endswith(" again"):
                    query = query[:-6].strip()
                if not query or query == "again":
                    return None
                if prefix == "play " and re.fullmatch(
                    r"(?:the )?(?:first|second|third|last|\d+(?:st|nd|rd|th)?) result",
                    query,
                ):
                    return None
                return VoiceCommand("search" if prefix == "search " else "play", query=query)
        return None


class WakeWordGate:
    def __init__(self, wake_word: str = "miku", confidence_threshold: float = 0.75,
                 cooldown_seconds: float = 2.0):
        self.wake_word = wake_word.casefold()
        self.threshold = confidence_threshold
        self.cooldown = cooldown_seconds
        self.last_trigger = float("-inf")

    def accept(self, text: str, confidence: float, now: float | None = None) -> bool:
        instant = time.monotonic() if now is None else now
        match = re.search(rf"\b{re.escape(self.wake_word)}\b", text.casefold())
        if match and confidence >= self.threshold and instant - self.last_trigger >= self.cooldown:
            self.last_trigger = instant
            return True
        return False


def pcm48_stereo_to_16k_mono(pcm: bytes) -> bytes:
    """Convert Discord's signed 16-bit 48 kHz stereo PCM frames to 16 kHz mono."""
    usable = len(pcm) - (len(pcm) % 4)
    if usable < 4:
        return b""
    samples = array.array("h")
    samples.frombytes(pcm[:usable])
    if samples.itemsize != 2:
        return b""
    mono = array.array("h", ((samples[i] + samples[i + 1]) // 2
                              for i in range(0, len(samples) - 1, 2)))
    frames = len(mono) - (len(mono) % 3)
    downsampled = array.array("h", (sum(mono[i:i + 3]) // 3 for i in range(0, frames, 3)))
    return downsampled.tobytes()


def pcm_rms(pcm: bytes) -> float:
    samples = array.array("h")
    usable = len(pcm) - (len(pcm) % 2)
    samples.frombytes(pcm[:usable])
    if not samples:
        return 0.0
    return (sum(sample * sample for sample in samples) / len(samples)) ** 0.5


class _SpeakerState:
    def __init__(self, wake_recognizer, command_recognizer_factory):
        self.wake_recognizer = wake_recognizer
        self.command_recognizer_factory = command_recognizer_factory
        self.ring: deque[bytes] = deque(maxlen=100)  # at most 2 seconds
        self.command_recognizer = None
        self.capture = bytearray()
        self.capture_frames = 0
        self.silent_frames = 0


class VoskAudioPipeline:
    """Wake-word-only recognition while idle, followed by short local command STT."""

    def __init__(self, model_path: str | Path, on_command: Callable[[int, str], None],
                 *, wake_word: str = "miku", wake_confidence: float = 0.75,
                 vad_rms_threshold: float = 250, max_capture_frames: int = 250,
                 silence_timeout_seconds: float = 0.6, cooldown_seconds: float = 2.0):
        try:
            from vosk import KaldiRecognizer, Model, SetLogLevel
        except ImportError as exc:
            raise RuntimeError("Voice commands need the optional 'vosk' package.") from exc
        if not Path(model_path).exists():
            raise RuntimeError("VOSK_MODEL_PATH does not point to an installed speech model.")
        SetLogLevel(-1)
        self._model = Model(str(model_path))
        self._recognizer_type = KaldiRecognizer
        self._on_command = on_command
        self.wake_word = wake_word
        self.wake_confidence = wake_confidence
        self.vad_threshold = vad_rms_threshold
        self.max_capture_frames = max_capture_frames
        self.silence_frames_to_end = max(1, int(silence_timeout_seconds * 50))
        self.cooldown_seconds = cooldown_seconds
        self._speakers: dict[int, _SpeakerState] = {}
        self._gates: dict[int, WakeWordGate] = {}

    def _new_state(self) -> _SpeakerState:
        grammar = json.dumps([self.wake_word, "[unk]"])
        wake = self._recognizer_type(self._model, 16000, grammar)
        wake.SetWords(True)
        return _SpeakerState(wake, lambda: self._recognizer_type(self._model, 16000))

    @staticmethod
    def _wake_match(result_json: str, wake_word: str) -> tuple[str, float]:
        try:
            result = json.loads(result_json)
            text = result.get("text", "")
            confidence = max((float(item.get("conf", 0)) for item in result.get("result", [])
                              if item.get("word", "").casefold() == wake_word.casefold()), default=0)
            return text, confidence
        except (TypeError, ValueError, json.JSONDecodeError):
            return "", 0.0

    def feed(self, speaker_id: int, pcm_48k_stereo: bytes) -> None:
        pcm = pcm48_stereo_to_16k_mono(pcm_48k_stereo)
        if not pcm:
            return
        state = self._speakers.get(speaker_id)
        if state is None:
            state = self._new_state()
            self._speakers[speaker_id] = state
        if state.command_recognizer is not None:
            state.capture.extend(pcm)
            state.capture_frames += 1
            state.silent_frames = state.silent_frames + 1 if pcm_rms(pcm) < self.vad_threshold else 0
            state.command_recognizer.AcceptWaveform(pcm)
            if (state.silent_frames >= self.silence_frames_to_end
                    or state.capture_frames >= self.max_capture_frames):
                self._finish_capture(speaker_id, state)
            return
        if pcm_rms(pcm) < self.vad_threshold:
            return
        state.ring.append(pcm)
        if state.wake_recognizer.AcceptWaveform(pcm):
            text, confidence = self._wake_match(state.wake_recognizer.Result(), self.wake_word)
            gate = self._gates.setdefault(
                speaker_id,
                WakeWordGate(self.wake_word, self.wake_confidence, self.cooldown_seconds),
            )
            if gate.accept(text, confidence):
                state.command_recognizer = state.command_recognizer_factory()
                state.command_recognizer.SetWords(True)
                state.capture = bytearray(b"".join(state.ring))
                state.capture_frames = len(state.ring)
                state.silent_frames = 0
                state.command_recognizer.AcceptWaveform(bytes(state.capture))
            state.wake_recognizer = self._new_state().wake_recognizer

    def _finish_capture(self, speaker_id: int, state: _SpeakerState) -> None:
        recognizer = state.command_recognizer
        try:
            final = recognizer.FinalResult() if recognizer else "{}"
            text = json.loads(final).get("text", "").strip()
            if text:
                self._on_command(speaker_id, text)
        except (ValueError, json.JSONDecodeError):
            pass
        state.command_recognizer = None
        state.capture.clear()
        state.capture_frames = 0
        state.silent_frames = 0
        state.ring.clear()
        state.wake_recognizer = self._new_state().wake_recognizer


def make_voice_sink(on_frame: Callable[[int, bytes], None]):
    """Create the optional discord-ext-voice-recv AudioSink without hard dependency."""
    try:
        from discord.ext import voice_recv
    except ImportError as exc:
        raise RuntimeError(
            "Voice receive needs the optional discord-ext-voice-recv package."
        ) from exc

    class MikuSink(voice_recv.AudioSink):
        def __init__(self):
            super().__init__()

        def wants_opus(self) -> bool:
            return False

        def write(self, user, data) -> None:
            if user is not None and data.pcm:
                on_frame(int(user.id), data.pcm)

        def cleanup(self) -> None:
            return None

    return MikuSink()
