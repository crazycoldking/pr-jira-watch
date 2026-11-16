"""Single-source rules shared by all three UIs.

`is_ready` is THE definition of "mergeable": GitHub's `mergeable` field only
means "no git conflict", so it must never drive a green badge on its own.
The web UI mirrors this rule in JS (prwatch_web.py PAGE) — change both together.
"""
import re

BACKPORT_RE = re.compile(r"backport", re.I)
ORIG_RE = re.compile(r"(?:Automated backport to .*? from #|Backport of \S*/pull/|Backport of #)(\d+)", re.I)

JIRA_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
JIRA_URL_RE = re.compile(r"/browse/([A-Z][A-Z0-9]+-\d+)")
JIRA_LABEL_RE = re.compile(r"jira\W{0,4}([A-Z][A-Z0-9]+-\d+)", re.I)
# Bare KEY-123 matches in a PR body are noisy; these prefixes are never tickets.
NOT_JIRA = {"UTF", "SHA", "ISO", "TLS", "HTTP", "RFC", "CVE", "MD", "X", "AES", "RSA"}

CI_BAD = ("FAILURE", "ERROR")


def find_jira(pr):
    """Guess the Jira key of a PR, in order: branch name, title, then body
    (a /browse/KEY link first, then 'Jira: KEY', then bare KEY-123)."""
    for text in (pr["headRefName"], pr["title"]):
        m = JIRA_RE.search(text)
        if m:
            return m.group(1)
    body = pr.get("body") or ""
    for rx in (JIRA_URL_RE, JIRA_LABEL_RE):
        m = rx.search(body)
        if m:
            return m.group(1)
    for m in JIRA_RE.finditer(body):
        if m.group(1).rsplit("-", 1)[0] not in NOT_JIRA:
            return m.group(1)
    return None


def is_backport(pr):
    return bool(BACKPORT_RE.search(pr["title"]) or pr["headRefName"].startswith("backport"))


def backport_of(pr):
    """Original PR number parsed from the body; None when not a backport,
    0 when it is one but the body doesn't say which."""
    if not is_backport(pr):
        return None
    m = ORIG_RE.search(pr.get("body") or "")
    return int(m.group(1)) if m else 0


def is_ready(draft, merge_state, review, approvals, changes):
    """True when GitHub says the PR can merge (CLEAN: no conflict, required
    checks passed, not behind base) AND a human actually approved it."""
    return (not draft and merge_state == "CLEAN" and review != "CHANGES_REQUESTED"
            and (review == "APPROVED" or (approvals > 0 and not changes)))


# merge_state -> (CLI cell text, color key) for states that are neither
# conflict, draft, changes-requested nor ready.
MERGE_BLOCKERS = {"BEHIND": ("behind", "y"), "BLOCKED": ("blocked", "y"),
                  "UNSTABLE": ("unstable", "y"), "HAS_HOOKS": ("hooks", "y"),
                  "CLEAN": ("no-approve", "d")}


def merge_cell(draft, merge, merge_state, review, approvals, changes):
    """(text, color key) for the MERGE column: green only when is_ready,
    otherwise say what is blocking."""
    if merge == "CONFLICTING" or merge_state == "DIRTY":
        return "conflict", "r"
    if draft:
        return "draft", "d"
    if review == "CHANGES_REQUESTED":
        return "changes", "r"
    if is_ready(draft, merge_state, review, approvals, changes):
        return "ready", "g"
    return MERGE_BLOCKERS.get(merge_state, ("?", "d"))


def severity(draft, ci, merge, review, approvals, changes, merge_state):
    """Urgency score shared by CLI/TUI/web sorting: higher = needs attention;
    -1 means ready-to-merge (it can go, stop highlighting it)."""
    if draft:
        return 0
    s = 1
    if ci in CI_BAD:
        s += 4
    if merge == "CONFLICTING":
        s += 3
    if review == "CHANGES_REQUESTED":
        s += 2
    if is_ready(draft, merge_state, review, approvals, changes):
        s = -1
    return s


def item_from_pr(pr, jira_url, me_login):
    """Normalize one raw GitHub PR node into the dict every UI renders.
    Single source of truth — previously web and TUI each had a copy that
    drifted (the web copy lacked fields the TUI added later)."""
    reviews = pr["latestOpinionatedReviews"]["nodes"]
    approvals = sum(r["state"] == "APPROVED" for r in reviews)
    changes = sum(r["state"] == "CHANGES_REQUESTED" for r in reviews)
    jira = find_jira(pr)
    ci = ci_of(pr)
    mergeable = pr["mergeable"]
    merge_state = pr["mergeStateStatus"]
    review = pr["reviewDecision"]
    return {
        "url": pr["url"], "repo": pr["repository"]["nameWithOwner"], "number": pr["number"],
        "title": pr["title"], "draft": pr["isDraft"], "branch": pr["headRefName"],
        "base": pr["baseRefName"], "updatedAt": pr["updatedAt"],
        "ci": ci, "review": review, "approvals": approvals, "changes": changes,
        "merge": mergeable, "mergeState": merge_state,
        "backport": backport_of(pr),
        "bot": pr["author"]["login"] != me_login,
        "jira": jira,
        "jiraUrl": f"{jira_url.rstrip('/')}/browse/{jira}" if jira and jira_url else None,
        # derived, precomputed so UIs don't each re-derive (and drift)
        "ready": is_ready(pr["isDraft"], merge_state, review, approvals, changes),
        "severity": severity(pr["isDraft"], ci, mergeable, review, approvals, changes, merge_state),
    }


def ci_of(pr):
    """CI state from a raw GitHub node (same rollup rule as github.ci_state)."""
    from .github import ci_state
    return ci_state(pr)


def sort_items(items):
    """Standard order everywhere: most urgent first, ties by recency."""
    return sorted(items, key=lambda p: (-p["severity"], p["updatedAt"]), reverse=False) or items