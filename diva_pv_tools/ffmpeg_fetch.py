"""Fetch a portable ffmpeg on demand into this add-on's `bin/` directory (Windows).

The add-on never ships the binary - the user's machine downloads it, on one explicit click,
from the build host recommended by ffmpeg.org, verifies it against the digest pinned below,
and unpacks only `ffmpeg.exe`, `ffprobe.exe` and `LICENSE`.  `audio_ogg.find_ffmpeg()`
probes `bin/` between PATH and the last-resort locations, so after a successful fetch the
music panel just works, and a manually "green installed" binary in the same directory is
found the same way.

The URL and the sha256 are a matched pair: the pinned build is immutable (versioned package,
not the floating one), and the fetch refuses a download whose digest differs.  A mirror can
replace only the URL, via $MMD2DIVA_FFMPEG_URL - never the verification.

usage:
    from diva_pv_tools import ffmpeg_fetch
    print(ffmpeg_fetch.bundled_path())            # installed binary, or ""
    temp = ffmpeg_fetch.download(progress=cb, cancel=cb)     # streaming, resumable off-thread
    path = ffmpeg_fetch.install(temp)                          # verify, unpack, publish
"""
import hashlib
import os
import shutil
import tempfile
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
BIN_DIR = os.path.join(HERE, "bin")

# The essentials build of ffmpeg 8.1.2 from gyan.dev - the Windows build host ffmpeg.org
# links.  It contains libvorbis (the encoder the DIVA song export needs), is static (so the
# exe is all `bin/` really needs), and the pair below is what its published checksum says.
DEFAULT_URL = ("https://www.gyan.dev/ffmpeg/builds/packages/"
               "ffmpeg-8.1.2-essentials_build.zip")
PINNED_SHA256 = "db580001caa24ac104c8cb856cd113a87b0a443f7bdf47d8c12b1d740584a2ec"

# Only these entries leave the archive; a fetch never writes anything else into the package.
# The license file inside the build is extensionless `LICENSE` at the archive root.
WANT = ("bin/ffmpeg.exe", "bin/ffprobe.exe", "LICENSE")

CHUNK = 1 << 16


class FetchError(RuntimeError):
    """A refused, cancelled or unverifiable fetch, or a host that cannot serve the download."""


def supported_platform():
    return os.name == "nt"


def source_url():
    return os.environ.get("MMD2DIVA_FFMPEG_URL") or DEFAULT_URL


def bundled_path():
    """The fetched ffmpeg.exe in `bin/`, or "" when nothing was installed there."""
    p = os.path.join(BIN_DIR, "ffmpeg.exe")
    return p if os.path.isfile(p) else ""


def download(cancel=None, progress=None):
    """Stream the pinned zip to a temp file; return (path, total_bytes).

    `progress(done, total)` is called per chunk (total is the Content-Length, 0 if the server
    hid it); `cancel()` is polled per chunk, and a true answer deletes the partial file and
    raises.  The caller owns the temp file and must pass it to `install()` or delete it.
    """
    url = source_url()
    req = urllib.request.Request(url, headers={"User-Agent": "diva_pv_tools"})
    try:
        resp = urllib.request.urlopen(req, timeout=30)
    except OSError as exc:
        raise FetchError("could not reach %s (%s)" % (url, exc))
    try:
        total = int(resp.headers.get("Content-Length") or 0)
    except ValueError:
        total = 0
    fd, temp = tempfile.mkstemp(prefix="diva_ffmpeg_", suffix=".zip")
    done = 0
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                if cancel is not None and cancel():
                    raise FetchError("the ffmpeg download was cancelled")
                blob = resp.read(CHUNK)
                if not blob:
                    break
                out.write(blob)
                done += len(blob)
                if progress is not None:
                    progress(done, total)
    except Exception:
        _drop(temp)
        raise
    finally:
        try:
            resp.close()
        except OSError:
            pass
    if total and done != total:
        _drop(temp)
        raise FetchError("the download ended short (%d of %d bytes)" % (done, total))
    return temp, done


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for blob in iter(lambda: fh.read(1 << 20), b""):
            digest.update(blob)
    return digest.hexdigest()


def install(temp_path, expected_sha256=None):
    """Verify the downloaded zip and publish `WANT` into `bin/`; return the ffmpeg.exe path.

    The digest check is unconditional: it compares against the pin in this module (or the
    caller's override), not against anything the download itself carried.  Publishing is
    all-or-nothing - every entry is unpacked to a staging directory first, and only a
    complete set of the wanted files is moved into `bin/`, so a cancelled or failed install
    never leaves a half-populated `bin/` behind.
    """
    want = expected_sha256 or PINNED_SHA256
    got = sha256_of(temp_path)
    if got.lower() != want.lower():
        _drop(temp_path)
        raise FetchError("checksum mismatch: expected %s, the download is %s - nothing installed"
                         % (want[:16] + "...", got[:16] + "..."))
    stage = tempfile.mkdtemp(prefix="diva_ffmpeg_stage_")
    try:
        with zipfile.ZipFile(temp_path) as zf:
            found = {}
            for info in zf.infolist():
                base = info.filename.replace("\\", "/")
                for name in WANT:
                    # the archive nests one version directory; match on the trailing path
                    if base.lower().endswith("/" + name.lower()) or base.lower() == name.lower():
                        if not info.is_dir():
                            found[name] = info
            missing = [n for n in WANT if n not in found]
            if missing:
                raise FetchError("the archive lacks %s - nothing installed" % ", ".join(missing))
            for name in WANT:
                out = os.path.join(stage, os.path.basename(name))
                with zf.open(found[name]) as src, open(out, "wb") as dst:
                    while True:
                        blob = src.read(1 << 20)
                        if not blob:
                            break
                        dst.write(blob)
        os.makedirs(BIN_DIR, exist_ok=True)
        for name in WANT:
            # flat inside `bin/`: BIN_DIR *is* the archive's `bin/`, so joining the two
            # would install to bin/bin/
            staged = os.path.join(stage, os.path.basename(name))
            _move_over(staged, os.path.join(BIN_DIR, os.path.basename(name)))
    finally:
        _drop(stage)
        _drop(temp_path)
    return os.path.join(BIN_DIR, "ffmpeg.exe")


def fetch(cancel=None, progress=None):
    """`download()` then `install()`; one call for a background session.  Returns the path."""
    temp, _bytes = download(cancel=cancel, progress=progress)
    return install(temp)


def _move_over(src, dst):
    # `shutil.move`, not `os.rename`: the staging directory is on the system drive and the
    # add-on may not be, and a rename across drives is the one thing os.rename refuses.  An
    # existing install is replaced, never merged into.
    if os.path.isfile(dst):
        os.remove(dst)
    shutil.move(src, dst)


def _drop(path):
    try:
        if os.path.isdir(path):
            import shutil
            shutil.rmtree(path, ignore_errors=True)
        elif os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass
