"""End-to-end orchestrator: one product row → new image(s) + copy."""

import asyncio
import random
import string
import traceback

import httpx
from tqdm.asyncio import tqdm_asyncio

from .asin_mapping import write_mapping_from_output
from .config import CFG
from .content_rewrite import generate_from_title_brand, rewrite
from .image_variations import generate_variations, make_provider
from .io_utils import (
    already_processed_ids,
    append_output_row,
    download_bytes,
    finalize_output,
    load_rows,
)
from .metadata import scrub_and_randomize
from .storage import make_key, upload


CONTENT_TYPES = {
    "webp": "image/webp",
    "jpeg": "image/jpeg",
    "jpg": "image/jpeg",
    "png": "image/png",
}


NEW_ID_COL_PREFIX = "new_"
SINGLE_IMAGE_COL = "image_url"
FALLBACK_DESCRIPTION_COL = "Description"
_ID_ALPHABET = string.ascii_uppercase + string.digits


def _variant_col(idx: int) -> str:
    """When N=1, output the single image under `image_url`. When N>1, use
    the numbered `variant_N_url` columns so a row makes sense unambiguously."""
    if CFG.variations_per_image == 1:
        return SINGLE_IMAGE_COL
    return f"variant_{idx}_url"


def _new_asin_like(original: str) -> str:
    """Generate an ASIN-lookalike: 10 uppercase alphanumeric chars, B0-prefix."""
    length = max(len(original), 10) if original else 10
    body = "".join(random.choices(_ID_ALPHABET, k=length - 2))
    return f"B0{body}"


def _new_id_column() -> str:
    return CFG.new_id_column_name or (NEW_ID_COL_PREFIX + CFG.col_id)


def _variant_id_column() -> str:
    return CFG.new_id_column_name or (NEW_ID_COL_PREFIX + CFG.col_variant_asin)


def _split_gallery(raw: str) -> list[str]:
    return [u.strip() for u in raw.split(CFG.gallery_url_separator) if u.strip()]


def _join_gallery(urls: list[str]) -> str:
    return f" {CFG.gallery_url_separator} ".join(urls)


def _compute_mrp(row: dict) -> float | None:
    """MRP = Selling Price ÷ (1 − Discount%). Only used when the row doesn't
    already carry an MRP and both source columns are configured."""
    if not CFG.col_selling_price or not CFG.col_discount_pct:
        return None
    try:
        price = float(row[CFG.col_selling_price])
        discount_pct = float(row[CFG.col_discount_pct])
    except (KeyError, TypeError, ValueError):
        return None
    if not (0 <= discount_pct < 100):
        return None
    return round(price / (1 - discount_pct / 100), 2)


def build_fieldnames(rows: list[dict]) -> list[str]:
    """Union of every key across every input row, in first-seen order, plus
    the pipeline's added columns. Used as CSV header — irrelevant for JSON."""
    priority = [CFG.col_id, _new_id_column(), CFG.col_title]
    priority.append(CFG.col_description or FALLBACK_DESCRIPTION_COL)
    priority.append(CFG.col_image_url)

    tail: list[str] = []
    if CFG.generate_images:
        tail.extend(_variant_col(i) for i in range(1, CFG.variations_per_image + 1))
    else:
        tail.append(SINGLE_IMAGE_COL)
        if CFG.col_gallery_image_urls:
            tail.append(CFG.col_gallery_image_urls)
    tail.append(CFG.col_mrp)
    tail.append("error")

    seen: set[str] = set(priority) | set(tail)
    middle: list[str] = []
    for r in rows:
        for k in r.keys():
            k = str(k)
            if k not in seen:
                seen.add(k)
                middle.append(k)
    return priority + middle + tail


async def _process_and_upload(raw: bytes, new_id: str, idx: int) -> str:
    """Strip original EXIF, inject fake EXIF, re-encode, upload. No AI call."""
    ext = CFG.output_format
    ctype = CONTENT_TYPES.get(ext, "application/octet-stream")
    scrubbed = await asyncio.to_thread(scrub_and_randomize, raw, ext, CFG.output_quality)
    key = make_key(new_id or "product", idx, ext)
    return await upload(key, scrubbed, ctype)


async def _process_variant_image(url: str, http: httpx.AsyncClient, new_id: str) -> str:
    raw = await download_bytes(url, http)
    return await _process_and_upload(raw, new_id, 1)


async def _process_one_variant(v, http: httpx.AsyncClient, brand: str):
    """Add the new-ID column, and — if configured and present on this
    variant — scrub+replace its own image and rewrite its own title.
    Every other field on the variant is left untouched."""
    if not isinstance(v, dict):
        return v
    v = dict(v)
    variant_id = str(v.get(CFG.col_variant_asin, "")).strip()
    v[_variant_id_column()] = _new_asin_like(variant_id)

    image_task = None
    if CFG.col_variant_image_url:
        url = str(v.get(CFG.col_variant_image_url, "") or "").strip()
        if url:
            image_task = asyncio.create_task(
                _process_variant_image(url, http, v[_variant_id_column()])
            )

    title_task = None
    if CFG.col_variant_title:
        vt = str(v.get(CFG.col_variant_title, "") or "").strip()
        if vt:
            title_task = asyncio.create_task(generate_from_title_brand(vt, brand))

    if image_task is not None:
        v[CFG.col_variant_image_url] = await image_task
    if title_task is not None:
        new_title, _ = await title_task
        v[CFG.col_variant_title] = new_title

    return v


