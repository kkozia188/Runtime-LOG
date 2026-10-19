"""MCAP (Runtime-LOG JSON cycles) -> CSV, for spreadsheets and tools without MCAP support."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path


def mcap_to_csv(path: Path) -> bytes:
    from mcap.reader import make_reader

    out = io.StringIO()
    writer = None
    with path.open("rb") as stream:
        for _, _, message in make_reader(stream).iter_messages():
            row = json.loads(message.data)
            if writer is None:
                columns = ["time_s", "seq", "period_us"] + [k for k in row if k not in ("seq", "period_us")]
                writer = csv.writer(out)
                writer.writerow(columns)
            writer.writerow([f"{message.log_time / 1e9:.6f}", row.get("seq"), row.get("period_us")]
                            + [row.get(k) for k in columns[3:]])
    return out.getvalue().encode("utf-8-sig")  # BOM: Excel opens UTF-8 correctly
