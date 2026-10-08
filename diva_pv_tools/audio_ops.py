"""Project DIVA song audio contract: probe, convert and gate-check Ogg/Vorbis files.

Importable from the Blender add-on *without* importing bpy - the operator calls these three
functions, and the module runs under plain system python too.

Everything here is built on top of the bundled `audio_ogg.py`, which already implements the
Ogg page walker, the Ogg CRC-32, the Vorbis header parsers, the ffmpeg/libvorbis discovery and the
encode plumbing.  This module does not re-implement any of that; it imports it and adds the parts
the music panel needs: a dict-shaped `probe()`, a DIVA-shaped `convert()`, a human-readable
`check_diva_ready()` and the `pv_<id>.ogg` naming rule.  `get_base()` returns the borrowed module
so callers can use `probe()`'s full OggInfo too.

WHAT THE GAME ACTUALLY REQUIRES (re-measured against real game files for every claim; no guesses):
  * 279/279 SEGA songs (204 in main/rom_steam + 75 in dlc00) are: Ogg container, Vorbis
    *I* (id-header version 0), 44100 Hz, **4 channels**, blocksize byte 0xB8 (256/2048),
    framing bit 1, single stream serial, last page EOS set, no torn last packet.
  * id-header bitrate triple, read in Vorbis I 4.2 order (maximum, nominal, lower):
    **maximum 0, nominal 440000, lower 0** on all 279.  The order matters and is easy to get
    backwards - audio_ogg._parse_id() reads it correctly (raw bytes `44ac0000 00000000 c0b60600`
    for pv_001; ffprobe's `bit_rate=440000`, `max_bit_rate=N/A` agrees).
  * **no LOOP_* tags** in any song (0/279).  Comments: exactly one, `ENCODER=...`.
  * vendor string `Xiph.Org libVorbis I 20101101 (Schaufenugget)` on all 279 (cosmetic; working
    mods ship Lavf vendors).
  * songs are loose files (not inside a FArC) at `rom/sound/song/pv_<id>.ogg`, registered in
    pv_db.txt as `pv_XXX.song_file_name=rom/sound/song/pv_XXX.ogg`.
  * 2-channel songs work in game: two installed 2-channel mods play.  The rear pair of SEGA's quads
    carries a *real independent mix* (one shipped quad, whole-file RMS 2238.7/2210.6 against front
    2905.6/2956.0), while an installed quad mod that plays has rear RMS 0.0/0.0 - so "4 channels" and
    "4 channels with rear content" are two different properties and only the first is required.
  * bitrate-field shapes actually installed and playing: max 0/nom 384000 on one quad mod, and
    **max -1/low -1**/nom 224000 on another.  So `maximum == 0` is what SEGA writes, not what the
    loader demands: this module *produces* SEGA's shape and only *blocks* a positive maximum.
  * duration comes from the last page's granule position (pv_db has no length key).  SEGA granules
    are multiples of a 60 fps frame (735 samples) or exactly 366 past one: `granule % 735 in
    {0, 366}` for 157 + 122 = 279/279.  Working mods break that (residues 201 and 720 on the two
    quad mods above), so the grid is reported for the panel, never enforced.
  * decoding the three files above renders *exactly* the granule count (delta 0 samples), so
    `check_diva_ready`'s granule-vs-decoded comparison has no slack on real game files.
  * one RMS window at the top of a file is not enough to prove rear content: one of those quad mods
    reads rear RMS 0.0/0.0 over its first 15 s and 3838.5/3809.9 by max-over-windows, so
    channel_levels() samples four positions and takes the per-channel maximum.

Why the gate exists: piping an ogg through a second lossy stage with `-t` can yield a song
4380 samples (~0.099 s, six DIVA frames at 60 fps) short.  DIVA takes
the PV clock from the granule, so a short file plays *while being* out of sync with every camera cut
and every note the user authored.  `convert()` therefore encodes to scratch, verifies, and only then
moves the file onto the target - and it never overwrites an existing file unless told to.

usage:
    import audio_ops
    info = audio_ops.probe("<pv_249.ogg>")
    report = audio_ops.convert("<music.flac>", "<pv_9001.ogg>", channels=4)
    reasons = audio_ops.check_diva_ready("<pv_9001.ogg>")   # [] == drop-in ready
    name = audio_ops.suggest_song_file_name("<song title>")

CLI:
    python -m diva_pv_tools.audio_ops probe <file.ogg> [...]
    python -m diva_pv_tools.audio_ops convert <src> <out> [--channels 2|4] [--quality F]
                              [--normalize-peak F] [--quad-fill copy|silence] [--force]
    python -m diva_pv_tools.audio_ops check <file.ogg> [...]
    python -m diva_pv_tools.audio_ops ffmpeg

Only stdlib + an ffmpeg binary that carries libvorbis.  If that ffmpeg is missing this module says
so in as many words instead of falling back to something that cannot encode Vorbis.
"""

import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import wave

AudioError = None      # bound below after the base module loads (subclass of its error type)
_ao = None


def _candidate_base_dirs():
    here = os.path.dirname(os.path.abspath(__file__))
    work = os.path.abspath(os.path.join(here, "..", ".."))
    return [p for p in (os.environ.get("MMD2DIVA_WORK"), work, here) if p]


def get_base():
    """The borrowed `audio_ogg` module (Ogg page/CRC/granule parser + ffmpeg plumbing).

    Looked up as $MMD2DIVA_WORK first and then this add-on's own directory, which is where the
    copy that ships with the add-on lives - an installed add-on needs no development checkout.
    """
    global _ao
    if _ao is not None:
        return _ao
    tried = []
    for directory in _candidate_base_dirs():
        if not directory or not os.path.isfile(os.path.join(directory, "audio_ogg.py")):
            tried.append(directory)
            continue
        if directory not in sys.path:
            sys.path.insert(0, directory)
        try:
            import audio_ogg
        except Exception as exc:                              # pragma: no cover - broken install
            raise RuntimeError("found audio_ogg.py in %s but importing it failed: %s" % (directory, exc))
        _ao = audio_ogg
        return _ao
    raise RuntimeError("audio_ogg.py not found (looked in: %s). It holds the Ogg parser this module "
                       "is built on; set MMD2DIVA_WORK to the directory that has it."
                       % ", ".join(tried))


try:
    _ao = get_base()
    _BaseError = _ao.OggVorbisError
except RuntimeError as _exc:                                  # pragma: no cover - broken install
    _BaseError = RuntimeError
    print(_exc, file=sys.stderr)


class AudioError(_BaseError):
    """Every failure this module reports.  English text only - the UI layer translates it."""


if _ao is not None:
    DIVA_SAMPLE_RATE = _ao.DIVA_SAMPLE_RATE        # 44100
    DIVA_VERSION = _ao.DIVA_VERSION                # 0 == Vorbis I
    DIVA_BLOCKSIZE = _ao.DIVA_BLOCKSIZE            # 0xB8
    DIVA_FRAMING_BIT = 1
    SEGA_BITRATE_MAXIMUM = 0
    SEGA_BITRATE_NOMINAL = 440000                  # measured on 279/279 SEGA songs
    SEGA_BITRATE_LOWER = 0
    DIVA_CHANNELS = (2, 4)
    DIVA_FRAME_RATE = 60.0                         # the game's PV clock
    DIVA_FRAME_SECONDS = 1.0 / DIVA_FRAME_RATE
    LOOP_KEYS = ("LOOP_BEGIN", "LOOP_END", "LOOPSTART", "LOOPEND", "LOOP")
