#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Transcribe a video to SRT with MiMo cloud ASR.

Pipeline:
1. extract mono 16 kHz WAV from video;
2. detect silence and split near breathing pauses;
3. transcribe each chunk with the existing MiMo cloud ASR backend;
4. write JSONL progress and a final SRT.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import mimo_cloud_asr  # noqa: E402


@dataclass(frozen=True)
class Segment:
    index: int
    start: float
    end: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


def run(cmd: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def ffprobe_duration(path: Path) -> float:
    proc = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nk=1:nw=1",
            str(path),
        ],
        capture=True,
    )
    return float(proc.stdout.strip())


def extract_wav(video: Path, wav: Path) -> None:
    if wav.exists() and wav.stat().st_size > 0:
        return
    wav.parent.mkdir(parents=True, exist_ok=True)
    tmp = wav.with_suffix(".tmp.wav")
    if tmp.exists():
        tmp.unlink()
    run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(video),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-sample_fmt",
            "s16",
            str(tmp),
        ]
    )
    tmp.replace(wav)


def detect_silences(wav: Path, threshold: str, min_silence: float) -> list[tuple[float, float]]:
    proc = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            str(wav),
            "-af",
            f"silencedetect=noise={threshold}:d={min_silence}",
            "-f",
            "null",
            "-",
        ],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    starts: list[float] = []
    silences: list[tuple[float, float]] = []
    for line in proc.stderr.splitlines():
        m = re.search(r"silence_start: ([0-9.]+)", line)
        if m:
            starts.append(float(m.group(1)))
            continue
        m = re.search(r"silence_end: ([0-9.]+)", line)
        if m and starts:
            start = starts.pop(0)
            end = float(m.group(1))
            if end > start:
                silences.append((start, end))
    return silences


def choose_cut(
    silences: list[tuple[float, float]],
    start: float,
    target: float,
    min_len: float,
    max_len: float,
    duration: float,
) -> float:
    min_cut = start + min_len
    max_cut = min(start + max_len, duration)
    if max_cut >= duration:
        return duration
    candidates = []
    for a, b in silences:
        mid = (a + b) / 2.0
        if min_cut <= mid <= max_cut:
            candidates.append(mid)
    if candidates:
        preferred = min(start + target, max_cut)
        return min(candidates, key=lambda x: abs(x - preferred))
    return max_cut


def build_segments(duration: float, silences: list[tuple[float, float]], target: float, min_len: float, max_len: float) -> list[Segment]:
    segments: list[Segment] = []
    start = 0.0
    idx = 1
    while start < duration - 0.1:
        cut = choose_cut(silences, start, target, min_len, max_len, duration)
        if cut <= start + 0.1:
            cut = min(start + max_len, duration)
        segments.append(Segment(idx, start, cut))
        idx += 1
        start = cut
    return segments


def extract_chunk(wav: Path, out: Path, segment: Segment) -> None:
    if out.exists() and out.stat().st_size > 0:
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp.wav")
    if tmp.exists():
        tmp.unlink()
    run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{segment.start:.3f}",
            "-t",
            f"{segment.duration:.3f}",
            "-i",
            str(wav),
            "-acodec",
            "pcm_s16le",
            str(tmp),
        ]
    )
    tmp.replace(out)


def ts(seconds: float) -> str:
    seconds = max(0.0, seconds)
    millis = int(round((seconds - math.floor(seconds)) * 1000))
    whole = int(math.floor(seconds))
    if millis == 1000:
        whole += 1
        millis = 0
    h = whole // 3600
    m = (whole % 3600) // 60
    s = whole % 60
    return f"{h:02d}:{m:02d}:{s:02d},{millis:03d}"


def clean_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text.strip())
    text = text.replace(" ,", "，").replace(" .", "。")
    return text


