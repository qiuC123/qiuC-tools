"""Isolated faster-whisper worker used by the versioned Phase 2A workflow."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("audio", nargs="+")
    args = parser.parse_args()
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    from faster_whisper import WhisperModel

    model = WhisperModel(args.model, device="cpu", compute_type="int8")
    for raw_audio in args.audio:
        audio = Path(raw_audio).resolve()
        segments, info = model.transcribe(
            str(audio), language="ja", beam_size=5, vad_filter=True, condition_on_previous_text=False
        )
        rows = [
            {"start": round(segment.start, 6), "end": round(segment.end, 6), "text": segment.text.strip()}
            for segment in segments
        ]
        print(
            json.dumps(
                {
                    "audio_path": str(audio),
                    "text_ja": "".join(part["text"] for part in rows),
                    "language": info.language,
                    "language_probability": round(info.language_probability, 6),
                    "segments": rows,
                    "asr_source": "faster-whisper-base-offline",
                    "review_status": "pending",
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()
