#!/usr/bin/env python3
"""Build an Oh Sh!t reaction library from a local folder of GIFs.

Run inside the ROOT of the GitHub Pages repository. No GitHub tokens or network
calls are made by this program; use GitHub Desktop to commit + push the output.
"""
from __future__ import annotations

import bisect
import hashlib
import json
import math
import re
import shutil
import sys
import tempfile
import unicodedata
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    print("Missing Pillow. Run: py -3 -m pip install --user Pillow")
    raise SystemExit(2)

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "publisher-config.json"
MAX_REACTIONS = 500
MAX_FRAMES = 24


def read_config():
    if not CONFIG_PATH.is_file():
        raise RuntimeError(f"Missing publisher-config.json in {ROOT}")
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    source_value = str(cfg.get("gif_folder", "gifs")).strip() or "gifs"
    source = Path(source_value).expanduser()
    if not source.is_absolute():
        source = ROOT / source
    source = source.resolve()
    if not source.is_dir():
        raise RuntimeError(f"GIF folder does not exist: {source}. Edit publisher-config.json first.")
    fps = int(cfg.get("fps", 5))
    max_seconds = float(cfg.get("max_seconds", 3))
    max_width = int(cfg.get("max_width", 560))
    quality = int(cfg.get("jpeg_quality", 79))
    if fps < 1 or fps > 8 or max_seconds < .5 or max_seconds > 5 or not (180 <= max_width <= 960) or not (60 <= quality <= 95):
        raise RuntimeError("Invalid settings: fps=1..8, max_seconds=.5..5, max_width=180..960, jpeg_quality=60..95")
    if fps * max_seconds > MAX_FRAMES:
        raise RuntimeError(f"fps x max_seconds must be <= {MAX_FRAMES}; try fps=5, max_seconds=3")
    return source, fps, max_seconds, max_width, quality


def clean_slug(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii").lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return (s[:48].strip("-") or "reaction")


def readable_title(stem):
    s = re.sub(r"[_-]+", " ", stem).strip()
    return s[:80] or "Reaction"


def extract_frame_samples(gif_file: Path, target_dir: Path, fps: int, max_seconds: float, width: int, quality: int):
    target_dir.mkdir(parents=True, exist_ok=True)
    with Image.open(gif_file) as img:
        if (getattr(img, "n_frames", 1) or 1) > 2000:
            raise ValueError("more than 2,000 GIF frames; trim this reaction first")
        duration_limit = 1000.0 * max_seconds
        images = []
        cumulative = []
        elapsed = 0.0
        for i in range(getattr(img, "n_frames", 1)):
            img.seek(i)
            # Pillow's GIF decoder handles palette transitions and disposal.
            im = img.convert("RGBA").copy()
            if im.width > width:
                im.thumbnail((width, 2000), Image.Resampling.LANCZOS)
            bg = Image.new("RGB", im.size, (18, 23, 27))
            bg.paste(im, mask=im.getchannel("A"))
            images.append(bg)
            millis = int(img.info.get("duration", 100) or 100)
            elapsed += max(30, min(30000, millis))
            cumulative.append(elapsed)
            if elapsed >= duration_limit:
                break
        if not images:
            raise ValueError("no frames found")
        duration_ms = min(elapsed, duration_limit)
        frame_count = min(MAX_FRAMES, max(1, math.ceil(duration_ms * fps / 1000)))
        outputs = []
        for k in range(frame_count):
            t = (k * 1000.0) / fps
            idx = bisect.bisect_right(cumulative, t)
            idx = min(len(images)-1, idx)
            filename = f"f{k+1:03}.jpg"
            images[idx].save(target_dir / filename, "JPEG", quality=quality, optimize=True)
            outputs.append(filename)
    return outputs, round(duration_ms / 1000.0, 3)


def build():
    src, fps, max_seconds, width, quality = read_config()
    files = sorted([p for p in src.rglob("*") if p.is_file() and p.suffix.lower() == ".gif"], key=lambda p: str(p).lower())
    if not files:
        raise RuntimeError(f"No .gif files found in {src}; no published files changed")
    if len(files) > MAX_REACTIONS:
        raise RuntimeError(f"Found {len(files)} GIFs. Limit is {MAX_REACTIONS} to protect TTS performance")
    docs = ROOT / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    new_reactions = []
    failures = []
    with tempfile.TemporaryDirectory(prefix="reaction-build-", dir=ROOT) as td:
        temp = Path(td)
        staged = temp / "reactions"
        staged.mkdir()
        for idx, gif in enumerate(files, 1):
            name = readable_title(gif.stem)
            short_rel = gif.relative_to(src).as_posix()
            unique_key = hashlib.sha1(short_rel.encode("utf8")).hexdigest()[:7]
            content_hash = hashlib.sha1(gif.read_bytes()).hexdigest()[:9]
            ident = clean_slug(gif.stem) + "-" + unique_key
            folder_name = ident + "-" + content_hash
            folder = staged / folder_name
            try:
                frames, seconds = extract_frame_samples(gif, folder, fps, max_seconds, width, quality)
            except Exception as e:
                shutil.rmtree(folder, ignore_errors=True)
                failures.append(f"{short_rel}: {e}")
                print(f"  SKIP {short_rel}: {e}")
                continue
            frame_paths = [f"reactions/{folder_name}/{fname}" for fname in frames]
            new_reactions.append({
                "id": ident,
                "name": name,
                "fps": fps,
                "durationSeconds": seconds,
                "frames": frame_paths,
            })
            print(f"  {idx}/{len(files)}  {name}: {len(frames)} frames ({seconds:.2f}s)")
        if not new_reactions:
            raise RuntimeError("Every GIF failed to convert; no published files changed")
        manifest = {
            "schema": 1,
            "title": "Oh Sh!t reaction library",
            "count": len(new_reactions),
            "fps": fps,
            "reactions": new_reactions,
        }
        content = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        if len(content) > 850000:
            raise RuntimeError(f"Manifest is {len(content)} bytes (TTS cap 850 KB). Lower GIF count")
        temp_manifest = temp / "library.json"
        temp_manifest.write_bytes(content)
        # Commit the staged library only after successful conversion of at least one GIF.
        dest_reactions = docs / "reactions"
        if dest_reactions.exists():
            shutil.rmtree(dest_reactions)
        shutil.move(str(staged), str(dest_reactions))
        shutil.copyfile(temp_manifest, docs / "library.json")
    (docs / ".nojekyll").touch()
    (docs / "index.html").write_text('''<!doctype html><html lang="en"><meta charset="utf-8">
<title>Oh Sh!t Reaction Library</title><meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{font:18px system-ui;margin:4rem auto;padding:0 1rem;max-width:660px;background:#15251e;color:#f4e9cf}a{color:#a8e2ff}</style>
<h1>Oh Sh!t Reaction Library</h1><p>Reaction images for a friends-only Tabletop Simulator mod.</p>
<p>Library index: <a href="library.json">library.json</a></p>
</html>''', encoding="utf8")
    print(f"\nREADY: {len(new_reactions)} GIFs, {sum(len(x['frames']) for x in new_reactions)} frames, index {len(content):,} bytes")
    print(f"Published output on disk: {docs}")
    print("\nNEXT: Open GitHub Desktop -> Commit changes -> Push origin.")
    print("Then in TTS: Set Library URL to https://YOUR_USERNAME.github.io/YOUR_REPO/library.json")
    if failures:
        print(f"\nWARNING: {len(failures)} GIF(s) skipped. See list above. Others will still work.")


if __name__ == "__main__":
    try:
        build()
    except Exception as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        sys.exit(1)
