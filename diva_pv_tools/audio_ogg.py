"""Any audio file -> a Project DIVA MEGA PACK+ song ogg, with no pip installs.

Two jobs.  `convert()` does the work (ffmpeg decodes whatever the user dropped in, libvorbis
encodes it into the container DIVA reads); `probe()` checks the result *without* ffmpeg, by
parsing the Ogg page structure and the Vorbis identification/comment headers in pure stdlib.
That second half exists because the Blender add-on has to be able to look at a file it just
wrote - or at a SEGA original - and answer "same sample rate? same channel count? same length?"
without trusting the encoder's own log lines.

Why duration is a hard requirement and not a nicety: DIVA takes the song length from the ogg
itself (the granule position of the last page) - pv_db has no length key at all, its only
time-ish fields are `sabi.start_time`/`sabi.play_time` and `bpm`, so nothing else can put the PV
clock in seconds.  A file that comes out even a tenth of a second short or long slides every
camera cut and every note against the animation the user authored in MMD, so the whole PV plays
out of sync while still "working".  `convert()` therefore encodes to a scratch file, refuses to
publish it unless its sample count equals the decoded source's within `tolerance_frames` (default
one sample), and only then moves it onto `out`.  This is not hypothetical: piping an ogg
through a second lossy stage with `-t` can yield a file 4380 samples (~0.099 s, six DIVA
frames at 60 fps) short.

What the container has to look like (measured off SEGA's own songs):
Ogg/Vorbis I, version 0, 44100 Hz, blocksize byte 0xB8 (256/2048), framing bit 1, and the
songs are 4 channels (quad): SEGA puts an independent vocal mix in the two rear channels
(pv_249 rear RMS ~2010 counts, correlation ~0.00 with the front pair, i.e. real separate audio).
Shipped mods prove the loader is relaxed about the rest - a working 2-channel song (pv_643), a working quad song with dead-silent rear channels (a shipped quad mod pv_8331) and
working files whose vendor string, ENCODER comment and id-header bitrates all differ from SEGA's.
So: rate, channel count and length are the fields we enforce; vendor/ENCODER/nominal bitrate are
cosmetic and are reported, not faked.

usage:
    import audio_ogg
    report = audio_ogg.convert("<music.wav>", "<out.ogg>", channels=4)
    info = audio_ogg.probe("<pv_249.ogg>")
    print(info.duration_seconds, info.vendor, info.bitrate_nominal)

Only stdlib + an ffmpeg binary.  Nothing here writes outside the requested output path except a
temp directory that is removed at the end (`keep_temp=True` to inspect it).
"""

import dataclasses
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import wave

# Last-resort probes for common manual installs that do not put ffmpeg on PATH.  `find_ffmpeg`
# tries $MMD2DIVA_FFMPEG first, then PATH, then this add-on's own `bin/` - filled by
# ffmpeg_fetch on one click, or by a user who green-installs a binary there - and only then
# these locations: set MMD2DIVA_FFMPEG to any ffmpeg built with --enable-libvorbis and the
# add-on will use it.
FFMPEG_CANDIDATES = (
    r"C:/ffmpeg/bin/ffmpeg.exe",
    r"C:/Program Files/ffmpeg/bin/ffmpeg.exe",
)

# The directory the add-on's own fetch step installs into.  Probed ahead of the last-resort
# system locations because a binary that lives here was put here for this add-on specifically.
BUNDLED_FFMPEG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin", "ffmpeg.exe")

DIVA_SAMPLE_RATE = 44100
DIVA_VERSION = 0                 # Vorbis I
DIVA_BLOCKSIZE = 0xB8            # short 2^8 = 256, long 2^11 = 2048 (SEGA songs, and libvorbis default)
SEGA_ID_VENDOR = "Xiph.Org libVorbis I 20101101 (Schaufenugget)"
SEGA_ID_COMMENT = "ENCODER=ogg_vorbis_encode/2011-02-07"

# pan expressions.  ffmpeg's pan filter rejects a bare literal 0 for a channel, so silence is
# spelled as a zeroed coefficient on an input that exists.
QUAD_FILL = {
    "silence": "pan=4c|c0=FL|c1=FR|c2=0*FL|c3=0*FL",
    "copy": "pan=4c|c0=FL|c1=FR|c2=FL|c3=FR",
}


class OggVorbisError(RuntimeError):
    """Raised for a missing/unusable ffmpeg, an unparseable ogg, or a failed output check."""


