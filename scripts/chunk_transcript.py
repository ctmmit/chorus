"""Group a transcript's fine segments into coarse time windows.

Two modes:
  index            one line per window: [MM:SS] + first ~22 words (a menu)
  full START END   full merged text for windows overlapping [START,END] seconds

Usage:
    python scripts/chunk_transcript.py <transcript.json> [--window 90] [index|full S E]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def mmss(sec: float) -> str:
    return f"{int(sec)//60:>3d}:{int(sec)%60:02d}"


def windows(segs: list[dict], win: int) -> list[dict]:
    out: list[dict] = []
    cur: dict | None = None
    for s in segs:
        st = float(s["start"])
        if cur is None or st - cur["start"] >= win:
            cur = {"start": st, "text": s["text"]}
            out.append(cur)
        else:
            cur["text"] += " " + s["text"]
    return out


def main() -> None:
    args = sys.argv[1:]
    if not args:
        raise SystemExit(__doc__)
    path = Path(args[0])
    win = int(args[args.index("--window") + 1]) if "--window" in args else 90
    rest = [a for a in args[1:] if a != "--window" and a != str(win)]
    mode = rest[0] if rest else "index"

    segs = json.loads(path.read_text(encoding="utf-8"))["segments"]
    wins = windows(segs, win)

    if mode == "index":
        for w in wins:
            head = " ".join(w["text"].split()[:22])
            print(f"[{mmss(w['start'])}] {head}")
        print(f"\n({len(wins)} windows @ {win}s)")
    elif mode == "full":
        lo, hi = float(rest[1]), float(rest[2])
        for w in wins:
            if w["start"] >= lo - win and w["start"] <= hi:
                print(f"\n[{mmss(w['start'])}]\n{w['text']}")
    else:
        raise SystemExit("mode must be 'index' or 'full S E'")


if __name__ == "__main__":
    main()
