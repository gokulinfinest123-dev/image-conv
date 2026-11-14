"""Compare EXIF metadata between source images and pipeline-processed ones.

Two modes:

1) Single pair (unchanged):
    python check_metadata.py <original_url> <new_url>

2) Batch — checks every product's main + gallery image in one go:
    python check_metadata.py <input.json> <output.json>

Batch mode matches rows between the two files by COL_ID (see src/config.py,
e.g. ASIN), reads the original main/gallery URLs from the INPUT file (since
the pipeline overwrites those same fields in the OUTPUT file with the new
links), and prints an EXIF comparison for every image pair.
"""

import json
import sys
from pathlib import Path

import httpx
import piexif

from src.config import CFG


def _dump_exif(label: str, image_bytes: bytes, indent: str = "  ") -> None:
    print(f"{indent}--- {label} ---")
    try:
        exif_dict = piexif.load(image_bytes)
    except Exception as e:
        print(f"{indent}  No readable EXIF ({type(e).__name__}: {e})")
        return

    any_tag = False
    for ifd_name in ("0th", "Exif", "GPS", "1st"):
        ifd = exif_dict.get(ifd_name) or {}
        for tag_id, value in ifd.items():
            tag_name = piexif.TAGS[ifd_name].get(tag_id, {}).get("name", str(tag_id))
            print(f"{indent}  [{ifd_name}] {tag_name}: {value}")
            any_tag = True
    if not any_tag:
        print(f"{indent}  (EXIF block present but empty)")


def _fetch(client: httpx.Client, url: str) -> bytes | None:
    try:
        r = client.get(url)
        r.raise_for_status()
        return r.content
    except Exception as e:
        print(f"    ! download failed for {url}: {type(e).__name__}: {e}")
        return None


def _split_gallery(raw: str) -> list[str]:
    if not raw:
        return []
    return [u.strip() for u in raw.split(CFG.gallery_url_separator) if u.strip()]


def _load_rows(path: str) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    return data["products"] if isinstance(data, dict) and "products" in data else data


def run_batch(input_path: str, output_path: str) -> None:
    input_rows = _load_rows(input_path)
    output_rows = _load_rows(output_path)

    by_id = {str(r.get(CFG.col_id, "")).strip(): r for r in input_rows}

    with httpx.Client(follow_redirects=True, timeout=60) as client:
        for out_row in output_rows:
            pid = str(out_row.get(CFG.col_id, "")).strip()
            orig_row = by_id.get(pid)
            print(f"\n=== {CFG.col_id}={pid} ===")
            if not orig_row:
                print("  ! no matching row in input file, skipping")
                continue

            orig_main = str(orig_row.get(CFG.col_image_url, "") or "")
            new_main = str(out_row.get(CFG.col_image_url, "") or out_row.get("image_url", ""))
            if orig_main and new_main:
                print(f"  Main image: {orig_main}\n           -> {new_main}")
                orig_bytes = _fetch(client, orig_main)
                new_bytes = _fetch(client, new_main)
                if orig_bytes is not None:
                    _dump_exif("ORIGINAL", orig_bytes)
                if new_bytes is not None:
                    _dump_exif("NEW", new_bytes)
            else:
                print("  ! missing main image URL on one side, skipping")

            if CFG.col_gallery_image_urls:
                orig_gallery = _split_gallery(str(orig_row.get(CFG.col_gallery_image_urls, "") or ""))
                new_gallery = _split_gallery(str(out_row.get(CFG.col_gallery_image_urls, "") or ""))
                for i, (ou, nu) in enumerate(zip(orig_gallery, new_gallery), start=1):
                    print(f"  Gallery #{i}: {ou}\n            -> {nu}")
                    ob = _fetch(client, ou)
                    nb = _fetch(client, nu)
                    if ob is not None:
                        _dump_exif("ORIGINAL", ob)
                    if nb is not None:
                        _dump_exif("NEW", nb)

    print(
        "\nWhat to look for on every NEW image:\n"
        "- A fake Make/Model (Canon/Nikon/Sony/Fuji/Panasonic), fake lens, "
        "aperture, ISO, and a DateTime within the last ~180 days.\n"
        "- NO GPS tags at all.\n"
        "- If a NEW image matches its ORIGINAL's Make/Model/DateTime, or "
        "carries GPS data, the scrub step didn't run on it."
    )


def run_single(original_url: str, new_url: str) -> None:
    with httpx.Client(follow_redirects=True, timeout=60) as client:
        original_bytes = client.get(original_url).content
        new_bytes = client.get(new_url).content
    _dump_exif("ORIGINAL (source)", original_bytes, indent="")
    _dump_exif("NEW (pipeline output)", new_bytes, indent="")


def main() -> None:
    if len(sys.argv) != 3:
        print(
            "Usage:\n"
            "  python check_metadata.py <original_url> <new_url>\n"
            "  python check_metadata.py <input.json> <output.json>"
        )
        sys.exit(2)

    a, b = sys.argv[1], sys.argv[2]
    if a.lower().endswith(".json") and b.lower().endswith(".json"):
        run_batch(a, b)
    else:
        run_single(a, b)


if __name__ == "__main__":
    main()
