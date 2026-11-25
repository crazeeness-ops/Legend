import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples"))

from generate_sample import build  # noqa: E402

from highlight_engine.heuristic import HeuristicSelector  # noqa: E402
from highlight_engine.llm import ClaudeSelector, chunk_sentences, parse_candidates  # noqa: E402
from highlight_engine.models import Candidate, Sentence, Transcript, Word  # noqa: E402
from highlight_engine.pipeline import EngineConfig, run  # noqa: E402
from highlight_engine.postprocess import finalize, fit_bounds  # noqa: E402
from highlight_engine.segment import split_sentences  # noqa: E402
from highlight_engine.signals import annotate_sentences  # noqa: E402


def make_sentences(specs):
    """specs: list of (duration, text); 0.5s gaps."""
    out, t = [], 0.0
    for i, (d, text) in enumerate(specs):
        out.append(Sentence(id=i, start=t, end=t + d, text=text, gap_before=0.5, gap_after=0.5))
        t += d + 0.5
    return out


# ---------- segmentation ----------

def test_split_on_punctuation_and_speaker():
    words = [
        Word("안녕하세요.", 0.0, 0.8, "A"),
        Word("반갑습니다", 1.0, 1.6, "A"),
        Word("네", 1.7, 1.9, "B"),
        Word("좋아요!", 2.0, 2.5, "B"),
    ]
    s = split_sentences(words)
    assert [x.text for x in s] == ["안녕하세요.", "반갑습니다", "네 좋아요!"]
    assert s[0].terminal and not s[1].terminal and s[2].terminal
    assert s[1].gap_after == pytest.approx(0.1)


def test_split_long_runon_by_max_len():
    words = [Word("말", i * 0.5, i * 0.5 + 0.4) for i in range(100)]
    s = split_sentences(words, max_len=10)
    assert all(x.duration <= 10.5 for x in s)
    assert len(s) >= 4


# ---------- boundary fitting ----------

def test_fit_extends_to_complete_thought():
    s = make_sentences([(10, "질문이 있어요?"), (10, "그게 말이죠"), (10, "결론은 이겁니다."), (10, "끝.")])
    # proposal ends on a non-terminal sentence -> should extend to S2
    assert fit_bounds(0, 1, s, 15, 40) == (0, 2)


def test_fit_grows_short_and_trims_long():
    s = make_sentences([(5, "a."), (5, "b."), (5, "c."), (5, "d."), (5, "e.")])
    a, b = fit_bounds(1, 1, s, 12, 20)
    assert s[b].end - s[a].start >= 12 and a == 1  # grows end first, keeps hook
    a, b = fit_bounds(0, 4, s, 5, 12)
    assert (a, b) == (0, 1)


def test_fit_impossible_returns_none():
    s = make_sentences([(50, "very long.")])
    assert fit_bounds(0, 0, s, 15, 30) is None


# ---------- finalize ----------

def test_finalize_dedupes_and_ranks():
    s = make_sentences([(10, f"문장 {i}.") for i in range(12)])
    hi = {"hook": 9, "completeness": 9, "emotion": 8, "value": 9, "trend": 7}
    lo = {"hook": 4, "completeness": 5, "emotion": 4, "value": 4, "trend": 4}
    cands = [
        Candidate(0, 2, "best", "h", "r", hi),
        Candidate(1, 3, "overlap", "h", "r", lo),  # heavy overlap with best
        Candidate(6, 8, "other", "h", "r", lo),
    ]
    clips = finalize(cands, s, min_len=20, max_len=40, top_n=5)
    assert [c.title for c in clips] == ["best", "other"]
    assert clips[0].rank == 1 and clips[0].score > clips[1].score
    assert 0 <= clips[1].score <= 99
    # padded into the silence but never into the neighbouring sentence
    assert s[0].start <= clips[0].start + 0.2 and clips[0].end <= s[3].start


def test_dangling_start_is_penalised():
    s = make_sentences([(10, "그래서 결론은요."), (10, "이거예요."), (10, "핵심은 이겁니다."), (10, "끝이에요.")])
    sc = {"hook": 7, "completeness": 8, "emotion": 6, "value": 7, "trend": 5}
    a, b = finalize([Candidate(0, 1, "a", "", "", sc), Candidate(2, 3, "b", "", "", sc)], s,
                    min_len=15, max_len=30, top_n=2)
    assert a.title == "b" and b.score_detail["completeness"] == 6


# ---------- LLM selector (mocked client) ----------

class FakeMessages:
    def __init__(self, payload, stop_reason="end_turn"):
        self.payload, self.stop_reason, self.calls = payload, stop_reason, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            stop_reason=self.stop_reason,
            content=[SimpleNamespace(type="text", text=json.dumps(self.payload))],
        )


def fake_client(payload, stop_reason="end_turn"):
    msgs = FakeMessages(payload, stop_reason)
    return SimpleNamespace(beta=SimpleNamespace(messages=msgs)), msgs


CLIP = {"start_sentence": 6, "end_sentence": 13, "title": "부업 월 100만 원의 진실", "hook": "진짜 가능한가요?",
        "reason": "질문 훅 + 숫자 + 명확한 결론", "keywords": ["100만 원", "포기"],
        "hook_score": 9, "completeness_score": 8, "emotion_score": 6, "value_score": 9, "trend_score": 8}


def test_claude_selector_request_and_parse():
    sentences = split_sentences(build().words)
    client, msgs = fake_client({"clips": [CLIP, {**CLIP, "start_sentence": 999, "end_sentence": 1000}]})
    sel = ClaudeSelector(client=client)
    cands = sel.select(sentences, min_len=20, max_len=60, target_count=3, user_prompt="부업 수익")
    assert len(cands) == 1 and cands[0].start_sid == 6 and cands[0].scores["hook"] == 9
    req = msgs.calls[0]
    assert req["model"] == "claude-opus-5-5"
    assert req["fallbacks"] == "default"
    assert req["output_config"]["format"]["type"] == "json_schema"
    assert "[S6]" in req["messages"][0]["content"] and "부업 수익" in req["messages"][0]["content"]


