"""End-to-end orchestrator: one product row → new image(s) + copy."""

import asyncio
import random
import string
import traceback

import httpx
from tqdm.asyncio import tqdm_asyncio

from .asin_mapping import write_mapping_from_output
from .config import CFG
from .content_rewrite import generate_from_title_brand, rewrite, rewrite_titles
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


class RowImages:
    """Turns one product's source image URLs into scrubbed, uploaded copies.

    A product's variants keep pointing at the same handful of pictures, so
    each distinct URL is converted once and every reference shares the
    result. `limit` caps conversions in flight; hand the same semaphore to
    every product to cap the whole run."""

    def __init__(self, http: httpx.AsyncClient, limit: asyncio.Semaphore):
        self._http = http
        self._limit = limit
        self._tasks: dict[str, asyncio.Task] = {}

    async def _convert(self, url: str, new_id: str, idx: int) -> str:
        async with self._limit:
            raw = await download_bytes(url, self._http)
            return await _process_and_upload(raw, new_id, idx)

    def convert(self, url: str, new_id: str, idx: int) -> asyncio.Task:
        task = self._tasks.get(url)
        if task is None:
            task = self._tasks[url] = asyncio.create_task(self._convert(url, new_id, idx))
        return task

    def cancel(self) -> None:
        for task in self._tasks.values():
            task.cancel()


def _variant_pending(v) -> bool:
    """A variant that already carries the id column was finished by an
    earlier run and is left exactly as it is."""
    return isinstance(v, dict) and not str(v.get(_variant_id_column(), "")).strip()


async def _process_one_variant(v, images: RowImages):
    """Add the new-ID column and scrub+replace the variant's own image and
    gallery. Every other field is left untouched (its title is rewritten
    separately, batched with the product's other variants).

    A variant that isn't pending is returned as-is — the same object — so
    re-running the pipeline over previously-processed output only pays for
    variants it hasn't touched yet."""
    if not _variant_pending(v):
        return v
    v = dict(v)
    new_id = _new_asin_like(str(v.get(CFG.col_variant_asin, "")).strip())
    v[_variant_id_column()] = new_id

    image_col = CFG.col_variant_image_url
    image_url = str(v.get(image_col, "") or "").strip() if image_col else ""
    gallery_col = CFG.col_variant_gallery_image_urls or CFG.col_gallery_image_urls
    gallery_urls = _split_gallery(str(v.get(gallery_col, "") or "")) if gallery_col else []

    jobs = [images.convert(image_url, new_id, 1)] if image_url else []
    jobs += [images.convert(u, new_id, i + 2) for i, u in enumerate(gallery_urls)]
    converted = await asyncio.gather(*jobs)

    if image_url:
        v[image_col] = converted.pop(0)
    if gallery_urls:
        v[gallery_col] = _join_gallery(converted)
    return v


async def _process_variants(variants: list, images: RowImages) -> list:
    return list(await asyncio.gather(*[_process_one_variant(v, images) for v in variants]))


def _pending_titles(variant_lists) -> list[str]:
    if not CFG.col_variant_title or not CFG.rewrite_titles:
        return []
    return [
        title
        for variants in variant_lists
        for v in variants
        if _variant_pending(v)
        and (title := str(v.get(CFG.col_variant_title, "") or "").strip())
    ]


def _apply_titles(source: list, processed: list, titles: dict[str, str]) -> list:
    if titles:
        for before, after in zip(source, processed):
            if after is before:
                continue  # not processed this run
            new = titles.get(str(before.get(CFG.col_variant_title, "") or "").strip())
            if new:
                after[CFG.col_variant_title] = new
    return processed


async def _write_copy(title: str, desc: str, brand: str) -> tuple[str, str]:
    if desc.strip():
        return await rewrite(title, desc)
    if brand.strip():
        return await generate_from_title_brand(title, brand)
    return await rewrite(title, "")


