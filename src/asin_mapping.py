"""Write an ASIN -> generated-ID lookup table as .xlsx.

Pure data export, no AI involved: reads the already-processed output rows
(top-level + every nested variant) and lists each (original ASIN, new ID)
pair so you can match them up outside the pipeline.
"""

import json
from pathlib import Path

from openpyxl import Workbook

from .config import CFG


def _new_id_column() -> str:
    return CFG.new_id_column_name or ("new_" + CFG.col_id)


def _variant_id_column() -> str:
    return CFG.new_id_column_name or ("new_" + CFG.col_variant_asin)


def _load_output_rows(output_path: str) -> list[dict]:
    p = Path(output_path)
    if p.suffix.lower() == ".jsonl":
        rows = []
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        return rows
    if p.suffix.lower() == ".json":
        return json.loads(p.read_text(encoding="utf-8"))
    return []  # .csv output JSON-encodes nested variants as a string cell;
    # not supported for this export — use .json/.jsonl output if you need it.


def build_mapping_rows(rows: list[dict]) -> list[tuple[str, str]]:
    id_col = _new_id_column()
    variant_id_col = _variant_id_column()
    pairs: list[tuple[str, str]] = []

    for row in rows:
        orig = str(row.get(CFG.col_id, "")).strip()
        new = str(row.get(id_col, "")).strip()
        if orig and new:
            pairs.append((orig, new))

        if CFG.col_variants_list:
            variants = row.get(CFG.col_variants_list)
            if isinstance(variants, list):
                for v in variants:
                    if not isinstance(v, dict):
                        continue
                    v_orig = str(v.get(CFG.col_variant_asin, "")).strip()
                    v_new = str(v.get(variant_id_col, "")).strip()
                    if v_orig and v_new:
                        pairs.append((v_orig, v_new))

    return pairs


def write_mapping_from_output(output_path: str, mapping_path: str) -> None:
    rows = _load_output_rows(output_path)
    pairs = build_mapping_rows(rows)
    if not pairs:
        return

    wb = Workbook()
    ws = wb.active
    ws.title = "ASIN Mapping"
    ws.append(["ASIN", _new_id_column()])
    for orig, new in pairs:
        ws.append([orig, new])
    wb.save(mapping_path)
