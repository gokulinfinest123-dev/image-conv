"""Compare EXIF metadata between a source image and a pipeline-processed one.

Usage:
    python check_metadata.py <original_url> <new_url>

Example:
    python check_metadata.py \
        "https://m.media-amazon.com/images/I/711l4y8aNlL._SL1500_.jpg" \
        "https://pub-548cc40013de448cbedcd7afe79cff6f.r2.dev/products/b0xmrxtb4b/472a0a55ea3942c49a41538744410832-v1.webp"
"""

import sys

import httpx
import piexif


def _dump_exif(label: str, image_bytes: bytes) -> None:
    print(f"\n=== {label} ===")
    try:
        exif_dict = piexif.load(image_bytes)
    except Exception as e:
        print(f"  No readable EXIF ({type(e).__name__}: {e})")
        return

    any_tag = False
    for ifd_name in ("0th", "Exif", "GPS", "1st"):
        ifd = exif_dict.get(ifd_name) or {}
        for tag_id, value in ifd.items():
            tag_name = piexif.TAGS[ifd_name].get(tag_id, {}).get("name", str(tag_id))
            print(f"  [{ifd_name}] {tag_name}: {value}")
            any_tag = True
    if not any_tag:
        print("  (EXIF block present but empty)")


def main() -> None:
    if len(sys.argv) != 3:
        print("Usage: python check_metadata.py <original_url> <new_url>")
        sys.exit(2)

    original_url, new_url = sys.argv[1], sys.argv[2]

    with httpx.Client(follow_redirects=True, timeout=60) as client:
        original_bytes = client.get(original_url).content
        new_bytes = client.get(new_url).content

    _dump_exif("ORIGINAL (source)", original_bytes)
    _dump_exif("NEW (pipeline output)", new_bytes)

    print(
        "\nWhat to look for:\n"
        "- ORIGINAL should have little/no EXIF (typical for web-optimized "
        "product photos), or whatever the source actually embedded.\n"
        "- NEW should show a fake Make/Model (Canon/Nikon/Sony/Fuji/"
        "Panasonic), a fake lens, aperture, ISO, and a DateTime within the "
        "last ~180 days — and NO GPS tags at all.\n"
        "- If NEW shows the SAME Make/Model/DateTime as ORIGINAL, or any "
        "GPS data, something is wrong with the scrub step."
    )


if __name__ == "__main__":
    main()
