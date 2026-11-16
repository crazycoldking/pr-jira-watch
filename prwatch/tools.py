"""Shell-out helpers: run an external CLI and parse its output.

Every external tool (gh / acli / twg) is invoked through this module so
error handling, timeouts and JSON parsing stay consistent.
"""
import json
import subprocess


class ToolError(RuntimeError):
    """An external CLI failed (non-zero exit, bad JSON, timeout)."""


def run(cmd, timeout=None, check=True):
    """Run a command, return stdout as text. On failure raise ToolError
    with whatever message the tool printed (stderr preferred)."""
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise ToolError(f"{cmd[0]} timed out after {timeout}s") from e
    if check and out.returncode != 0:
        msg = (out.stderr or out.stdout).strip()
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