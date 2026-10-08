import random
from datetime import timedelta

from handoff import exportfile as ef
from handoff import generator as gen

from conftest import DAY, at


def pair(n=30, row_date=None):
    return gen.make_pair(random.Random(1), "BR07", DAY, n, at(5, 30), row_date=row_date)


def test_valid_file_passes():
    data, ctl = pair()
    c = ef.validate(data, ctl, DAY)
    assert c.ok and c.rows == 30 and c.sha256 == ef.sha256(data)


def test_empty():
    assert ef.validate(b"", b"rows=0\n", DAY).code == "empty"


def test_bad_header():
    data, ctl = pair()
    assert ef.validate(b"a,b,c\n1,2,3\n", ctl, DAY).code == "bad_header"


def test_no_control_file_yet():
    data, _ = pair()
    assert ef.validate(data, None, DAY).code == "no_control"


def test_truncated_mid_line():
    data, ctl = pair()
    c = ef.validate(data[: len(data) // 2], ctl, DAY)
    assert c.code == "truncated" and "declares 30 rows" in c.detail


def test_truncated_on_line_boundary():
    data, ctl = pair()
    cut = data[: data.rfind(b"\n", 0, len(data) - 1) + 1]  # drop the last full row
    assert ef.validate(cut, ctl, DAY).code == "truncated"


def test_checksum_mismatch_same_rows():
    data, ctl = pair()
    tampered = data.replace(b",SALE,", b",SAL3,", 1)
    c = ef.validate(tampered, ctl, DAY)
    assert c.code == "checksum_mismatch" and "rows match: 30 of 30" in c.detail


def test_stale_rows():
    data, ctl = pair(row_date=DAY - timedelta(days=1))
    c = ef.validate(data, ctl, DAY)
    assert c.code == "stale" and "2026-10-07" in c.detail


def test_names():
    assert ef.csv_name("BR07", DAY) == "BR07_20261008_export.csv"
    assert ef.NAME_RE.match(ef.ctl_name("BR07", DAY))
