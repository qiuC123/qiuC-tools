"""Isolated UVR5 worker used by the versioned Phase 2A workflow."""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkout", required=True)
    parser.add_argument("--weight", required=True)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    checkout = Path(args.checkout).resolve()
    uvr_code = checkout / "tools" / "uvr5"
    sys.path.insert(0, str(uvr_code))
    from vr import AudioPre

    with tempfile.TemporaryDirectory(prefix="gpt-sovits-phase2a-uvr-") as temporary_dir:
        temporary = Path(temporary_dir)
        reformatted = temporary / "input-44100-stereo.wav"
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", args.input, "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", "-y", str(reformatted)],
            check=True,
        )
        separator = AudioPre(agg=10, model_path=args.weight, device="cuda", is_half=True)
        separator._path_audio_(str(reformatted), vocal_root=str(temporary), format="wav")
        del separator
        generated = next(temporary.glob("vocal_*.wav"))
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(generated), "-ac", "1", "-ar", "32000", "-c:a", "pcm_s16le", "-y", args.output],
            check=True,
        )


if __name__ == "__main__":
    main()
