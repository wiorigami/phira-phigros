#!/usr/bin/env python3
"""Publish the Phira/Phigros dataset as GitHub Releases (big static assets) + GitHub Pages (JSON/config/docs).

Inputs   data/catalog.json, data/charts/*.zip, data/images/*   (produced by pipeline/build.py)
Outputs  published/                        GitHub Pages artifact (JSON pages, per-id JSON, stubs, configs, docs)
         staging/charts/ch-<id>.zip        hardlinks -> upload to release `<tag>-charts`
         staging/assets/img-*.jpg|prev-*.ogg hardlinks -> upload to release `<tag>-assets`

Release tag is content-derived (`data-<sha8>`) so unchanged data re-uses the same URLs and
changed data gets fresh URLs (the Phira app caches resources by URL).

CI:
  python3 publish/publish.py --repo "$GITHUB_REPOSITORY" --pages-base "$PAGES_BASE"
Dry run (local, no staging hardlinks):
  python3 publish/publish.py --repo owner/repo --pages-base https://owner.github.io/repo --dry-run
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

PAGE_NUM = 28


def log(msg: str) -> None:
    print(f"[publish] {msg}", flush=True)


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(obj, (dict, list)):
        text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    else:
        text = str(obj)
    path.write_text(text + "\n", encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def basename(url: str) -> str:
    return url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]


def link_or_copy(src: Path, dst: Path) -> None:
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""), help="owner/name")
    ap.add_argument("--pages-base", default="", help="e.g. https://owner.github.io/repo")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--out-dir", default="published")
    ap.add_argument("--staging", default="staging")
    ap.add_argument("--templates", default="shadowrocket")
    ap.add_argument("--tag", default="", help="override release tag (default: content hash)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-staging", action="store_true", help="do not build staging hardlinks")
    args = ap.parse_args()

    if not args.repo or "/" not in args.repo:
        sys.exit("--repo owner/name (or GITHUB_REPOSITORY) is required")
    owner, repo_name = args.repo.split("/", 1)
    pages_base = (args.pages_base or f"https://{owner}.github.io/{repo_name}").rstrip("/")

    root = Path.cwd()
    data = (root / args.data_dir).resolve()
    out = (root / args.out_dir).resolve()
    staging = (root / args.staging).resolve()
    charts_dir = data / "charts"
    images_dir = data / "images"
    templates = (root / args.templates).resolve()
    catalog_path = data / "catalog.json"
    if not catalog_path.is_file():
        sys.exit(f"missing {catalog_path}; run pipeline/build.py first")
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    entries = catalog.get("entries") or []
    if not entries:
        sys.exit("catalog has no entries")
    n = len(entries)
    pages = math.ceil(n / PAGE_NUM)

    # ---- resolve local files + content hash (before rewriting URLs) -------------
    plan = []  # (entry, zip_local, img_local, prev_local, zip_remote, img_remote, prev_remote)
    for e in entries:
        zl = charts_dir / basename(e["file"])
        il = images_dir / basename(e["illustration"])
        pl = images_dir / basename(e["preview"])
        for p in (zl, il, pl):
            if not p.is_file():
                sys.exit(f"missing asset for id {e['id']}: {p}")
        plan.append(
            (
                e,
                zl,
                il,
                pl,
                f"ch-{e['id']}.zip",
                f"img-{basename(e['illustration'])}",
                f"prev-{basename(e['preview']).replace('_preview.ogg', '.ogg')}",
            )
        )

    h = hashlib.sha256()
    h.update(str(catalog.get("version", "")).encode())
    for e, zl, il, pl, *_ in plan:
        h.update(str(e["id"]).encode())
        h.update(e["name"].encode("utf-8", "ignore"))
        h.update(e["level"].encode("utf-8", "ignore"))
        h.update(str(zl.stat().st_size).encode())
        h.update(str(il.stat().st_size).encode())
    tag = args.tag or ("data-" + h.hexdigest()[:8])
    charts_tag, assets_tag = f"{tag}-charts", f"{tag}-assets"
    rel_charts = f"https://github.com/{args.repo}/releases/download/{charts_tag}"
    rel_assets = f"https://github.com/{args.repo}/releases/download/{assets_tag}"
    log(f"repo={args.repo} tag={tag} entries={n} pages={pages}")
    log(f"pages_base={pages_base}")
    log(f"release charts={rel_charts}")
    log(f"release assets={rel_assets}")

    # ---- rewrite catalog URLs to release-asset URLs -----------------------------
    for e, zl, il, pl, zr, ir, pr in plan:
        e["file"] = f"{rel_charts}/{zr}"
        e["illustration"] = f"{rel_assets}/{ir}"
        e["preview"] = f"{rel_assets}/{pr}"

    # ---- output dir ------------------------------------------------------------
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)

    # ---- staging hardlinks (for gh release upload) ------------------------------
    chart_uploads, asset_uploads = [], []
    skip_staging = args.dry_run or args.no_staging
    if not skip_staging:
        if staging.exists():
            shutil.rmtree(staging)
        ch_stage = staging / "charts"
        as_stage = staging / "assets"
        ch_stage.mkdir(parents=True)
        as_stage.mkdir(parents=True)
        for e, zl, il, pl, zr, ir, pr in plan:
            dst = ch_stage / zr
            link_or_copy(zl, dst)
            chart_uploads.append(str(dst))
            for src, name in ((il, ir), (pl, pr)):
                dst2 = as_stage / name
                if not dst2.exists():
                    link_or_copy(src, dst2)
                    asset_uploads.append(str(dst2))
        write_json(root / "release-plan.json", {
            "tag": tag,
            "charts_tag": charts_tag,
            "assets_tag": assets_tag,
            "chart_count": len(chart_uploads),
            "asset_count": len(asset_uploads),
            "chart_files": chart_uploads,
            "asset_files": asset_uploads,
        })
        (root / "upload-charts.txt").write_text("\n".join(chart_uploads) + "\n")
        (root / "upload-assets.txt").write_text("\n".join(asset_uploads) + "\n")

    # ---- GitHub Pages content ---------------------------------------------------
    write_json(out / "catalog.json", catalog)
    for i in range(1, pages + 1):
        lo, hi = (i - 1) * PAGE_NUM, i * PAGE_NUM
        write_json(out / "popular" / f"page{i}.json", {"count": n, "results": entries[lo:hi]})
    for e, *_ in plan:
        write_json(out / "chart" / f"{e['id']}.json", e)
    write_json(out / "stub" / "rate.json", {"score": 0})
    write_json(out / "stub" / "best.json", {"score": 0, "accuracy": 0.0, "fullCombo": False})
    write_json(out / "stub" / "record-list.json", [])
    write_json(out / "stub" / "user.json", {
        "id": 1, "name": "Phira", "email": None, "hykb_uid": None, "avatar": None,
        "badge": None, "badges": [], "badgeNames": {}, "language": "zh-CN", "bio": None,
        "exp": 0, "rks": 0.0, "roles": 0,
        "joined": "1970-01-01T00:00:00Z", "last_login": "1970-01-01T00:00:00Z",
    })
    write_json(out / "version.json", {
        "tag": tag, "count": n, "pages": pages, "page_size": PAGE_NUM,
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pages_base": pages_base,
    })
    write_json(out / "health.json", {"ok": True, "service": "phira-static-pages", "tag": tag, "count": n, "pages": pages})

    # configs from templates
    conf_variants = {
        "phira-static.conf": ["phira-static.conf.tmpl", "phira-popular.conf.tmpl"],
        "phira-static.sgmodule": ["phira-static.sgmodule.tmpl", "phira-popular.sgmodule.tmpl"],
    }
    generated_configs = []
    for out_name, candidates in conf_variants.items():
        for cand in candidates:
            src = templates / cand
            if src.is_file():
                text = src.read_text(encoding="utf-8").replace("{{PAGES_BASE}}", pages_base)
                write_text(out / "shadowrocket" / out_name, text)
                generated_configs.append(out_name)
                break
        if out_name not in generated_configs:
            log(f"WARN template missing for {out_name} (looked in {templates})")

    # docs + index
    setup = f"""# Phira × Phigros 静态版（GitHub 托管）

本仓库由 GitHub Actions 自动生成，共 **{n}** 张谱面（{pages} 页，每页 {PAGE_NUM} 条），
资源托管在 GitHub Releases，数据 JSON 托管在 GitHub Pages。不需要任何本地服务器。

## 手机（iPhone / iPad）使用步骤

1. 安装并信任 Shadowrocket 证书（配置 → ⓘ → HTTPS 解密 → 生成 CA → 安装证书 → 设置里信任 → 回到配置开启 HTTPS 解密）。
2. 在 Safari 打开下面链接导入配置（或复制到 Shadowrocket → 配置 → ➕）：
   `{pages_base}/shadowrocket/phira-static.conf`
   已有自己代理配置的用户请改用模块：
   `{pages_base}/shadowrocket/phira-static.sgmodule`
3. 打开 Phira → 谱面库 → **未上架(Unstable)** → 筛选(漏斗) → 分区选 **观赏(Visual)**。
4. 即可看到 Phigros 曲目：可翻页（1/{pages}）、打开、下载、游玩。

> 静态模式的已知限制：热门/上架/特殊/本地等其它页面保持官方内容；
> 「观赏」页的**搜索框、排序、标签/评分筛选在静态模式下都不生效**（始终显示固定顺序的分页）；
> 翻页正常。若需要完整搜索/排序，请使用项目里的隧道+本地服务器版本。

## 数据
- 版本标记：`{tag}`（数据变化会自动换新 tag 以绕过 App 的 URL 缓存）
- catalog：`{pages_base}/catalog.json`
- 谱面包 / 曲绘：GitHub Releases（见 catalog 内 URL）

## 更新数据
GitHub → Actions → “Build & Publish Phira Static Data” → Run workflow。完成后本页与配置会自动更新。
"""
    write_text(out / "SETUP.md", setup)
    index = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<title>Phira × Phigros 静态资源</title>
<style>body{{font:16px/1.6 -apple-system,sans-serif;max-width:760px;margin:40px auto;padding:0 16px;color:#222;background:#fafafa}}
code{{background:#eee;padding:2px 6px;border-radius:4px;word-break:break-all}}a{{color:#06c}}</style>
<h1>Phira × Phigros 静态资源</h1>
<p>共 <b>{n}</b> 张谱面 · {pages} 页 · 数据版本 <code>{tag}</code></p>
<h2>手机使用</h2>
<ol>
<li>安装并信任 Shadowrocket 证书，开启 HTTPS 解密。</li>
<li>导入配置：<a href="shadowrocket/phira-static.conf">phira-static.conf</a>（已有配置的用户用
    <a href="shadowrocket/phira-static.sgmodule">phira-static.sgmodule</a> 模块）。</li>
<li>Phira → 谱面库 → <b>未上架</b> → 筛选 → 分区选 <b>观赏</b>。</li>
</ol>
<h2>数据文件</h2>
<ul>
<li><a href="catalog.json">catalog.json</a> · <a href="version.json">version.json</a> · <a href="health.json">health.json</a></li>
<li><a href="popular/page1.json">popular/page1.json</a> … page{pages}.json（静态分页，每页 {PAGE_NUM} 条）</li>
<li>谱面包与曲绘在 GitHub Releases（URL 见 catalog.json）。</li>
</ul>
<p><a href="SETUP.md">完整说明</a></p>
</html>
"""
    write_text(out / "index.html", index)
    write_text(out / ".nojekyll", "")

    # quick self-check
    page1 = json.loads((out / "popular" / "page1.json").read_text())
    assert page1["count"] == n and len(page1["results"]) == min(PAGE_NUM, n)
    assert (out / "chart" / f"{entries[0]['id']}.json").is_file()
    for cfg in generated_configs:
        txt = (out / "shadowrocket" / cfg).read_text()
        assert "{{PAGES_BASE}}" not in txt, f"unreplaced placeholder in {cfg}"
    log(f"OK: {n} entries, {pages} pages, {len(generated_configs)} configs, tag={tag}")
    if skip_staging:
        log("staging skipped (dry-run); pages generated at " + str(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
