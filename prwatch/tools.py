"""Shell-out helpers: run an external CLI and parse its output.

Every external tool (gh / acli / twg) is invoked through this module so
error handling, timeouts and JSON parsing stay consistent.
"""
import json
import subprocess
import time


class ToolError(RuntimeError):
    """An external CLI failed (non-zero exit, bad JSON, timeout)."""


_TRANSIENT = ("HTTP 502", "HTTP 503", "HTTP 504", "stream error", "connection reset",
              "EOF", "timeout", "temporarily unavailable")
RETRIES = 3


def run(cmd, timeout=None, check=True):
    """Run a command, return stdout as text. On failure raise ToolError
    with whatever message the tool printed (stderr preferred).
    gh calls are retried with backoff on transient GitHub 5xx errors."""
    attempts = RETRIES if cmd and cmd[0] == "gh" else 1
    for i in range(attempts):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise ToolError(f"{cmd[0]} timed out after {timeout}s") from e
        if out.returncode == 0 or not check:
            return out.stdout
        msg = (out.stderr or out.stdout).strip()
        if i < attempts - 1 and any(t.lower() in msg.lower() for t in _TRANSIENT):
            time.sleep(1.5 * (i + 1))
            continue
        raise ToolError(msg or f"{cmd[0]} exited {out.returncode}")
    return out.stdout


def run_json(cmd, timeout=None):
    """Run a command and parse stdout as JSON."""
    out = run(cmd, timeout=timeout)
    try:
        return json.loads(out)
    except ValueError as e:
        raise ToolError(f"{cmd[0]} returned non-JSON output: {out[:200]}") from e


def run_json_query(cmd, timeout=None, data_key="data"):
    """Run a command whose stdout is a JSON envelope like {"ok": .., "data": ..}
    (twg style). Raises on non-zero exit, non-JSON, ok:false and missing data."""
    doc = run_json(cmd, timeout=timeout)
    if doc.get("ok") is False:
        raise ToolError(str((doc.get("error") or {}).get("message", doc)))
    if data_key not in doc:
        raise ToolError(f"{cmd[0]} output has no '{data_key}': {str(doc)[:200]}")
    return doc[data_key]


def available(name):
    """True when the given executable exists on PATH."""
    import shutil
    return shutil.which(name) is not None