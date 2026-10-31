#!/usr/bin/env python3
"""Build Phira chart packages from Phigros_Resource and emit data/catalog.json.

The pipeline turns the community resource dump at
https://github.com/7aGiven/Phigros_Resource (orphan branches ``info``,
``chart``, ``music``, ``illustration``, ``illustrationLowRes``) into

  data/charts/<chartId>.zip        one self-contained Phira chart package
  data/images/<songHash>.jpg       illustration (list thumbnail + bg), <=1280px
  data/images/<songHash>_preview.ogg  15 s audio preview for the chart list
  data/catalog.json                the JSON consumed by our resource server
  data/build-manifest.json         resumability / stable-id bookkeeping

Design goals
  * deterministic  - fixed zip metadata, stable ordering, SOURCE_DATE_EPOCH support
  * resumable      - every remote fetch and every derived file is cached; zips are
                     rebuilt only when their input fingerprint changes
  * parameterized  - see --help / pipeline/README.md

No network access is required for a re-run that only rebuilds local artifacts;
only missing source files are downloaded (raw.githubusercontent.com, no API).
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - broken environment
    sys.exit("PyYAML is required for info.yml validation (pip install pyyaml)")

# --------------------------------------------------------------------------- #
# Paths / constants
# --------------------------------------------------------------------------- #
PIPELINE_DIR = Path(__file__).resolve().parent
# Repo root resolved from the script location (works for phira-mitm/ locally and
# for a fresh GitHub checkout where the repo root is the cwd); never a
# machine-specific absolute path.
ROOT = PIPELINE_DIR.parent
DEFAULT_DATA_DIR = ROOT / "data"

# Configured data/output directories (see configure_paths()); defaults are
# applied at import time so validate.py and library callers keep working.
DATA: Path = DEFAULT_DATA_DIR
RAW: Path = DATA / "raw"
RAW_INFO: Path = RAW / "info"
RAW_CHART: Path = RAW / "chart"
RAW_MUSIC: Path = RAW / "music"
RAW_ILL: Path = RAW / "illustration"
RAW_ILL_LOW: Path = RAW / "illustrationLowRes"
CHARTS: Path = DATA / "charts"
IMAGES: Path = DATA / "images"
MANIFEST_PATH: Path = DATA / "build-manifest.json"
CATALOG_PATH: Path = DATA / "catalog.json"
REPORT_PATH: Path = DATA / "validate-report.json"


def configure_paths(data_dir: str | os.PathLike | None = None) -> Path:
    """Resolve every data/output path used by the pipeline.

    Default: ``<repo root>/data`` where the repo root is inferred from this
    file's location (``pipeline/build.py`` -> ``..``). ``--data-dir`` overrides
    that for a fully relocatable cache/output tree (raw sources, zips, images,
    catalog, manifest, report all move together). Must be called before any
    filesystem access; main() does this from the CLI.
    """
    global DATA, RAW, RAW_INFO, RAW_CHART, RAW_MUSIC, RAW_ILL, RAW_ILL_LOW
    global CHARTS, IMAGES, MANIFEST_PATH, CATALOG_PATH, REPORT_PATH
    DATA = Path(data_dir).expanduser().resolve() if data_dir else DEFAULT_DATA_DIR
    RAW = DATA / "raw"
    RAW_INFO = RAW / "info"
    RAW_CHART = RAW / "chart"
    RAW_MUSIC = RAW / "music"
    RAW_ILL = RAW / "illustration"
    RAW_ILL_LOW = RAW / "illustrationLowRes"
    CHARTS = DATA / "charts"
    IMAGES = DATA / "images"
    MANIFEST_PATH = DATA / "build-manifest.json"
    CATALOG_PATH = DATA / "catalog.json"
    REPORT_PATH = DATA / "validate-report.json"
    return DATA


configure_paths()

RAW_BASE = "https://raw.githubusercontent.com/7aGiven/Phigros_Resource"
LEVELS = ["EZ", "HD", "IN", "AT"]

# Generated chart-package parameters (see README for the rationale).
ASPECT_RATIO = 16.0 / 9.0       # Phigros charts are authored for 16:9
BACKGROUND_DIM = 0.6            # prpr ChartInfo default
LINE_LENGTH = 6.0               # prpr ChartInfo default
DEFAULT_PREVIEW_START = 30.0    # seconds into the song
PREVIEW_DURATION = 15.0         # seconds
IMAGE_MAX_DIM = 1280            # longest side of the emitted JPEG illustration
IMAGE_JPEG_QUALITY = 80
ZIP_DATE_TIME = (2026, 1, 1, 0, 0, 0)   # fixed timestamp -> deterministic zips
ZIP_FILE_MODE = 0o644
# Bump when media derivation (encoder flags/params) changes so cached previews
# and JPEGs are regenerated even if the upstream sources did not change.
MEDIA_VERSION = "media-v3-vorbis-fallback"
UPLOADER_ID = 1                 # fake user used by the replacement server
TAGS = ["Phigros"]

USER_AGENT = "phira-mitm-resource-pipeline/1.0"
MB = 1024 * 1024

# python.org macOS builds ship without a usable trust store; prefer certifi.
try:
    import certifi as _certifi

    _SSL_CONTEXT = ssl.create_default_context(cafile=_certifi.where())
except Exception:  # noqa: BLE001
    _SSL_CONTEXT = ssl.create_default_context()

_QUIET = False


def log(msg: str) -> None:
    if not _QUIET:
        print(msg, flush=True)


def warn(msg: str) -> None:
    print(f"WARNING: {msg}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# Small utilities
# --------------------------------------------------------------------------- #
def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def song_hash(song_key: str) -> str:
    """Stable ASCII id used for image/preview filenames (song keys contain CJK)."""
    return hashlib.sha1(song_key.encode("utf-8")).hexdigest()[:12]


def rfc3339(epoch: float | None = None) -> str:
    epoch = epoch if epoch is not None else now_epoch()
    return _dt.datetime.fromtimestamp(int(epoch), _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_epoch() -> float:
    """Deterministic clock: honour SOURCE_DATE_EPOCH when set."""
    src = os.environ.get("SOURCE_DATE_EPOCH")
    if src:
        try:
            return float(src)
        except ValueError:
            pass
    return _dt.datetime.now(_dt.timezone.utc).timestamp()


def fmt_float(value: float) -> str:
    """Stable float formatting for JSON/YAML (12.6 -> '12.6', 15.0 -> '15.0')."""
    return repr(float(value))


def human_bytes(n: int | float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} GB"


def load_json(path: Path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def atomic_write_json(path: Path, obj) -> None:
    atomic_write_bytes(path, (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


# --------------------------------------------------------------------------- #
# External tooling (ffmpeg/ffprobe mandatory; sips optional, ffmpeg fallback)
# --------------------------------------------------------------------------- #
def _tool(name: str) -> str | None:
    return shutil.which(name)


def require_tools(need_encoding: bool = True) -> None:
    """Fail early and clearly when a CI runner lacks required binaries.

    ``ffprobe``+``ffmpeg`` are mandatory for music probing / preview cutting
    (and ffmpeg also covers JPEG conversion when macOS ``sips`` is absent, so
    the pipeline runs on Linux runners too). ``sips`` is optional.
    """
    required = ["ffmpeg", "ffprobe"] if need_encoding else ["ffprobe"]
    missing = [name for name in required if _tool(name) is None]
    if missing:
        sys.exit(
            "missing required tool(s) on PATH: " + ", ".join(missing) + "\n"
            "  install: macOS `brew install ffmpeg` (sips is built in); "
            "Ubuntu `sudo apt-get install -y ffmpeg`"
        )
    if need_encoding and _tool("sips") is None:
        log("      note: `sips` not found - illustration downscaling will use ffmpeg")


# --------------------------------------------------------------------------- #
# Remote fetching
# --------------------------------------------------------------------------- #
class NotFound(Exception):
    """Asset does not exist on the remote branch (HTTP 404)."""


def raw_url(branch: str, rel_path: str) -> str:
    return f"{RAW_BASE}/{branch}/" + urllib.parse.quote(rel_path, safe="/")


def http_fetch(url: str, dest: Path, retries: int = 3) -> Path:
    """Download ``url`` to ``dest`` unless already cached. Atomic write."""
    if dest.is_file() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=90, context=_SSL_CONTEXT) as resp:
                expected = resp.headers.get("Content-Length")
                with open(tmp, "wb") as f:
                    shutil.copyfileobj(resp, f, 1 << 20)
            got = tmp.stat().st_size
            if got == 0:
                raise IOError(f"empty response for {url}")
            if expected is not None and got != int(expected):
                raise IOError(f"truncated response for {url}: {got}/{expected} bytes")
            os.replace(tmp, dest)
            return dest
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                tmp.unlink(missing_ok=True)
                raise NotFound(url) from None
            last_err = exc
        except Exception as exc:  # network hiccup / timeout
            last_err = exc
        if attempt < retries:
            import time

            time.sleep(min(2 ** attempt, 8))
    tmp.unlink(missing_ok=True)
    raise IOError(f"failed to fetch {url}: {last_err}")


def fetch_checked(url: str, dest: Path, validator, kind: str) -> Path:
    """Download ``url`` and reject obviously corrupted payloads (redownload once).

    ``validator`` receives the local Path and returns a reason string when the
    file is bad, or None when it looks complete. This guards against truncated
    upstream responses (raw.githubusercontent sometimes drops connections) and
    against stale corrupted files in the cache.
    """
    attempts = 2
    last_reason = ""
    for attempt in range(attempts):
        if attempt and dest.exists():
            log(f"      re-downloading corrupted {kind}: {dest.name} ({last_reason})")
            dest.unlink(missing_ok=True)
        try:
            http_fetch(url, dest)
        except NotFound:
            raise
        if dest.is_file():
            last_reason = validator(dest) or ""
            if not last_reason:
                return dest
            if attempt == attempts - 1:
                raise IOError(f"corrupted {kind} {dest.name}: {last_reason}")
            continue
        last_reason = "file missing after download"
    raise IOError(f"could not fetch valid {kind}: {url}")


def validate_chart_bytes(path: Path) -> str | None:
    try:
        chart = json.loads(path.read_bytes().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return f"not JSON: {exc}"
    if not isinstance(chart, dict) or not isinstance(chart.get("judgeLineList"), list):
        return "missing judgeLineList"
    return None


def validate_music_ogg(path: Path) -> str | None:
    with open(path, "rb") as f:
        head = f.read(4)
    if head != b"OggS":
        return "not an Ogg stream"
    try:
        duration = _ffprobe_duration(path)
    except Exception as exc:  # noqa: BLE001
        return f"ffprobe failed: {exc}"
    if duration < 20:
        return f"implausibly short for a full song ({duration:.1f}s)"
    return None


def validate_png(path: Path) -> str | None:
    with open(path, "rb") as f:
        if f.read(8) != b"\x89PNG\r\n\x1a\n":
            return "not a PNG"
        f.seek(-16, os.SEEK_END)
        if b"IEND" not in f.read():
            return "truncated PNG (missing IEND chunk)"
    return None


def ensure_info_files(force: bool = False) -> None:
    for name in ("info.tsv", "difficulty.tsv"):
        dest = RAW_INFO / name
        try:
            if force:
                dest.unlink(missing_ok=True)
            http_fetch(raw_url("info", name), dest)
        except (NotFound, IOError) as exc:
            if dest.is_file():
                warn(f"using cached {dest} ({exc})")
            else:
                raise


# --------------------------------------------------------------------------- #
# Metadata model
# --------------------------------------------------------------------------- #
def parse_info(raw_info: Path = RAW_INFO) -> list[dict]:
    """Parse info.tsv + difficulty.tsv into an ordered song list.

    info.tsv columns:  songKey, name, composer, illustrator, charter[EZ], charter[HD], ...
    difficulty.tsv:    songKey, EZ, HD, IN, [AT]   (levels present = rating count)
    """
    info: dict[str, dict] = {}
    with open(raw_info / "info.tsv", "r", encoding="utf-8", newline="") as f:
        for line in f:
            line = line.rstrip("\r\n")
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) < 4:
                continue
            info[parts[0]] = {
                "key": parts[0],
                "name": parts[1],
                "composer": parts[2],
                "illustrator": parts[3],
                "charters": parts[4:],
            }

    difficulties: dict[str, list[str]] = {}
    with open(raw_info / "difficulty.tsv", "r", encoding="utf-8", newline="") as f:
        for line in f:
            line = line.rstrip("\r\n")
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) >= 2:
                difficulties[parts[0]] = parts[1:]

    songs: list[dict] = []
    for key, meta in info.items():
        ratings = difficulties.get(key, [])
        levels: list[str] = []
        values: list[float] = []
        raw_values: list[str] = []
        for idx, raw in enumerate(ratings[: len(LEVELS)]):
            try:
                value = float(raw)
            except ValueError:
                continue
            if value <= 0:
                continue
            levels.append(LEVELS[idx])
            values.append(value)
            raw_values.append(raw.strip())
        if not levels:
            continue
        charters = meta["charters"]
        song = dict(meta)
        song["levels"] = levels
        song["ratings"] = values
        song["ratingStrings"] = raw_values
        song["chartersByLevel"] = {
            lv: (charters[i] if i < len(charters) else (charters[-1] if charters else ""))
            for i, lv in enumerate(levels)
        }
        songs.append(song)
    return songs


def plan_entries(songs: list[dict], count: int | None, levels: list[str],
                 max_entries: int) -> list[dict]:
    """Deterministically select (song, level) chart entries in info.tsv order.

    ID ORDERING RULE (stable across runs; do not change casually):
      song-major - for each song in info.tsv order, emit its requested
      difficulties in canonical EZ,HD,IN,AT order.
    A fresh ``--levels IN,AT`` build therefore interleaves, e.g.
      990001 Glaciaxion IN, 990002 Eradication Catastrophe IN,
      990003 Credits IN, 990004 Credits AT, 990005 Dlyrotz IN, ...
    The live tunnel dataset was built incrementally (IN block 990001-990327,
    then AT appended 990328-990383), so its ids differ, but each build is
    contiguous from ``--start-id`` and deterministic for the same selection.
    """
    plans: list[dict] = []
    seen_songs = 0
    for song in songs:
        if count is not None and seen_songs >= count:
            break
        if not any(lv in song["levels"] for lv in levels):
            continue
        seen_songs += 1
        for lv in levels:                      # fixed canonical order per song
            if lv not in song["levels"]:
                continue
            idx = song["levels"].index(lv)
            plans.append({
                "song": song,
                "songKey": song["key"],
                "level": lv,
                "difficulty": song["ratings"][idx],
                "ratingString": song["ratingStrings"][idx],
                "charter": song["chartersByLevel"][lv],
            })
            if max_entries and len(plans) >= max_entries:
                return plans
    return plans


# --------------------------------------------------------------------------- #
# Source asset fetching
# --------------------------------------------------------------------------- #
def fetch_assets(entries: list[dict], illustration_source: str, jobs: int) -> None:
    """Download/cache all raw assets needed by ``entries`` (in-place annotations).

    Adds to every entry:  entry["chartRaw"] -> Path
    Adds to every song:   song["musicRaw"], song["illustrationRaw"]
    Entries/songs whose assets are unavailable get an ``error`` field instead.
    """
    entries_by_song: dict[str, list[dict]] = {}
    for entry in entries:
        entries_by_song.setdefault(entry["songKey"], []).append(entry)

    def fetch_chart(entry: dict) -> None:
        rel = f"{entry['songKey']}.0/{entry['level']}.json"
        dest = RAW_CHART / rel
        try:
            fetch_checked(raw_url("chart", rel), dest, validate_chart_bytes, "chart")
            entry["chartRaw"] = dest
        except NotFound:
            entry["error"] = f"chart not found ({rel})"
        except Exception as exc:  # noqa: BLE001
            entry["error"] = f"chart fetch failed: {exc}"

    def fetch_music(song_key: str) -> None:
        rel = f"{song_key}.ogg"
        dest = RAW_MUSIC / rel
        try:
            fetch_checked(raw_url("music", rel), dest, validate_music_ogg, "music")
            for entry in entries_by_song.get(song_key, []):
                entry["songMusicRaw"] = dest
                entry["song"]["musicRaw"] = dest
        except NotFound:
            for entry in entries_by_song.get(song_key, []):
                entry["error"] = f"music not found ({rel})"
        except Exception as exc:  # noqa: BLE001
            for entry in entries_by_song.get(song_key, []):
                entry["error"] = f"music fetch failed: {exc}"

    def fetch_illustration(song: dict) -> None:
        song_key = song["key"]
        candidates: list[tuple[str, str, Path]] = []
        if illustration_source in ("full", "auto"):
            candidates.append(("illustration", f"{song_key}.png", RAW_ILL / f"{song_key}.png"))
        if illustration_source in ("lowres", "auto"):
            candidates.append(("illustrationLowRes", f"{song_key}.png",
                               RAW_ILL_LOW / f"{song_key}.png"))
            for lv in song["levels"]:
                candidates.append(("illustrationLowRes", f"{song_key}_{lv}.png",
                                   RAW_ILL_LOW / f"{song_key}_{lv}.png"))
        last: Exception | None = None
        for branch, rel, dest in candidates:
            try:
                fetch_checked(raw_url(branch, rel), dest, validate_png, "illustration")
                song["illustrationRaw"] = dest
                for entry in entries_by_song.get(song_key, []):
                    entry["songIllustrationRaw"] = dest
                return
            except NotFound as exc:
                last = exc
                continue
            except Exception as exc:  # noqa: BLE001
                last = exc
                continue
        for entry in entries_by_song.get(song_key, []):
            entry["error"] = f"illustration not found: {last}"

    tasks = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        for entry in entries:
            tasks.append(pool.submit(fetch_chart, entry))
        for song_key in entries_by_song:
            song = next(e["song"] for e in entries_by_song[song_key])
            tasks.append(pool.submit(fetch_music, song_key))
            tasks.append(pool.submit(fetch_illustration, song))
        for task in concurrent.futures.as_completed(tasks):
            exc = task.exception()
            if exc is not None:
                warn(f"fetch task crashed: {exc}")

    # Drop entries that are not buildable.
    ok = []
    for entry in entries:
        if "error" in entry:
            warn(f"skip {entry['songKey']} [{entry['level']}]: {entry['error']}")
            continue
        ok.append(entry)
    entries[:] = ok


# --------------------------------------------------------------------------- #
# Media derivation (illustration JPEG + preview ogg)
# --------------------------------------------------------------------------- #
def _sips_dims(path: Path) -> tuple[int, int] | None:
    try:
        out = subprocess.run(
            ["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)],
            check=True, capture_output=True, text=True,
        ).stdout
        w = int(re.search(r"pixelWidth:\s*(\d+)", out).group(1))
        h = int(re.search(r"pixelHeight:\s*(\d+)", out).group(1))
        return w, h
    except Exception:  # noqa: BLE001
        return None


def make_illustration(src: Path, dest: Path, force: bool = False,
                      max_dim: int = IMAGE_MAX_DIM,
                      quality: int = IMAGE_JPEG_QUALITY) -> None:
    """Convert a PNG illustration into a small JPEG (never upscales).

    Uses macOS ``sips`` when available; otherwise falls back to ffmpeg so the
    pipeline runs unchanged on Linux runners (image bytes may differ between the
    two toolchains, which is fine for a fresh dataset).
    """
    if dest.is_file() and dest.stat().st_size > 0 and not force:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp.jpg")
    if _tool("sips") is not None:
        dims = _sips_dims(src)
        cmd = ["sips", "-s", "format", "jpeg", "-s", "formatOptions", str(quality)]
        if dims and max(dims) > max_dim:
            cmd += ["-Z", str(max_dim)]
        cmd += [str(src), "--out", str(tmp)]
    else:
        # ffmpeg scale filter never upscales: min(max_dim, iw); keep aspect even.
        cmd = ["ffmpeg", "-v", "error", "-y", "-i", str(src),
               "-vf", f"scale='min({max_dim},iw)':-2",
               "-q:v", "3", str(tmp)]
    subprocess.run(cmd, check=True, capture_output=True)
    os.replace(tmp, dest)


def _ffprobe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return float(out)


def make_preview(src: Path, dest: Path, force: bool = False,
                 start_pref: float = DEFAULT_PREVIEW_START,
                 seconds: float = PREVIEW_DURATION) -> tuple[float, float]:
    """Cut a short Ogg Vorbis preview out of the full song. Returns (start, end)."""
    duration = _ffprobe_duration(src)
    start = 0.0
    if duration > start_pref + seconds:
        start = start_pref
    elif duration > seconds * 2:
        start = max(0.0, duration * 0.25)
    end = min(duration, start + seconds)
    if dest.is_file() and dest.stat().st_size > 0 and not force:
        return round(start, 3), round(end, 3)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp.ogg")
    try:
        # Primary: re-encode a short vorbis clip (small, deterministic).
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y",
             "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}",
             "-i", str(src), "-map_metadata", "-1", "-vn",
             "-c:a", "vorbis", "-strict", "-2", "-q:a", "2",
             "-fflags", "+bitexact", "-flags", "+bitexact",
             str(tmp)],
            check=True, capture_output=True,
        )
    except subprocess.CalledProcessError:
        # ffmpeg's experimental native vorbis encoder asserts (floor_encode) on
        # some 48 kHz sources; fall back to an Ogg page-boundary stream copy of
        # a 15 s segment. Bigger (~430 KB) but always decodable.
        warn(f"vorbis re-encode failed for {src.name}; using stream-copy preview")
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y",
             "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}",
             "-i", str(src), "-vn", "-c:a", "copy", str(tmp)],
            check=True, capture_output=True,
        )
    os.replace(tmp, dest)
    try:
        got = _ffprobe_duration(dest)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"preview for {src.name} is not decodable: {exc}") from exc
    if got < 3:
        raise RuntimeError(f"preview for {src.name} implausibly short ({got:.2f}s)")
    return round(start, 3), round(end, 3)


def derive_media(entries: list[dict], force: bool, jobs: int,
                 manifest: dict, image_max_dim: int = IMAGE_MAX_DIM,
                 image_quality: int = IMAGE_JPEG_QUALITY,
                 preview_start: float = DEFAULT_PREVIEW_START,
                 preview_seconds: float = PREVIEW_DURATION) -> dict[str, dict]:
    """Create data/images/<hash>.jpg and <hash>_preview.ogg for each selected song.

    ``manifest["songs"]`` remembers which source assets a derived pair was made
    from, so changed upstream files invalidate the cached JPEG/preview.
    """
    songs: dict[str, dict] = {}
    for entry in entries:
        songs.setdefault(entry["songKey"], entry["song"])
    song_records = manifest.setdefault("songs", {})

    def derive(item: tuple[str, dict]) -> tuple[str, dict, str | None]:
        song_key, song = item
        h = song_hash(song_key)
        img = IMAGES / f"{h}.jpg"
        prev = IMAGES / f"{h}_preview.ogg"
        record = song_records.setdefault(song_key, {})
        src_hash = ":".join((
            MEDIA_VERSION,
            sha256_file(song["illustrationRaw"]),
            sha256_file(song["musicRaw"]),
            str(image_max_dim), str(image_quality),
            f"{preview_start:g}", f"{preview_seconds:g}",
        ))
        try:
            regenerate = (force or record.get("srcHash") != src_hash
                          or not img.is_file() or not prev.is_file())
            if regenerate:
                make_illustration(song["illustrationRaw"], img, force=True,
                                  max_dim=image_max_dim, quality=image_quality)
                start, end = make_preview(song["musicRaw"], prev, force=True,
                                          start_pref=preview_start, seconds=preview_seconds)
                record["srcHash"] = src_hash
                record["image"] = img.name
                record["preview"] = prev.name
                record["previewStart"] = start
                record["previewEnd"] = end
            else:
                start = float(record.get("previewStart", 0.0))
                end = float(record.get("previewEnd", PREVIEW_DURATION))
            return song_key, {"hash": h, "image": img, "preview": prev,
                              "previewStart": start, "previewEnd": end}, None
        except subprocess.CalledProcessError as exc:
            return song_key, {}, f"media derivation failed: {exc.stderr.decode(errors='replace')[-300:]}"
        except Exception as exc:  # noqa: BLE001
            return song_key, {}, f"media derivation failed: {exc}"

    media: dict[str, dict] = {}
    IMAGES.mkdir(parents=True, exist_ok=True)
    failures: dict[str, str] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(jobs, 6))) as pool:
        for song_key, info, err in pool.map(derive, songs.items()):
            if err:
                failures[song_key] = err
            else:
                media[song_key] = info
    for song_key, err in failures.items():
        warn(f"skip {song_key}: {err}")
    entries[:] = [e for e in entries if e["songKey"] in media]
    return media


# --------------------------------------------------------------------------- #
# Chart package (zip) construction
# --------------------------------------------------------------------------- #
def make_info_yml(entry: dict, media: dict) -> bytes:
    """Render the Phira ChartInfo YAML (camelCase, prpr::info::ChartInfo)."""
    song = entry["song"]
    level_text = f"{entry['level']} Lv.{entry['ratingString']}"
    lines = [
        f"name: {json.dumps(song['name'], ensure_ascii=False)}",
        f"difficulty: {fmt_float(entry['difficulty'])}",
        f"level: {json.dumps(level_text, ensure_ascii=False)}",
        f"charter: {json.dumps(entry['charter'], ensure_ascii=False)}",
        f"composer: {json.dumps(song['composer'], ensure_ascii=False)}",
        f"illustrator: {json.dumps(song['illustrator'], ensure_ascii=False)}",
        'chart: "chart.json"',
        'format: "pgr"',
        'music: "music.ogg"',
        'illustration: "illustration.jpg"',
        f"previewStart: {fmt_float(media['previewStart'])}",
        f"previewEnd: {fmt_float(media['previewEnd'])}",
        f"aspectRatio: {fmt_float(ASPECT_RATIO)}",
        f"backgroundDim: {fmt_float(BACKGROUND_DIM)}",
        f"lineLength: {fmt_float(LINE_LENGTH)}",
        "offset: 0.0",
        f"tags: {json.dumps(TAGS, ensure_ascii=False)}",
        'intro: ""',
        "holdPartialCover: false",
        "noteUniformScale: false",
        "forceAspectRatio: false",
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def zip_add(zf: zipfile.ZipFile, name: str, data: bytes, compress: bool) -> None:
    info = zipfile.ZipInfo(name, ZIP_DATE_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    info.external_attr = (ZIP_FILE_MODE & 0xFFFF) << 16
    info.create_system = 3  # unix
    zf.writestr(info, data, compresslevel=9 if compress else None)


def entry_fingerprint(entry: dict, media: dict, info_yml: bytes) -> str:
    h = hashlib.sha256()
    h.update(info_yml)
    # The preview clip is not embedded in the zip (only its start/end are, via
    # info.yml), so it is intentionally not part of the fingerprint.
    for path in (entry["chartRaw"], entry["songMusicRaw"], media["image"]):
        h.update(b"\0")
        h.update(sha256_file(path).encode())
    return h.hexdigest()


def assign_ids(entries: list[dict], manifest: dict, start_id: int, reset_ids: bool) -> None:
    """Give every entry a stable local chart id tracked in the manifest."""
    if reset_ids:
        manifest["entries"] = {}
        manifest["next_id"] = start_id
    next_id = max(int(manifest.get("next_id", start_id)), start_id)
    store = manifest.setdefault("entries", {})
    for entry in entries:
        key = f"{entry['songKey']}|{entry['level']}"
        entry["stableKey"] = key
        if key in store and isinstance(store[key].get("id"), int):
            entry["id"] = store[key]["id"]
        else:
            entry["id"] = next_id
            next_id += 1
            store[key] = {"id": entry["id"]}
    manifest["next_id"] = next_id


def build_packages(entries: list[dict], media: dict[str, dict], manifest: dict,
                   force: bool, limit_mb: float) -> list[dict]:
    """Create one standalone zip per entry. Returns list of built/reused build records."""
    CHARTS.mkdir(parents=True, exist_ok=True)
    store = manifest.setdefault("entries", {})
    builds: list[dict] = []
    total = 0
    for entry in entries:
        info_yml = make_info_yml(entry, media[entry["songKey"]])
        fingerprint = entry_fingerprint(entry, media[entry["songKey"]], info_yml)
        zip_path = CHARTS / f"{entry['id']}.zip"
        record = store[entry["stableKey"]]
        record["fingerprint"] = fingerprint
        if record.get("created") is None:
            record["created"] = rfc3339()
        if limit_mb and total >= limit_mb * MB:
            warn(f"limit {limit_mb:g} MB reached; not building {entry['id']}")
            continue
        if (not force and zip_path.is_file() and zip_path.stat().st_size > 0
                and record.get("zipFingerprint") == fingerprint):
            size = zip_path.stat().st_size
            total += size
            builds.append({"entry": entry, "zip": zip_path, "size": size, "reused": True})
            continue

        tmp = CHARTS / f".{entry['id']}.zip.tmp"
        chart_bytes = entry["chartRaw"].read_bytes()
        music_bytes = entry["songMusicRaw"].read_bytes()
        image_bytes = media[entry["songKey"]]["image"].read_bytes()
        with zipfile.ZipFile(tmp, "w") as zf:
            zip_add(zf, "info.yml", info_yml, compress=True)
            zip_add(zf, "chart.json", chart_bytes, compress=True)
            zip_add(zf, "music.ogg", music_bytes, compress=False)
            zip_add(zf, "illustration.jpg", image_bytes, compress=False)
        os.replace(tmp, zip_path)
        size = zip_path.stat().st_size
        total += size
        record["zipFingerprint"] = fingerprint
        record["size"] = size
        record["builtAt"] = rfc3339()
        builds.append({"entry": entry, "zip": zip_path, "size": size, "reused": False,
                       "chartSize": len(chart_bytes), "musicSize": len(music_bytes),
                       "imageSize": len(image_bytes)})
        log(f"  built {entry['id']} {entry['songKey']} [{entry['level']}] "
            f"{human_bytes(size)} (total {human_bytes(total)})")
    return builds


# --------------------------------------------------------------------------- #
# Catalog
# --------------------------------------------------------------------------- #
def build_catalog(builds: list[dict], manifest: dict, base_url: str, uploader: int,
                  version: int | None) -> dict:
    now = rfc3339()
    store = manifest.get("entries", {})
    catalog_entries = []
    for build in sorted(builds, key=lambda b: b["entry"]["id"]):
        entry = build["entry"]
        song = entry["song"]
        h = song_hash(entry["songKey"])
        record = store.get(entry["stableKey"], {})
        created = record.get("created") or now
        catalog_entries.append({
            "id": entry["id"],
            "name": song["name"],
            "level": f"{entry['level']} Lv.{entry['ratingString']}",
            "difficulty": round(entry["difficulty"], 2),
            "charter": entry["charter"],
            "composer": song["composer"],
            "illustrator": song["illustrator"],
            "description": None,
            "ranked": False,
            "reviewed": True,
            "stable": False,
            "stableRequest": False,
            "illustration": f"{base_url}/img/{h}.jpg",
            "preview": f"{base_url}/img/{h}_preview.ogg",
            "file": f"{base_url}/files/{entry['id']}.zip",
            "uploader": uploader,
            "tags": list(TAGS),
            "rating": None,
            "created": created,
            "updated": now,
            "chartUpdated": now,
        })

    previous = load_json(CATALOG_PATH, None)
    if version is None:
        version = (int(previous.get("version", 0)) + 1) if isinstance(previous, dict) else 1
    catalog = {
        "version": version,
        "base": None,
        "count": len(catalog_entries),
        "entries": catalog_entries,
    }
    return catalog


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
_REQUIRED_CHART_KEYS = ("judgeLineDisappearEvents", "judgeLineRotateEvents",
                        "judgeLineMoveEvents", "speedEvents", "notesAbove", "notesBelow")


def validate_chart_json(data: bytes) -> list[str]:
    errors: list[str] = []
    try:
        chart = json.loads(data.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return [f"chart json parse error: {exc}"]
    if not isinstance(chart, dict):
        return ["chart json root is not an object"]
    version = chart.get("formatVersion")
    if version not in (1, 3):
        errors.append(f"unexpected formatVersion {version!r}")
    lines = chart.get("judgeLineList")
    if not isinstance(lines, list) or not lines:
        errors.append("judgeLineList missing/empty")
        return errors
    for i, line in enumerate(lines):
        if not isinstance(line, dict):
            errors.append(f"line {i} not an object")
            continue
        if "bpm" not in line:
            errors.append(f"line {i} missing bpm")
        for key in _REQUIRED_CHART_KEYS:
            if key not in line:
                errors.append(f"line {i} missing {key}")
        speed = line.get("speedEvents")
        if isinstance(speed, list) and not speed:
            errors.append(f"line {i} has empty speedEvents (Phira pgr parser crashes)")
    if not any(isinstance(l, dict) and (l.get("notesAbove") or l.get("notesBelow")) for l in lines):
        errors.append("chart has no notes at all")
    return errors


def validate_catalog(verbose: bool = True, write_report: bool = True, deep: bool = True) -> int:
    """Validate data/catalog.json against the files on disk. Returns #failures."""
    catalog = load_json(CATALOG_PATH, None)
    if not isinstance(catalog, dict):
        print(f"FAIL: {CATALOG_PATH} missing or unreadable", file=sys.stderr)
        return 1
    entries = catalog.get("entries") or []
    failures: list[dict] = []
    rows: list[dict] = []
    total_zip = 0
    for e in entries:
        errs: list[str] = []
        def fail(msg: str) -> None:
            errs.append(msg)

        # types
        for key, typ in (("id", int), ("uploader", int), ("difficulty", (int, float))):
            if not isinstance(e.get(key), typ) or isinstance(e.get(key), bool):
                fail(f"field {key} bad type")
        for key in ("name", "level", "charter", "composer", "illustrator",
                    "illustration", "preview", "file", "created", "updated", "chartUpdated"):
            if not isinstance(e.get(key), str) or not e[key]:
                fail(f"field {key} missing/empty")
        for key in ("ranked", "reviewed", "stable", "stableRequest"):
            if not isinstance(e.get(key), bool):
                fail(f"field {key} not bool")
        if not isinstance(e.get("tags"), list):
            fail("tags not a list")

        # URLs / files
        def basename(url: str) -> str:
            return url.rsplit("/", 1)[-1]
        zip_path = CHARTS / basename(e.get("file", ""))
        img_path = IMAGES / basename(e.get("illustration", ""))
        prev_path = IMAGES / basename(e.get("preview", ""))
        chart_bytes = music_bytes = image_bytes = b""
        chart_size = music_size = image_size = 0
        if not zip_path.is_file():
            fail(f"missing zip {zip_path.name}")
        else:
            zip_size = zip_path.stat().st_size
            total_zip += zip_size
            try:
                with zipfile.ZipFile(zip_path) as zf:
                    names = set(zf.namelist())
                    for need in ("info.yml", "chart.json", "music.ogg", "illustration.jpg"):
                        if need not in names:
                            fail(f"zip missing {need}")
                    bad = zf.testzip()
                    if bad:
                        fail(f"zip corrupt entry {bad}")
                    try:
                        info = yaml.safe_load(zf.read("info.yml"))
                    except Exception as exc:  # noqa: BLE001
                        info = None
                        fail(f"info.yml YAML error: {exc}")
                    if isinstance(info, dict):
                        for field in ("name", "difficulty", "level", "charter", "composer",
                                      "illustrator", "chart", "music", "illustration",
                                      "previewStart", "aspectRatio", "backgroundDim",
                                      "lineLength", "offset", "holdPartialCover",
                                      "noteUniformScale", "forceAspectRatio"):
                            if field not in info:
                                fail(f"info.yml missing {field}")
                        if info.get("format") != "pgr":
                            fail(f"info.yml format={info.get('format')!r} (want 'pgr')")
                        if info.get("chart") != "chart.json":
                            fail("info.yml chart path != chart.json")
                        if info.get("name") != e.get("name"):
                            fail("info.yml name != catalog name")
                        if info.get("level") != e.get("level"):
                            fail(f"info.yml level {info.get('level')!r} != {e.get('level')!r}")
                        if info.get("illustration") != "illustration.jpg":
                            fail("info.yml illustration path != illustration.jpg")
                    else:
                        fail("info.yml did not parse to a mapping")
                    if "chart.json" in names:
                        chart_bytes = zf.read("chart.json")
                        chart_size = len(chart_bytes)
                    tmp_music: str | None = None
                    if "music.ogg" in names:
                        music_size = zf.getinfo("music.ogg").file_size
                        with zf.open("music.ogg") as fh:
                            music_bytes = fh.read(4)
                        if deep:
                            tf = tempfile.NamedTemporaryFile(suffix=".ogg", delete=False)
                            tmp_music = tf.name
                            with zf.open("music.ogg") as fh:
                                shutil.copyfileobj(fh, tf)
                            tf.close()
                            try:
                                dur = _ffprobe_duration(Path(tmp_music))
                                if dur < 20:
                                    fail(f"music.ogg implausibly short ({dur:.1f}s)")
                            except Exception as exc:  # noqa: BLE001
                                fail(f"music.ogg not decodable: {exc}")
                            finally:
                                os.unlink(tmp_music)
                    if "illustration.jpg" in names:
                        with zf.open("illustration.jpg") as fh:
                            image_bytes = fh.read(3)
            except zipfile.BadZipFile:
                fail("not a valid zip")
            if chart_bytes:
                for msg in validate_chart_json(chart_bytes):
                    fail(f"chart: {msg}")
            if music_bytes and not music_bytes.startswith(b"OggS"):
                fail("music.ogg does not start with OggS")
            if image_bytes and not image_bytes.startswith(b"\xff\xd8\xff"):
                fail("illustration.jpg is not a JPEG")
            if img_path.is_file():
                image_size = img_path.stat().st_size
                head = img_path.read_bytes()[:3]
                if not head.startswith(b"\xff\xd8\xff"):
                    fail("data/images illustration is not JPEG")
            else:
                fail(f"missing illustration {img_path.name}")
            if not prev_path.is_file():
                fail(f"missing preview {prev_path.name}")
            else:
                with open(prev_path, "rb") as fh:
                    if not fh.read(4).startswith(b"OggS"):
                        fail("preview audio is not Ogg")
                if deep:
                    try:
                        pdur = _ffprobe_duration(prev_path)
                        if not 5 <= pdur <= 25:
                            fail(f"preview duration out of range ({pdur:.1f}s)")
                    except Exception as exc:  # noqa: BLE001
                        fail(f"preview not decodable: {exc}")
        rows.append({
            "id": e.get("id"), "level": e.get("level"), "zip": zip_size if zip_path.is_file() else 0,
            "chart": chart_size, "music": music_size, "image": image_size,
            "errors": errs,
        })
        if errs:
            failures.append({"id": e.get("id"), "level": e.get("level"), "errors": errs})

    ok = len(entries) - len(failures)
    if verbose:
        print(f"{'id':>7}  {'level':<14} {'zip':>9} {'chart(unz)':>11} {'img':>8}  ok")
        for row in rows:
            print(f"{row['id']:>7}  {row['level']:<14} {human_bytes(row['zip']):>9} "
                  f"{human_bytes(row['chart']):>11} {human_bytes(row['image']):>8}  "
                  f"{'yes' if not row['errors'] else 'NO'}")
            for msg in row["errors"]:
                print(f"         ^ {msg}")
        avg = total_zip / len(entries) if entries else 0
        print()
        print(f"catalog: {CATALOG_PATH}")
        print(f"entries: {len(entries)}  valid: {ok}  failures: {len(failures)}")
        print(f"zips total: {human_bytes(total_zip)}  avg: {human_bytes(avg)}")
        if DATA.is_dir():
            for sub in ("charts", "images"):
                d = DATA / sub
                if d.is_dir():
                    files = [p for p in d.iterdir() if p.is_file()]
                    print(f"{sub}/: {len(files)} files, {human_bytes(sum(p.stat().st_size for p in files))}")
    if write_report:
        atomic_write_json(REPORT_PATH, {
            "generated": rfc3339(),
            "catalog": str(CATALOG_PATH),
            "count": len(entries),
            "valid": ok,
            "failures": failures,
            "zipsTotalBytes": total_zip,
            "rows": [{k: v for k, v in r.items() if k != "errors"} for r in rows],
        })
    return len(failures)