async def materialize(
    row: dict,
    provider,
    http: httpx.AsyncClient,
    image_limit: asyncio.Semaphore | None = None,
) -> dict:
    """Run the pipeline against a single input row and return the processed
    output dict. Pure computation — no locks, no file I/O. Safe to call from
    a web handler. Errors are caught and reported in the returned dict's
    `error` field so callers always get a usable row.

    A row that already carries the id column from a prior successful run
    (no `error`) has its top-level image/title/description left untouched —
    no re-download, no re-upload, no AI call. This makes it cheap to re-run
    the pipeline over its own previous output: nested variant lists (see
    below) still get checked and any variant that isn't done yet still gets
    processed, but a row that's already correct doesn't cost anything again."""
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

    id_col = _new_id_column()
    already_done = bool(str(row.get(id_col, "")).strip()) and not row.get("error")

    out: dict = dict(row)
    out["error"] = ""

    if not already_done:
        new_id = _new_asin_like(pid)
        out[id_col] = new_id
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
    new_id = out[id_col]

    images = RowImages(http, image_limit or asyncio.Semaphore(CFG.image_concurrency))

    async def top_level(copy: asyncio.Future) -> dict:
        if CFG.generate_images:
            source_bytes = await download_bytes(image_url, http)
            variants, (new_title, new_desc) = await asyncio.gather(
                generate_variations(provider, source_bytes, CFG.variations_per_image),
                copy,
            )
            uploaded = await asyncio.gather(
                *[_process_and_upload(v, new_id, i + 1) for i, v in enumerate(variants)]
            )
            fields = {_variant_col(idx): url for idx, url in enumerate(uploaded, start=1)}
        else:
            (new_title, new_desc), main, *gallery = await asyncio.gather(
                copy,
                images.convert(image_url, new_id, 1),
                *[images.convert(u, new_id, i + 2) for i, u in enumerate(gallery_urls)],
            )
            fields = {SINGLE_IMAGE_COL: main, CFG.col_image_url: main}
            if gallery_urls:
                fields[CFG.col_gallery_image_urls] = _join_gallery(gallery)
        fields[CFG.col_title] = new_title
        fields[CFG.col_description or FALLBACK_DESCRIPTION_COL] = new_desc
        return fields

    # Everything this row starts, so a failure can stop the rest instead of
    # leaving LLM calls and uploads running for a row that's already lost.
    work: list[asyncio.Future] = []
    try:
        if not already_done and not image_url:
            raise ValueError("empty image_url")

        # Nested per-variant lists are checked (and, per-variant, processed)
        # independently of `already_done` above — a product can be fully
        # done at the top level while a variant list added later (or newly
        # named in COL_VARIANTS_LIST) still has unprocessed entries.
        variant_lists = {
            name: row[name]
            for name in CFG.col_variants_lists
            if isinstance(row.get(name), list)
        }

        # LLM calls and image work are independent, so they all start now
        # and overlap rather than waiting on each other.
        titles = asyncio.ensure_future(
            rewrite_titles(_pending_titles(variant_lists.values()), brand)
        )
        work.append(titles)
        top = None
        if not already_done:
            copy = asyncio.ensure_future(_write_copy(title, desc, brand))
            top = asyncio.ensure_future(top_level(copy))
            work += [copy, top]
        lists = [
            asyncio.ensure_future(_process_variants(variants, images))
            for variants in variant_lists.values()
        ]
        work += lists
        await asyncio.gather(*work)

        if top is not None:
            out.update(top.result())
        for (name, variants), processed in zip(variant_lists.items(), lists):
            out[name] = _apply_titles(variants, processed.result(), titles.result())

    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        traceback.print_exc()
    finally:
        for task in work:
            task.cancel()
        images.cancel()

    return out


async def process_row(
    row: dict,
    provider,
    http: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    output_path: str,
    fieldnames: list[str],
    image_limit: asyncio.Semaphore,
) -> None:
    async with sem:
        out = await materialize(row, provider, http, image_limit)
        append_output_row(output_path, out, fieldnames)


async def run(input_path: str, output_path: str) -> None:
    rows = load_rows(input_path)
    fieldnames = build_fieldnames(rows)
    done = already_processed_ids(output_path, CFG.col_id)
    todo = [r for r in rows if str(r.get(CFG.col_id, "")).strip() not in done]
    print(f"Loaded {len(rows)} rows; {len(done)} already processed; {len(todo)} to do.")

    if not todo:
        finalize_output(output_path, CFG.col_id)
        return

    provider = make_provider() if CFG.generate_images else None
    sem = asyncio.Semaphore(CFG.concurrency)
    image_limit = asyncio.Semaphore(CFG.image_concurrency)
    async with httpx.AsyncClient() as http:
        tasks = [
            process_row(r, provider, http, sem, output_path, fieldnames, image_limit)
            for r in todo
        ]
        await tqdm_asyncio.gather(*tasks, desc="Products")

    finalize_output(output_path, CFG.col_id)

    if CFG.asin_mapping_path:
        write_mapping_from_output(output_path, CFG.asin_mapping_path)
        print(f"ASIN mapping written to {CFG.asin_mapping_path}")
