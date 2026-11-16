"""Small text helpers shared by CLI and TUI."""
import re
import unicodedata
from datetime import datetime, timezone

ANSI_LINK_RE = re.compile(r"\033\]8;;[^\033]*\033\\")
ANSI_COLOR_RE = re.compile(r"\033\[[0-9;]*m")


def ago(iso_or_ts):
    """Human 'age' for an ISO timestamp (GitHub updatedAt) or unix seconds."""
    if isinstance(iso_or_ts, (int, float)):
        s = int(max(0, iso_or_ts))
    else:
        t = datetime.fromisoformat(str(iso_or_ts).replace("Z", "+00:00"))
        s = int((datetime.now(timezone.utc) - t).total_seconds())
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= n:
            return f"{s // n}{unit}"
    return f"{s}s"


def vlen(s):
    """Visible length of a string carrying ANSI colors and OSC8 links."""
    s = ANSI_LINK_RE.sub("", s)
    return len(ANSI_COLOR_RE.sub("", s))


def wcw(s):
    """Terminal cell width, counting CJK wide chars as 2 columns."""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def fit(s, w):
    """Truncate/pad a string to exactly w terminal cells ('…' when cut)."""
    out, n = "", 0
    for c in s:
        cw = wcw(c)
        if n + cw > w - (1 if wcw(s) > w else 0):
            break
        out, n = out + c, n + cw
    if wcw(s) > w:
        out += "…"
        n += 1
    return out + " " * max(0, w - n)


def osc8(text, url, on):
    """Wrap text in an OSC8 hyperlink when `on` (terminal supports it)."""
    return f"\033]8;;{url}\033\\{text}\033]8;;\033\\" if on and url else text