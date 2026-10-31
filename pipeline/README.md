# Phigros_Resource → Phira chart-package pipeline

This directory builds the replacement chart corpus served by the
`phira-mitm` resource server (未上架/观赏 list at `/api/list`, and the
`/api/popular` list): standalone Phira chart packages converted from the
community Phigros resource dump, plus the `data/catalog.json` the server
consumes.

## Final state (2026-10-31)

The corpus currently served at `https://phira.oriweb.cc` (`/api/list` for the
未上架/观赏 tab and `/api/popular` for 热门), catalog **version 12**:

| | |
|---|---|
| chart entries | **383** = **327 IN** (every song in `info.tsv`) + **56 AT** (every song with an AT chart+rating), ids **990001–990383** contiguous |
| chart packages | `data/charts/990001.zip … 990383.zip`, **1711.7 MB (1.67 GiB)** = 1439.5 MB IN + 272.2 MB AT, **4.47 MB avg** |
| illustration JPEGs | `data/images/<songHash>.jpg` × 327, **70.3 MB** (≈220 KB each, 1280 px longest side) — AT entries share their song's IN image |
| audio previews | `data/images/<songHash>_preview.ogg` × 327, **73.4 MB** (≈230 KB each, 15 s) — AT entries share their song's IN preview; images+previews = 143.7 MB / 654 files |
| list pagination | **14 pages of 28** = `383 = 13×28 + 19` (page 14 = 19 results) |
| difficulty spread | IN Lv.7.0–17.0, AT Lv.13.4–18.0 |
| validation | `383/383` entries pass, `data/validate-report.json` |
| uploader | fake user id `1` (configurable with `--uploader`) |

> Note for the GitHub-Action static build: it runs one fresh
> `--songs all --levels IN,AT` pass, so its ids are **song-major interleaved**
> (IN,AT per song) instead of this live dataset's IN-block-then-AT layout. Both
> are contiguous/deterministic; see "GitHub Actions usage" below.

Build history / commands used (incremental, resumable — every step reused the
previous cache and kept existing ids/files/URLs byte-identical):

```bash
# phase 1: first 28 songs → ids 990001–990028 (E2E batch, later kept as-is)
python3 pipeline/build.py --songs 28  --levels IN

# phase 2: extend to the whole info.tsv corpus (327 IN entries)
python3 pipeline/build.py --songs all --levels IN --jobs 16

# after adding the stream-copy preview fallback: recover the 8 songs whose
# 48 kHz sources break ffmpeg's experimental vorbis encoder. Existing 319 entries
# are fingerprint-reused (same ids/files/URLs); the 8 recovered charts are
# appended as ids 990320–990327:
python3 pipeline/build.py --songs all --levels IN --jobs 16

# phase 3 (user request "why only IN?"): add ALL AT charts. The 327 IN entries
# are reused unchanged; one AT entry per song is appended as 990328–990383:
python3 pipeline/build.py --songs all --levels IN,AT --jobs 16
```

Composition:
* IN set (990001–990327): 319 entries from the first full pass + 8 recovered
  with stream-copy previews (`Retribution`, `Initialize`, `Journey with You`,
  `月詠に鳴る`, `ストレイソウル・アラウンド`, `白と黒のバケモノ`,
  `QZKago Requiem`, `Quantum Hyperspace`). Catalog order is `info.tsv` order for
  `990001–990319`, then those 8 appended (ids preserved instead of renumbering,
  so no URL/cache churn).
* AT set (990328–990383): exactly the 56 songs that have **both** an
  `<songKey>.0/AT.json` in the chart branch **and** a 4th rating column in
  `difficulty.tsv` (verified: the two sets are identical — 56/56, no misses on
  either side). Appended in `info.tsv` order; charter from the 4th `info.tsv`
  charter column (present for all 56); illustration/preview URLs are shared with
  that song's IN entry. 272.2 MB of new standalone zips.

