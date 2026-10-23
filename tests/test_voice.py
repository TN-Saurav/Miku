from array import array

import pytest

from miku.voice import VoiceCommand, VoiceCommandParser, WakeWordGate, pcm48_stereo_to_16k_mono


def test_voice_parser_accepts_only_supported_wake_prefixed_commands():
    parser = VoiceCommandParser("miku")
    assert parser.parse("Miku, play Kalyani") == VoiceCommand("play", "kalyani")
    assert parser.parse("Miku, search Kalyani again").action == "search"
    assert parser.parse("Miku, change Kalyani") is None
    assert parser.parse("Miku, choose another Kalyani") is None
    assert parser.parse("Miku, play the second result") is None
    assert parser.parse("Miku, pause").action == "pause"
    assert parser.parse("Miku, resume").action == "resume"
    assert parser.parse("Miku, skip").action == "skip"
    assert parser.parse("Miku, queue").action == "queue"
    assert parser.parse("Miku, leave").action == "leave"


def test_ordinary_conversation_and_unsupported_voice_phrases_are_silent():
    parser = VoiceCommandParser("miku")
    assert parser.parse("Can you play Kalyani") is None
    assert parser.parse("Miku, what is the weather") is None
    assert parser.parse("") is None


def test_wake_word_gate_uses_confidence_and_cooldown():
    gate = WakeWordGate("miku", confidence_threshold=0.8, cooldown_seconds=2)
    assert gate.accept("miku play", 0.79, now=10) is False
    assert gate.accept("miku play", 0.9, now=10) is True
    assert gate.accept("miku pause", 0.99, now=11) is False
    assert gate.accept("ordinary speech", 0.99, now=13) is False
    assert gate.accept("miku skip", 0.99, now=13) is True


def test_pcm_downsample_produces_short_mono_buffer():
    stereo = array("h", [100, -100, 200, -200, 300, -300, 400, -400, 500, -500, 600, -600])
    result = pcm48_stereo_to_16k_mono(stereo.tobytes())
    assert len(result) == 4  # two 16-bit mono samples from six 48 kHz frames


def test_optional_discord_voice_receiver_sink_constructs_when_installed():
    pytest.importorskip("discord.ext.voice_recv")
    from miku.voice import make_voice_sink

    sink = make_voice_sink(lambda speaker_id, pcm: None)
    assert sink.__class__.__name__ == "MikuSink"
    sink.cleanup()
