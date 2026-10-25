"""Whisper accuracy A/B bench. Usage: bench_whisper.py AUDIO OUTDIR"""
import json, sys, time
from pathlib import Path
from faster_whisper import WhisperModel

audio, out = sys.argv[1], Path(sys.argv[2])
out.mkdir(parents=True, exist_ok=True)
GOOD = ["診察券", "保険金請求", "身分証", "書類", "皮弁", "届いて"]
BAD = ["新冊券", "保険寄給", "見分証"]
PROMPT = "診察券、保険金請求、身分証、書類、皮弁、届いて。"
RUNS = [
    ("turbo_b1", "large-v3-turbo", 1, None),
    ("turbo_b5", "large-v3-turbo", 5, None),
    ("kotoba_b5", "kotoba-tech/kotoba-whisper-v2.0-faster", 5, None),
    ("medium_b5", "medium", 5, None),
    ("turbo_b5_prompt", "large-v3-turbo", 5, PROMPT),
]
for name, model, beam, prompt in RUNS:
    try:
        t0 = time.time()
        m = WhisperModel(model, device="cpu", compute_type="int8", cpu_threads=4)
        segs, _ = m.transcribe(audio, language="ja", beam_size=beam, vad_filter=True,
                               condition_on_previous_text=False, initial_prompt=prompt)
        text = "".join(s.text for s in segs)
        rec = {"name": name, "sec": round(time.time() - t0),
               "good": {w: text.count(w) for w in GOOD}, "bad": {w: text.count(w) for w in BAD}}
        (out / f"{name}.txt").write_text(text)
    except Exception as e:
        rec = {"name": name, "error": repr(e)}
    with open(out / "bench.log", "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