```
phira-mitm/
├── pipeline/
│   ├── build.py            # fetch → derive media → build zips → catalog (main entrypoint)
│   ├── validate.py         # validate catalog + every zip/image/preview on disk
│   ├── requirements.txt    # PyYAML (only third-party Python dependency)
│   └── README.md           # this file
└── data/                   # generated + cached, safe to delete/rebuild
    ├── raw/                # cached upstream source (gitignored by convention)
    │   ├── info/           #   info.tsv, difficulty.tsv
    │   ├── chart/          #   <songKey>.0/<LEVEL>.json
    │   ├── music/          #   <songKey>.ogg
    │   ├── illustration/   #   <songKey>.png        (full-res, 2048×1080-ish)
    │   └── illustrationLowRes/
    ├── charts/<chartId>.zip        # one self-contained Phira package per catalog entry
    ├── images/<songHash>.jpg       # illustration: list thumbnail + in-game background
    ├── images/<songHash>_preview.ogg  # 15 s audio preview for the song list
    ├── build-manifest.json         # id assignment + fingerprints (resumability)
    ├── validate-report.json        # last validation result
    └── catalog.json                # ← what the server reads (atomic writes)
```

## Source

Upstream: <https://github.com/7aGiven/Phigros_Resource>. Resources live on
orphan branches (the `master` branch only has the extractor scripts). Verified
layout:

| branch | path | notes |
|---|---|---|
| `info` | `info.tsv`, `difficulty.tsv` | 327 songs; also avatar.txt/tips.txt/collection.tsv (unused) |
| `chart` | `<songKey>.0/{EZ,HD,IN,AT}.json` | official Phigros PGR chart JSON, `formatVersion: 3` |
| `music` | `<songKey>.ogg` | 327 Ogg Vorbis full songs (~4–5 MB each, ~232 kbps) |
| `illustration` | `<songKey>.png` | full-res illustration (~1.7 MB, ~2048×1080) |
| `illustrationLowRes` | `<songKey>.png` | 512×270 thumbnail (~270 KB); one song only has `<songKey>_<LEVEL>.png` variants |

`songKey` is the upstream id (e.g. `Glaciaxion.SunsetRay`), **not** a file-safe
name: keys contain CJK characters and `&`. Downloads are fetched through
`raw.githubusercontent.com` (no GitHub API ⇒ no rate limit) with per-file
caching, so an interrupted run resumes where it stopped.

Every downloaded asset is integrity-checked before use (HTTP `Content-Length`
match, chart JSON must parse and contain `judgeLineList`, music must be `OggS`
and decode with a plausible duration, illustration PNG must have a trailing
`IEND`); suspicious cached files are deleted and re-downloaded once. This was
added after a real truncated 4.7 MB ogg arrived as 64 KB during the phase-1
build (caught and repaired for chart 990014).

`info.tsv` columns: `songKey, name, composer, illustrator, charter[EZ], charter[HD], …`
`difficulty.tsv`: `songKey, rating[EZ], rating[HD], rating[IN], rating[AT]` — the
number of rating columns decides which difficulties exist for that song.

## Chart package format (what is inside `<chartId>.zip`)

Every zip is standalone and uncompressed where it matters:

| file | compression | content |
|---|---|---|
| `info.yml` | deflate | Phira `ChartInfo` YAML (camelCase), `format: pgr` |
| `chart.json` | deflate | verbatim official Phigros PGR chart (≈1 MB raw, ≈150 KB deflated) |
| `music.ogg` | store | full song (already compressed) |
| `illustration.jpg` | store | downscaled illustration, 1280 px longest side, q80 |

`info.yml` looks like:

```yaml
name: "Glaciaxion"
difficulty: 12.6
level: "IN Lv.12.6"
charter: "Barbarianerman vs. NerSAN"
composer: "SunsetRay"
illustrator: "艾若拉"
chart: "chart.json"
format: "pgr"
music: "music.ogg"
illustration: "illustration.jpg"
previewStart: 30.0
previewEnd: 45.0
aspectRatio: 1.7777777777777777
backgroundDim: 0.6
lineLength: 6.0
offset: 0.0
tags: ["Phigros"]
intro: ""
holdPartialCover: false
noteUniformScale: false
forceAspectRatio: false
```