else:                                             # pragma: no cover - keep the module importable
    DIVA_SAMPLE_RATE = 44100
    DIVA_VERSION = 0
    DIVA_BLOCKSIZE = 0xB8
    DIVA_FRAMING_BIT = 1
    SEGA_BITRATE_MAXIMUM = 0
    SEGA_BITRATE_NOMINAL = 440000
    SEGA_BITRATE_LOWER = 0
    DIVA_CHANNELS = (2, 4)
    DIVA_FRAME_RATE = 60.0
    DIVA_FRAME_SECONDS = 1.0 / DIVA_FRAME_RATE
    LOOP_KEYS = ("LOOP_BEGIN", "LOOP_END", "LOOPSTART", "LOOPEND", "LOOP")

# Vorbis I 4.3.9 canonical channel order for a given channel count.  probe() reports this as the
# layout name: the *mapping table* lives in the setup header behind the codebook/floor/residue
# sections, and this module deliberately does not bit-parse it - what a quad file has to carry is
# verified by measuring channels 2/3 (channel_levels), which is the thing a header cannot prove.
VORBIS_CHANNEL_ORDER = {
    1: ["FL"],
    2: ["FL", "FR"],
    3: ["FL", "FR", "FC"],
    4: ["FL", "FR", "RL", "RR"],
    5: ["FL", "FR", "FC", "RL", "RR"],
    6: ["FL", "FR", "FC", "RL", "RR", "BC"],
    7: ["FL", "FR", "FC", "SL", "SR", "RL", "RR"],
    8: ["FL", "FR", "FC", "BC", "RL", "RR", "FLC", "FRC"],
}
VORBIS_LAYOUT_NAME = {1: "mono", 2: "stereo", 3: "3.0", 4: "quad", 5: "5.0", 6: "5.1",
                      7: "7.0(back)", 8: "7.1"}

# -q:a (VBR) is the mode that reproduces SEGA's id-header shape: measured with a libvorbis
# encode, quality mode writes maximum 0 / lower 0 and a nominal that depends only on the
# quality number and channel count, while bitrate mode (-b:a) writes maximum -1 / lower -1.
# 4ch q6 -> nominal 440000 (SEGA's exact value); 2ch q7 -> nominal 224000 (112 k/channel, the same
# per-channel allocation as SEGA's 440000/4).  Full measured table: 2ch q3..q10 = 112/128/160/192/
# 224/256/320/499 k, 4ch q3..q10 = 320/344/384/440/480/560/640/959 k.
DEFAULT_QUALITY = {2: "7", 4: "6"}
QUALITY_RANGE = (-0.2, 10.0)
# pan expressions, identical to audio_ogg.QUAD_FILL.
# `4c` and not `quad`: measured against real encodes, naming the layout makes ffmpeg up-mix stereo->quad
# *before* the expressions are applied, so `pan=quad|c2=FL` lands at rear RMS 3426.1 against 8378.6
# for `pan=4c|c2=FL` - the expression no longer means what it says.  The positional 4c route does
# print "4.0 not supported by Vorbis: output stream will have incorrect channel layout", but that
# names ffmpeg's intermediate layout: the muxed file carries 4 channels in the id header and both
# libavcodec and ffprobe read it back as `quad`, which is the canonical Vorbis order (FL FR RL RR)
# the game's own songs use (measured on a shipped song and on an installed quad mod).  Silence is
# spelled as a zeroed coefficient because ffmpeg's pan rejects a bare literal 0.
QUAD_FILL = {
    "silence": "pan=4c|c0=FL|c1=FR|c2=0*FL|c3=0*FL",
    "copy": "pan=4c|c0=FL|c1=FR|c2=FL|c3=FR",
}
MIN_DURATION_SECONDS = 1.0
MAX_DURATION_SECONDS = 1800.0
# Peak normalisation tolerances, from the measured encode/decode curve (a 15 s excerpt of a
# 0 dBFS-hot master, encoded at q5 and q7 and decoded back to float): the Ogg/Vorbis stage puts the
# peak +0.94 to +2.23 dB *above* the PCM peak it was handed, and that ratio is not constant, so the
# only honest promise is "within NORM_TOLERANCE_DB of the target and never above full scale".
NORM_HEADROOM_DB = 2.0
NORM_TOLERANCE_DB = 1.5
NORM_MAX_ATTEMPTS = 3
LEVEL_WINDOW_SECONDS = 30.0          # channels 2/3 are measured over this window
LENGTH_TOLERANCE_FRAMES = 1          # one audio frame == one sample at 44100
# measured by channel_levels() itself (max over 4 x 30 s windows, no -ac) on a shipped quad:
# the rear pair is a real second mix, not a copy of the front.
SEGA_REAR_RMS_SHIPPED = (2410.8, 2361.6)
SEGA_FRONT_RMS_SHIPPED = (3103.2, 3162.1)
# granule % 735 over all 279 SEGA songs: 157 land on an exact 1/60 s frame (0) and 122 on 366.
# Never enforced - two installed quad mods (residues 201 and 720) play.
SEGA_FRAME_GRID_RESIDUES = (0, 366)


# ----------------------------------------------------------------------------- helpers
def _require_base():
    if _ao is None:
        get_base()
    return _ao


def ffmpeg_path(required=True):
    """The ffmpeg this module will use, or None.

    Wraps audio_ogg.find_ffmpeg(), which probes each candidate for existence *and* for a real
    libvorbis row in `-encoders` - some bundled builds ship an ffmpeg without libvorbis,
    so testing existence alone picks a binary that fails later.  Raises AudioError when required.
    """
    base = _require_base()
    try:
        return base.find_ffmpeg(required=required)
    except base.OggVorbisError as exc:
        if required:
            raise AudioError(str(exc))
        return None


def ffprobe_path(ffmpeg=None):
    return _require_base().find_ffprobe(ffmpeg or ffmpeg_path(required=False))


def _run_capture(cmd, cancel=None, poll=0.05):
    """`subprocess.run(..., capture_output=True)` that can be stopped, bytes in and bytes out.

    `channel_levels` reads raw PCM from the child's stdout, so it cannot use the text form above.  The
    same argument applies: a decode of a four-minute song is seconds of work in another process, and
    without polling there is no point at which a cancel can reach it.
    """
    if cancel is None:
        return subprocess.run(cmd, capture_output=True)
    child = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    while True:
        try:
            out, err = child.communicate(timeout=poll)
            break
        except subprocess.TimeoutExpired:
            if cancel():
                child.terminate()
                try:
                    child.communicate(timeout=2.0)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.communicate()
                raise AudioError("ffmpeg stopped: the operation was cancelled")
    return subprocess.CompletedProcess(cmd, child.returncode, out, err)


