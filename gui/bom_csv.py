"""Read the CSV written by bomer2.py (default format) for deducting a BOM
from stock.

The default format has a few "Key:, value" lines, then a header row starting
with Refs, id, Qty, …, one row per component group, and a total row at the
end. Only the default format has the component id; the compact (-c) and JLC
(-j) formats cannot be used.
"""

import csv
from dataclasses import dataclass, field


@dataclass
class BomLine:
    dev_id: int
    qty: int
    refs: list = field(default_factory=list)
    value: str = ""


@dataclass
class Bom:
    lines: list            # BomLine, one per component id (groups merged)
    source: str = ""       # the schematic, from the "Source:" line
    skipped: list = field(default_factory=list)  # refs without a usable id


def read(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    source, header_at = "", None
    for i, row in enumerate(rows):
        if row and row[0].strip() == "Source:" and len(row) > 1:
            source = row[1].strip()
        if row and row[0].strip() == "Refs" and "id" in row and "Qty" in row:
            header_at = i
            break
    if header_at is None:
        raise ValueError("No 'Refs, id, Qty' header found. Use the default "
                         "bomer2.py format (without -c or -j).")
    header = rows[header_at]
    c_refs, c_id, c_qty = header.index("Refs"), header.index("id"), header.index("Qty")
    c_val = header.index("Value") if "Value" in header else None
    lines, skipped = {}, []
    for row in rows[header_at + 1:]:
        if len(row) <= max(c_refs, c_id, c_qty) or not row[c_refs].strip():
            continue  # total line or empty
        refs = [r.strip() for r in row[c_refs].split(",") if r.strip()]
        try:
            dev_id, qty = int(row[c_id]), int(row[c_qty])
        except ValueError:
            skipped += refs
            continue
        if dev_id <= 0:
            skipped += refs
            continue
        line = lines.setdefault(dev_id, BomLine(dev_id, 0, [],
                                                row[c_val] if c_val is not None else ""))
        line.qty += qty
        line.refs += refs
    return Bom(list(lines.values()), source, skipped)