The schema matches `prpr::info::ChartInfo` (the Phira chart engine), which is
what the shipped app parses after downloading a zip. `format: pgr` selects the
official-PGR JSON parser explicitly (other values: `rpe`, `pec`, `pbc`).
`previewStart/End` describe the 30 s → 45 s segment also used for the list
preview clip. Uncompressed sizes: chart 0.1–2 MB, music 3–5 MB, image ≈200 KB.

Zip entries use a fixed timestamp (`2026-01-01 00:00`) and fixed permissions, so
the same inputs produce byte-identical zips.

## Catalog (`data/catalog.json`)

Schema consumed by `server/server.mjs` (see `server/README.md`):

```json
{
  "version": 11, "base": null, "count": 327,
  "entries": [{
    "id": 990001,
    "name": "Glaciaxion", "level": "IN Lv.12.6", "difficulty": 12.6,
    "charter": "…", "composer": "…", "illustrator": "…",
    "description": null, "ranked": false, "reviewed": true,
    "stable": false, "stableRequest": false,
    "illustration": "{BASE}/img/c7bec6f6f1b6.jpg",
    "preview": "{BASE}/img/c7bec6f6f1b6_preview.ogg",
    "file": "{BASE}/files/990001.zip",
    "uploader": 1, "tags": ["Phigros"], "rating": null,
    "created": "…", "updated": "…", "chartUpdated": "…"
  }]
}
```

* `{BASE}` is substituted server-side with `BASE_URL` + version path
  (`https://phira.oriweb.cc/v1`). Absolute or root-relative URLs also work;
  `--base-url` switches style.
* `illustration` / `preview` basenames map to `data/images/`, `file` basename
  maps to `data/charts/` (server routes `/img/<name>`, `/files/<name>`).
* **`preview` is an audio URL**, not an image: the app decodes it as a short
  audio clip. The pipeline emits a 15 s Ogg Vorbis clip and keeps it in
  `data/images/` so the existing `/img/` route serves it with `audio/ogg`
  (server-tunnel confirmed).
* `uploader` is a fake user id. Default is `1`; if the app complains that the
  uploader user is missing, rebuild with `--uploader 990001` (the replacement
  server can fake that id too — coordinated with `server-tunnel`). Rebuilding
  only rewrites `catalog.json` (zips/images are reused).
* Entry ids start at 990001 (`--start-id`); one id per (song, difficulty).
  Ids are persisted in `data/build-manifest.json`, so incremental rebuilds keep
  URLs stable ("app caches by URL").

## Usage

```bash
cd phira-mitm

# Phase 1 demo batch: first 28 songs (info.tsv order) × IN difficulty
python3 pipeline/build.py --songs 28 --levels IN

# Validate only (also runs automatically at the end of build.py)
python3 pipeline/validate.py

# Fetch/cache the raw source + media without building zips
python3 pipeline/build.py --songs 28 --fetch-only

# Full corpus: 327 songs × IN + 56 × AT (≈1.7 GB of zips; current served corpus)
python3 pipeline/build.py --songs all --levels IN,AT

# All four difficulties for a subset (EZ,HD,IN,AT where they exist)
python3 pipeline/build.py --songs 140 --levels EZ,HD,IN,AT --limit-mb 2500

# Rebuild packages after changing package parameters
python3 pipeline/build.py --songs 28 --levels IN --force
```

Useful flags (`--help` lists everything):