def _run(cmd, ffmpeg_label="ffmpeg", cancel=None, poll=0.05):
    """Run a subprocess to completion, or stop it as soon as `cancel` says so.

    `subprocess.run` is not interruptible: it blocks in a wait that Python cannot reach, so a 7-second
    ffmpeg encode was a 7-second hole in the plugin's responsiveness no scheduler could chunk its way
    out of - the work is in another process and the only way to bound it is to poll.

    `cancel` is a zero-argument predicate.  When it returns true the child is terminated, then killed
    if it does not stop, and `AudioError` is raised so the caller's cleanup runs; the partial output
    file is the caller's to remove, and the encoder writes to a scratch name for exactly that reason.
    """
    if cancel is None:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace")
        if proc.returncode != 0:
            raise AudioError("%s failed (exit %s):\n  %s\n%s"
                             % (ffmpeg_label, proc.returncode, " ".join(cmd),
                                (proc.stderr or "").strip()[-1500:]))
        return proc
    child = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             encoding="utf-8", errors="replace")
    while True:
        try:
            out, err = child.communicate(timeout=poll)
            break
        except subprocess.TimeoutExpired:
            if cancel():
                child.terminate()
                try:
                    child.communicate(timeout=2.0)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.communicate()
                raise AudioError("%s stopped: the operation was cancelled" % ffmpeg_label)
    if child.returncode != 0:
        raise AudioError("%s failed (exit %s):\n  %s\n%s"
                         % (ffmpeg_label, child.returncode, " ".join(cmd),
                            (err or "").strip()[-1500:]))
    proc = subprocess.CompletedProcess(cmd, child.returncode, out, err)
    return proc


def _wave_info(path):
    with wave.open(path, "rb") as handle:
        return handle.getnframes(), handle.getframerate(), handle.getnchannels()


# ----------------------------------------------------------------------------- probe
def probe(path):
    """Everything a caller needs about one ogg, as a plain JSON-serialisable dict.

    Pure stdlib: it walks the Ogg pages itself (recomputing every page CRC, checking page order,
    the single-serial rule, the EOS flag and the trailing lacing value), decodes the Vorbis
    identification and comment headers, and takes the duration from the last granule position -
    the same number DIVA's decoder walks to.  Raises AudioError for a non-Ogg/non-Vorbis file.
    """
    base = _require_base()
    if not os.path.isfile(path):
        raise AudioError("no such file: %s" % path.replace("\\", "/"))
    try:
        info = base.probe(path)
    except base.OggVorbisError as exc:
        raise AudioError(str(exc))
    except Exception as exc:
        raise AudioError("cannot read %s: %s" % (path.replace("\\", "/"), exc))
    comments = list(info.comments)
    loop = [c for c in comments if any(k in c.upper() for k in LOOP_KEYS)]
    layout = VORBIS_CHANNEL_ORDER.get(info.channels)
    frame_samples = float(info.sample_rate) / DIVA_FRAME_RATE      # 735 at 44100/60fps
    frames = info.duration_samples / frame_samples
    granule = info.duration_samples
    residue = int(granule % round(frame_samples)) if round(frame_samples) else 0
    out = info.as_dict()
    out.update({
        "container": "ogg",
        "codec": "vorbis-I" if info.version == 0 else "vorbis-not-I",
        "version_hex": "0x%08X" % (info.version & 0xFFFFFFFF),
        "channel_layout": VORBIS_LAYOUT_NAME.get(info.channels, "unknown-%dch" % info.channels),
        "channel_positions": layout,
        "channel_layout_source": "vorbis identification header channel byte %d + Vorbis I 4.3.9 "
                                 "canonical order (setup-header mapping not bit-parsed; rear content "
                                 "is verified by measuring, see channel_levels)" % info.channels,
        "sample_rate_matches_diva": info.sample_rate == DIVA_SAMPLE_RATE,
        "channels_supported_by_diva": info.channels in DIVA_CHANNELS,
        "bitrate_nominal": info.bitrate_nominal,
        "bitrate_maximum": info.bitrate_maximum,
        "bitrate_lower": info.bitrate_lower,
        "sega_bitrate_shape": (info.bitrate_maximum == SEGA_BITRATE_MAXIMUM
                               and info.bitrate_lower == SEGA_BITRATE_LOWER),
        "duration_seconds": info.duration_seconds,
        "duration_samples": info.duration_samples,
        "duration_frames_at_60fps": frames,
        "duration_frames_rounded": int(round(frames)),
        "frame_grid_residue_samples": residue,
        "on_sega_frame_grid": residue in SEGA_FRAME_GRID_RESIDUES,
        "frame_grid_note": "informational only: of 279 SEGA granules 157 are exact 1/60 s frames and "
                           "122 sit at residue 366, but two installed quad mods (residues 201 and "
                           "720) both play, so the grid is never enforced",
        "bitrate_actual_bps": (os.path.getsize(path) * 8.0 / info.duration_seconds
                               if info.duration_seconds > 0 else 0.0),
        "metadata_tags": comments,
        "tag_count": len(comments),
        "has_metadata_tags": bool(comments),
        "loop_tags": loop,
        "has_loop_tags": bool(loop),
        "integrity_ok": (info.crc_bad_pages == 0 and info.sequence_gaps == 0
                         and info.last_page_eos == 1 and info.trailing_incomplete_packet == 0),
    })
    out["path"] = path.replace("\\", "/")
    return out


