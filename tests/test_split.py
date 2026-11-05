from voice_memory.ingest.split import SplitConfig, split_transcript
from voice_memory.ingest.transcribe import TimedTranscript

CFG = SplitConfig()


def talk(start, end, step=10.0, chars=60, tag="x"):
    """Continuous speech from start to end: one speech span and one segment per `step` seconds."""
    segs, t = [], start
    while t < end:
        segs.append((t, min(t + step, end), (tag + str(int(t))).ljust(chars, "あ")))
        t += step
    return segs, [(start, end)]


def build(*blocks):
    segs, speech = [], []
    for s, sp in blocks:
        segs += s
        speech += sp
    return TimedTranscript(segs, speech)


def test_splits_on_long_gap():
    t = build(talk(0, 300, tag="a"), talk(420, 800, tag="b"))
    chunks = split_transcript(t, CFG)
    assert [(c.start, c.end) for c in chunks] == [(0, 300), (420, 800)]
    assert chunks[0].text.startswith("a0") and chunks[1].text.startswith("b420")


def test_gap_below_threshold_does_not_split():
    t = build(talk(0, 300), talk(340, 600))  # 40 s < 45 s
    assert len(split_transcript(t, CFG)) == 1


def test_short_fragment_merges_into_previous():
    t = build(talk(0, 300, tag="a"), talk(400, 410, tag="s", chars=10), talk(600, 900, tag="c"))
    chunks = split_transcript(t, CFG)
    assert [(c.start, c.end) for c in chunks] == [(0, 410), (600, 900)]
    assert "s400" in chunks[0].text


def test_few_characters_merge_even_when_long_enough():
    t = build(talk(0, 300, tag="a"), talk(400, 460, step=60, chars=20, tag="h"))
    chunks = split_transcript(t, CFG)
    assert len(chunks) == 1 and chunks[0].end == 460


def test_leading_short_fragment_merges_into_next():
    t = build(talk(0, 10, tag="s", chars=10), talk(200, 500, tag="b"))
    chunks = split_transcript(t, CFG)
    assert [(c.start, c.end) for c in chunks] == [(0, 500)]
    assert chunks[0].text.startswith("s0")


def test_trailing_short_fragment_merges_into_previous():
    t = build(talk(0, 300, tag="a"), talk(500, 515, tag="z", chars=10))
    chunks = split_transcript(t, CFG)
    assert [(c.start, c.end) for c in chunks] == [(0, 515)]
    assert "z500" in chunks[0].text


def test_forced_split_at_largest_gap_of_3s_or_more():
    # 40 min of 30 s blocks with 1 s pauses, except a 5 s pause after block 19 and a 4 s pause after block 27
    speech, segs, t = [], [], 0.0
    ends = {}
    for i in range(70):
        speech.append((t, t + 30))
        segs.append((t, t + 30, f"s{i}".ljust(60, "あ")))
        ends[i] = t + 30
        t += 30 + (5.0 if i == 19 else 4.0 if i == 27 else 1.0)
    chunks = split_transcript(TimedTranscript(segs, speech), CFG)
    assert chunks[0].end == ends[19]  # the 5 s pause is the largest one inside the first 15 minutes
    assert all(c.duration <= 900 + 60 for c in chunks)
    assert "\n".join(c.text for c in chunks) == "\n".join(s[2] for s in segs)  # nothing lost or duplicated


def test_forced_split_without_3s_gap_uses_boundary_nearest_to_limit():
    speech = [(i * 31.0, i * 31.0 + 30.0) for i in range(80)]  # 1 s pauses only
    segs = [(a, b, f"s{int(a)}".ljust(60, "あ")) for a, b in speech]
    chunks = split_transcript(TimedTranscript(segs, speech), CFG)
    first_end = chunks[0].end
    assert abs(first_end - 900) <= 31  # nearest span boundary to 15 minutes
    assert len(chunks) >= 2


def test_every_segment_lands_in_exactly_one_chunk():
    t = build(talk(0, 200, tag="a"), talk(400, 700, tag="b"), talk(900, 1000, tag="c"))
    chunks = split_transcript(t, CFG)
    joined = "\n".join(c.text for c in chunks)
    assert joined == "\n".join(s[2] for s in t.segments)


def test_long_segment_assigned_to_chunk_with_most_of_its_speech():
    # one 400 s segment starting in silence before block b (Whisper absorbs pauses into segments)
    segs = talk(0, 300, tag="a")[0] + [(300, 700, "long".ljust(60, "あ"))] + talk(700, 900, tag="b")[0]
    speech = [(0, 300), (650, 900)]
    chunks = split_transcript(TimedTranscript(segs, speech), CFG)
    assert len(chunks) == 2 and "long" in chunks[1].text


def test_spans_cut_at_decode_boundary_are_one_span():
    t = build(talk(0, 1800, tag="a"), talk(1800.2, 2400, tag="b"))
    chunks = split_transcript(t, SplitConfig(max_chunk_sec=10_000))
    assert len(chunks) == 1


def test_no_speech_spans_or_no_segments():
    assert split_transcript(TimedTranscript([], [(0, 5)]), CFG) == []
    c = split_transcript(TimedTranscript([(0, 5, "hello")], []), CFG)
    assert len(c) == 1 and c[0].text == "hello"


def test_config_from_env():
    cfg = SplitConfig.from_env({"VOICE_MEMORY_SPLIT_GAP_SEC": "60", "VOICE_MEMORY_SPLIT_MIN_CHUNK_CHARS": "80",
                                "VOICE_MEMORY_SPLIT": "0"})
    assert cfg.gap == 60 and cfg.min_chunk_chars == 80 and not cfg.enabled and cfg.max_chunk_sec == 900
    assert SplitConfig.from_env({}).enabled


def test_timed_transcript_json_roundtrip():
    t = build(talk(0, 20, tag="a"))
    again = TimedTranscript.from_json(t.to_json())
    assert again.segments == t.segments and again.speech == t.speech