| flag | default | meaning |
|---|---|---|
| `--songs N\|all` | `28` | number of songs selected in `info.tsv` order |
| `--levels LIST` | `IN` | difficulties to emit, e.g. `IN` or `EZ,HD,IN,AT` |
| `--max-entries N` | 0 | hard cap on chart entries |
| `--start-id N` | 990001 | first local chart id |
| `--reset-ids` | off | renumber ids from `--start-id` (clears manifest id map) |
| `--base-url` | `{BASE}` | URL prefix in the catalog |
| `--uploader N` | 1 | catalog uploader user id (fallback: `--uploader 990001`, see below) |
| `--limit-mb` | 0 | stop building once this many MB of zips exist |
| `--illustration-source auto\|full\|lowres` | `auto` | illustration quality tradeoff |
| `--image-max-dim N` | 1280 | longest side of the illustration JPEG (e.g. 960 to slim the list) |
| `--image-quality N` | 80 | JPEG quality of the illustration |
| `--preview-start S` | 30.0 | preferred start of the 15 s audio preview |
| `--preview-seconds S` | 15.0 | audio preview length |
| `--force` | off | rebuild media + zips even if unchanged |
| `--jobs N` | 8 | parallel downloads (media derivation is capped at 6 workers) |
| `--data-dir PATH` | `<repo root>/data` | relocate the whole data tree (raw cache + charts/ + images/ + catalog + manifest); path is resolved from the script location by default, so a fresh checkout works from its repo root |
| `--no-validate` / `--validate-only` / `--fetch-only` | | phase control |
| `--refresh-info` | off | re-download `info.tsv`/`difficulty.tsv` |

Environment knobs:

* `SOURCE_DATE_EPOCH=<epoch>` — freeze catalog timestamps (fully reproducible
  catalog).
* `CATALOG_FILE`/`BASE_URL`/`VERSION_PATH` are **server-side** env vars (see
  `server/README.md`); bump `server/RESOURCE_VERSION_PATH` (`v1` → `v2`) to make
  the app re-download changed resources.

## GitHub Actions usage (static-hosting repo template)

The pipeline is designed to run unattended from a **fresh checkout** in the
GitHub repo template (`phira-mitm/gh/`, where the repo root contains
`pipeline/` and an empty `data/`). Nothing is machine-specific: every path is
derived from `pipeline/build.py`'s own location (`<repo root>/data`) and can be
overridden with `--data-dir`.

### Dependencies

* Python **3.12** + **PyYAML** only:
  `python3 -m pip install -r pipeline/requirements.txt`
  (`requirements.txt` contains just `PyYAML>=6.0`; `certifi` is used
  opportunistically for TLS, with a stdlib fallback).
* `ffmpeg` **and** `ffprobe` are required (music probing, preview cutting, and
  JPEG conversion on non-macOS runners). On Ubuntu:
  `sudo apt-get install -y ffmpeg`; on macOS: `brew install ffmpeg`.
* `sips` is used for illustration downscaling on macOS if present; if absent
  the pipeline transparently falls back to ffmpeg (so Linux runners work too).
  A missing mandatory tool fails immediately with an install hint.

### Canonical workflow invocation (repo root as cwd)

```bash
python3 -m pip install -r pipeline/requirements.txt   # PyYAML
python3 pipeline/build.py --songs all --levels IN,AT --jobs 8
python3 pipeline/validate.py --quiet                  # optional extra gate
```

`python3 pipeline/build.py --help` lists every knob. The build is
**non-interactive**, uses only `data/` for its cache + output, and is
**resumable**: re-running the same command after a timeout/partial run reuses
already-fetched sources and already-built packages (`data/` may be cached
between CI runs); a clean `data/` simply fetches/buids from scratch. Fresh
checkout → catalog + zips + images, no other state.

For the full corpus on a `macos-14` runner this downloads ≈2.5 GB (sources) and
produces ≈1.7 GB of zips + ≈144 MB of images; budget ~15–30 min (the workflow
uses `--jobs 8`; the full build is bounded by the four upstream downloads).

### Deterministic id ordering (documented rule)