def channel_levels(path, seconds=LEVEL_WINDOW_SECONDS, ffmpeg=None,
                   positions=(0.02, 0.28, 0.55, 0.82), cancel=None):
    """Per-channel RMS/peak of an ogg, measured from decoded PCM at several points in the file.

    A single head window lies often enough to matter: measured on a shipped quad mod,
    the rear pair reads RMS 0.0/0.0 across its first 15 s (instrumental intro) but
    3727.3/3690.6 at the 25% mark.  So several short windows are decoded and, per channel, the
    maximum RMS/peak over them is what `levels` reports; every window is kept in `windows`.

    No `-ac` on purpose: asking for a channel count makes
    ffmpeg pick the 4.0 layout for a quad source and rematrix it, scaling every channel by 0.707
    and moving the rear pair, so the numbers would describe the rematrix instead of the file.
    Returns {"channels", "window_seconds", "levels" (max RMS/peak per channel across the windows),
    "windows" (one entry per decoded window), "max_peak_linear"}.
    """
    ff = ffmpeg or ffmpeg_path()
    info = probe(path)
    count, duration = info["channels"], info["duration_seconds"]
    import array
    spots = tuple(positions) if duration > seconds * 2 else (0.0,)
    windows = []
    for fraction in spots:
        start = 0.0 if duration <= seconds else min(duration * fraction, max(0.0, duration - seconds))
        proc = _run_capture([ff, "-hide_banner", "-nostdin", "-v", "error",
                             "-ss", "%.3f" % start, "-t", "%.3f" % seconds, "-i", path,
                             "-map", "0:a:0", "-f", "f32le", "-acodec", "pcm_f32le", "-"],
                            cancel=cancel)
        if proc.returncode != 0:
            raise AudioError("cannot decode %s to measure its channels: %s"
                             % (path.replace("\\", "/"),
                                (proc.stderr or b"").decode("utf-8", "replace")[-400:]))
        buf = proc.stdout
        buf = buf[:len(buf) - len(buf) % 4]
        if not buf:
            raise AudioError("decoding %s at %0.3f s produced no samples, so its channels cannot be "
                             "measured" % (path.replace("\\", "/"), start))
        samples = array.array("f")
        samples.frombytes(buf)
        # float, not s16: a lossy Vorbis pass reconstructs transients *above* full scale, and an
        # s16 measurement clamps that to 1.000000 so the real overshoot is invisible.  The counts
        # are still reported x32768 so they line up with the s16 numbers quoted in audio_ogg.py.
        levels = []
        for ch in range(count):
            vals = samples[ch::count]
            total = 0.0
            peak = 0.0
            for value in vals:
                total += value * value
                if abs(value) > peak:
                    peak = abs(value)
            rms = (total / len(vals)) ** 0.5 if vals else 0.0
            levels.append({"rms": round(rms * 32768.0, 1), "peak": round(peak * 32768.0, 1),
                           "rms_float": round(rms, 6), "peak_float": round(peak, 6),
                           "clipped": bool(peak > 1.0), "samples": len(vals),
                           "position": (VORBIS_CHANNEL_ORDER.get(count) or [None] * count)[ch]})
        windows.append({"start_seconds": round(start, 3), "seconds": seconds,
                        "decoded_samples_per_channel": len(samples) // max(1, count),
                        "levels": levels})
    aggregate = [{"rms": max(w["levels"][ch]["rms"] for w in windows),
                  "peak": max(w["levels"][ch]["peak"] for w in windows),
                  "peak_float": max(w["levels"][ch]["peak_float"] for w in windows),
                  "clipped": any(w["levels"][ch]["clipped"] for w in windows),
                  "position": windows[0]["levels"][ch]["position"]} for ch in range(count)]
    return {"channels": count, "window_seconds": seconds, "windows": windows, "levels": aggregate,
            "measured_in": "pcm_f32le (counts are x32768, so >1.0 full scale is visible, not clamped)",
            "max_peak_linear": max(l["peak_float"] for l in aggregate),
            "any_clipped": any(l["clipped"] for l in aggregate)}


def decoded_length(path, sample_rate=DIVA_SAMPLE_RATE, ffmpeg=None, workdir=None):
    """How many samples a decoder really renders, and whether that equals the granule.

    The granule is what DIVA's clock uses; if the two disagree the PV plays out of sync with the
    notes/camera while still "working".  Decodes to a scratch wav in `workdir` (or a private temp
    directory that is removed here) and reads the frame count with stdlib `wave`.
    """
    ff = ffmpeg or ffmpeg_path()
    own = workdir is None
    directory = workdir or tempfile.mkdtemp(prefix="diva_audio_")
    try:
        wav_path = os.path.join(directory, "decode_%s.wav" % re.sub(r"\W+", "_",
                                                                   os.path.basename(path))[:40])
        _run([ff, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-i", path,
              "-map", "0:a:0", "-vn", "-c:a", "pcm_s16le", wav_path])
        frames, rate, channels = _wave_info(wav_path)
        return {"decoded_samples": frames, "decoded_sample_rate": rate, "decoded_channels": channels,
                "decoded_seconds": frames / float(rate) if rate else 0.0}
    finally:
        if own:
            shutil.rmtree(directory, ignore_errors=True)


