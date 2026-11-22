#!/usr/bin/env python3
"""Resumable upload of built charts/assets to GitHub Releases (rate-limit friendly).

CI usage (after publish/publish.py):
  python3 publish/upload_assets.py \
      --repo "$GITHUB_REPOSITORY" \
      --tag "$TAG" \
      --charts-list upload-charts.txt \
      --assets-list upload-assets.txt \
      --meta published/catalog.json published/version.json

- Creates `<tag>-charts` / `<tag>-assets` releases if missing.
- Uploads only assets that are not already present (idempotent / resumable).
- Handles GitHub secondary rate limits with exponential backoff.
- Extra `--meta` files are uploaded (clobber) to the charts release (e.g. catalog.json),
  so the hybrid VM mode can fetch the rewritten catalog even if Pages is unavailable.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path


def gh(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], check=check, capture_output=True, text=True)


def ensure_release(repo: str, tag: str, title: str, notes: str) -> None:
    r = gh("release", "view", tag, "--repo", repo, check=False)
    if r.returncode == 0:
        return
    print(f"[upload] create release {tag}", flush=True)
    r = gh("release", "create", tag, "--repo", repo, "--title", title, "--notes", notes, check=False)
    if r.returncode != 0 and "already exists" not in (r.stdout + r.stderr):
        raise SystemExit(f"cannot create release {tag}: {r.stdout}{r.stderr}")


def existing_assets(repo: str, tag: str) -> set[str]:
    r = gh("release", "view", tag, "--repo", repo, "--json", "assets", "--jq", ".assets[].name", check=False)
    if r.returncode != 0:
        return set()
    return {line.strip() for line in r.stdout.splitlines() if line.strip()}


def upload_one(repo: str, tag: str, path: Path, attempts: int = 12) -> bool:
    for attempt in range(1, attempts + 1):
        r = gh("release", "upload", tag, str(path), "--clobber", "--repo", repo, check=False)
        if r.returncode == 0:
            print(f"[upload] ok {path.name}", flush=True)
            return True
        out = (r.stdout or "") + (r.stderr or "")
        if "rate limit" in out.lower() or "secondary rate" in out.lower():
            wait = min(300, 45 * attempt)
            print(f"[upload] rate limited on {path.name}; sleep {wait}s (attempt {attempt})", flush=True)
            time.sleep(wait)
        else:
            print(f"[upload] retry {attempt}/{attempts} {path.name}: {out.strip().splitlines()[-1] if out.strip() else 'error'}", flush=True)
            time.sleep(10)
    print(f"[upload] FAILED {path.name}", flush=True)
    return False


def upload_list(repo: str, tag: str, list_file: Path, note: str) -> int:
    names = [line.strip() for line in list_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    have = existing_assets(repo, tag)
    todo = [Path(p) for p in names if Path(p).name not in have]
    print(f"[upload] {note}: total={len(names)} existing={len(have)} missing={len(todo)}", flush=True)
    failed = 0
    for p in todo:
        if not p.is_file():
            print(f"[upload] missing local file {p}", flush=True)
            failed += 1
            continue
        if not upload_one(repo, tag, p):
            failed += 1
    return failed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--charts-list", default="upload-charts.txt")
    ap.add_argument("--assets-list", default="upload-assets.txt")
    ap.add_argument("--meta", nargs="*", default=[])
    args = ap.parse_args()
    charts_tag, assets_tag = f"{args.tag}-charts", f"{args.tag}-assets"
    ensure_release(args.repo, charts_tag, f"Phira data {args.tag} (charts)", "Chart packages for the static/hybrid setup.")
    ensure_release(args.repo, assets_tag, f"Phira data {args.tag} (images)", "Illustrations / preview audio for the static/hybrid setup.")
    failed = 0
    failed += upload_list(args.repo, charts_tag, Path(args.charts_list), "charts")
    failed += upload_list(args.repo, assets_tag, Path(args.assets_list), "assets")
    for m in args.meta:
        mp = Path(m)
        if mp.is_file():
            if not upload_one(args.repo, charts_tag, mp):
                failed += 1
        else:
            print(f"[upload] meta file missing: {m}", flush=True)
            failed += 1
    # final counts
    for tag in (charts_tag, assets_tag):
        print(f"[upload] {tag} assets={len(existing_assets(args.repo, tag))}", flush=True)
    if failed:
        print(f"[upload] FAILED count={failed}", flush=True)
        return 1
    print("[upload] all done", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