Selection and ids are deterministic: **song-major** — for each song in
`info.tsv` order, emit its requested difficulties in canonical
`EZ,HD,IN,AT` order, then assign ids sequentially from `--start-id` (default
990001) via `data/build-manifest.json`. A fresh `--levels IN,AT` build therefore
**interleaves** IN/AT per song, e.g.:

```
990001 Glaciaxion              IN Lv.12.6
990002 Eradication Catastrophe IN Lv.12.7
990003 Credits                 IN Lv.13.6
990004 Credits                 AT Lv.15.8
990005 Dlyrotz                 IN Lv.13.7
...
```

This differs from the live tunnel dataset, which was built incrementally
(IN block 990001–990327, then AT appended 990328–990383); both are contiguous
and stable per build. Rebuilding a fresh dataset with the same selection
reproduces the same ids and byte-identical artifacts (verified below).

### CI smoke test (what was verified)

From a clean copy of the repo template (no `data/`), run from the repo root:

```bash
# 1) tiny build: 2 songs, IN+AT requested (first 2 songs have no AT yet)
python3 pipeline/build.py --songs 2 --levels IN,AT --jobs 8
#   -> count=2, ids 990001-990002, validation 2/2 PASS, exit 0 (~12 s, ~9 MB zips)

# 2) re-run same command -> resume
python3 pipeline/build.py --songs 2 --levels IN,AT --jobs 8
#   -> "2 packages (2 reused, 0 built)", exit 0 (~0.4 s)

# 3) extend to exercise AT + ordering
python3 pipeline/build.py --songs 4 --levels IN,AT --jobs 8
#   -> 5 entries: ids 990001-990005, order Glaciaxion IN, Eradication IN,
#      Credits IN, Credits AT, Dlyrotz IN; validation 5/5 PASS

# 4) relocatable data tree (script-location default + explicit override)
python3 /path/to/checkout/pipeline/build.py --songs 1 --levels IN,AT \
    --data-dir /tmp/fake-data-dir     # works from any cwd; catalog lands in /tmp/fake-data-dir

# 5) validation of the fresh checkout
python3 pipeline/validate.py --fast   # 5/5 PASS

# 6) determinism: wipe data/ and rerun the same command
#   -> identical artifact set (sha256 over all zips/images) and identical ids
```

The repo template's `.github/workflows/build.yml` runs the canonical invocation
above and then `publish/publish.py` to write Releases + Pages JSON; `data/` is
gitignored so CI never commits large files.

## Selection strategy

Selection is deterministic: songs in upstream `info.tsv` order, then
difficulties in canonical `EZ,HD,IN,AT` order (**song-major / interleaved**, see
the ordering rule in "GitHub Actions usage"). A song with no requested
difficulty is skipped (no entry). Assets that are missing upstream are skipped
with a warning; the pipeline never fabricates chart data.

* **Phase 1** (`--songs 28 --levels IN`): 28 entries, ids `990001–990028`, one
  IN chart for the first 28 songs in `info.tsv` — exactly one mobile page
  (`PAGE_NUM = 28`), used for the end-to-end check.
* **Phase 2 (final corpus)**: `--songs all --levels IN` → 327 entries, ids
  `990001–990327`, one IN chart per song = 12 pages. Built on 2026-10-31 and
  served at `https://phira.oriweb.cc/api/list` (未上架/观赏 tab) and
  `/api/popular`.
  * The first 319 entries were built in one pass; 8 famous songs
    (`Retribution`, `Initialize.iF`, `Journey with You`, `月詠に鳴る`,
    `ストレイソウル・アラウンド`, `白と黒のバケモノ`, `QZKago Requiem`,
    `Quantum Hyperspace`) initially failed only because ffmpeg's experimental
    vorbis encoder asserts on their 48 kHz sources. They were recovered with a
    stream-copy preview fallback and **appended** as ids `990320–990327`
    (without renumbering the existing entries, to keep URLs/caches stable), so
    catalog order is *info.tsv order for 990001–990319, then those 8 at the
    end*. Rebuilding from scratch would put them in info.tsv order.