def source_stream_info(path, ffmpeg=None, ffprobe=None):
    """Channel count / rate of any *source* file (not necessarily ogg), best effort."""
    base = _require_base()
    try:
        own = probe(path)
        return {"channels": own["channels"], "sample_rate": own["sample_rate"],
                "channel_layout": own["channel_layout"], "method": "own ogg parser"}
    except AudioError:
        pass
    fp = ffprobe or base.find_ffprobe(ffmpeg or ffmpeg_path(required=False))
    if not fp:
        return {"channels": None, "sample_rate": None, "method": "no ffprobe and not an ogg"}
    proc = subprocess.run([fp, "-v", "error", "-select_streams", "a:0", "-show_entries",
                           "stream=channels,channel_layout,sample_rate", "-of", "json", path],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    try:
        stream = json.loads(proc.stdout or "{}").get("streams", [{}])[0]
    except ValueError:
        stream = {}
    return {"channels": stream.get("channels"), "sample_rate": stream.get("sample_rate"),
            "channel_layout": stream.get("channel_layout"), "method": "ffprobe"}


# ----------------------------------------------------------------------------- conversion
def convert(src, out, *, channels=2, quality=None, sample_rate=DIVA_SAMPLE_RATE,
            normalize_peak=None, quad_fill="copy", ffmpeg=None, overwrite=False,
            keep_temp=False, tolerance_frames=LENGTH_TOLERANCE_FRAMES, cancel=None):
    """Any audio file -> a drop-in DIVA song ogg, written only after it passes every check.

    Keyword-only, matching the panel's four knobs: `channels` (2 or 4), `quality` (libvorbis
    -q:a, -0.2..10; default DEFAULT_QUALITY per channel count, which reproduces SEGA's id-header
    bitrate triple - see the module docstring), `sample_rate` (44100; anything else is refused
    because all 279 SEGA songs are 44100), `normalize_peak` (linear target 0<v<=1, e.g. 0.95): the
    source peak is measured with ffmpeg volumedetect, the gain is applied at the encode stage with
    NORM_HEADROOM_DB of headroom because the lossy stage puts the peak back up (measured
    +0.94..+2.23 dB), and the *encoded* file is re-measured and corrected up to NORM_MAX_ATTEMPTS
    times - the guarantee is NORM_TOLERANCE_DB around the target and never above full scale, not an
    exact peak, which no lossy codec can promise.
    `quad_fill` decides what channels 2/3 get when the source is stereo: "copy" duplicates the
    front pair (real content, 6 dB hot if the game ever downmixes) or "silence" leaves the rear
    pair empty (which a shipped quad mod does, and it plays).

    Stages: (1) decode the source to PCM at `sample_rate`, keeping a 4-channel source's rear mix
    intact and only filling/pan when the channel counts differ; (2) libvorbis-encode to a scratch
    file.  Nothing is written to `out` until the scratch file has been `probe()`d, its per-channel
    content measured and its decoded length compared with the granule; on any failure an AudioError
    lists every failed check and `out` is left exactly as it was.  `out` is never overwritten
    unless `overwrite=True`.

    Returns a dict of what was actually measured (not what was intended).
    """
    import time
    started = time.time()
    if channels not in DIVA_CHANNELS:
        raise AudioError("channels must be 2 or 4 (every SEGA song is 4, working mods ship 2); got %r"
                         % (channels,))
    if quad_fill not in QUAD_FILL:
        raise AudioError("quad_fill must be one of %s, got %r" % (sorted(QUAD_FILL), quad_fill))
    if int(sample_rate) != int(DIVA_SAMPLE_RATE):
        raise AudioError("DIVA songs are all %d Hz (measured on 279/279 files); refusing to publish a "
                         "%d Hz file: %s" % (DIVA_SAMPLE_RATE, sample_rate, str(out)))
    quality_defaulted = quality is None
    if quality_defaulted:
        quality = DEFAULT_QUALITY[channels]
    try:
        q = float(quality)
    except (TypeError, ValueError):
        raise AudioError("quality must be a number between %s and %s, got %r"
                         % (QUALITY_RANGE[0], QUALITY_RANGE[1], quality))
    if not QUALITY_RANGE[0] <= q <= QUALITY_RANGE[1]:
        raise AudioError("quality %r is outside the libvorbis -q range %s..%s" % (quality, *QUALITY_RANGE))
    if normalize_peak is not None:
        if not (0.0 < float(normalize_peak) <= 1.0):
            raise AudioError("normalize_peak must be a linear peak target in (0, 1], got %r"
                             % (normalize_peak,))
    if not os.path.isfile(src):
        raise AudioError("source not found: %s" % src.replace("\\", "/"))
    src = os.path.abspath(src)
    out = os.path.abspath(out)
    if os.path.normcase(src) == os.path.normcase(out):
        raise AudioError("refusing to convert %s onto itself" % src.replace("\\", "/"))
    if os.path.exists(out) and not overwrite:
        raise AudioError("output already exists: %s - pass overwrite=True (CLI --force) to replace it; "
                         "this module never replaces an existing user audio file by default"
                         % out.replace("\\", "/"))
    replacing = os.path.exists(out)

    ff = ffmpeg or ffmpeg_path()
    base = _require_base()
    ffprobe = base.find_ffprobe(ff)
    src_info = source_stream_info(src, ff, ffprobe)
    src_samples, src_how = base.source_duration(src, ffprobe, sample_rate)

    workdir = tempfile.mkdtemp(prefix="diva_audio_")
    try:
        # ---- peak normalisation, measured first so the gain is a number that can be verified later.
        # The target is aimed NORM_HEADROOM_DB *under* the requested peak, because the lossy stage
        # puts the peak back up again: measured on this material at q5/q7, the encoded float peak
        # lands +0.94 to +2.23 dB above the PCM peak it was handed, and not as a constant, so a
        # "hit 0.95 exactly" controller cannot converge.  What this actually promises is
        # NORM_TOLERANCE_DB around the target and never above full scale.
        norm = {"requested": None if normalize_peak is None else float(normalize_peak),
                "headroom_db": NORM_HEADROOM_DB, "tolerance_db": NORM_TOLERANCE_DB,
                "source_peak_db": None, "source_peak_linear": None, "gain_db": None,
                "applied_gain_db": None, "published_peak_linear": None,
                "published_peak_error_db": None}
        if norm["requested"] is not None:
            proc = _run([ff, "-hide_banner", "-nostdin", "-loglevel", "info", "-i", src,
                         "-vn", "-map", "0:a:0", "-af", "volumedetect", "-f", "null", "-"],
                        ffmpeg_label="ffmpeg volumedetect", cancel=cancel)
            match = re.search(r"max_volume:\s*(-?[0-9.]+)\s*dB", (proc.stderr or ""))
            if not match:
                raise AudioError("peak normalisation was requested but ffmpeg reported no max_volume "
                                 "for %s, so no gain can be computed" % src.replace("\\", "/"))
            measured_db = float(match.group(1))
            target_db = 20.0 * math.log10(norm["requested"])
            norm["source_peak_db"] = measured_db
            norm["source_peak_linear"] = round(10.0 ** (measured_db / 20.0), 6)
            norm["target_db"] = round(target_db, 4)
            norm["gain_db"] = round(target_db - measured_db - NORM_HEADROOM_DB, 4)

        # ---- stage 1: decode to PCM (never gain-staged here: the normalisation has to be
        # re-applied against the *encoded* result, so stage 1 stays the one stable input)
        pcm = os.path.join(workdir, "stage1.wav")
        decode = [ff, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-i", src,
                  "-vn", "-map", "0:a:0"]
        plan = []
        if norm["gain_db"] is not None:
            plan.append("peak-normalise with gain %+0.2f dB at the encode stage" % norm["gain_db"])
        same_layout = (src_info["channels"] == channels)
        if same_layout:
            decode += ["-ar", str(sample_rate)]
            plan.append("source already %dch: decode straight through, no channel conversion%s"
                        % (channels, " (the game's rear mix survives)" if channels == 4 else ""))
        else:
            decode += ["-ac", "2", "-ar", str(sample_rate)]
            plan.append("decode to %dch stereo PCM" % 2)
        _run(decode + ["-c:a", "pcm_s16le", pcm], cancel=cancel)
        frames, rate, got_channels = _wave_info(pcm)
        if rate != int(sample_rate):
            raise AudioError("decode stage produced %d Hz, expected %d" % (rate, sample_rate))
        fill = None
        if got_channels == 4 and channels == 2:
            raise AudioError("decode stage produced 4 channels while 2 were asked for (%s)" % pcm)
        if channels == 4 and got_channels == 2:
            fill = quad_fill
            plan.append("fill rear pair with quad_fill=%s" % quad_fill)
        elif channels == 2 and got_channels == 2:
            pass
        elif channels == 4 and got_channels == 4:
            pass
        else:
            raise AudioError("unexpected channel count after decoding: %d (asked for %d)"
                             % (got_channels, channels))

        # ---- stage 2: encode, with up to two corrective retries when peak normalisation was
        # requested.  A lossy Vorbis pass reconstructs peaks *above* the PCM it was handed (measured
        # here: a -0.35 dB gain aimed at 0.95 came back at 1.1094 in the float domain), so the target
        # is verified against the encoded file and the gain corrected from that measurement instead
        # of shipping a file hotter than the user asked for.
        chain = [QUAD_FILL[fill]] if fill is not None else []
        gain = norm["gain_db"]
        candidate = os.path.join(workdir, "candidate.ogg")
        attempts = []
        for attempt in range(1, NORM_MAX_ATTEMPTS + 1):
            encode = [ff, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-i", pcm,
                      "-map_metadata", "-1", "-map", "0:a:0"]
            af = list(chain)
            if gain is not None:
                af = ["volume=%.4fdB" % gain] + af
            if af:
                encode += ["-af", ",".join(af)]
            encode += ["-c:a", "libvorbis", "-q:a", "%g" % q, "-ar", str(sample_rate), candidate]
            _run(encode, cancel=cancel)
            info = probe(candidate)
            levels = channel_levels(candidate, ffmpeg=ff, cancel=cancel)
            measured_peak = levels["max_peak_linear"]
            attempts.append({"attempt": attempt, "quality": "%g" % q,
                             "gain_db": None if gain is None else round(gain, 4),
                             "measured_peak_linear": round(measured_peak, 6),
                             "granule_samples": info["duration_samples"],
                             "bytes": os.path.getsize(candidate)})
            if norm["requested"] is None:
                break
            target = norm["requested"]
            error_db = 20.0 * math.log10(max(measured_peak, 1e-6) / target)
            norm["published_peak_error_db"] = round(error_db, 3)
            if measured_peak > 1.0 or abs(error_db) > NORM_TOLERANCE_DB:
                if attempt < NORM_MAX_ATTEMPTS:
                    gain = gain - error_db           # correct by exactly what was measured
                    continue
            break
        norm["applied_gain_db"] = None if gain is None else round(gain, 4)
        norm["published_peak_linear"] = round(measured_peak, 6)
        rear = levels["levels"][2:4] if info["channels"] == 4 else []
        back = decoded_length(candidate, sample_rate, ff, workdir)

        checks = [
            ("container/codec Vorbis I", info["version"] == DIVA_VERSION and info["codec"] == "vorbis-I",
             "version %d" % info["version"]),
            ("sample rate", info["sample_rate"] == int(sample_rate), "%d Hz" % info["sample_rate"]),
            ("channel count", info["channels"] == channels, "%d ch (asked %d)" % (info["channels"], channels)),
            ("blocksize byte", info["blocksize_byte"] == DIVA_BLOCKSIZE,
             "0x%02X (SEGA 279/279 = 0xB8)" % info["blocksize_byte"]),
            ("framing bit", info["framing_bit"] == DIVA_FRAMING_BIT, str(info["framing_bit"])),
            ("id-header maximum bitrate", info["bitrate_maximum"] == SEGA_BITRATE_MAXIMUM,
             "%d (SEGA 279/279 write %d)" % (info["bitrate_maximum"], SEGA_BITRATE_MAXIMUM)),
            ("id-header lower bitrate", info["bitrate_lower"] == SEGA_BITRATE_LOWER,
             "%d (SEGA 279/279 write %d)" % (info["bitrate_lower"], SEGA_BITRATE_LOWER)),
            ("id-header nominal bitrate", info["bitrate_nominal"] > 0, "%d bps" % info["bitrate_nominal"]),
            ("no LOOP tags", not info["has_loop_tags"], "%d loop tag(s) in %d comment(s)"
             % (len(info["loop_tags"]), info["tag_count"])),
            ("page CRC", info["crc_bad_pages"] == 0, "%d bad of %d pages" % (info["crc_bad_pages"], info["pages"])),
            ("page order", info["sequence_gaps"] == 0, "%d gap(s)" % info["sequence_gaps"]),
            ("stream closed", info["last_page_eos"] == 1, "last page EOS flag %d" % info["last_page_eos"]),
            ("no torn last packet", info["trailing_incomplete_packet"] == 0, "last segment lacing ok"
             if not info["trailing_incomplete_packet"] else "last segment 255: packet continues past EOF"),
            ("granule duration", info["duration_samples"] > 0, "%d samples = %.6f s"
             % (info["duration_samples"], info["duration_seconds"])),
            ("duration in the PV range",
             MIN_DURATION_SECONDS <= info["duration_seconds"] <= MAX_DURATION_SECONDS,
             "%.3f s, accepting %.0f-%.0f s (the same bound check_diva_ready uses, so convert() never "
             "publishes a file its own checker would reject)" % (info["duration_seconds"],
                                                                 MIN_DURATION_SECONDS,
                                                                 MAX_DURATION_SECONDS)),
            ("length vs decoded PCM", abs(info["duration_samples"] - frames) <= tolerance_frames,
             "granule %d vs decoded %d, delta %+d sample(s), tolerance %d"
             % (info["duration_samples"], frames, info["duration_samples"] - frames, tolerance_frames)),
            ("length vs source container", True if src_samples is None else
             abs(frames - src_samples) <= max(tolerance_frames, sample_rate / DIVA_FRAME_RATE),
             "decoded %.6f s vs source %.6f s (%s)" % (frames / float(sample_rate),
                                                       (src_samples or 0) / float(sample_rate), src_how)),
            ("decoded-back channels", back["decoded_channels"] == channels,
             "%d ch when re-decoded" % back["decoded_channels"]),
            ("decoded-back length", abs(back["decoded_samples"] - info["duration_samples"]) <= tolerance_frames,
             "%d back vs %d granule" % (back["decoded_samples"], info["duration_samples"])),
        ]
        if channels == 4:
            if fill == "silence":
                checks.append(("rear pair intentionally silent", all(l["rms"] == 0.0 for l in rear),
                               "ch2/ch3 RMS %s (quad_fill=silence; a shipped mod proves the game "
                               "plays this)"
                               % [l["rms"] for l in rear]))
            else:
                checks.append(("rear pair carries content", all(l["rms"] > 0.0 for l in rear),
                               "ch2/ch3 RMS %s, peaks %s (%d decoded windows of %.0f s, measured "
                               "rather than read from a header)"
                               % ([l["rms"] for l in rear], [l["peak"] for l in rear],
                                  len(levels["windows"]), levels["window_seconds"])))
        if norm["requested"] is not None:
            error_db = norm["published_peak_error_db"]
            checks.append(("peak normalisation within tolerance",
                           measured_peak <= 1.0 and abs(error_db) <= NORM_TOLERANCE_DB,
                           "published peak %.6f is %+0.2f dB vs the %.3f target after %d attempt(s) "
                           "(source peak %.6f = %+0.2f dBFS, final gain %+0.2f dB); the lossy stage "
                           "by itself adds +0.94..+2.23 dB, so exactness is not promised but never "
                           "going over full scale is"
                           % (measured_peak, error_db, norm["requested"], len(attempts),
                              norm["source_peak_linear"], norm["source_peak_db"],
                              norm["applied_gain_db"])))

        report = {
            "src": src.replace("\\", "/"),
            "out": out.replace("\\", "/"),
            "ffmpeg": ff,
            "ffprobe": ffprobe,
            "channels": channels,
            "quality": "%g" % q,
            "quality_defaulted": quality_defaulted,
            "encoder_args": ["-c:a", "libvorbis", "-q:a", "%g" % q],
            "sample_rate": int(sample_rate),
            "quad_fill": fill,
            "channel_plan": plan,
            "normalize_peak": norm,
            "encode_attempts": attempts,
            "source_stream": src_info,
            "decoded_pcm": {"samples": frames, "sample_rate": rate, "channels": got_channels,
                            "seconds": frames / float(rate)},
            "source_container_seconds": (src_samples / float(sample_rate)) if src_samples else None,
            "source_duration_method": src_how,
            "levels": levels,
            "info": info,
            "checks": checks,
            "elapsed_seconds": round(time.time() - started, 2),
        }
        failed = ["%s: %s" % (name, detail) for name, ok, detail in checks if not ok]
        if failed:
            raise AudioError("refusing to publish %s; %s\n%s"
                             % (out.replace("\\", "/"), "the encoded file did not match the DIVA contract",
                                 "\n".join("  - " + f for f in failed) +
                                 "\n(temp files kept in %s%s)" % (workdir.replace("\\", "/"),
                                                                  "" if keep_temp else " - removed below")))
        parent = os.path.dirname(out)
        if parent:
            os.makedirs(parent, exist_ok=True)
        shutil.move(candidate, out)
        # Re-probe the file that actually landed: the checks above ran on the scratch copy, and a
        # move that silently truncated (network share, antivirus) must not be reported as verified.
        published = probe(out)
        report["info"] = published
        report["published"] = {"path": out.replace("\\", "/"), "size": os.path.getsize(out),
                               "replaced_existing": replacing}
        report["published_levels"] = channel_levels(out, ffmpeg=ff)
        if (published["size"] != info["size"] or published["duration_samples"] != info["duration_samples"]
                or published["channels"] != info["channels"] or published["crc_bad_pages"]):
            raise AudioError("wrote %s but the file on disk re-probes differently from the checked "
                             "scratch file: %s samples/%s ch vs %s samples/%s ch"
                             % (out.replace("\\", "/"), published["duration_samples"], published["channels"],
                                info["duration_samples"], info["channels"]))
        report["duration_frames_at_60fps"] = published["duration_frames_at_60fps"]
        report["seconds"] = round(time.time() - started, 2)
        return report
    finally:
        if not keep_temp:
            shutil.rmtree(workdir, ignore_errors=True)


# ----------------------------------------------------------------------------- gate
def check_diva_ready(path, *, allow_silent_rear=False, require_sega_bitrate_shape=False,
                     deep=True, ffmpeg=None, seconds=LEVEL_WINDOW_SECONDS):
    """Reasons this ogg would NOT drop into the game, in human-readable English; [] means good.

    `deep=True` additionally decodes the file to compare the rendered sample count with the granule
    (the class of bug that makes a PV play out of sync) and, for a quad file, measures channels 2/3
    instead of trusting the header.  Both need ffmpeg; if it is missing that is reported as a reason
    rather than skipped silently, because a file that cannot be finished checking is a file whose
    behaviour cannot be promised.  `allow_silent_rear=True` demotes the "quad with empty rear pair"
    case, and
    `require_sega_bitrate_shape=True` turns maximum/lower != 0 into a reason; both stay demoted by
    default because an installed, playable quad mod has rear RMS 0.0/0.0 *and* maximum -1/lower -1 -
    inventing a blocker that a real working file violates would make the gate useless.
    Extra comment tags are deliberately NOT a reason for the same reason (that mod ships 3).
    """
    try:
        info = probe(path)
    except AudioError as exc:
        return [str(exc)]
    reasons = []
    if info["codec"] != "vorbis-I":
        reasons.append("codec is Vorbis version %r (%s), but all 279 SEGA songs are Vorbis I "
                       "(identification-header version 0)" % (info["version"], info["codec"]))
    if info["sample_rate"] != DIVA_SAMPLE_RATE:
        reasons.append("sample rate is %d Hz, but every SEGA song is %d Hz - the PV clock and the "
                       "note layout are authored against 44100" % (info["sample_rate"], DIVA_SAMPLE_RATE))
    if info["channels"] not in DIVA_CHANNELS:
        reasons.append("it has %d channels; SEGA ships 4 (quad, all 279 songs) and working mods ship "
                       "2, so %d has no layout the game is known to accept"
                       % (info["channels"], info["channels"]))
    if info["blocksize_byte"] != DIVA_BLOCKSIZE:
        reasons.append("blocksize byte is 0x%02X (%d/%d); every SEGA song writes 0x%02X (256/2048)"
                       % (info["blocksize_byte"], info["blocksize_short"], info["blocksize_long"],
                          DIVA_BLOCKSIZE))
    if info["framing_bit"] != DIVA_FRAMING_BIT:
        reasons.append("the identification header framing bit is %d, Vorbis I requires 1"
                       % info["framing_bit"])
    if info["bitrate_maximum"] > 0:
        reasons.append("id-header maximum bitrate is %d bps; all 279 SEGA songs write 0 there (a "
                       "positive maximum makes decoders treat the stream as bounded/CBR)"
                       % info["bitrate_maximum"])
    if info["bitrate_nominal"] <= 0:
        reasons.append("id-header nominal bitrate is %d; SEGA writes %d and every playable file seen "
                       "here has a positive nominal" % (info["bitrate_nominal"], SEGA_BITRATE_NOMINAL))
    if info["has_loop_tags"]:
        reasons.append("it carries LOOP tag(s) %s - no SEGA song has any LOOP_* tag in 279 files "
                       "checked, and a loop point the game does not expect can restart the track"
                       % info["loop_tags"])
    if info["crc_bad_pages"]:
        reasons.append("%d of %d Ogg pages fail their own CRC - the decoder will drop those packets"
                       % (info["crc_bad_pages"], info["pages"]))
    if info["sequence_gaps"]:
        reasons.append("%d page sequence-number gap(s): the stream is not contiguous" % info["sequence_gaps"])
    if not info["last_page_eos"]:
        reasons.append("the last page has no EOS flag: the muxer never finished writing this file")
    if info["trailing_incomplete_packet"]:
        reasons.append("the last page ends on a 255 lacing value, so the final packet is cut off")
    if info["duration_samples"] <= 0:
        reasons.append("the last granule position is %d, so there is no length for the PV clock to "
                       "use" % info["duration_samples"])
    elif not (MIN_DURATION_SECONDS <= info["duration_seconds"] <= MAX_DURATION_SECONDS):
        reasons.append("duration %0.3f s is outside the sane PV range %s-%s s"
                       % (info["duration_seconds"], MIN_DURATION_SECONDS, MAX_DURATION_SECONDS))
    if require_sega_bitrate_shape and not info["sega_bitrate_shape"]:
        reasons.append("id-header maximum/lower bitrate are %d/%d instead of the 0/0 every SEGA song "
                       "writes (this looks like a bitrate-mode -b encode; re-encode with a quality "
                       "value) - advisory: an installed quad mod also writes -1/-1 and plays"
                       % (info["bitrate_maximum"], info["bitrate_lower"]))

    # ---- measurements that need a decoder, not a header
    wants_levels = info["channels"] == 4 and (deep or not allow_silent_rear)
    if wants_levels or deep:
        ff = ffmpeg or ffmpeg_path(required=False)
        if not ff:
            reasons.append("this file could not be measured, only read: no usable ffmpeg was found "
                           "(needs a build with --enable-libvorbis); set MMD2DIVA_FFMPEG to one")
        else:
            if wants_levels:
                try:
                    levels = channel_levels(path, seconds, ffmpeg=ff)
                except AudioError as exc:
                    reasons.append("the file cannot be decoded for checking: %s" % exc)
                else:
                    rear = levels["levels"][2:4]
                    if not allow_silent_rear and all(l["rms"] == 0.0 for l in rear):
                        reasons.append("it claims 4 channels but channels 2/3 are silent (max RMS %s "
                                       "over %d decoded windows of %.0f s); SEGA's own quads hold a real "
                                       "second mix there (a shipped quad measures RMS %s/%s) - pass "
                                       "allow_silent_rear=True for a shipped silent rear pair"
                                       % ([l["rms"] for l in rear], len(levels["windows"]),
                                          levels["window_seconds"],
                                          SEGA_REAR_RMS_SHIPPED[0], SEGA_REAR_RMS_SHIPPED[1]))
            if deep:
                try:
                    back = decoded_length(path, info["sample_rate"], ff)
                except AudioError as exc:
                    reasons.append("decoded-length cross-check failed: %s" % exc)
                else:
                    delta = info["duration_samples"] - back["decoded_samples"]
                    if abs(delta) > max(2, int(round(info["sample_rate"] / DIVA_FRAME_RATE))):
                        reasons.append("the granule says %d samples but decoding renders %d "
                                       "(delta %+d = %+0.3f s = %+d DIVA frames at 60 fps): the PV "
                                       "clock and the audible audio disagree, which slides every "
                                       "note and camera cut"
                                       % (info["duration_samples"], back["decoded_samples"], delta,
                                          delta / float(info["sample_rate"]),
                                          round(delta / (info["sample_rate"] / DIVA_FRAME_RATE))))
                    if back["decoded_channels"] != info["channels"]:
                        reasons.append("the id header says %d channels but a decoder renders %d"
                                       % (info["channels"], back["decoded_channels"]))
    return reasons


# ----------------------------------------------------------------------------- naming
_PV_ID = re.compile(r"^[a-z0-9][a-z0-9_]{0,19}$")
_EXISTING = re.compile(r"^pv[_-]?([a-z0-9_]{1,20})(?:\.ogg)?$", re.I)
_NUMERIC = re.compile(r"^\d{1,6}([_-][A-Za-z]{2,6})?$")


def suggest_song_file_name(name, *, pv_id=None, taken=()):
    """A safe `pv_<id>.ogg` slug for the panel to prefill.

    Rules (all measurable, no locale magic): ASCII only, lowercase, `[a-z0-9_]` after `pv_`, the id
    part at most 20 characters, `.ogg` appended once.  Game evidence: every song in main/dlc00 is
    `pv_<id>.ogg` (longest is pv_238_kaito.ogg = 16 chars) and every installed mod song is too
    (longest 20 chars, e.g. pv_00099.ogg / pv_002_len.ogg); Japanese or spaces in the name would go
    straight into `pv_XXX.song_file_name` in pv_db.txt, so they are never allowed to survive here.
    A name with no ASCII stem (pure Japanese) becomes `pv_t<4 hex>` derived from the string, so the
    suggestion is deterministic and collision-free rather than a constant.  `pv_id` (digits, or
    digits+suffix like 002_len) wins over the slug when the panel has a PV number field; `taken` is
    an optional list of already-used names and the suggestion gets `_2`, `_3`... appended to avoid it.
    """
    if pv_id is not None:
        candidate = str(pv_id).strip().lower().replace("-", "_")
        if not re.match(r"^\d{1,6}([_][a-z]{2,6})?$", candidate):
            raise AudioError("pv_id must be 1-6 digits, optionally plus a short alphabetic suffix "
                             "like 002_len (that is the shape of every song name in the game); got %r"
                             % (pv_id,))
        stem = candidate
    else:
        text = unicodedata.normalize("NFKC", "" if name is None else str(name)).strip()
        text = re.sub(r"\.(ogg|wav|mp3|flac|m4a|aac|opus|wma|aiff?)$", "", text, flags=re.I)
        existing = _EXISTING.match(text)
        if existing and (len(existing.group(1)) <= 20):
            stem = existing.group(1).lower().replace("-", "_")
        elif _NUMERIC.match(text):
            stem = text.lower().replace("-", "_")
        else:
            ascii_only = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
            slug = re.sub(r"[^a-z0-9]+", "_", ascii_only.lower()).strip("_")
            slug = re.sub(r"_+", "_", slug)
            stem = slug[:20].strip("_") if slug else "t%s" % _digest(text)[:4]
        if not stem:
            stem = "t%s" % _digest(text)[:4]
    if not _PV_ID.match(stem):
        raise AudioError("cannot build a safe song name from %r (id part must match %s after "
                         "sanitising)" % (name, _PV_ID.pattern))
    out = "pv_%s.ogg" % stem
    used = {str(t).lower() for t in taken}
    index = 2
    while out.lower() in used:
        stem = "%s%d" % (stem[:19], index)
        out = "pv_%s.ogg" % stem
        index += 1
    return out


def _digest(text):
    import hashlib
    return hashlib.md5((text or "").encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------------- CLI
def _print_probe(path):
    info = probe(path)
    print(path)
    for key in ("container", "codec", "sample_rate", "channels", "channel_layout", "channel_positions",
                "blocksize_byte", "blocksize_short", "blocksize_long", "framing_bit", "bitrate_maximum",
                "bitrate_nominal", "bitrate_lower", "sega_bitrate_shape", "duration_samples",
                "duration_seconds", "duration_frames_rounded", "on_sega_frame_grid", "pages",
                "crc_bad_pages", "last_page_eos", "trailing_incomplete_packet", "has_loop_tags",
                "tag_count", "vendor", "bitrate_actual_bps"):
        print("   %-24s %s" % (key, info[key]))
    reasons = check_diva_ready(path)
    print("   %-24s %s" % ("check_diva_ready", "OK, drop-in ready" if not reasons else ""))
    for reason in reasons:
        print("     - %s" % reason)
    return 0 if not reasons else 1


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        print("ffmpeg: %s" % (ffmpeg_path(required=False) or "NOT FOUND"))
        return 0
    command, rest = argv[0], argv[1:]
    if command == "ffmpeg":
        print("ffmpeg : %s" % ffmpeg_path())
        print("ffprobe: %s" % (ffprobe_path() or "none"))
        print("quality defaults: %s  (measured nominal: 2ch q7 = 224000, 4ch q6 = 440000)"
              % DEFAULT_QUALITY)
        return 0
    if command == "probe":
        failing = 0
        for path in rest:
            failing |= _print_probe(path)
        return failing
    if command == "check":
        failing = 0
        for path in rest:
            reasons = check_diva_ready(path)
            print(path)
            if reasons:
                failing = 1
                for reason in reasons:
                    print("   - %s" % reason)
            else:
                print("   OK, drop-in ready")
        return failing
    if command == "convert":
        if len(rest) < 2:
            raise AudioError("usage: python -m diva_pv_tools.audio_ops convert <src> <out.ogg> "
                             "[--channels 2|4] [--quality F] [--normalize-peak F] "
                             "[--quad-fill copy|silence] [--force]")
        src, out = rest[0], rest[1]
        options = {"channels": 2, "quality": None, "normalize_peak": None, "quad_fill": "copy",
                   "overwrite": False}
        index = 2
        while index < len(rest):
            flag = rest[index]
            if flag in ("--channels", "--quality", "--normalize-peak", "--quad-fill"):
                value = rest[index + 1]
                key = flag[2:].replace("-", "_")
                options[key] = (int(value) if key == "channels"
                                else float(value) if key in ("quality", "normalize_peak") else value)
                index += 2
            elif flag == "--force":
                options["overwrite"] = True
                index += 1
            else:
                raise AudioError("unknown option %r" % flag)
        report = convert(src, out, **options)
        print(json.dumps({k: v for k, v in report.items() if k not in ("info", "levels", "checks")},
                         indent=2, ensure_ascii=False, default=str))
        print("checks:")
        for name, ok, detail in report["checks"]:
            print("   [%s] %-28s %s" % ("OK" if ok else "FAIL", name, detail))
        print("wrote: %s (%d bytes, %.3f s, %d ch, nominal %d / maximum %d)"
              % (report["out"], report["published"]["size"], report["info"]["duration_seconds"],
                 report["info"]["channels"], report["info"]["bitrate_nominal"],
                 report["info"]["bitrate_maximum"]))
        return 0
    if command == "name":
        print(suggest_song_file_name(*rest))
        return 0
    raise AudioError("unknown command %r (use probe|convert|check|name|ffmpeg)" % command)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AudioError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        sys.exit(2)

