import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


def _get(name: str, default: str | None = None, required: bool = False) -> str:
    v = os.getenv(name, default)
    if required and not v:
        raise RuntimeError(f"Missing required env var: {name}")
    return v or ""


@dataclass(frozen=True)
class Config:
    image_provider: str = _get("IMAGE_PROVIDER", "openai")

    openai_api_key: str = _get("OPENAI_API_KEY", "")
    openai_image_model: str = _get("OPENAI_IMAGE_MODEL", "gpt-image-1")
    openai_image_size: str = _get("OPENAI_IMAGE_SIZE", "1024x1024")
    openai_image_quality: str = _get("OPENAI_IMAGE_QUALITY", "high")

    replicate_api_token: str = _get("REPLICATE_API_TOKEN", "")
    replicate_model: str = _get("REPLICATE_MODEL", "black-forest-labs/flux-kontext-pro")

    anthropic_api_key: str = _get("ANTHROPIC_API_KEY", "")
    anthropic_model: str = _get("ANTHROPIC_MODEL", "claude-opus-4-7")
    rewrite_copy: bool = _get("REWRITE_COPY", "true").lower() == "true"

    s3_endpoint_url: str = _get("S3_ENDPOINT_URL", "")
    s3_access_key_id: str = _get("S3_ACCESS_KEY_ID", "")
    s3_secret_access_key: str = _get("S3_SECRET_ACCESS_KEY", "")
    s3_bucket: str = _get("S3_BUCKET", "")
    s3_region: str = _get("S3_REGION", "auto")
    public_base_url: str = _get("PUBLIC_BASE_URL", "").rstrip("/")

    variations_per_image: int = int(_get("VARIATIONS_PER_IMAGE", "1"))
    concurrency: int = int(_get("CONCURRENCY", "4"))
    output_format: str = _get("OUTPUT_FORMAT", "webp").lower()
    output_quality: int = int(_get("OUTPUT_QUALITY", "85"))

    col_id: str = _get("COL_ID", "id")
    col_title: str = _get("COL_TITLE", "title")
    col_description: str = _get("COL_DESCRIPTION", "description")
    col_image_url: str = _get("COL_IMAGE_URL", "image_url")

    # Optional columns for schemas that don't have a plain description field
    # (e.g. Brand/Model/Selling-Price/Discount scrapes). Leave blank to disable
    # the feature that depends on it.
    col_brand: str = _get("COL_BRAND", "")
    col_gallery_image_urls: str = _get("COL_GALLERY_IMAGE_URLS", "")
    col_selling_price: str = _get("COL_SELLING_PRICE", "")
    col_discount_pct: str = _get("COL_DISCOUNT_PCT", "")
    col_mrp: str = _get("COL_MRP", "MRP (INR)")
    gallery_url_separator: str = _get("GALLERY_URL_SEPARATOR", "|")

    # false = do not call the AI image-edit provider at all; just re-encode
    # the original image(s) with EXIF stripped + fake EXIF injected.
    generate_images: bool = _get("GENERATE_IMAGES", "true").lower() == "true"

    # Nested per-variant list (e.g. "Weight Variants": [{"Weight":...,
    # "ASIN":...}, ...]). Leave COL_VARIANTS_LIST blank to disable — each
    # variant dict gets a new id column added alongside its existing fields.
    # COL_VARIANT_TITLE / COL_VARIANT_IMAGE_URL: if a variant carries its own
    # title/image, leave these set to also rewrite/scrub those per variant
    # (blank disables that sub-feature only).
    col_variants_list: str = _get("COL_VARIANTS_LIST", "")
    col_variant_asin: str = _get("COL_VARIANT_ASIN", "ASIN")
    col_variant_title: str = _get("COL_VARIANT_TITLE", "")
    col_variant_image_url: str = _get("COL_VARIANT_IMAGE_URL", "")

    # Name of the generated-ID column, used both top-level and inside each
    # variant. Blank falls back to "new_<COL_ID>" / "new_<COL_VARIANT_ASIN>".
    new_id_column_name: str = _get("NEW_ID_COLUMN_NAME", "")

    # Comma-separated top-level field names to drop from the output row
    # entirely (e.g. "Rating,Reviews Count,Best Sellers Rank").
    drop_columns: tuple[str, ...] = tuple(
        c.strip() for c in _get("DROP_COLUMNS", "").split(",") if c.strip()
    )

    # Deterministic (no AI) ASIN -> generated-ID lookup written as .xlsx
    # after each run. Blank disables it.
    asin_mapping_path: str = _get("ASIN_MAPPING_PATH", "asin_mapping.xlsx")


CFG = Config()