* **Phase 3 / final (AT batch)**: `--songs all --levels IN,AT` → the 327 IN
  entries are reused unchanged and 56 AT entries are appended as
  `990328–990383` (one per song with both `AT.json` and an AT rating; AT chart +
  full music + shared illustration in each standalone zip).
* Multi-difficulty is possible but multiplies size because each zip embeds its
  own copy of the song: all four difficulties for all songs would be ≈5–6 GB, so
  it is only enabled on request with `--levels EZ,HD,IN,AT`
  (+ `--songs`/`--limit-mb`). The served corpus (IN+AT) is 1.67 GiB.

## Size table (final corpus, measured 2026-10-31)

| item | count | size |
|---|---|---|
| chart zips (`data/charts/*.zip`) | 383 | **1711.7 MB** (1.67 GiB) total, 4.47 MB avg (2.25–7.43 MB) |
| — IN entries (990001–990327) | 327 | 1439.5 MB, 4.40 MB avg |
| — AT entries (990328–990383) | 56 | 272.2 MB, 4.86 MB avg |
| illustration JPEGs (`data/images/*.jpg`) | 327 | 70.3 MB total, 220 KB avg (shared by IN+AT) |
| preview clips (`data/images/*_preview.ogg`) | 327 | 73.4 MB total, 230 KB avg (shared by IN+AT) |
| raw cache (`data/raw/`) | — | ≈3.6 GB (deletable, re-fetched on demand) |

Difficulty ranges: IN Lv.7.0–17.0, AT Lv.13.4–18.0. Phase-1 subset for
comparison: 28 zips / 105.7 MB.

Per-zip breakdown is dominated by `music.ogg` (2.4–4.7 MB, stored); the chart
JSON is 0.1–15.9 MB raw (AT charts are the biggest, ≈6 MB avg) but deflates to
50 KB–1.5 MB; the illustration is ≈200 KB. Preview clips are ≈150–250 KB for
re-encoded vorbis and ≈400 KB for the 8 stream-copied ones (see caveats).

**Estimates for other scales** (`--songs all --levels IN` is the current 327):
raw cache grows to ≈ 3.2 GB while building (delete `data/raw/` afterwards if
disk matters). Multi-difficulty multiplies the music copies again: all four
difficulties for all songs would be ≈5–6 GB, so only build it behind
`--songs`/`--limit-mb` when explicitly requested.

## Validation

`pipeline/validate.py` (and the final phase of `build.py`) checks, for every
catalog entry:

1. every zip exists, unzips, and `testzip()` passes;
2. `info.yml` is valid YAML, has all required `ChartInfo` fields, and its
   `name`/`level`/`format`/`chart`/`music`/`illustration` agree with the catalog;
3. `chart.json` parses as JSON, has `formatVersion ∈ {1,3}`, a non-empty
   `judgeLineList`, and every judge line carries `bpm` + the six event arrays
   (the Phira PGR parser crashes on empty `speedEvents`, so that is a hard
   error here);
4. `music.ogg` starts with `OggS`, is full-length (ffprobe duration ≥ 20 s);
   `illustration.jpg` is a JPEG; the preview file exists, is an Ogg stream and
   is 5–25 s long (all deep checks skipped with `--fast`);
5. catalog URLs' basenames match the files on disk; types are correct
   (ints/floats/bools/RFC3339 strings).

It prints a per-entry table (zip size, uncompressed chart size, image size) and
writes `data/validate-report.json`. Exit code 1 if anything fails.
`python3 pipeline/validate.py --fast` skips the ffprobe/decode checks (structure
only, much faster for large corpora); `--quiet` prints just the summary line.

## Rebuild / rescale

* Everything is resumable: source downloads and derived media are cached and
  each zip is fingerprinted (sha256 of `info.yml` + chart + music + image).
  Re-running without changes reuses zips (verified: second run is ~8 s and
  rebuilds nothing).
