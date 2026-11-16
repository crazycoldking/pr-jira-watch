"""Central configuration: every tunable in one place.

Env-overridable values are read at import so a user can change behavior
without touching code (prefix `PRWATCH_`). Plain constants have no runtime
variation worth exposing.
"""
import os

# --- GitHub -------------------------------------------------------------
# The "review-requested PRs" card covers review requests aimed at this team
# (gh search: team-review-requested:ORG/SLUG). Override when forking.
SDET_TEAM = os.environ.get("PRWATCH_SDET_TEAM", "Kong/gateway-sdets")

# GitHub search page size (one page per query, no pagination: 50 is plenty
# for one person's open PRs).
SEARCH_LIMIT = int(os.environ.get("PRWATCH_SEARCH_LIMIT", "50"))

# --- Jira ---------------------------------------------------------------
JIRA_LIMIT = int(os.environ.get("PRWATCH_JIRA_LIMIT", "100"))
JIRA_TIMEOUT = int(os.environ.get("PRWATCH_JIRA_TIMEOUT", "60"))

# Assignee is me and the ticket is actively being worked (In Review counts).
JQL_ACTIVE = 'assignee = currentUser() AND statusCategory = "In Progress" ORDER BY updated DESC'

# SDET work often lands before development starts (Backlog/To Do), so this
# includes every unfinished (non-Done) ticket where I am the SDET.
JQL_SDET = 'SDET = currentUser() AND statusCategory != Done ORDER BY updated DESC'

# Assigned to me but still parked in Backlog, most urgent first.
JQL_BACKLOG = 'assignee = currentUser() AND status = "Backlog" ORDER BY priority DESC, updated DESC'

# --- Linked PRs (twg) ---------------------------------------------------
JPR_BATCH = 20        # twg rejects identifier batches larger than this
JPR_TTL = 120         # seconds; one twg batch costs ~5s, so cache briefly
TWG_TIMEOUT = 120     # seconds
TWG_SINCE = "3650d"   # how far back twg looks for linked PRs
JPR_ENRICH_BATCH = 20 # gh GraphQL aliases per enrichment query

# --- Web ----------------------------------------------------------------
WEB_PORT = int(os.environ.get("PRWATCH_WEB_PORT", "8765"))
WEB_CACHE_TTL = 15    # seconds; ten open tabs cost one GitHub query