@dataclasses.dataclass
class OggInfo:
    """Everything `probe()` reads out of an Ogg/Vorbis bitstream (no external tool involved)."""

    path: str
    size: int
    pages: int
    serial: int
    version: int
    channels: int
    sample_rate: int
    blocksize_byte: int
    blocksize_short: int
    blocksize_long: int
    framing_bit: int
    bitrate_maximum: int
    bitrate_nominal: int
    bitrate_lower: int
    vendor: str
    comments: list
    duration_samples: int
    duration_seconds: float
    eos_granule: int
    crc_bad_pages: int
    sequence_gaps: int
    last_page_eos: int
    trailing_incomplete_packet: int

    @property
    def codec(self):
        return "vorbis-I" if self.version == 0 else "vorbis-?%d" % self.version

    def as_dict(self):
        out = dataclasses.asdict(self)
        out["codec"] = self.codec
        return out

    def line(self):
        return ("%d Hz / %dch / bs 0x%02X(%d/%d) / framing %d / br max %d nom %d low %d / %.6f s (%d samples)"
                % (self.sample_rate, self.channels, self.blocksize_byte, self.blocksize_short,
                   self.blocksize_long, self.framing_bit, self.bitrate_maximum, self.bitrate_nominal,
                   self.bitrate_lower, self.duration_seconds, self.duration_samples))


# ------------------------------------------------------------------ pure stdlib ogg reader
_CRC_POLY = 0x04C11DB7


def _crc_table():
    table = []
    for i in range(256):
        crc = i << 24
        for _ in range(8):
            crc = ((crc << 1) ^ _CRC_POLY) & 0xFFFFFFFF if crc & 0x80000000 else (crc << 1) & 0xFFFFFFFF
        table.append(crc)
    return table


_CRC = _crc_table()


def _ogg_crc32(data):
    """Ogg's CRC-32: poly 0x04c11db7, MSB-first, no reflection, init 0, no final xor."""
    crc = 0
    for byte in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _CRC[((crc >> 24) ^ byte) & 0xFF]
    return crc


def _iter_pages(data, path):
    """Yield (header_type, granule, serial, seq, crc, segments, body, offset) for every page.

    A short/truncated tail is an error rather than a silent stop: probe() is the thing that tells
    the user their file is usable, so it must not shrug at a half-written page.
    """
    pos = 0
    while pos < len(data):
        if data[pos:pos + 4] != b"OggS":
            raise OggVorbisError("%s: not an Ogg stream at byte %d (found %r); "
                                 "expected capture pattern 'OggS'" % (path, pos, data[pos:pos + 4]))
        if pos + 27 > len(data):
            raise OggVorbisError("%s: truncated page header at byte %d" % (path, pos))
        if data[pos + 4] != 0:
            raise OggVorbisError("%s: unsupported stream structure version %d (only 0 exists)"
                                 % (path, data[pos + 4]))
        header_type = data[pos + 5]
        granule = struct.unpack_from("<q", data, pos + 6)[0]
        serial = struct.unpack_from("<I", data, pos + 14)[0]
        seq = struct.unpack_from("<I", data, pos + 18)[0]
        crc = struct.unpack_from("<I", data, pos + 22)[0]
        nsegs = data[pos + 26]
        seg_start = pos + 27
        seg_end = seg_start + nsegs
        if seg_end > len(data):
            raise OggVorbisError("%s: truncated segment table in page at byte %d" % (path, pos))
        segments = list(data[seg_start:seg_end])
        body_start = seg_end
        body_end = body_start + sum(segments)
        if body_end > len(data):
            raise OggVorbisError("%s: page at byte %d claims %d body bytes but the file ends"
                                 % (path, pos, sum(segments)))
        yield (header_type, granule, serial, seq, crc, segments, data[body_start:body_end], pos, seg_start)
        pos = body_end


def _read_packets(data, path, want=3):
    """Reassemble the first `want` logical packets (header packets never span a page gap in practice)."""
    packets = []
    buf = bytearray()
    for header_type, granule, serial, seq, crc, segments, body, pos, seg_start in _iter_pages(data, path):
        off = 0
        for value in segments:
            buf += body[off:off + value]
            off += value
            if value < 255:
                packets.append(bytes(buf))
                buf = bytearray()
                if len(packets) >= want:
                    return packets
    return packets