def write_srt(records: list[dict[str, object]], path: Path) -> None:
    lines: list[str] = []
    out_idx = 1
    for rec in records:
        text = clean_text(str(rec.get("text") or ""))
        if not text:
            continue
        lines.extend(
            [
                str(out_idx),
                f"{ts(float(rec['start']))} --> {ts(float(rec['end']))}",
                text,
                "",
            ]
        )
        out_idx += 1
    path.write_text("\n".join(lines), encoding="utf-8")


def read_records(path: Path) -> dict[int, dict[str, object]]:
    records: dict[int, dict[str, object]] = {}
    if not path.exists():
        return records
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        records[int(rec["index"])] = rec
    return records


def append_record(path: Path, rec: dict[str, object]) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("video", type=Path)
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument("--target", type=float, default=25.0, help="preferred chunk length in seconds")
    parser.add_argument("--min", dest="min_len", type=float, default=8.0, help="minimum chunk length in seconds")
    parser.add_argument("--max", dest="max_len", type=float, default=38.0, help="maximum chunk length in seconds")
    parser.add_argument("--silence-threshold", default="-35dB")
    parser.add_argument("--min-silence", type=float, default=0.28)
    args = parser.parse_args()

    video = args.video.expanduser().resolve()
    if not video.exists():
        raise SystemExit(f"video not found: {video}")
    stem = video.stem
    out_dir = (args.out_dir or video.with_name(stem + "_srt_work")).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    wav = out_dir / "audio_16k_mono.wav"
    segments_path = out_dir / "segments.json"
    jsonl_path = out_dir / "transcript.jsonl"
    srt_path = out_dir / f"{stem}.srt"
    chunks_dir = out_dir / "chunks"

    print(f"[1/4] extract audio -> {wav}", flush=True)
    extract_wav(video, wav)

    duration = ffprobe_duration(wav)
    print(f"[2/4] detect silence duration={duration:.3f}s", flush=True)
    if segments_path.exists():
        seg_data = json.loads(segments_path.read_text(encoding="utf-8"))
        segments = [Segment(int(x["index"]), float(x["start"]), float(x["end"])) for x in seg_data]
    else:
        silences = detect_silences(wav, args.silence_threshold, args.min_silence)
        segments = build_segments(duration, silences, args.target, args.min_len, args.max_len)
        segments_path.write_text(
            json.dumps([s.__dict__ for s in segments], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    print(f"[3/4] segments={len(segments)} jsonl={jsonl_path}", flush=True)

    done = read_records(jsonl_path)
    for seg in segments:
        if seg.index in done and str(done[seg.index].get("text") or "").strip():
            continue
        chunk = chunks_dir / f"chunk_{seg.index:04d}_{seg.start:.3f}_{seg.end:.3f}.wav"
        extract_chunk(wav, chunk, seg)
        started = time.monotonic()
        try:
            text = mimo_cloud_asr.transcribe(str(chunk))
            err = ""
        except Exception as exc:  # keep resumable progress on one failed chunk
            text = ""
            err = str(exc)
        rec: dict[str, object] = {
            "index": seg.index,
            "start": seg.start,
            "end": seg.end,
            "duration": seg.duration,
            "chunk": str(chunk),
            "text": text,
            "error": err,
            "elapsed": round(time.monotonic() - started, 3),
        }
        append_record(jsonl_path, rec)
        status = "ok" if text else "ERR"
        print(f"[{seg.index}/{len(segments)}] {status} {ts(seg.start)}-{ts(seg.end)} chars={len(text)} {err[:120]}", flush=True)

    records = [read_records(jsonl_path).get(s.index, {"index": s.index, "start": s.start, "end": s.end, "text": ""}) for s in segments]
    write_srt(records, srt_path)
    errors = [r for r in records if r.get("error")]
    print(f"[4/4] wrote {srt_path}", flush=True)
    if errors:
        print(f"warning: {len(errors)} chunk(s) failed; rerun the same command to retry missing chunks", flush=True)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
