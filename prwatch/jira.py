"""Jira data access via acli, and linked-PR lookups via twg.

Every JQL used by the UIs lives in config.py; this module only executes
them and normalizes the field shapes.
"""
import re
import time

from . import config, tools

PR_URL_RE = re.compile(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)")

# twg is slow (~5s per batch) and its answers are stable for minutes, so the
# linked-PR result is cached briefly.
_JPR_CACHE = {}


def fetch(jql):
    """Run one JQL via acli and normalize each ticket to:
    {key, summary, status, category, priority, type, labels}."""
    out = tools.run(
        ["acli", "jira", "workitem", "search", "--jql", jql, "--json",
         "--limit", str(config.JIRA_LIMIT),
         "--fields", "key,summary,status,priority,issuetype,labels"],
        timeout=config.JIRA_TIMEOUT)
    if not out.strip():
        return []
    items = []
    for i in _parse(out):
        f = i["fields"]
        items.append({"key": i["key"], "summary": f.get("summary", ""),
                      "status": (f.get("status") or {}).get("name", "?"),
                      "category": ((f.get("status") or {}).get("statusCategory") or {}).get("name", ""),
                      "priority": (f.get("priority") or {}).get("name", "-"),
                      "type": (f.get("issuetype") or {}).get("name", "-"),
                      "labels": f.get("labels") or []})
    return items


def _parse(out):
    import json
    return json.loads(out)


def fetch_all(site):
    """The three Jira views in one pass. `site` is the base URL used for
    ticket links. Returns {active: [...], sdet: [...], backlog: [...]}."""
    def add_url(tickets):
        return [dict(t, url=f"{site}/browse/{t['key']}") for t in tickets]

    return {
        "active": add_url(fetch(config.JQL_ACTIVE)),
        "sdet": add_url(fetch(config.JQL_SDET)),
        "backlog": add_url(fetch(config.JQL_BACKLOG)),
    }


def site():
    """Jira base URL from acli's login info, e.g. https://<site>.atlassian.net."""
    out = tools.run(["acli", "jira", "auth", "status"], check=False)
    m = re.search(r"Site:\s*(\S+)", out)
    return f"https://{m.group(1)}" if m else None


def status_of(key):
    """Current status of one ticket; used to explain where a ticket went
    after it left the watched lists. Returns None when acli fails."""
    try:
        out = tools.run(["acli", "jira", "workitem", "view", key,
                         "--fields", "status", "--json"], timeout=30)
        return _parse(out)["fields"]["status"]["name"]
    except (tools.ToolError, KeyError, TypeError):
        return None


def linked_prs(keys):
    """PRs linked to each ticket, from Jira's own development info (so it
    includes PRs authored by anyone). Returns {key: [pr-item, ...]}.

    pr-item: {url, repo, number, title, state, branch, base, ci, review,
    merge, draft, approvals} — enriched with live GitHub state when possible.
    """
    now = time.time()
    stale = [k for k in keys if k not in _JPR_CACHE or now - _JPR_CACHE[k][0] > config.JPR_TTL]
    for i in range(0, len(stale), config.JPR_BATCH):
        batch = stale[i:i + config.JPR_BATCH]
        got = _fetch_batch(batch)
        for k in batch:
            _JPR_CACHE[k] = (now, got.get(k, []))
    return {k: _JPR_CACHE[k][1] for k in keys}


def _fetch_batch(batch):
    cmd = ["twg", "context", "jira", "workitem", *batch,
           "--since", config.TWG_SINCE, "--detail", "full",
           "--types", "ExternalPullRequest", "-o", "json", "--output-summary", "none"]
    doc = tools.run_json_query(cmd, timeout=config.TWG_TIMEOUT)
    items = doc.get("items") or [{"identifier": batch[0], "data": doc}]  # single key: no items wrapper
    got = {}
    for it in items:
        prs = []
        for rel in (it.get("data") or {}).get("relationships", []):
            for t in rel.get("targets", []):
                m = PR_URL_RE.search(t.get("url") or "")
                if not m:
                    continue
                st = t.get("status", "OPEN")  # DRAFT is normalized to OPEN + draft
                prs.append({"url": t["url"], "repo": f"{m.group(1)}/{m.group(2)}",
                            "number": int(m.group(3)), "title": t.get("title", ""),
                            "state": "OPEN" if st == "DRAFT" else st,
                            "branch": (t.get("sourceBranch") or {}).get("name", ""),
                            "base": (t.get("destinationBranch") or {}).get("name", ""),
                            "ci": "NONE", "review": None, "merge": None,
                            "draft": st == "DRAFT", "approvals": 0})
        got[it["identifier"]] = sorted(prs, key=lambda p: p["number"])
    from . import github
    github.enrich([p for v in got.values() for p in v])
    return got