def test_claude_selector_refusal_returns_empty():
    client, _ = fake_client({"clips": [CLIP]}, stop_reason="refusal")
    sentences = split_sentences(build().words)
    assert ClaudeSelector(client=client).select(sentences, min_len=20, max_len=60, target_count=3) == []


def test_parse_clamps_scores_and_swaps_range():
    s = make_sentences([(5, "a.")] * 5)
    c = parse_candidates({"clips": [{**CLIP, "start_sentence": 3, "end_sentence": 1, "hook_score": 15}]}, s)
    assert (c[0].start_sid, c[0].end_sid) == (1, 3) and c[0].scores["hook"] == 10


def test_chunking_covers_everything_with_overlap():
    s = make_sentences([(10, f"s{i}.") for i in range(300)])  # ~52 min
    chunks = chunk_sentences(s, chunk_seconds=1200, overlap_seconds=90)
    ids = {x.id for c in chunks for x in c}
    assert ids == set(range(300)) and len(chunks) >= 3
    assert chunks[1][0].id < chunks[0][-1].id  # overlaps


# ---------- signals + end to end ----------

def test_energy_annotation():
    s = make_sentences([(2, "a."), (2, "b.")])
    rms = np.array([-40] * 4 + [-10] * 6, dtype=np.float32)  # second sentence louder
    annotate_sentences(s, rms, window=0.5)
    assert s[1].energy_z > s[0].energy_z


def test_pipeline_end_to_end_heuristic(tmp_path):
    path = tmp_path / "t.json"
    build().save(path)
    result = run(str(path), HeuristicSelector(), EngineConfig(min_len=15, max_len=40, top_n=3))
    assert result["clips"], result
    for c in result["clips"]:
        assert 15 <= c["end"] - c["start"] <= 40 + 0.7
        assert c["text"].rstrip()[-1] in ".?!"


@pytest.mark.skipif(subprocess.run(["which", "ffmpeg"], capture_output=True).returncode, reason="no ffmpeg")
def test_pipeline_with_audio_and_cli(tmp_path):
    t = build()
    tpath = tmp_path / "t.json"
    t.save(tpath)
    wav = tmp_path / "a.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency=220:duration={t.duration}",
                    "-af", "volume='if(between(t,40,80),1,0.05)':eval=frame", str(wav)], check=True)
    out = tmp_path / "out.json"
    proc = subprocess.run(
        [sys.executable, "-m", "highlight_engine", str(tpath), "--audio", str(wav), "--selector", "heuristic",
         "--length", "15-40", "--top", "3", "--out", str(out), "--export-dir", str(tmp_path / "clips")],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(out.read_text())
    assert any(c["score_detail"]["signal"] != 50 for c in data["clips"])
    assert len(list((tmp_path / "clips").glob("*.mp4"))) == len(data["clips"])


def test_transcribe_feeds_whisper_ffmpeg_decoded_samples(tmp_path, monkeypatch):
    """faster-whisper's own decoder breaks with newer PyAV; we must pass a numpy array."""
    import types

    from highlight_engine import transcribe as tr

    wav = tmp_path / "a.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=300:duration=2", str(wav)], check=True)
    seen = {}

    class FakeModel:
        def __init__(self, *a, **k):
            seen["threads"] = k.get("cpu_threads")

        def transcribe(self, audio, **kw):
            seen["audio"] = audio
            word = types.SimpleNamespace(word=" 안녕", start=0.1, end=0.5, probability=0.9)
            return [types.SimpleNamespace(words=[word])], types.SimpleNamespace(language="ko", duration=2.0)

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=FakeModel))
    updates = []
    t = tr.transcribe(str(wav), model_size="small", progress=lambda f, m: updates.append((f, m)))
    assert isinstance(seen["audio"], np.ndarray) and seen["audio"].dtype == np.float32
    assert seen["threads"] >= 1 and updates[0][1].startswith("음성 인식 준비") and "%" in updates[-1][1]
    assert abs(len(seen["audio"]) - 2 * tr.SAMPLE_RATE) < 400
    assert t.words[0].text == "안녕" and t.language == "ko"


def test_transcribe_falls_back_to_cpu_when_cuda_libs_are_missing(tmp_path, monkeypatch):
    import types

    from highlight_engine import transcribe as tr

    wav = tmp_path / "a.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=300:duration=1", str(wav)], check=True)
    devices = []

    class FakeModel:
        def __init__(self, size, device, compute_type, **kw):
            devices.append((device, compute_type))
            self.device = device

        def transcribe(self, audio, **kw):
            def gen():
                if self.device != "cpu":  # like CTranslate2: fails lazily once decoding starts
                    raise RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")
                yield types.SimpleNamespace(words=[types.SimpleNamespace(word="네", start=0.1, end=0.3, probability=1.0)])
            return gen(), types.SimpleNamespace(language="ko", duration=1.0)

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=FakeModel))
    monkeypatch.delenv("LEGEND_WHISPER_DEVICE", raising=False)
    t = tr.transcribe(str(wav), model_size="small")
    assert devices == [("auto", "auto"), ("cpu", "int8")] and t.words[0].text == "네"

    class Broken(FakeModel):
        def transcribe(self, audio, **kw):
            raise RuntimeError("model file is corrupted")

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=Broken))
    with pytest.raises(RuntimeError, match="corrupted"):  # unrelated errors are not hidden
        tr.transcribe(str(wav), model_size="small")
