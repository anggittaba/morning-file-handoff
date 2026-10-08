"""The branch export format and the checks every file must pass before it is delivered.

Each branch drops two files on a staging server:

    BR07_20261008_export.csv   data, with a header row
    BR07_20261008_export.ctl   control file written after the CSV: row count and SHA-256

The control file is how the receiver knows the CSV is complete and unchanged.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date

HEADER = ["record_id", "branch_id", "business_date", "doc_type", "item_code", "quantity", "amount"]
NAME_RE = re.compile(r"^(BR\d{2})_(\d{8})_export\.(csv|ctl)$")


def csv_name(branch_id: str, day: date) -> str:
    return f"{branch_id}_{day:%Y%m%d}_export.csv"


def ctl_name(branch_id: str, day: date) -> str:
    return f"{branch_id}_{day:%Y%m%d}_export.ctl"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def render_csv(rows: list[list[str]]) -> bytes:
    lines = [",".join(HEADER)] + [",".join(r) for r in rows]
    return ("\n".join(lines) + "\n").encode()


def render_ctl(file_name: str, day: date, rows: int, digest: str, created_at: str) -> bytes:
    return (f"file={file_name}\nbusiness_date={day.isoformat()}\nrows={rows}\n"
            f"sha256={digest}\ncreated_at={created_at}\n").encode()


def parse_ctl(data: bytes) -> dict[str, str]:
    out = {}
    for line in data.decode(errors="replace").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


@dataclass
class Check:
    ok: bool
    code: str  # "ok" or a short failure code
    detail: str  # plain sentence with the numbers, written to the audit log
    rows: int = 0
    sha256: str = ""
    size: int = 0


def validate(data: bytes, ctl: bytes | None, day: date) -> Check:
    """Run every check in order and stop at the first failure.

    Codes: empty, bad_header, no_control, truncated, checksum_mismatch, stale, ok.
    """
    size, digest = len(data), sha256(data)
    if size == 0:
        return Check(False, "empty", "file is 0 bytes", 0, digest, 0)
    text = data.decode(errors="replace")
    lines = text.split("\n")
    header = lines[0].strip().split(",")
    if header != HEADER:
        return Check(False, "bad_header", f"header is {lines[0][:80]!r}, expected {','.join(HEADER)!r}",
                     0, digest, size)
    complete_last_line = text.endswith("\n")
    body = [ln for ln in lines[1:] if ln != ""]
    rows = len(body)
    if ctl is None:
        return Check(False, "no_control", "control file (.ctl) not present yet", rows, digest, size)
    meta = parse_ctl(ctl)
    try:
        declared = int(meta.get("rows", "-1"))
    except ValueError:
        declared = -1
    bad_last = bool(body) and len(body[-1].split(",")) != len(HEADER)
    if declared >= 0 and rows < declared or not complete_last_line or bad_last:
        last = body[-1] if body else ""
        return Check(False, "truncated",
                     f"control file declares {declared} rows, file has {rows}; last line has "
                     f"{len(last.split(','))} of {len(HEADER)} fields"
                     f"{'' if complete_last_line else ' and no final newline'}", rows, digest, size)
    if meta.get("sha256", "") != digest:
        return Check(False, "checksum_mismatch",
                     f"sha256 mismatch: control file says {meta.get('sha256', '')[:12]}…, file is {digest[:12]}… "
                     f"(rows match: {rows} of {declared})", rows, digest, size)
    if declared != rows:
        return Check(False, "truncated", f"control file declares {declared} rows, file has {rows}", rows, digest, size)
    dates = {ln.split(",")[2] for ln in body}
    if dates != {day.isoformat()}:
        found = ", ".join(sorted(dates))
        return Check(False, "stale", f"business_date in rows is {found}, expected {day.isoformat()}", rows, digest, size)
    return Check(True, "ok", f"{rows} rows, header ok, sha256 {digest[:12]}… matches control file", rows, digest, size)
