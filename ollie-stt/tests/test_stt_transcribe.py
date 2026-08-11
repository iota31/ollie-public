"""Unit tests for ollie-stt/stt_transcribe.py — faster-whisper MOCKED.

We can't load the real ~464MB WhisperModel in this worktree (no install
allowed), so we monkeypatch the module's `WhisperModel` import away with a
fake that returns a deterministic transcript. The tests cover the runner's
arg handling, output shaping (stdout vs --out file), the lazy model cache,
and the venv re-exec handoff.

Run from the ollie-stt/ dir:
    PYTHONPATH=. python3 -m unittest tests.test_stt_transcribe -v
"""

import importlib
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock


HERE = os.path.dirname(os.path.abspath(__file__))
STT_DIR = os.path.dirname(HERE)


class FakeSegment:
    def __init__(self, text):
        self.text = text


class FakeInfo:
    def __init__(self, language="en", duration=1.5):
        self.language = language
        self.duration = duration


class FakeWhisperModel:
    """Stand-in for faster_whisper.WhisperModel.

    Records the kwargs it was constructed / called with so tests can assert
    on them, and returns a canned (segments, info) pair from transcribe().
    """
    instances = []

    def __init__(self, model_name, device="cpu", compute_type="int8"):
        self.model_name = model_name
        self.device = device
        self.compute_type = compute_type
        self.transcribe_calls = []
        FakeWhisperModel.instances.append(self)

    def transcribe(self, audio_path, **kwargs):
        self.transcribe_calls.append((audio_path, kwargs))
        return (
            [FakeSegment("hello"), FakeSegment("world"), FakeSegment("")],
            FakeInfo(language="en", duration=1.5),
        )


def _import_runner():
    """Import (or reload) stt_transcribe with faster_whisper + sys mocked so
    the test runner never has to actually install faster-whisper."""
    sys.modules.setdefault("faster_whisper", mock.MagicMock(WhisperModel=FakeWhisperModel))
    if "stt_transcribe" in sys.modules:
        del sys.modules["stt_transcribe"]
    sys.path.insert(0, STT_DIR)
    return importlib.import_module("stt_transcribe")


class TestArgHandling(unittest.TestCase):
    def setUp(self):
        FakeWhisperModel.instances = []
        self.mod = _import_runner()
        self.tmpdir = tempfile.mkdtemp()
        self.audio = os.path.join(self.tmpdir, "voice.ogg")
        with open(self.audio, "wb") as f:
            f.write(b"fake-audio-bytes")

    def _run(self, *argv):
        return self.mod.main.__wrapped__ if hasattr(self.mod.main, "__wrapped__") else None

    def test_default_beam_and_model(self):
        with mock.patch.object(sys, "argv", ["stt_transcribe.py", "--in", self.audio]):
            with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
                rc = self.mod.main()
        self.assertEqual(rc, 0)
        # Stdout is the joined transcript.
        self.assertEqual(out.getvalue().strip(), "hello world")
        # Stderr carries the metadata line.
        self.assertIn("lang=en", err.getvalue())
        self.assertIn("model=small", err.getvalue())
        self.assertIn("beam=5", err.getvalue())
        # WhisperModel constructed with the lab-decided defaults.
        self.assertEqual(len(FakeWhisperModel.instances), 1)
        inst = FakeWhisperModel.instances[0]
        self.assertEqual(inst.model_name, "small")
        self.assertEqual(inst.device, "cpu")
        self.assertEqual(inst.compute_type, "int8")
        # transcribe got beam_size=5 and vad_filter=True by default.
        path, kwargs = inst.transcribe_calls[0]
        self.assertEqual(path, self.audio)
        self.assertEqual(kwargs["beam_size"], 5)
        self.assertTrue(kwargs["vad_filter"])
        self.assertIsNone(kwargs["language"])

    def test_custom_beam_language_and_out(self):
        out_path = os.path.join(self.tmpdir, "transcript.txt")
        with mock.patch.object(sys, "argv", [
            "stt_transcribe.py", "--in", self.audio,
            "--out", out_path,
            "--beam-size", "1", "--language", "en",
            "--no-vad", "--model", "tiny.en",
        ]):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                rc = self.mod.main()
        self.assertEqual(rc, 0)
        with open(out_path) as f:
            self.assertEqual(f.read().strip(), "hello world")
        inst = FakeWhisperModel.instances[0]
        self.assertEqual(inst.model_name, "tiny.en")
        _, kwargs = inst.transcribe_calls[0]
        self.assertEqual(kwargs["beam_size"], 1)
        self.assertEqual(kwargs["language"], "en")
        self.assertFalse(kwargs["vad_filter"])  # --no-vad

    def test_missing_audio_returns_2(self):
        bogus = os.path.join(self.tmpdir, "does-not-exist.ogg")
        with mock.patch.object(sys, "argv", ["stt_transcribe.py", "--in", bogus]):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                rc = self.mod.main()
        self.assertEqual(rc, 2)
        # Nothing constructed when the file's missing.
        self.assertEqual(len(FakeWhisperModel.instances), 0)

    def test_transcribe_failure_returns_3(self):
        class BoomModel(FakeWhisperModel):
            def transcribe(self, audio_path, **kwargs):
                raise RuntimeError("decode exploded")
        FakeWhisperModel.instances = []
        sys.modules["faster_whisper"].WhisperModel = BoomModel
        try:
            if "stt_transcribe" in sys.modules:
                del sys.modules["stt_transcribe"]
            mod = importlib.import_module("stt_transcribe")
            sys.path.insert(0, STT_DIR)
            with mock.patch.object(sys, "argv", ["stt_transcribe.py", "--in", self.audio]):
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    rc = mod.main()
            self.assertEqual(rc, 3)
        finally:
            sys.modules["faster_whisper"].WhisperModel = FakeWhisperModel


class TestModelCache(unittest.TestCase):
    def setUp(self):
        FakeWhisperModel.instances = []
        self.mod = _import_runner()

    def test_get_model_is_cached(self):
        m1 = self.mod.get_model("small", "cpu", "int8")
        m2 = self.mod.get_model("small", "cpu", "int8")
        self.assertIs(m1, m2)
        # Only one WhisperModel ever constructed despite two get_model calls.
        self.assertEqual(len(FakeWhisperModel.instances), 1)

    def test_different_compute_type_is_separate_instance(self):
        m1 = self.mod.get_model("small", "cpu", "int8")
        m2 = self.mod.get_model("small", "cpu", "float32")
        self.assertIsNot(m1, m2)
        self.assertEqual(len(FakeWhisperModel.instances), 2)


class TestTranscribeReturn(unittest.TestCase):
    def setUp(self):
        FakeWhisperModel.instances = []
        self.mod = _import_runner()
        self.tmpdir = tempfile.mkdtemp()
        self.audio = os.path.join(self.tmpdir, "voice.wav")
        with open(self.audio, "wb") as f:
            f.write(b"x")

    def test_text_and_info_shape(self):
        text, info = self.mod.transcribe(self.audio)
        # Empty/whitespace-only segments are dropped, then joined by " ".
        self.assertEqual(text, "hello world")
        self.assertEqual(info["language"], "en")
        self.assertAlmostEqual(info["duration"], 1.5)
        # Only two real segments (the third was empty).
        self.assertEqual(info["segments"], 2)
        self.assertEqual(info["model"], "small")
        self.assertEqual(info["beam_size"], 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
