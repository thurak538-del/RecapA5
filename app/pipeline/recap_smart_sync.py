"""recap-en-to-mm style segmentation + Smart Sync for Recap mode.

- atomicize_units(): word-timestamp based ~4.2s (max 5.6s) narration units.
- assign_full_coverage_boundaries(): every unit owns a contiguous slice of the picture.
- render_smart_sync(): each picture slice is re-timed to the length of ITS OWN Burmese
  narration slot (setpts, speed capped at 1.25x then trimmed), frame-locked at 30 fps,
  concatenated, then muxed with the narration track. Narrator-only audio (as in recap-en-to-mm).
"""
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from app.pipeline.gpu_utils import get_video_encoder_args, get_active_encoder_name

MAX_VIDEO_SPEED = 1.25
FPS = 30
FREEZE_FRAME_DURATION = 0.8
FREEZE_ZOOM_MAX = 1.12

_ABBREV = {"mr.", "mrs.", "ms.", "dr.", "prof.", "sr.", "jr.", "st.", "vs.", "etc.", "no.", "mt.",
           "gen.", "col.", "lt.", "capt.", "sgt.", "u.s.", "u.k.", "a.m.", "p.m."}


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _comparison_text(value: Any) -> str:
    return re.sub(r"[^\w\u1000-\u109f]+", "", _clean(value).lower(), flags=re.UNICODE)


