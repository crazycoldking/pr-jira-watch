"""Desktop notifications (WSL Windows toast / Linux notify-send / bell)
and the transition detection that decides when one should fire."""
import shutil
import subprocess
import sys

from . import config
from .tools import ToolError


def desktop(title, msg):
    """Fire a toast: WSL -> Windows toast, Linux -> notify-send, else bell.
    Never raises: notification failures must not kill a poll cycle."""
    try:
        if shutil.which("powershell.exe"):
            esc = lambda t: t.replace("'", "''").replace("&", "&amp;").replace("<", "&lt;")
            ps = (
                "[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime]|Out-Null;"
                "$x=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent('ToastText02');"
                f"$t=$x.GetElementsByTagName('text');$t.Item(0).AppendChild($x.CreateTextNode('{esc(title)}'))|Out-Null;"
                f"$t=$x.GetElementsByTagName('text');$t.Item(1).AppendChild($x.CreateTextNode('{esc(msg)}'))|Out-Null;"
                "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('prwatch').Show("
                "[Windows.UI.Notifications.ToastNotification]::new($x))")
            subprocess.Popen(["powershell.exe", "-NoProfile", "-Command", ps],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif shutil.which("notify-send"):
            subprocess.Popen(["notify-send", "-u", "critical", title, msg])
        else:
            sys.stdout.write("\a")
    except OSError:
        pass


def snapshot(pr, old):
    """The three fields worth notifying about. A UNKNOWN mergeable (GitHub
    still computing) inherits the previous value so it cannot flap."""
    from . import github
    mg = pr["mergeable"]
    if mg == "UNKNOWN" and old:
        mg = old["merge"]
    return {"ci": github.ci_state(pr), "review": pr["reviewDecision"], "merge": mg}


def check_pr_changes(prs, prev):
    """Notify on PR transitions (CI red, approved, changes requested,
    conflict) by comparing against `prev`. First run only builds a baseline.
    Returns the new {url: snapshot} state."""
    cur = {p["url"]: snapshot(p, (prev or {}).get(p["url"])) for p in prs}
    if prev is None:
        return cur
    for p in prs:
        new, old = cur[p["url"]], prev.get(p["url"])
        if old is None:  # first sighting: record, don't notify
            continue
        name = f"{p['repository']['nameWithOwner']}#{p['number']} {p['title']}"
        if new["ci"] in config_bad() and old["ci"] not in config_bad():
            desktop("CI 失败", name)
        if new["review"] == "APPROVED" and old["review"] != "APPROVED":
            desktop("PR 已 Approve", name)
        if new["review"] == "CHANGES_REQUESTED" and old["review"] != "CHANGES_REQUESTED":
            desktop("PR 被要求修改", name)
        if new["merge"] == "CONFLICTING" and old["merge"] != "CONFLICTING":
            desktop("PR 有冲突", name)
    return cur


def config_bad():
    from .classify import CI_BAD
    return CI_BAD


def check_jira_changes(items, prev, status_lookup=None):
    """Notify on Jira transitions: ticket added, status changed, or removed
    from the watched set (the removed case asks Jira where it went).
    First run only builds a baseline. Returns the new {key: status} state."""
    if status_lookup is None:
        from . import jira
        status_lookup = jira.status_of
    cur = {j["key"]: j for j in items}
    state = {k: j["status"] for k, j in cur.items()}
    if prev is None:
        return state
    for k, j in cur.items():
        if k not in prev:
            desktop("Jira 新增关注", f"{k} [{j['status']}] {j['summary']}")
        elif prev[k] != j["status"]:
            desktop("Jira 状态变化", f"{k}: {prev[k]} → {j['status']}  {j['summary']}")
    for k, old in prev.items():
        if k not in cur:
            now = status_lookup(k)
            desktop("Jira 已移出关注", f"{k}: {old} → {now or '?'}")
    return state