"""FastAPI service that wraps the image/copy pipeline.

Endpoints:
    GET  /health                        health + counts
    GET  /search?q=...&limit=20         catalog match (metadata only)
    GET  /product/{asin}                cached product; kicks off materialization
                                        the first time it is called
    POST /materialize/{asin}            force re-materialization (bypass cache)

All endpoints except /health require an X-API-Key header matching SERVICE_API_KEY.
"""

import asyncio
import logging

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

from src.config import CFG
from src.image_variations import make_provider
from src.pipeline import materialize

from .cache import Cache
from .catalog import Catalog
from .config import (
    ALLOWED_ORIGINS,
    CACHE_DB_PATH,
    CATALOG_PATH,
    SERVICE_API_KEY,
)
from .schemas import HealthResponse, ProductResponse, SearchHit, SearchResponse

logger = logging.getLogger("service")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

app = FastAPI(title="Product Materialization API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS or ["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


def _require_key(x_api_key: str | None = Header(default=None)) -> None:
    if not SERVICE_API_KEY:
        raise HTTPException(500, "SERVICE_API_KEY is not configured on the server")
    if x_api_key != SERVICE_API_KEY:
        raise HTTPException(401, "invalid or missing X-API-Key")


@app.on_event("startup")
async def _startup() -> None:
    app.state.catalog = Catalog(CATALOG_PATH)
    app.state.cache = Cache(CACHE_DB_PATH)
    app.state.provider = make_provider() if CFG.generate_images else None
    app.state.http = httpx.AsyncClient()
    app.state.inflight: dict[str, asyncio.Task] = {}
    logger.info("catalog loaded: %d rows", len(app.state.catalog))


@app.on_event("shutdown")
async def _shutdown() -> None:
    await app.state.http.aclose()


@app.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    return HealthResponse(
        ok=True,
        catalog_size=len(request.app.state.catalog),
        cached=request.app.state.cache.count(),
    )


def _price_of(row: dict) -> tuple[float | None, str | None]:
    for key in ("Price (INR)", "price", "Price"):
        if key in row and row[key] is not None:
            try:
                return float(row[key]), "INR"
            except (TypeError, ValueError):
                pass
    delivery = row.get("Delivery") or {}
    price = delivery.get("price") or {}
    if price.get("value") is not None:
        return float(price["value"]), price.get("currency") or "INR"
    return None, None


def _hit_from_row(row: dict, cache: Cache) -> SearchHit:
    asin = str(row.get(CFG.col_id, ""))
    price, currency = _price_of(row)
    cached = cache.get(asin)
    status = cached["status"] if cached else "cold"
    return SearchHit(
        asin=asin,
        title=str(row.get(CFG.col_title, "")),
        price=price,
        currency=currency,
        source_image_url=row.get(CFG.col_image_url),
        status=status,
    )


@app.get(
    "/search",
    response_model=SearchResponse,
    dependencies=[Depends(_require_key)],
)
async def search(request: Request, q: str, limit: int = 20) -> SearchResponse:
    hits_rows = request.app.state.catalog.search(q, limit=limit)
    cache: Cache = request.app.state.cache
    hits = [_hit_from_row(r, cache) for r in hits_rows]
    return SearchResponse(query=q, count=len(hits), hits=hits)


async def _materialize_and_cache(request: Request, asin: str, row: dict) -> dict:
    cache: Cache = request.app.state.cache
    cache.put(asin, "processing", {})
    try:
        out = await materialize(
            row, request.app.state.provider, request.app.state.http
        )
        status = "error" if out.get("error") else "ready"
        cache.put(asin, status, out)
        return out
    except Exception as e:
        logger.exception("materialize failed for %s", asin)
        cache.put(asin, "error", {"error": f"{type(e).__name__}: {e}"})
        raise


def _kick_off(request: Request, asin: str, row: dict) -> None:
    inflight: dict[str, asyncio.Task] = request.app.state.inflight
    if asin in inflight and not inflight[asin].done():
        return
    task = asyncio.create_task(_materialize_and_cache(request, asin, row))
    inflight[asin] = task
    task.add_done_callback(lambda _t: inflight.pop(asin, None))


@app.get(
    "/product/{asin}",
    response_model=ProductResponse,
    dependencies=[Depends(_require_key)],
)
async def product(request: Request, asin: str) -> ProductResponse:
    cached = request.app.state.cache.get(asin)
    if cached and cached["status"] == "ready":
        return ProductResponse(asin=asin, status="ready", product=cached["product"])
    if cached and cached["status"] == "processing":
        return ProductResponse(asin=asin, status="processing")

    row = request.app.state.catalog.get(asin)
    if not row:
        raise HTTPException(404, f"asin {asin} not in catalog")

    _kick_off(request, asin, row)
    return ProductResponse(asin=asin, status="processing")


@app.post(
    "/materialize/{asin}",
    response_model=ProductResponse,
    dependencies=[Depends(_require_key)],
)
async def materialize_now(request: Request, asin: str) -> ProductResponse:
    row = request.app.state.catalog.get(asin)
    if not row:
        raise HTTPException(404, f"asin {asin} not in catalog")
    out = await _materialize_and_cache(request, asin, row)
    status = "error" if out.get("error") else "ready"
    return ProductResponse(asin=asin, status=status, product=out)