def _parse_id(packet, path):
    if len(packet) < 30 or packet[0] != 1 or packet[1:7] != b"vorbis":
        raise OggVorbisError("%s: first packet is not a Vorbis identification header "
                             "(type %r, tag %r)" % (path, packet[:1], packet[1:7]))
    framing = packet[29]
    if framing != 1:
        raise OggVorbisError("%s: identification header framing bit is %d, must be 1" % (path, framing))
    bs = packet[28]
    return {
        "version": struct.unpack_from("<I", packet, 7)[0],
        "channels": packet[11],
        "sample_rate": struct.unpack_from("<I", packet, 12)[0],
        # Vorbis I §4 orders these as maximum, nominal, lower.  SEGA's songs read maximum 0,
        # nominal 440000, lower 0 - the order matters and is easy to get backwards.
        "bitrate_maximum": struct.unpack_from("<i", packet, 16)[0],
        "bitrate_nominal": struct.unpack_from("<i", packet, 20)[0],
        "bitrate_lower": struct.unpack_from("<i", packet, 24)[0],
        "blocksize_byte": bs,
        "blocksize_short": 2 ** (bs & 0x0F),
        "blocksize_long": 2 ** (bs >> 4),
        "framing_bit": framing,
    }


def _parse_comment(packet, path):
    if len(packet) < 11 or packet[0] != 3 or packet[1:7] != b"vorbis":
        raise OggVorbisError("%s: second packet is not a Vorbis comment header" % path)
    off = 7
    vlen = struct.unpack_from("<I", packet, off)[0]
    off += 4
    vendor = packet[off:off + vlen].decode("utf-8", "replace")
    off += vlen
    count = struct.unpack_from("<I", packet, off)[0] if off + 4 <= len(packet) else 0
    off += 4
    comments = []
    for _ in range(count):
        if off + 4 > len(packet):
            break
        clen = struct.unpack_from("<I", packet, off)[0]
        off += 4
        comments.append(packet[off:off + clen].decode("utf-8", "replace"))
        off += clen
    return vendor, comments


def probe(path):
    """Parse an .ogg with stdlib only and return an OggInfo.

    Covers the whole container check: every page's CRC is recomputed, page sequence numbers are
    checked for gaps, exactly one beginning-of-stream page is required, the last page's EOS flag
    and the final lacing value are recorded (an unset EOS or a trailing 255 means the muxer never
    finished writing), the three Vorbis header packets are decoded field by field, and the duration
    is taken from the final granule position - which is exactly the number DIVA's decoder walks to,
    so it is the number that has to match the source.
    """
    with open(path, "rb") as handle:
        data = handle.read()
    if not data:
        raise OggVorbisError("%s: empty file" % path)

    serials = set()
    pages = 0
    crc_bad = 0
    gaps = 0
    last_seq = None
    bos_pages = 0
    granule = -1
    last_header_type = 0
    last_segments = []
    for header_type, g, serial, seq, crc, segments, body, pos, seg_start in _iter_pages(data, path):
        pages += 1
        serials.add(serial)
        last_header_type = header_type
        last_segments = segments
        if header_type & 0x02:
            bos_pages += 1
        if last_seq is not None and seq != last_seq + 1:
            gaps += 1
        last_seq = seq
        if g >= 0:
            granule = g
        if not _page_crc_ok(data, pos, seg_start, crc):
            crc_bad += 1
    if len(serials) != 1:
        raise OggVorbisError("%s: pages carry %d different stream serials (%s); DIVA songs are one stream"
                             % (path, len(serials), sorted(serials)))
    if bos_pages != 1:
        raise OggVorbisError("%s: found %d beginning-of-stream pages, expected exactly 1" % (path, bos_pages))

    packets = _read_packets(data, path, want=3)
    if len(packets) < 3:
        raise OggVorbisError("%s: only %d Vorbis header packet(s) found, need identification, "
                             "comment and setup" % (path, len(packets)))
    if len(packets[2]) < 7 or packets[2][0] != 5 or packets[2][1:7] != b"vorbis":
        raise OggVorbisError("%s: third packet is not a Vorbis setup header" % path)

    ident = _parse_id(packets[0], path)
    vendor, comments = _parse_comment(packets[1], path)
    samples = granule if granule >= 0 else 0
    return OggInfo(
        path=path.replace("\\", "/"),
        size=len(data),
        pages=pages,
        serial=next(iter(serials)),
        last_page_eos=1 if last_header_type & 0x04 else 0,
        trailing_incomplete_packet=1 if last_segments and last_segments[-1] == 255 else 0,
        crc_bad_pages=crc_bad,
        sequence_gaps=gaps,
        eos_granule=granule,
        duration_samples=samples,
        duration_seconds=samples / float(ident["sample_rate"]),
        vendor=vendor,
        comments=comments,
        **ident
    )


