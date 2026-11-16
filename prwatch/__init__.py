"""prwatch_core: shared data layer for the three prwatch faces.

Layout (keep it this way):

    config.py    every tunable and every JQL
    tools.py     shell-out helpers (gh / acli / twg go through here)
    github.py    GitHub search + enrichment
    jira.py      Jira search (acli) + linked PRs (twg)
    classify.py  the shared rules: mergeable, severity, Jira-key guessing,
                 backport detection, raw-PR -> item normalization
    notify.py    desktop toasts + transition detection
    textutil.py  ANSI/CJK text helpers

The entry scripts (prwatch.py CLI, prwatch_tui.py, prwatch_web.py)
stay thin: argument parsing and rendering only. Business rules belong
here, so a change lands once and all three faces agree.
"""
from . import classify, config, github, jira, textutil
from . import notify as _notify_mod

# --- re-exports: the historical module-level API, unchanged -------------
find_jira = classify.find_jira
is_backport = classify.is_backport
backport_of = classify.backport_of
is_ready = classify.is_ready
merge_cell = classify.merge_cell
severity = classify.severity
item_from_pr = classify.item_from_pr
CI_BAD = classify.CI_BAD

me = github.me
fetch = github.fetch_mine
fetch_review_requested = github.fetch_review_requested
ci_state = github.ci_state

fetch_jira = jira.fetch
fetch_jira_prs = jira.linked_prs
jira_site = jira.site
jira_status = jira.status_of

desktop = _notify_mod.desktop  # legacy alias was `prwatch.notify` (a function); renamed to avoid shadowing the module
check_changes = _notify_mod.check_pr_changes
check_jira_changes = _notify_mod.check_jira_changes

ago = textutil.ago