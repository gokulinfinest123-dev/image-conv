import csv
import io
import json
from pathlib import Path

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential


def load_rows(path: str) -> list[dict]:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() == ".json":
        data = json.loads(text)
        if isinstance(data, dict) and "products" in data:
            return list(data["products"])
        if isinstance(data, list):
            return data
        raise ValueError(f"Unrecognized JSON shape in {path}")
    reader = csv.DictReader(io.StringIO(text))
    return list(reader)


def _checkpoint_path(output_path: str) -> Path:
    """.json output uses a .jsonl sidecar as the append-only checkpoint;
    the aggregated .json file is written at the end of the run."""
    p = Path(output_path)
    if p.suffix.lower() == ".json":
        return p.with_suffix(".jsonl")
    return p


def _csv_encode(v):
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, bool):
        return "true" if v else "false"
    return v


def append_output_row(output_path: str, row: dict, fieldnames: list[str]) -> None:
    p = Path(output_path)
    ext = p.suffix.lower()
    ckpt = _checkpoint_path(output_path)
    ckpt.parent.mkdir(parents=True, exist_ok=True)

    if ext in (".json", ".jsonl"):
        with open(ckpt, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return

    exists = ckpt.exists()
    with open(ckpt, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if not exists:
            w.writeheader()
        w.writerow({k: _csv_encode(v) for k, v in row.items()})


def already_processed_ids(output_path: str, id_field: str = "id") -> set[str]:
    """IDs that succeeded on a prior run. A row with a non-empty `error`
    does NOT count as done — it's left out so the next run retries it."""
    p = _checkpoint_path(output_path)
    if not p.exists():
        return set()
    ext = p.suffix.lower()
    if ext == ".jsonl":
        ids: set[str] = set()
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                if r.get(id_field) and not r.get("error"):
                    ids.add(str(r[id_field]))
        return ids
    with open(p, newline="", encoding="utf-8") as f:
        return {
            r[id_field]
            for r in csv.DictReader(f)
            if r.get(id_field) and not r.get("error")
        }


def finalize_output(output_path: str, id_field: str = "id") -> None:
    """When the user asked for .json, aggregate the .jsonl checkpoint into a
    single JSON array. Safe to call multiple times; no-op for csv/jsonl.

    A retried row is appended after its earlier failed attempt, so when
    collapsing to one row per ID, the LAST occurrence wins — the retry's
    result (success or a newer error) replaces the stale one."""
    p = Path(output_path)
    if p.suffix.lower() != ".json":
        return
    ckpt = p.with_suffix(".jsonl")
    if not ckpt.exists():
        return
    by_id: dict[str, dict] = {}
    order: list[str] = []
    with open(ckpt, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            key = str(row.get(id_field, "")) or f"__row_{len(order)}"
            if key not in by_id:
                order.append(key)
            by_id[key] = row
    rows = [by_id[k] for k in order]
    with open(p, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)


@retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=1, min=2, max=16))
async def download_bytes(url: str, client: httpx.AsyncClient) -> bytes:
    r = await client.get(url, timeout=60, follow_redirects=True)
    r.raise_for_status()
    return r.content