def _fmt_ts(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    return f"{int(seconds // 60):02d}:{seconds % 60:05.2f}"


def _word_ends_sentence(text: str) -> bool:
    cleaned = _clean(text).lower()
    if cleaned in _ABBREV:
        return False
    if re.fullmatch(r"[a-z]\.['’\"”)]?", cleaned):
        return False
    return bool(re.search(r"[.!?…။।॥。！？][\"'’”)]?$", cleaned))


def _word_ends_soft_clause(text: str) -> bool:
    return bool(re.search(r"[;,:၊；，、][\"'’”)]?$", _clean(text)))


def _fallback_tokens(text: str, start: float, end: float, tid: int) -> List[Dict[str, Any]]:
    words = re.findall(r"\S+", _clean(text))
    if not words or end <= start:
        return []
    step = (end - start) / len(words)
    return [{"text": w, "start": start + i * step, "end": start + (i + 1) * step,
             "source_transcript_id": tid} for i, w in enumerate(words)]


def _word_tokens(transcript: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Measured word times (transcript['words']) when present, else even split per segment."""
    measured: Dict[int, List[Dict[str, Any]]] = {}
    for w in transcript.get("words") or []:
        text, st = _clean(w.get("text")), float(w.get("start", 0.0) or 0.0)
        en = float(w.get("end", st) or st)
        if text and en > st:
            measured.setdefault(int(w.get("source_transcript_id", -1)), []).append(
                {"text": text, "start": st, "end": en, "source_transcript_id": int(w.get("source_transcript_id", -1))})
    tokens: List[Dict[str, Any]] = []
    for idx, seg in enumerate(transcript.get("segments") or []):
        text = _clean(seg.get("text"))
        st = float(seg.get("start", 0.0) or 0.0)
        en = float(seg.get("end", st) or st)
        if not text or en <= st:
            continue
        tid = int(seg.get("id", idx))
        tokens.extend(measured.get(tid) or _fallback_tokens(text, st, en, tid))
    tokens.sort(key=lambda t: (float(t["start"]), float(t["end"])))
    return tokens


def atomicize_units(transcript: Dict[str, Any], *, max_duration: float = 5.6,
                    target_duration: float = 4.2) -> List[Dict[str, Any]]:
    tokens = _word_tokens(transcript)
    units: List[Dict[str, Any]] = []
    current: List[Dict[str, Any]] = []
    target_duration = max(2.5, float(target_duration))
    max_duration = max(target_duration + 0.5, float(max_duration))
    min_natural = min(1.8, target_duration * 0.4)

    def close(boundary: str) -> None:
        nonlocal current
        if not current:
            return
        ids = list(dict.fromkeys(int(w["source_transcript_id"]) for w in current))
        units.append({
            "source_transcript_ids": ids,
            "speech_start": round(float(current[0]["start"]), 3),
            "speech_end": round(float(current[-1]["end"]), 3),
            "source_text": _clean(" ".join(str(w["text"]) for w in current)),
            "semantic_boundary": boundary,
        })
        current = []

    for i, tok in enumerate(tokens):
        current.append(tok)
        duration = float(current[-1]["end"]) - float(current[0]["start"])
        gap = 0.0
        if i + 1 < len(tokens):
            gap = max(0.0, float(tokens[i + 1]["start"]) - float(tok["end"]))
        if gap >= 1.0:
            close("pause")
        elif _word_ends_sentence(tok["text"]):
            close("sentence")
        elif _word_ends_soft_clause(tok["text"]) and duration >= min_natural:
            close("clause")
        elif duration >= min_natural and gap >= 0.50:
            close("pause")
        elif duration >= target_duration and gap >= 0.16:
            close("pause")
        elif duration >= max_duration:
            close("forced_local")
    close("end_of_transcript")

    merged: List[Dict[str, Any]] = []
    i = 0
    while i < len(units):
        u = units[i]
        dur = float(u["speech_end"]) - float(u["speech_start"])
        ntok = len(re.findall(r"\S+", u["source_text"]))
        nxt_gap = (float(units[i + 1]["speech_start"]) - float(u["speech_end"])) if i + 1 < len(units) else 9999.0
        if dur <= 0.5 and ntok <= 2 and i + 1 < len(units) and nxt_gap <= 0.60:
            f = dict(units[i + 1])
            lt, rt = _clean(u["source_text"]), _clean(f["source_text"])
            lw = _comparison_text(lt.rstrip(".!?…。！？"))
            rf = _comparison_text(rt.split(maxsplit=1)[0] if rt else "")
            f["speech_start"] = u["speech_start"]
            f["source_text"] = rt if lw and lw == rf else _clean(f"{lt} {rt}")
            f["source_transcript_ids"] = list(dict.fromkeys(list(u["source_transcript_ids"]) + list(f["source_transcript_ids"])))
            units[i + 1] = f
            i += 1
            continue
        merged.append(u)
        i += 1
    for n, u in enumerate(merged, 1):
        u["id"] = n
        u["timestamp"] = f"{_fmt_ts(u['speech_start'])} --> {_fmt_ts(u['speech_end'])}"
    return merged


def assign_full_coverage_boundaries(units: List[Dict[str, Any]], video_duration: float) -> List[Dict[str, Any]]:
    if not units:
        return []
    out = [dict(x) for x in units]
    out[0]["video_start"] = 0.0
    for i in range(len(out) - 1):
        le, rs = float(out[i]["speech_end"]), float(out[i + 1]["speech_start"])
        boundary = (le + rs) / 2.0 if rs >= le else max(le, rs)
        boundary = max(0.0, min(float(video_duration), boundary))
        out[i]["video_end"] = round(boundary, 3)
        out[i + 1]["video_start"] = round(boundary, 3)
    out[-1]["video_end"] = round(float(video_duration), 3)
    return out


def sync_filter(source_duration: float, target_duration: float, max_speed: float = MAX_VIDEO_SPEED) -> str:
    """Exact setpts to the narration slot; speed-up capped at max_speed, then trimmed."""
    desired = target_duration / source_duration
    min_factor = 1.0 / max(1.0, max_speed)
    if desired < min_factor:
        return (f"trim=duration={source_duration:.6f},setpts={min_factor:.9f}*(PTS-STARTPTS),"
                f"trim=duration={target_duration:.6f},setpts=PTS-STARTPTS,fps={FPS}")
    return f"trim=duration={source_duration:.6f},setpts={desired:.9f}*(PTS-STARTPTS),fps={FPS}"


_LONG_EDGE = {"540p": 960, "720p": 1280, "1080p": 1920, "tiktok1080": 1920, "2k": 2560, "tiktok2k": 2560, "4k": 3840, "tiktok4k": 3840}


def _probe_duration(path: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                        "default=noprint_wrappers=1:nokey=1", str(path)],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def _probe_dimensions(path: Path) -> tuple[int, int]:
    """Return source dimensions for the freeze zoom output canvas."""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        width, height = (int(v) for v in r.stdout.strip().split("x", 1))
        if width > 0 and height > 0:
            return width, height
    except (TypeError, ValueError):
        pass
    return 1920, 1080


def _scaled_dimensions(width: int, height: int, long_edge: int) -> tuple[int, int]:
    """Mirror the landscape/portrait scale expression used by the renderer."""
    if width >= height:
        return long_edge, max(2, int(round(height * long_edge / width / 2.0) * 2))
    return max(2, int(round(width * long_edge / height / 2.0) * 2)), long_edge


def render_smart_sync(
    video_path: Path,
    tts_audio_path: Path,
    segments: List[Dict[str, Any]],
    output_path: Path,
    work_dir: Path,
    resolution: str = "1080p",
    enable_4k_filter: bool = False,
    mirror_mode_7s: bool = False,
    freeze_frame: bool = True,
    freeze_frame_duration: float = FREEZE_FRAME_DURATION,
    freeze_zoom: bool = False,
    freeze_zoom_max: float = FREEZE_ZOOM_MAX,
    progress: Optional[Callable[[str, float], None]] = None,
) -> Path:
    """Render Smart Sync segments with a frame-budgeted freeze frame.

    By default, the final 0.8 seconds of every narration slot hold on that
    segment's last frame without zooming. Optional center zoom is retained for
    callers that explicitly set ``freeze_zoom=True``. The effect is included
    in the slot, so it never changes narration timing.
    """
    video_path, tts_audio_path, output_path = Path(video_path), Path(tts_audio_path), Path(output_path)
    work_dir = Path(work_dir)
    parts_dir = work_dir / "smart_sync_parts"
    parts_dir.mkdir(parents=True, exist_ok=True)
    for old in parts_dir.glob("*.mp4"):
        old.unlink(missing_ok=True)

    edge = _LONG_EDGE.get(str(resolution or "1080p").strip().lower(), 1920)
    scale = (f"scale=w='if(gte(iw,ih),{edge},-2)':h='if(gte(iw,ih),-2,{edge})':flags=lanczos")
    encoder_args = get_video_encoder_args(cq=18, crf=17)
    cpu_args = ["-c:v", "libx264", "-preset", "fast", "-crf", "17", "-pix_fmt", "yuv420p"]
    use_cpu = False
    source_width, source_height = _probe_dimensions(video_path)
    frame_width, frame_height = _scaled_dimensions(source_width, source_height, edge)
    freeze_duration = max(0.0, float(freeze_frame_duration))
    zoom_max = max(1.0, float(freeze_zoom_max))

    parts: List[Path] = []
    cursor, frames_done, total = 0.0, 0, len(segments)
    for idx, seg in enumerate(segments, 1):
        src_dur = max(0.04, float(seg["video_end"]) - float(seg["video_start"]))
        target = max(0.04, float(seg["slot_duration"]))
        cursor += target
        frames = max(1, int(round(cursor * FPS)) - frames_done)
        frames_done += frames
        if freeze_frame and frames > 1:
            freeze_frames = min(frames - 1, max(1, int(round(min(freeze_duration, target * 0.8) * FPS))))
        else:
            freeze_frames = 0
        motion_frames = max(1, frames - freeze_frames)
        motion_target = motion_frames / FPS
        motion = parts_dir / f"part_{idx:05d}_motion.mp4"
        freeze = parts_dir / f"part_{idx:05d}_freeze.mp4"
        dst = parts_dir / f"part_{idx:05d}.mp4"

        def _encode_motion(enc_args: List[str]) -> subprocess.CompletedProcess:
            # Alternate the mirrored orientation per Smart Sync segment (first segment stays normal).
            mirror_filter = ",hflip" if mirror_mode_7s and idx % 2 == 0 else ""
            vf = f"{sync_filter(src_dur, motion_target)},{scale}{mirror_filter}"
            cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                   "-ss", f"{float(seg['video_start']):.6f}", "-t", f"{src_dur:.6f}",
                   "-i", str(video_path), "-an", "-vf", vf, "-frames:v", str(motion_frames),
                   *enc_args, "-movflags", "+faststart", str(motion)]
            return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        res = _encode_motion(cpu_args if use_cpu else encoder_args)
        if res.returncode != 0 and not use_cpu and not parts:
            # GPU encoder unusable: switch the whole job to CPU (first part only, so codecs never mix)
            use_cpu = True
            res = _encode_motion(cpu_args)
        if res.returncode != 0:
            raise RuntimeError(f"Smart Sync part {idx} render failed: {(res.stderr or '')[-1500:]}")

        if freeze_frames:
            # Extract the last motion frame, hold it, and animate a restrained
            # center zoom. This is a real freeze-frame rather than slow motion.
            zoom_expression = (
                f"min(zoom+({zoom_max - 1.0:.6f}/{freeze_frames}),{zoom_max:.6f})"
                if freeze_zoom else "1"
            )
            zoom_filter = (
                f"select='eq(n\\,0)',zoompan=z='{zoom_expression}':"
                f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
                f"d={freeze_frames}:s={frame_width}x{frame_height}:fps={FPS}"
            )
            freeze_cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                          "-sseof", "-0.05", "-i", str(motion), "-an", "-vf", zoom_filter,
                          "-frames:v", str(freeze_frames),
                          *(cpu_args if use_cpu else encoder_args), "-movflags", "+faststart", str(freeze)]
            freeze_res = subprocess.run(freeze_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if freeze_res.returncode != 0:
                raise RuntimeError(f"Smart Sync freeze zoom {idx} render failed: {(freeze_res.stderr or '')[-1500:]}")
            part_list = parts_dir / f"part_{idx:05d}_list.txt"
            part_list.write_text(
                "file '" + str(motion.resolve()).replace("\\", "/").replace("'", "'\\''") + "'\n"
                "file '" + str(freeze.resolve()).replace("\\", "/").replace("'", "'\\''") + "'\n",
                encoding="utf-8",
            )
            concat_res = subprocess.run(
                ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0",
                 "-i", str(part_list), "-c", "copy", "-movflags", "+faststart", str(dst)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            if concat_res.returncode != 0:
                raise RuntimeError(f"Smart Sync freeze concat {idx} failed: {(concat_res.stderr or '')[-1500:]}")
        else:
            motion.replace(dst)
        parts.append(dst)
        if progress:
            progress(f"Smart Sync video {idx}/{total}", 5.0 + 70.0 * idx / total)

    list_file = work_dir / "smart_sync_video_parts.txt"
    list_file.write_text("\n".join("file '" + str(p.resolve()).replace("\\", "/").replace("'", "'\\''") + "'"
                                   for p in parts) + "\n", encoding="utf-8")
    silent = work_dir / "smart_sync_video_noaudio.mp4"
    res = subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0",
                          "-i", str(list_file), "-c", "copy", "-movflags", "+faststart", str(silent)],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"Smart Sync concat failed: {(res.stderr or '')[-1500:]}")

    v_len, a_len = _probe_duration(silent), _probe_duration(tts_audio_path)
    if progress:
        progress(f"SYNC check: picture={v_len:.2f}s narration={a_len:.2f}s diff={v_len - a_len:+.3f}s", 80.0)

    post = []
    if enable_4k_filter:
        post += ["eq=contrast=1.08:brightness=0.02:saturation=1.08", "unsharp=5:5:0.45:5:5:0.0"]
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(silent), "-i", str(tts_audio_path),
           "-map", "0:v:0", "-map", "1:a:0"]
    if post:
        cmd += ["-vf", ",".join(post), *(cpu_args if use_cpu else encoder_args)]
    else:
        cmd += ["-c:v", "copy"]
    cmd += ["-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", str(output_path)]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0 and post:
        res = subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(silent),
                              "-i", str(tts_audio_path), "-map", "0:v:0", "-map", "1:a:0", "-vf", ",".join(post),
                              *cpu_args, "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart",
                              str(output_path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0 or not output_path.exists() or output_path.stat().st_size == 0:
        raise RuntimeError(f"Smart Sync final mux failed: {(res.stderr or '')[-1500:]}")
    if progress:
        progress("Smart Sync ပြီးပါပြီ။", 100.0)
    return output_path