def _page_crc_ok(data, page_start, seg_start, crc):
    """Recompute a page CRC with the stored field zeroed, without copying the whole file."""
    nsegs = data[page_start + 26]
    head = bytearray(data[page_start:seg_start])
    head[22:26] = b"\0\0\0\0"
    body_end = seg_start + nsegs + sum(data[seg_start:seg_start + nsegs])
    return _ogg_crc32(bytes(head) + data[seg_start:body_end]) == crc


# ------------------------------------------------------------------ ffmpeg plumbing
def _run(cmd):
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise OggVorbisError("command failed (%s): %s\n%s"
                             % (proc.returncode, " ".join(cmd), (proc.stderr or "").strip()[-2000:]))
    return proc


def _has_encoder(ffmpeg, encoder="libvorbis"):
    """True if `ffmpeg -encoders` lists an audio encoder with that exact name.

    An `-encoders` line looks like ` A..X.D libvorbis            libvorbis (codec vorbis)`, so the
    name is matched in the second column of an audio row rather than as a substring of the help
    text (which mentions libvorbis even when the binary was built without it).
    """
    try:
        out = _run([ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-encoders"]).stdout
    except (OggVorbisError, OSError):
        return False
    for line in out.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0].startswith("A") and fields[1] == encoder:
            return True
    return False


def find_ffmpeg(extra_paths=(), encoder="libvorbis", required=True):
    """First ffmpeg that exists and really encodes `encoder`, or None / a raised error.

    Existence alone is not enough - many third-party ffmpeg builds (those bundled with video
    and ML tools) ship one with no libvorbis, and picking one of those only produces a confusing
    failure three steps later inside the encode.  $MMD2DIVA_FFMPEG wins over the list so the
    add-on can point at a bundled binary; PATH comes next, then this package's own `bin/`.
    """
    probed = []
    candidates = []
    env = os.environ.get("MMD2DIVA_FFMPEG")
    # Order matters: what the user set, then what PATH offers, then the binary this add-on
    # fetched into `bin/` (or a user green-installed there), then the last-resort known
    # install locations.  A shipped add-on needs none of the last group - they are a
    # convenience for manual installs that are not on PATH, not a dependency.
    for path in ([env], extra_paths, [shutil.which("ffmpeg") or ""], [BUNDLED_FFMPEG],
                 list(FFMPEG_CANDIDATES)):
        for item in path:
            if item and item not in candidates:
                candidates.append(item)
    for cand in candidates:
        if not os.path.isfile(cand):
            probed.append("%s: no such file" % cand.replace("\\", "/"))
            continue
        if not _has_encoder(cand, encoder):
            probed.append("%s: present but no %s encoder" % (cand.replace("\\", "/"), encoder))
            continue
        return cand.replace("\\", "/")
    if required:
        raise OggVorbisError(
            "no usable ffmpeg found (needs an ffmpeg built with --enable-libvorbis).\nProbed:\n  "
            + "\n  ".join(probed) +
            "\nUse the music panel's FFmpeg download button, set the environment variable "
            "MMD2DIVA_FFMPEG to your ffmpeg.exe, or pass ffmpeg=<path> to convert()/find_ffmpeg().")
    return None


def find_ffprobe(ffmpeg=None):
    """A ffprobe to cross-check source durations with, or None.

    Only used for the *optional* source-container comparison in convert(); nothing in probe()
    needs it.  It is looked for next to the chosen ffmpeg first, then next to the other known
    install locations, then on PATH - so a machine with one lone ffprobe still gets the cross-check.
    Some builds ship ffmpeg.exe only while others bundle ffprobe.exe beside it, hence the search.
    """
    dirs = [os.path.dirname(ffmpeg)] if ffmpeg else []
    dirs += [os.path.dirname(BUNDLED_FFMPEG)]
    dirs += [os.path.dirname(c) for c in FFMPEG_CANDIDATES]
    found = [os.environ.get("MMD2DIVA_FFPROBE")]
    found += [os.path.join(d, name) for d in dirs if d for name in ("ffprobe.exe", "ffprobe")]
    found.append(shutil.which("ffprobe") or "")
    for cand in found:
        if cand and os.path.isfile(cand):
            return cand.replace("\\", "/")
    return None


def _wav_frames(path):
    with wave.open(path, "rb") as handle:
        return handle.getnframes(), handle.getframerate(), handle.getnchannels()


def source_duration(path, ffprobe=None, sample_rate=DIVA_SAMPLE_RATE):
    """Source length expressed in samples at `sample_rate`, plus how that number was obtained.

    Returns (samples_or_None, method_string).  .wav is read with stdlib `wave` (exact, no tool
    needed); anything else asks ffprobe for the container duration and scales it.  For a lossy
    source this is the container's own claim, which can differ from what a decoder renders by the
    codec's encoder-delay trim - that is why convert() treats it as a cross-check, not as the
    contract.
    """
    if path.lower().endswith(".wav"):
        try:
            frames, rate, _ch = _wav_frames(path)
            return frames / float(rate) * float(sample_rate), "wave module"
        except Exception as exc:
            return None, "wave module failed (%s)" % exc
    if ffprobe:
        proc = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                               "-of", "default=nw=1:nk=1", path],
                              capture_output=True, text=True, encoding="utf-8", errors="replace")
        text = (proc.stdout or "").strip()
        try:
            return float(text) * float(sample_rate), "ffprobe format=duration"
        except ValueError:
            return None, "ffprobe gave %r" % text
    return None, "no ffprobe and source is not .wav"