* To change the package parameters (aspect ratio, preview window, tags, image
  size…), edit the constants at the top of `build.py` and run with `--force`.
* To change the song set only, run with a different `--songs`/`--levels`
  (existing zips are reused by fingerprint). Use `--reset-ids` to renumber.
* Delete `data/charts/`, `data/images/`, `data/catalog.json`,
  `data/build-manifest.json` for a clean rebuild (keep `data/raw/` to avoid
  re-downloading).
* Servers hot-reload `data/catalog.json` on mtime change; bump
  `server/RESOURCE_VERSION_PATH` when content changes so the app's URL-keyed
  cache is bypassed.

## Known caveats

* **Size is dominated by music** (copy of the full song in every zip). Adding
  difficulties for the same song duplicates the song again; plan around that.
* **AT difficulty** exists for only 56 songs (34 have `AT.json` in the chart
  branch). Without `AT.json` no AT entry is emitted.
* `info.tsv` charter/rating column counts disagree for a few songs
  (`ENERGY SYNERGY MATRIX`, `Aleph0`, `Break Through The Barrier`, `Lyrith`);
  charters are matched by index and the last one fills in if missing.
* One song (`WhatdoyouwantmorethanaHappyending…`) has only per-difficulty
  illustrations (`<songKey>_<LEVEL>.png`); the pipeline falls back to those.
* Illustration downsizing needs `sips` (macOS builtin). Preview cutting needs
  `ffmpeg` + `ffprobe`; the Homebrew ffmpeg build on this machine has **no
  libvorbis**, so the pipeline uses ffmpeg's built-in (experimental) `vorbis`
  encoder — output is standard Ogg Vorbis and decodes correctly (verified), but
  it is not the highest-quality encoder. That encoder *asserts* on some 48 kHz
  sources (8 songs here), so `make_preview` falls back to an Ogg page-boundary
  **stream copy** of the 30–45 s segment (no re-encode, ≈400 KB, always
  decodable). If sound quality matters, install `vorbis-tools`/`libvorbis` and
  switch the primary encoder to `-c:a libvorbis`.
* The upstream full illustration is fetched for the best visual quality and
  downscaled to 1280 px (≈200 KB). `--illustration-source lowres` uses the
  512×270 branch instead (slightly smaller/faster, visibly blurrier in-game);
  `--image-max-dim 960 --image-quality 78` slims the list further if the app
  feels heavy. Rebuilds after changing these flags regenerate only media/zips
  affected, but the app caches by URL — bump `server/RESOURCE_VERSION_PATH`.
* `previewStart/End` are fixed at 30 s/45 s (clamped for short songs); they are
  not aligned to the music's chorus and can be tuned per song later.
* Chart zips are consumed by the shipped Phira app; in-app E2E verification is
  owned by teammate `app-runtime`.

## Final state (2026-10-31, delivered dataset)

* Output: `data/catalog.json` version 11, **327 entries**, ids `990001`–`990327` contiguous.
* `data/charts/`: 327 zips, ~1.4 GB; `data/images/`: 654 files, ~145 MB; `data/raw/` cache ~3.3 GB (kept for rebuilds).
* Build: Phase 1 (`--songs 28 --levels IN`) → Phase 2 (`--songs all --levels IN`, 319 built) → recovery run for the 8
  songs whose vorbis preview re-encode failed (Ogg page-boundary stream copy fallback), appended as `990320`–`990327`.
* Serving: `GET https://phira.oriweb.cc/api/list?page&pageNum&search&order` (Shadowrocket rewrites the native
  `...type=2&division=visual...` query verbatim); 28/page → **12 pages**.
* Verified: `docs/verify_catalog.py` PASS 327/327; `docs/acceptance.sh --proxy` ALL CHECKS PASSED;
  E2E in the shipped app (list/filter/search/pagination/download/play) PASS.
