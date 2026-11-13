"""Upload bytes to any S3-compatible bucket (Cloudflare R2, AWS S3, ...)."""

import asyncio
import uuid
from functools import lru_cache

import boto3
from botocore.config import Config as BotoConfig
from slugify import slugify

from .config import CFG


@lru_cache(maxsize=1)
def _s3_client():
    if not CFG.s3_bucket:
        raise RuntimeError("S3_BUCKET is required")
    kwargs = {
        "aws_access_key_id": CFG.s3_access_key_id,
        "aws_secret_access_key": CFG.s3_secret_access_key,
        "region_name": CFG.s3_region,
        "config": BotoConfig(
            signature_version="s3v4",
            max_pool_connections=max(10, CFG.image_concurrency),
        ),
    }
    if CFG.s3_endpoint_url:
        kwargs["endpoint_url"] = CFG.s3_endpoint_url
    return boto3.client("s3", **kwargs)


def make_key(product_id: str, variant_idx: int, extension: str) -> str:
    slug = slugify(product_id, max_length=40) or "product"
    return f"products/{slug}/{uuid.uuid4().hex}-v{variant_idx}.{extension}"


def _put_sync(key: str, data: bytes, content_type: str) -> None:
    _s3_client().put_object(
        Bucket=CFG.s3_bucket,
        Key=key,
        Body=data,
        ContentType=content_type,
        CacheControl="public, max-age=31536000, immutable",
    )


async def upload(key: str, data: bytes, content_type: str) -> str:
    await asyncio.to_thread(_put_sync, key, data, content_type)
    base = CFG.public_base_url or (
        CFG.s3_endpoint_url.rstrip("/") + "/" + CFG.s3_bucket
        if CFG.s3_endpoint_url else ""
    )
    if not base:
        raise RuntimeError("Set PUBLIC_BASE_URL or S3_ENDPOINT_URL to derive the URL")
    return f"{base}/{key}"
