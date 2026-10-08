import pytest

from handoff import classify

CASES = [
    ("connect", "PermissionDenied: Permission denied for user br20-drop on host 127.0.0.1", "auth_failure"),
    ("connect", "OSError: [Errno 111] Connect call failed ('127.0.0.1', 2301)", "network"),
    ("upload", "attempt 1 of 3 to dest-br22: SFTPFailure: No space left on device", "disk_full"),
    ("upload", "SFTPPermissionDenied: Permission denied (dest-br23)", "permission_denied"),
    ("upload", "attempt 1 of 3 to dest-br09: ConnectionLost: Connection lost", "network"),
    ("verify", "checksum after upload does not match", "verify_failed"),
    ("validate", "file is 0 bytes", "empty"),
    ("validate", "control file declares 812 rows, file has 503; last line has 3 of 7 fields", "truncated"),
    ("validate", "sha256 mismatch: control file says 1a2b…, file is 9f8e… (rows match: 812 of 812)",
     "checksum_mismatch"),
    ("validate", "business_date in rows is 2026-10-07, expected 2026-10-08", "stale"),
    ("validate", "header is 'a,b', expected 'record_id,...'", "bad_header"),
    ("validate", "control file (.ctl) not present by the deadline 07:30", "incomplete_upload"),
    ("discover", "different contents under the same name: staging-01 (...) and staging-05 (...)",
     "duplicate_conflict"),
    ("discover", "BR03_20261008_export.csv not found by the deadline 07:30; searched 9 of 9 staging servers", "missing"),
    ("discover", "BR03_20261008_export.csv not found; expected by 06:20; searched 9 of 9", "late"),
    ("upload", "something nobody has seen before", "unknown"),
]


@pytest.mark.parametrize("stage,text,expected", CASES)
def test_rules(stage, text, expected):
    assert classify.classify(stage, text).category == expected


def test_same_words_different_stage():
    """'Permission denied' at login is a credentials problem; during upload it is a folder permission problem."""
    assert classify.classify("connect", "Permission denied").category == "auth_failure"
    assert classify.classify("upload", "Permission denied").category == "permission_denied"


def test_every_category_has_a_template():
    cats = {c for *_, c in classify.RULES} | {"unknown"}
    assert cats <= set(classify.TEMPLATES)
    for t in classify.TEMPLATES.values():
        assert t.decided_by in ("person", "automatic") and t.next_step.endswith(".")


def test_signature_changes_with_the_error():
    assert classify.signature("failed", "upload", "a") != classify.signature("failed", "upload", "b")
