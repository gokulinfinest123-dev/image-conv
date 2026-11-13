# Product Image Variation Pipeline

Take a CSV/JSON of products, generate 2–3 AI-edited variants of each product image (same product, new angle/lighting/background), scrub and randomize EXIF, rename, re-encode to WebP, upload to your S3-compatible bucket (Cloudflare R2, AWS S3, ...), rewrite the title + description via Claude, and emit an updated CSV with fresh URLs. No trace of the original image or copy is preserved.

## Pipeline

```
CSV/JSON  ─►  download  ─►  AI image edit (N variants)  ─►  scrub + randomize EXIF
                                                         ─►  re-encode WebP
                                                         ─►  rename (uuid) + upload
                                                         ─►  rewrite title/description (Claude)
                                                         ─►  output CSV
```

## Install

```
pip install -r requirements.txt
cp .env.example .env
# fill in .env
```

## Run

```
python run.py sample_products.csv out/products_new.csv
python run.py sample_products.json out/products_new.json     # JSON array
python run.py sample_products.json out/products_new.jsonl    # NDJSON
```

Output format is picked from the extension. All three are resumable —
kill the run and re-invoke with the same command; already-processed IDs
are skipped. For `.json` output a `.jsonl` sidecar is written during the
run and the final `.json` array is emitted at the end.

## Configuration

Everything in `.env`. Key knobs:

- `IMAGE_PROVIDER` — `openai` (gpt-image-1) or `replicate` (Flux Kontext)
- `VARIATIONS_PER_IMAGE` — default 3
- `CONCURRENCY` — default 4 products in flight
- `IMAGE_CONCURRENCY` — default 8 image downloads/uploads in flight across all products; lower it if the image host throttles you
- `REWRITE_COPY` — set `false` to skip Claude rewrites
- `REWRITE_TITLES` — default `false`: titles are kept exactly as in the input and only the description is written. Set `true` to rewrite titles (main and variants) too
- `COL_ID` / `COL_TITLE` / `COL_DESCRIPTION` / `COL_IMAGE_URL` — map your input columns

## Input formats

**CSV** (header row required):

```
id,title,description,image_url
SKU-001,...,...,https://...
```

**JSON** — either a list of objects or `{"products": [...]}`.

## Output

```
<id_col>,new_<id_col>,<title_col>,<description_col>,<image_url_col>,variant_1_url,variant_2_url,variant_3_url,error
```

Two ID columns: the original (preserved) and a freshly generated ASIN-style
lookalike (`B0` + 8 random uppercase alphanumeric) so downstream systems can
be seeded with new IDs while keeping the mapping to the source.

Example row:
```
B0FV2PQ1NQ,B0K7X2M9QW,<rewritten title>,<rewritten desc>,<original url>,https://cdn.../v1.webp,...,...,
```

Failed rows are still written with the `error` column populated so you can retry them.

## What gets erased

- Original EXIF (make/model, timestamps, GPS, software, thumbnail, comments) — nuked by re-encoding
- Filename — regenerated as `products/<slug>/<uuid>-vN.webp`
- Perceptual similarity — the image is regenerated, not just re-encoded
- Copy — rewritten with different structure/wording, facts preserved

## What gets injected

- Fake but plausible EXIF (random camera make/model, lens, aperture, ISO, timestamp within the last 6 months, no GPS)
- New URL path with no relationship to the source

## Layout

```
src/
├── config.py            # .env loader
├── prompts.py           # edit prompts + rewrite system prompt
├── io_utils.py          # CSV/JSON load, checkpointed output, http download
├── metadata.py          # scrub + randomize EXIF, re-encode
├── image_variations.py  # pluggable OpenAI / Replicate provider
├── storage.py           # S3-compatible upload
├── content_rewrite.py   # Claude-based title/desc rewriter
└── pipeline.py          # orchestrator
run.py                   # CLI
```