async def _process_variants(variants: list, http: httpx.AsyncClient, brand: str) -> list:
    return list(
        await asyncio.gather(*[_process_one_variant(v, http, brand) for v in variants])
    )


async def materialize(
    row: dict,
    provider,
    http: httpx.AsyncClient,
) -> dict:
    """Run the pipeline against a single input row and return the processed
    output dict. Pure computation — no locks, no file I/O. Safe to call from
    a web handler. Errors are caught and reported in the returned dict's
    `error` field so callers always get a usable row."""
    pid = str(row.get(CFG.col_id, "")).strip()
    title = str(row.get(CFG.col_title, "") or "")
    desc = str(row.get(CFG.col_description, "") or "") if CFG.col_description else ""
    brand = str(row.get(CFG.col_brand, "") or "") if CFG.col_brand else ""
    image_url = str(row.get(CFG.col_image_url, "") or "").strip()
    gallery_urls = (
        _split_gallery(str(row.get(CFG.col_gallery_image_urls, "") or ""))
        if CFG.col_gallery_image_urls
        else []
    )

    new_id = _new_asin_like(pid)

    out: dict = dict(row)
    out[_new_id_column()] = new_id
    out["error"] = ""
    if CFG.generate_images:
        for i in range(1, CFG.variations_per_image + 1):
            out.setdefault(_variant_col(i), "")
    else:
        out.setdefault(SINGLE_IMAGE_COL, "")

    for col in CFG.drop_columns:
        out.pop(col, None)

    if row.get(CFG.col_mrp) in (None, ""):
        mrp = _compute_mrp(row)
        if mrp is not None:
            out[CFG.col_mrp] = mrp

    try:
        if not image_url:
            raise ValueError("empty image_url")

        source_bytes = await download_bytes(image_url, http)

        if desc.strip():
            copy_task = rewrite(title, desc)
        elif brand.strip():
            copy_task = generate_from_title_brand(title, brand)
        else:
            copy_task = rewrite(title, "")

        if CFG.generate_images:
            variants_task = generate_variations(
                provider, source_bytes, CFG.variations_per_image
            )
            variants, (new_title, new_desc) = await asyncio.gather(
                variants_task, copy_task
            )
            uploaded = await asyncio.gather(
                *[_process_and_upload(v, new_id, i + 1) for i, v in enumerate(variants)]
            )
            for idx, url in enumerate(uploaded, start=1):
                out[_variant_col(idx)] = url
        else:
            gallery_bytes_task = asyncio.gather(
                *[download_bytes(u, http) for u in gallery_urls]
            )
            variants_raw = row.get(CFG.col_variants_list) if CFG.col_variants_list else None
            variants_task = (
                _process_variants(variants_raw, http, brand)
                if isinstance(variants_raw, list)
                else asyncio.sleep(0, result=None)
            )

            (new_title, new_desc), gallery_raw, processed_variants = await asyncio.gather(
                copy_task, gallery_bytes_task, variants_task
            )

            main_and_gallery = await asyncio.gather(
                _process_and_upload(source_bytes, new_id, 1),
                *[
                    _process_and_upload(raw, new_id, i + 2)
                    for i, raw in enumerate(gallery_raw)
                ],
            )
            out[SINGLE_IMAGE_COL] = main_and_gallery[0]
            out[CFG.col_image_url] = main_and_gallery[0]
            if gallery_urls:
                out[CFG.col_gallery_image_urls] = _join_gallery(main_and_gallery[1:])
            if processed_variants is not None:
                out[CFG.col_variants_list] = processed_variants

        out[CFG.col_title] = new_title
        if CFG.col_description:
            out[CFG.col_description] = new_desc
        else:
            out[FALLBACK_DESCRIPTION_COL] = new_desc

    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        traceback.print_exc()

    return out


async def process_row(
    row: dict,
    provider,
    http: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    output_path: str,
    fieldnames: list[str],
) -> None:
    async with sem:
        out = await materialize(row, provider, http)
        append_output_row(output_path, out, fieldnames)


async def run(input_path: str, output_path: str) -> None:
    rows = load_rows(input_path)
    fieldnames = build_fieldnames(rows)
    done = already_processed_ids(output_path, CFG.col_id)
    todo = [r for r in rows if str(r.get(CFG.col_id, "")).strip() not in done]
    print(f"Loaded {len(rows)} rows; {len(done)} already processed; {len(todo)} to do.")

    if not todo:
        finalize_output(output_path)
        return

    provider = make_provider() if CFG.generate_images else None
    sem = asyncio.Semaphore(CFG.concurrency)
    async with httpx.AsyncClient() as http:
        tasks = [
            process_row(r, provider, http, sem, output_path, fieldnames) for r in todo
        ]
        await tqdm_asyncio.gather(*tasks, desc="Products")

    finalize_output(output_path)

    if CFG.asin_mapping_path:
        write_mapping_from_output(output_path, CFG.asin_mapping_path)
        print(f"ASIN mapping written to {CFG.asin_mapping_path}")