# ------------------------------------------------------------------ conversion
def convert(src, out, channels=2, bitrate="224k", ffmpeg=None, quad_fill="silence",
            sample_rate=DIVA_SAMPLE_RATE, keep_metadata=False, tolerance_frames=1,
            verify_roundtrip=False, keep_temp=False):
    """Decode `src` (mp3/wav/flac/m4a/ogg/...), then encode one Ogg/Vorbis I file for DIVA.

    Two stages on purpose:
      1. ffmpeg decodes to a scratch 44100 Hz stereo PCM wav.  Doing the resample and the channel
         downmix here (rather than inside the encoder) is what makes the length check meaningful -
         the PCM frame count is the source length as any decoder will render it.  Sources with
         more than two channels are folded to stereo at this point; a quad source that should keep
         its four channels is not what this function is for.
      2. libvorbis encodes that wav.  `channels=4` adds the pan filter in the *PCM* domain, so a
         quad song is still one lossy pass (`quad_fill="silence"` leaves the rear channels
         empty, which SEGA's own files do not have but working mods prove is fine;
         `"copy"` duplicates FL/FR to the rear for a loader or a downmix that expects content).

    Before `out` is touched the candidate is probed with `probe()` and checked for: Vorbis I,
    `sample_rate`, `channels`, framing bit, page CRCs intact, and a granule-derived sample count
    equal to the decoded source within `tolerance_frames` (default one audio frame = one sample).
    A rejected file stays in the temp directory and the error lists every failed check.
    `verify_roundtrip=True` additionally decodes the ogg back to PCM and compares the frame count,
    catching a muxer that reports one length and plays another.

    Returns the report dict (source numbers, output OggInfo, check results).
    """
    if channels not in (2, 4):
        raise OggVorbisError("channels must be 2 or 4, got %r" % (channels,))
    if channels == 4 and quad_fill not in QUAD_FILL:
        raise OggVorbisError("quad_fill must be one of %s, got %r" % (sorted(QUAD_FILL), quad_fill))
    if not os.path.isfile(src):
        raise OggVorbisError("source not found: %s" % src)

    ff = ffmpeg or find_ffmpeg()
    ffprobe = find_ffprobe(ff)
    src_samples, src_how = source_duration(src, ffprobe, sample_rate)

    workdir = tempfile.mkdtemp(prefix="audio_ogg_")
    try:
        pcm = os.path.join(workdir, "stage1.wav")
        candidate = os.path.join(workdir, "candidate.ogg")
        base = [ff, "-hide_banner", "-nostdin", "-loglevel", "error", "-y"]
        # stage 1: decode.  -map 0:a:0 keeps embedded album art (mp3/m4a) from being treated as a
        # stream to encode, and the explicit -ac/-ar make the resample part of the contract.
        _run(base + ["-i", src, "-vn", "-map", "0:a:0", "-ac", "2", "-ar", str(sample_rate),
                     "-c:a", "pcm_s16le", pcm])
        expected, pcm_rate, pcm_channels = _wav_frames(pcm)
        if pcm_rate != sample_rate or pcm_channels != 2:
            raise OggVorbisError("decode stage produced %d Hz / %dch, expected %d Hz / 2ch"
                                 % (pcm_rate, pcm_channels, sample_rate))

        # stage 2: encode
        encode = base + ["-i", pcm]
        if not keep_metadata:
            encode += ["-map_metadata", "-1"]
        if channels == 4:
            encode += ["-af", QUAD_FILL[quad_fill]]
        encode += ["-c:a", "libvorbis", "-b:a", str(bitrate), "-ar", str(sample_rate), candidate]
        _run(encode)

        info = probe(candidate)
        checks = [
            ("vorbis I", info.version == DIVA_VERSION, "version %d" % info.version),
            ("sample rate", info.sample_rate == sample_rate, "%d" % info.sample_rate),
            ("channels", info.channels == channels, "%d" % info.channels),
            ("framing bit", info.framing_bit == 1, str(info.framing_bit)),
            ("blocksize byte", info.blocksize_byte == DIVA_BLOCKSIZE, "0x%02X" % info.blocksize_byte),
            ("page CRC", info.crc_bad_pages == 0, "%d bad of %d pages" % (info.crc_bad_pages, info.pages)),
            ("page sequence", info.sequence_gaps == 0, "%d gap(s)" % info.sequence_gaps),
            ("stream closed (EOS page)", info.last_page_eos == 1, "last page EOS flag = %d" % info.last_page_eos),
            ("no torn last packet", info.trailing_incomplete_packet == 0,
             "last segment lacing value %s" % ("255 (packet continues past end of file)"
                                               if info.trailing_incomplete_packet else "ok")),
            ("length vs decoded source", abs(info.duration_samples - expected) <= tolerance_frames,
             "%d ogg vs %d pcm, delta %+d samples (tolerance %d)"
             % (info.duration_samples, expected, info.duration_samples - expected, tolerance_frames)),
        ]
        if src_samples is not None:
            delta = expected - src_samples
            frame_tolerance = max(tolerance_frames, sample_rate / 60.0)
            checks.append(("length vs source container", abs(delta) <= frame_tolerance,
                           "decoded %.6f s vs source %.6f s (%s), delta %+.6f s"
                           % (expected / float(sample_rate), src_samples / float(sample_rate),
                              src_how, delta / float(sample_rate))))
        if verify_roundtrip:
            back = os.path.join(workdir, "roundtrip.wav")
            _run(base + ["-i", candidate, "-c:a", "pcm_s16le", back])
            got, _r, _c = _wav_frames(back)
            checks.append(("decoded-back length", abs(got - expected) <= tolerance_frames,
                           "%d samples back vs %d in" % (got, expected)))

        failed = [name + ": " + detail for name, ok, detail in checks if not ok]
        report = {
            "src": src.replace("\\", "/"),
            "out_path": out.replace("\\", "/"),
            "ffmpeg": ff,
            "ffprobe": ffprobe,
            "channels": channels,
            "quad_fill": quad_fill if channels == 4 else None,
            "bitrate": str(bitrate),
            "decoded_source_samples": expected,
            "decoded_source_seconds": expected / float(sample_rate),
            "source_container_samples": src_samples,
            "source_container_seconds": (src_samples / float(sample_rate)) if src_samples is not None else None,
            "source_duration_method": src_how,
            "info": info,
            "checks": checks,
        }
        if failed:
            raise OggVorbisError("refusing to publish %s: %s\n(temp files kept in %s%s)"
                                 % (out.replace("\\", "/"), "; ".join(failed), workdir,
                                    "" if keep_temp else " - removed below"))
        if os.path.dirname(os.path.abspath(out)):
            os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        shutil.move(candidate, out)
        # re-probe what actually landed: the checks above were made on the scratch file, and a
        # move that silently truncated (network path, antivirus) must not be reported as verified.
        report["info"] = probe(out)
        report["out_size"] = os.path.getsize(out)
        if (report["info"].size != info.size or report["info"].duration_samples != info.duration_samples
                or report["info"].channels != info.channels or report["info"].crc_bad_pages):
            raise OggVorbisError("published %s but the re-probe of the file on disk differs from the "
                                 "checked scratch file: %s vs %s"
                                 % (out.replace("\\", "/"), report["info"].line(), info.line()))
        return report
    finally:
        if not keep_temp:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        for arg in sys.argv[1:]:
            info = probe(arg)
            print(arg)
            print("   ", info.line())
            print("    vendor=%r comments=%r" % (info.vendor, info.comments))
    else:
        print("usage: python audio_ogg.py <song.ogg> [...]", file=sys.stderr)
        print("ffmpeg: %s" % (find_ffmpeg(required=False) or "NOT FOUND"), file=sys.stderr)
