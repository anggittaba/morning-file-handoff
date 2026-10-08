"""Rule-based failure classifier with plain-English templates.

This is the fallback when no LLM key is set, and it always runs: its category is stored
next to any LLM explanation so the two can be compared. It reads only what the engine
logged (the stage and the error text), never the simulator's injected labels.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Template:
    title: str
    explanation: str
    likely_cause: str
    next_step: str
    decided_by: str  # "automatic" or "person"


TEMPLATES: dict[str, Template] = {
    "late": Template(
        "File is late",
        "No export file for today has reached any staging server, and the branch's expected time has passed.",
        "The branch's end-of-day export has not run yet, is still running, or saved the file somewhere the "
        "staging servers do not collect from.",
        "The engine keeps looking every pass. If the deadline is close, contact the branch to check that the "
        "export ran.",
        "person"),
    "missing": Template(
        "No file by the deadline",
        "No export file for today reached any staging server before the deadline.",
        "The branch's export did not run or failed, or the file was saved to the wrong place.",
        "Contact the branch. If they re-send, the next pass picks it up; otherwise mark it handled or skip it "
        "with a reason.",
        "person"),
    "empty": Template(
        "Empty file",
        "The file arrived but contains 0 bytes, so there is nothing to deliver.",
        "The export job created the file and then stopped before writing, often after a crash or a full disk "
        "at the branch.",
        "Ask the branch to run the export again. A corrected file is picked up on the next pass automatically.",
        "person"),
    "truncated": Template(
        "File is cut short",
        "The file has fewer rows than its control file declares, and the last line is incomplete.",
        "The upload to staging, or the export itself, stopped part-way through.",
        "Ask the branch to re-send both the CSV and the control file. Do not deliver the partial file.",
        "person"),
    "checksum_mismatch": Template(
        "Checksum does not match",
        "The row count matches the control file, but the file's SHA-256 does not, so the content changed after "
        "the control file was written.",
        "Someone edited the file after export, or a transfer altered it (for example a text-mode copy changing "
        "line endings).",
        "Ask the branch to re-send both files from the original export. A corrected file is picked up "
        "automatically.",
        "person"),
    "duplicate_conflict": Template(
        "Two different copies",
        "The same file name turned up with different contents (on two staging servers, or after one copy was "
        "already delivered), so the engine cannot tell which one is right.",
        "The branch exported twice (for example a re-run after a fix) and both copies were collected.",
        "Confirm with the branch which copy is correct and remove the other from staging. If nothing was "
        "delivered yet, the next pass sends the remaining one; if a copy was already delivered, the engine waits "
        "until a person presses Retry.",
        "person"),
    "stale": Template(
        "Old data under today's name",
        "The file name has today's date, but the rows inside are dated another day.",
        "The export ran with the wrong business date, or an old file was re-sent under a new name.",
        "Ask the branch to re-run today's export. Do not deliver this file.",
        "person"),
    "bad_header": Template(
        "Unexpected header",
        "The first line of the file does not match the expected column list.",
        "The export template changed at the branch, or a different report was saved under this name.",
        "Check the file with the branch before anything is delivered.",
        "person"),
    "incomplete_upload": Template(
        "Control file never arrived",
        "The CSV is on staging but its control file did not arrive before the deadline, so completeness cannot "
        "be checked.",
        "The branch's upload stopped after the CSV, or the control file step failed.",
        "Ask the branch to re-send the control file, or confirm the CSV by phone and mark it handled.",
        "person"),
    "auth_failure": Template(
        "Destination rejected the login",
        "The destination endpoint refused the username and password the engine has stored.",
        "The endpoint's password was changed or has expired, or the account was locked.",
        "Check the credentials with the endpoint owner and update them, then authorise one retry. The engine "
        "does not retry logins by itself, to avoid locking the account.",
        "person"),
    "disk_full": Template(
        "Destination disk is full",
        "The destination accepted the login but failed the write with 'no space left on device'.",
        "The destination server's disk or the account's quota is full.",
        "Ask the endpoint owner to free space, then authorise one retry. Nothing partial was left in place.",
        "person"),
    "permission_denied": Template(
        "No permission to write",
        "The login works, but the destination refused to create the file in its inbound folder.",
        "The folder's permissions or the account's rights changed.",
        "Ask the endpoint owner to restore write access to the inbound folder, then authorise one retry.",
        "person"),
    "network": Template(
        "Connection problem",
        "The connection dropped or could not be opened during the transfer.",
        "A network blip, or the endpoint restarted or is overloaded.",
        "The engine retries on its own (within the pass and on later passes). If it keeps failing, check that "
        "the endpoint is up.",
        "automatic"),
    "verify_failed": Template(
        "Upload check failed",
        "After uploading, the size or checksum read back from the destination did not match.",
        "The transfer was corrupted or the destination altered the file.",
        "The engine retries the upload. If it fails again, check the destination's storage.",
        "automatic"),
    "unknown": Template(
        "Unrecognised error",
        "The engine logged an error that none of the rules recognise.",
        "Unknown; see the raw error in the timeline.",
        "Read the timeline and decide whether to retry, handle it by hand, or skip it.",
        "person"),
}

RULES: list[tuple[str, str, re.Pattern | None, str]] = [
    # (rule name, stage it applies to or "*", pattern on the error text, category)
    ("login refused", "connect", re.compile(r"permission denied|authentication|auth fail", re.I), "auth_failure"),
    ("connection failed", "connect", re.compile(r"refused|connect call failed|timed? ?out|unreachable|reset|lost|closed|no route|"
                                             r"name or service not known|errno", re.I),
     "network"),
    ("no space on write", "upload", re.compile(r"no space left|disk full|quota", re.I), "disk_full"),
    ("write refused", "upload", re.compile(r"permission denied|access denied|read-only", re.I), "permission_denied"),
    ("connection dropped", "upload", re.compile(r"connection (lost|reset|closed)|broken pipe|timed? ?out|eof", re.I),
     "network"),
    ("read-back mismatch", "verify", None, "verify_failed"),
    ("zero bytes", "validate", re.compile(r"\b0 bytes\b"), "empty"),
    ("rows short", "validate", re.compile(r"declares \d+ rows, file has \d+|no final newline", re.I), "truncated"),
    ("checksum differs", "validate", re.compile(r"sha256 mismatch|checksum", re.I), "checksum_mismatch"),
    ("wrong business date", "validate", re.compile(r"business_date .* expected", re.I), "stale"),
    ("header differs", "validate", re.compile(r"^header is", re.I), "bad_header"),
    ("no control file", "validate", re.compile(r"control file .*not present", re.I), "incomplete_upload"),
    ("copies differ", "discover", re.compile(r"different (copies|contents|copy)", re.I), "duplicate_conflict"),
    ("not found by deadline", "discover", re.compile(r"not found by the deadline", re.I), "missing"),
    ("not found, past expected time", "discover", re.compile(r"not found; expected by", re.I), "late"),
]


@dataclass
class Result:
    category: str
    rule: str
    template: Template


def classify(stage: str | None, text: str | None) -> Result:
    stage, text = stage or "", text or ""
    for name, rule_stage, pattern, category in RULES:
        if rule_stage not in ("*", stage):
            continue
        if pattern is None or pattern.search(text):
            return Result(category, name, TEMPLATES[category])
    return Result("unknown", "no rule matched", TEMPLATES["unknown"])


def signature(status: str, stage: str | None, text: str | None) -> str:
    return hashlib.sha1(f"{status}|{stage}|{text}".encode()).hexdigest()[:16]
