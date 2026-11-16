"""GitHub data access: search PRs via gh, plus tiny identity/cache helpers."""
import re
import time

from . import config, tools

QUERY = """
query($q: String!) {
  search(query: $q, type: ISSUE, first: %(limit)d) {
    nodes {
      ... on PullRequest {
        number title url isDraft headRefName baseRefName updatedAt
        author { login } body
        mergeable mergeStateStatus reviewDecision
        repository { nameWithOwner }
        commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
        latestOpinionatedReviews(first: 20) { nodes { state author { login } } }
        reviewRequests(first: 10) { totalCount }
      }
    }
  }
}
""" % {"limit": config.SEARCH_LIMIT}

BASE_QUERY = "is:pr is:open archived:false sort:updated-desc"

_ME = None


def me():
    """Current GitHub login (cached; one `gh api user` per process)."""
    global _ME
    if _ME is None:
        _ME = tools.run(["gh", "api", "user", "--jq", ".login"]).strip()
    return _ME


def _search(q):
    """One GitHub search; returns raw PullRequest nodes."""
    out = tools.run(["gh", "api", "graphql", "-f", "query=" + QUERY, "-f", f"q={q}"])
    return [n for n in _json_nodes(out)]


def _json_nodes(out):
    import json
    return json.loads(out)["data"]["search"]["nodes"]


def search_prs(query, org=None):
    """Search open PRs matching `query` (a GitHub search qualifier string)."""
    base = BASE_QUERY + (f" org:{org}" if org else "")
    out = tools.run(["gh", "api", "graphql",
                     "-f", "query=" + QUERY,
                     "-f", f"q={base} {query}"])
    return [n for n in _json_nodes(out) if n]


def fetch_mine(org=None):
    """PRs I authored, plus PRs assigned to me that look like backports
    (bots create those and assign the original author; they show up even
    after the original PR is merged)."""
    prs = {p["url"]: p for p in search_prs("author:@me", org)}
    for p in search_prs("assignee:@me", org):
        if p["url"] not in prs and classify.is_backport(p):
            prs[p["url"]] = p
    return sorted(prs.values(), key=lambda p: p["updatedAt"], reverse=True)


def fetch_review_requested(org=None):
    """Open PRs requesting review from me or from the SDET team. Kept
    separate from fetch_mine(): those are other people's PRs and must not
    inflate the "my PRs" views."""
    prs = {}
    for q in (f"review-requested:@me", f"team-review-requested:{config.SDET_TEAM}"):
        for p in search_prs(q, org):
            prs.setdefault(p["url"], p)
    return sorted(prs.values(), key=lambda p: p["updatedAt"], reverse=True)


def ci_state(pr):
    """Overall status of the head commit's check rollup ('NONE' when absent)."""
    rollup = (pr["commits"]["nodes"] or [{}])[0].get("commit", {}).get("statusCheckRollup")
    return rollup["state"] if rollup else "NONE"


# --- Linked-PR enrichment (used by jira.py) ------------------------------
_ENRICH_FRAG = ("isDraft mergeable mergeStateStatus reviewDecision"
                " commits(last:1){nodes{commit{statusCheckRollup{state}}}}"
                " latestOpinionatedReviews(first:20){nodes{state}}")


def enrich(prs):
    """Fill live CI / review / conflict state into open PR dicts that came
    from Jira's development panel (which only knows OPEN/DRAFT/MERGED)."""
    open_prs = [p for p in prs if p.get("state") == "OPEN"]
    if not open_prs:
        return
    for i in range(0, len(open_prs), config.JPR_ENRICH_BATCH):
        batch = open_prs[i:i + config.JPR_ENRICH_BATCH]
        q = "query{" + " ".join(
            f'p{j}:repository(owner:"{p["repo"].split("/")[0]}",name:"{p["repo"].split("/")[1]}")'
            f'{{pullRequest(number:{p["number"]}){{{_ENRICH_FRAG}}}}}'
            for j, p in enumerate(batch)) + "}"
        try:
            data = tools.run_json(["gh", "api", "graphql", "-f", f"query={q}"])["data"]
        except tools.ToolError:
            return  # enrichment is best-effort; stale CI info is fine
        for j, p in enumerate(batch):
            live = ((data.get(f"p{j}") or {}).get("pullRequest")) or {}
            if not live:
                continue
            p.update(ci=ci_state(live), review=live["reviewDecision"], merge=live["mergeable"],
                     mergeState=live["mergeStateStatus"], draft=live["isDraft"],
                     approvals=sum(r["state"] == "APPROVED" for r in live["latestOpinionatedReviews"]["nodes"]))


# late import: classify.py imports nothing from github.py, but keeping it
# below the functions avoids a reader wondering about the direction
from . import classify  # noqa: E402