# --------------------------------------------------------------------------- #
# High level phases
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Convert Phigros_Resource into Phira chart packages + data/catalog.json",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--songs", default="28",
                   help="number of songs (info.tsv order) to select, or 'all'")
    p.add_argument("--levels", default="IN",
                   help="comma separated difficulties to include, e.g. IN or EZ,HD,IN,AT")
    p.add_argument("--max-entries", type=int, default=0,
                   help="hard cap on number of chart entries (0 = no cap)")
    p.add_argument("--start-id", type=int, default=990001, help="first local chart id")
    p.add_argument("--reset-ids", action="store_true",
                   help="renumber all ids from --start-id (clears manifest id map)")
    p.add_argument("--base-url", default="{BASE}",
                   help="URL prefix for catalog resources; '{BASE}' is substituted by the server")
    p.add_argument("--uploader", type=int, default=UPLOADER_ID, help="catalog uploader user id")
    p.add_argument("--version", type=int, default=None, help="override catalog version")
    p.add_argument("--jobs", type=int, default=8,
                   help="parallel downloads (bounded; media derivation caps at 6 workers)")
    p.add_argument("--data-dir", default=None,
                   help="relocate the whole data tree (raw cache + charts/ + images/ + "
                        "catalog.json + manifest); default is <repo root>/data")
    p.add_argument("--limit-mb", type=float, default=0,
                   help="stop adding chart packages once this many MB are built (0 = off)")
    p.add_argument("--illustration-source", choices=["auto", "full", "lowres"], default="auto",
                   help="which illustration branch to use (full -> downscaled JPEG)")
    p.add_argument("--image-max-dim", type=int, default=IMAGE_MAX_DIM,
                   help="longest side of the illustration JPEG embedded in zips / served to app")
    p.add_argument("--image-quality", type=int, default=IMAGE_JPEG_QUALITY,
                   help="JPEG quality for the illustration")
    p.add_argument("--preview-start", type=float, default=DEFAULT_PREVIEW_START,
                   help="preferred start of the audio preview (seconds)")
    p.add_argument("--preview-seconds", type=float, default=PREVIEW_DURATION,
                   help="length of the audio preview (seconds)")
    p.add_argument("--force", action="store_true",
                   help="rebuild media and zips even if fingerprints match")
    p.add_argument("--no-validate", action="store_true", help="skip post-build validation")
    p.add_argument("--validate-only", action="store_true",
                   help="only validate existing catalog + files")
    p.add_argument("--fetch-only", action="store_true",
                   help="only fetch source assets + derived media, do not build zips")
    p.add_argument("--refresh-info", action="store_true", help="re-download info.tsv/difficulty.tsv")
    p.add_argument("--quiet", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    global _QUIET
    args = parse_args(argv)
    _QUIET = args.quiet
    configure_paths(args.data_dir)

    if args.validate_only:
        require_tools(need_encoding=False)
        return 1 if validate_catalog() else 0

    require_tools(need_encoding=True)
    for d in (RAW_INFO, RAW_CHART, RAW_MUSIC, RAW_ILL, RAW_ILL_LOW, CHARTS, IMAGES):
        d.mkdir(parents=True, exist_ok=True)

    log("[1/6] metadata (info.tsv / difficulty.tsv)")
    ensure_info_files(force=args.refresh_info)
    songs = parse_info()
    log(f"      {len(songs)} songs with ratings")

    requested_levels = [lv.strip().upper() for lv in args.levels.split(",") if lv.strip()]
    for lv in requested_levels:
        if lv not in LEVELS:
            sys.exit(f"invalid level {lv!r}; choose from {LEVELS}")
    requested_levels = [lv for lv in LEVELS if lv in requested_levels]

    count = None if str(args.songs).lower() == "all" else int(args.songs)
    plans = plan_entries(songs, count, requested_levels, args.max_entries)
    if not plans:
        sys.exit("no chart entries selected")
    log(f"      planned {len(plans)} entries "
        f"({len({p['songKey'] for p in plans})} songs, levels={','.join(requested_levels)})")

    log(f"[2/6] fetching source assets ({args.jobs} parallel workers)")
    fetch_assets(plans, args.illustration_source, args.jobs)
    if not plans:
        sys.exit("no buildable entries (all source assets missing)")
    log(f"      {len(plans)} buildable entries")

    log("[3/6] deriving illustration JPEG + preview ogg")
    manifest = load_json(MANIFEST_PATH, {})
    if not isinstance(manifest, dict):
        manifest = {}
    manifest.setdefault("version", 1)
    manifest.setdefault("entries", {})
    media = derive_media(plans, force=args.force, jobs=args.jobs, manifest=manifest,
                         image_max_dim=args.image_max_dim,
                         image_quality=args.image_quality,
                         preview_start=args.preview_start,
                         preview_seconds=args.preview_seconds)
    if not plans:
        sys.exit("no entries survived media derivation")

    if args.fetch_only:
        log("fetch-only: done")
        return 0

    log("[4/6] building chart packages")
    assign_ids(plans, manifest, args.start_id, args.reset_ids)
    builds = build_packages(plans, media, manifest, args.force, args.limit_mb)
    if not builds:
        sys.exit("no packages built")
    atomic_write_json(MANIFEST_PATH, manifest)
    log(f"      {len(builds)} packages "
        f"({sum(1 for b in builds if b['reused'])} reused, "
        f"{sum(1 for b in builds if not b['reused'])} built)")

    log("[5/6] writing catalog")
    catalog = build_catalog(builds, manifest, args.base_url, args.uploader, args.version)
    atomic_write_json(CATALOG_PATH, catalog)
    log(f"      {CATALOG_PATH} count={catalog['count']} version={catalog['version']}")

    if args.no_validate:
        return 0
    log("[6/6] validating")
    failures = validate_catalog()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
