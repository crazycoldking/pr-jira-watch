#!/usr/bin/env python3
"""prwatch - 终端里监测自己未 merge 的 GitHub PR，定时刷新。

依赖: gh CLI (已 `gh auth login`)，Python 3.8+，无第三方库。

用法:
  ./prwatch.py                       # 每 60s 刷新
  ./prwatch.py -i 30                 # 每 30s 刷新
  ./prwatch.py --once                # 只打印一次
  ./prwatch.py --org my-org          # 只看某个 org
  ./prwatch.py --jira-url https://<your-site>.atlassian.net   # Jira 显示为可点击链接
环境变量: JIRA_URL 等同 --jira-url
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone

QUERY = """
query($q: String!) {
  search(query: $q, type: ISSUE, first: 50) {
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
"""

JIRA_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
JIRA_URL_RE = re.compile(r"/browse/([A-Z][A-Z0-9]+-\d+)")
JIRA_LABEL_RE = re.compile(r"jira\W{0,4}([A-Z][A-Z0-9]+-\d+)", re.I)
NOT_JIRA = {"UTF", "SHA", "ISO", "TLS", "HTTP", "RFC", "CVE", "MD", "X", "AES", "RSA"}


def find_jira(pr):
    """依次从 分支名、标题、PR description 里找 Jira 号。
    description 里优先 /browse/KEY 链接，其次 'Jira: KEY'，最后才是裸 KEY-123。"""
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


C = {"g": "\033[32m", "r": "\033[31m", "y": "\033[33m", "b": "\033[34m",
     "d": "\033[2m", "bold": "\033[1m", "x": "\033[0m"}


def color(s, c, on):
    return f"{C[c]}{s}{C['x']}" if on else s


BACKPORT_RE = re.compile(r"backport", re.I)
ORIG_RE = re.compile(r"(?:Automated backport to .*? from #|Backport of \S*/pull/|Backport of #)(\d+)", re.I)


def is_backport(pr):
    return bool(BACKPORT_RE.search(pr["title"]) or pr["headRefName"].startswith("backport"))


def backport_of(pr):
    """返回原 PR 编号(从 body 里解析)，不是 backport 则为 None。"""
    if not is_backport(pr):
        return None
    m = ORIG_RE.search(pr.get("body") or "")
    return int(m.group(1)) if m else 0


JIRA_JQL = 'assignee = currentUser() AND statusCategory = "In Progress" ORDER BY updated DESC'


# SDET 的工作常在开发开始前(Backlog/To Do)就分配给我，所以包含所有未完成(非 Done)的工单
JIRA_JQL_SDET = 'SDET = currentUser() AND statusCategory != Done ORDER BY updated DESC'


def fetch_jira(jql=JIRA_JQL):
    """用 acli 查 In Progress 类别(含 In Review 等)的 Jira 工单；默认是 assignee 为我的，
    传 JIRA_JQL_SDET 则是 SDET 字段为我的。"""
    out = subprocess.run(
        ["acli", "jira", "workitem", "search", "--jql", jql, "--json", "--limit", "100",
         "--fields", "key,summary,status,priority,issuetype,labels"],
        capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout).strip() or "acli failed")
    if not out.stdout.strip():
        return []
    items = []
    for i in json.loads(out.stdout):
        f = i["fields"]
        items.append({"key": i["key"], "summary": f.get("summary", ""),
                      "status": (f.get("status") or {}).get("name", "?"),
                      "category": ((f.get("status") or {}).get("statusCategory") or {}).get("name", ""),
                      "priority": (f.get("priority") or {}).get("name", "-"),
                      "type": (f.get("issuetype") or {}).get("name", "-"),
                      "labels": f.get("labels") or []})
    return items


PR_URL_RE = re.compile(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)")
_JPR_CACHE = {}  # key -> (时间戳, [pr...])；twg 比较慢(一批约 5s)，缓存一小段时间
JPR_TTL = 120


def _enrich_prs(prs):
    """用 gh 补充 open PR 的 CI / review / 冲突状态(Jira 里是 OPEN/DRAFT/MERGED/DECLINED，DRAFT 已在取数时规范成 OPEN+draft)。"""
    open_prs = [p for p in prs if p["state"] == "OPEN"]
    if not open_prs:
        return
    frag = ("isDraft mergeable mergeStateStatus reviewDecision commits(last:1){nodes{commit{statusCheckRollup{state}}}}"
            " latestOpinionatedReviews(first:20){nodes{state}}")
    q = "query{" + " ".join(
        f'p{i}:repository(owner:"{p["repo"].split("/")[0]}",name:"{p["repo"].split("/")[1]}")'
        f'{{pullRequest(number:{p["number"]}){{{frag}}}}}' for i, p in enumerate(open_prs)) + "}"
    out = subprocess.run(["gh", "api", "graphql", "-f", f"query={q}"], capture_output=True, text=True)
    try:
        data = json.loads(out.stdout)["data"]
    except (ValueError, KeyError, TypeError):
        return
    for i, p in enumerate(open_prs):
        pr = ((data.get(f"p{i}") or {}).get("pullRequest")) or {}
        if not pr:
            continue
        pr["headRefName"] = p["branch"]
        p.update(ci=ci_state(pr), review=pr["reviewDecision"], merge=pr["mergeable"], mergeState=pr["mergeStateStatus"], draft=pr["isDraft"],
                 approvals=sum(r["state"] == "APPROVED" for r in pr["latestOpinionatedReviews"]["nodes"]))


def fetch_jira_prs(keys):
    """从 Jira 自身的 development 信息里取每个工单关联的 PR(不管是谁创建的)。
    返回 {key: [{url, repo, number, title, state, branch, base, ci, review, merge, draft, approvals}]}。"""
    now = time.time()
    stale = [k for k in keys if k not in _JPR_CACHE or now - _JPR_CACHE[k][0] > JPR_TTL]
    if stale:
        cmd = ["twg", "context", "jira", "workitem", *stale, "--since", "3650d", "--detail", "full",
               "--types", "ExternalPullRequest", "-o", "json", "--output-summary", "none"]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        doc = json.loads(out.stdout)["data"]
        items = doc["items"] if "items" in doc else [{"identifier": stale[0], "data": doc}]
        got = {}
        for it in items:
            prs = []
            for rel in it.get("data", {}).get("relationships", []):
                for t in rel.get("targets", []):
                    m = PR_URL_RE.search(t.get("url") or "")
                    if not m:
                        continue
                    st = t.get("status", "OPEN")  # Jira 把草稿 PR 单独标成 DRAFT; 实质仍是 open，统一成 OPEN + draft
                    prs.append({"url": t["url"], "repo": f"{m.group(1)}/{m.group(2)}", "number": int(m.group(3)),
                                "title": t.get("title", ""), "state": "OPEN" if st == "DRAFT" else st,
                                "branch": (t.get("sourceBranch") or {}).get("name", ""),
                                "base": (t.get("destinationBranch") or {}).get("name", ""),
                                "ci": "NONE", "review": None, "merge": None, "draft": st == "DRAFT", "approvals": 0})
            got[it["identifier"]] = sorted(prs, key=lambda p: p["number"])
        _enrich_prs([p for v in got.values() for p in v])
        for k in stale:
            _JPR_CACHE[k] = (now, got.get(k, []))
    return {k: _JPR_CACHE[k][1] for k in keys}


def jira_status(key):
    """查单个工单当前状态(工单离开 In Progress 列表后用来说明去了哪)。"""
    try:
        out = subprocess.run(["acli", "jira", "workitem", "view", key, "--fields", "status", "--json"],
                             capture_output=True, text=True, timeout=30)
        return json.loads(out.stdout)["fields"]["status"]["name"]
    except Exception:
        return None


def check_jira_changes(items, prev, lookup=jira_status):
    """items: 当前关注的工单(assignee 或 SDET 是我，已去重)。对比上一轮发通知；首轮只建基线。
    返回新的 {key: status}。"""
    cur = {j["key"]: j for j in items}
    state = {k: j["status"] for k, j in cur.items()}
    if prev is None:
        return state
    for k, j in cur.items():
        if k not in prev:
            notify("Jira 新增关注", f"{k} [{j['status']}] {j['summary']}")
        elif prev[k] != j["status"]:
            notify("Jira 状态变化", f"{k}: {prev[k]} → {j['status']}  {j['summary']}")
    for k, old in prev.items():
        if k not in cur:
            now = lookup(k)
            notify("Jira 已移出关注", f"{k}: {old} → {now or '?'}")
    return state


def jira_site():
    """从 acli 登录信息里取 Jira 站点 URL，例如 https://<your-site>.atlassian.net。"""
    out = subprocess.run(["acli", "jira", "auth", "status"], capture_output=True, text=True).stdout
    m = re.search(r"Site:\s*(\S+)", out)
    return f"https://{m.group(1)}" if m else None


_ME = None


def me():
    global _ME
    if _ME is None:
        _ME = subprocess.run(["gh", "api", "user", "--jq", ".login"],
                             capture_output=True, text=True).stdout.strip()
    return _ME


def _search(q):
    out = subprocess.run(
        ["gh", "api", "graphql", "-f", f"query={QUERY}", "-f", f"q={q}"],
        capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or "gh api failed")
    return [n for n in json.loads(out.stdout)["data"]["search"]["nodes"] if n]


def fetch(org):
    """我创建的未 merge PR + assign 给我的 backport PR (机器人创建的
    backport PR 作者是 bot，但会 assign 给原 PR 作者；即使原 PR 已 merge 也会出现)。"""
    base = "is:pr is:open archived:false sort:updated-desc" + (f" org:{org}" if org else "")
    prs = {p["url"]: p for p in _search(f"{base} author:@me")}
    for p in _search(f"{base} assignee:@me"):
        if p["url"] not in prs and is_backport(p):
            prs[p["url"]] = p
    return sorted(prs.values(), key=lambda p: p["updatedAt"], reverse=True)


def ci_state(pr):
    rollup = (pr["commits"]["nodes"] or [{}])[0].get("commit", {}).get("statusCheckRollup")
    return rollup["state"] if rollup else "NONE"


def notify(title, msg):
    """桌面通知: WSL -> Windows toast; Linux -> notify-send; 否则终端响铃。"""
    try:
        if shutil.which("powershell.exe"):
            esc = lambda t: t.replace("'", "''").replace("&", "&amp;").replace("<", "&lt;")
            ps = (
                "[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime]|Out-Null;"
                "$x=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent('ToastText02');"
                f"$t=$x.GetElementsByTagName('text');$t.Item(0).AppendChild($x.CreateTextNode('{esc(title)}'))|Out-Null;"
                f"$t.Item(1).AppendChild($x.CreateTextNode('{esc(msg)}'))|Out-Null;"
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
    # mergeable 偶尔是 UNKNOWN(GitHub 还在计算)，此时沿用上一轮的值，避免重复通知
    mg = pr["mergeable"]
    if mg == "UNKNOWN" and old:
        mg = old["merge"]
    return {"ci": ci_state(pr), "review": pr["reviewDecision"], "merge": mg}


def check_changes(prs, prev):
    """对比上一轮，状态发生关键变化的 PR 发通知；首轮只建立基线。"""
    cur = {p["url"]: snapshot(p, (prev or {}).get(p["url"])) for p in prs}
    if prev is None:
        return cur
    for p in prs:
        new, old = cur[p["url"]], prev.get(p["url"])
        if old is None:  # 新出现的 PR，仅记录
            continue
        name = f"{p['repository']['nameWithOwner']}#{p['number']} {p['title']}"
        bad = ("FAILURE", "ERROR")
        if new["ci"] in bad and old["ci"] not in bad:
            notify("CI 失败", name)
        if new["review"] == "APPROVED" and old["review"] != "APPROVED":
            notify("PR 已 Approve", name)
        if new["review"] == "CHANGES_REQUESTED" and old["review"] != "CHANGES_REQUESTED":
            notify("PR 被要求修改", name)
        if new["merge"] == "CONFLICTING" and old["merge"] != "CONFLICTING":
            notify("PR 有冲突", name)
    return cur


def is_ready(draft, merge_state, review, approvals, changes):
    """Single source of truth for "mergeable" in the CLI and the TUI (the web UI mirrors it in JS).
    GitHub's `mergeable` only means "no git conflict", so it must never drive a green badge.
    CLEAN = no conflict, required checks passed, not behind base."""
    return (not draft and merge_state == "CLEAN" and review != "CHANGES_REQUESTED"
            and (review == "APPROVED" or (approvals > 0 and not changes)))


MERGE_BLOCKERS = {"BEHIND": ("behind", "y"), "BLOCKED": ("blocked", "y"), "UNSTABLE": ("unstable", "y"),
                  "HAS_HOOKS": ("hooks", "y"), "CLEAN": ("no-approve", "d")}


def merge_cell(draft, merge, merge_state, review, approvals, changes):
    """(text, color key) for the MERGE column: green only when is_ready, otherwise say what blocks it."""
    if merge == "CONFLICTING" or merge_state == "DIRTY":
        return "conflict", "r"
    if draft:
        return "draft", "d"
    if review == "CHANGES_REQUESTED":
        return "changes", "r"
    if is_ready(draft, merge_state, review, approvals, changes):
        return "ready", "g"
    return MERGE_BLOCKERS.get(merge_state, ("?", "d"))


def ago(iso):
    t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    s = int((datetime.now(timezone.utc) - t).total_seconds())
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= n:
            return f"{s // n}{unit}"
    return f"{s}s"


def link(text, url, on):
    return f"\033]8;;{url}\033\\{text}\033]8;;\033\\" if on and url else text


def row(pr, jira_url, tty):
    ci = ci_state(pr)
    ci_txt = {"SUCCESS": ("✔ pass", "g"), "FAILURE": ("✖ fail", "r"), "ERROR": ("✖ error", "r"),
              "PENDING": ("● running", "y"), "EXPECTED": ("● waiting", "y"),
              "NONE": ("- none", "d")}.get(ci, (ci, "d"))

    dec = pr["reviewDecision"]
    reviews = pr["latestOpinionatedReviews"]["nodes"]
    n_app = sum(1 for r in reviews if r["state"] == "APPROVED")
    n_chg = sum(1 for r in reviews if r["state"] == "CHANGES_REQUESTED")
    if dec == "APPROVED":
        rv = (f"✔ approved({n_app})", "g")
    elif dec == "CHANGES_REQUESTED":
        rv = (f"✖ changes({n_chg})", "r")
    elif dec == "REVIEW_REQUIRED":
        rv = (f"… need review" + (f" ({n_app} ok)" if n_app else ""), "y")
    else:
        rv = (f"✔ {n_app} ok" if n_app else "- none", "d")

    merge = merge_cell(pr["isDraft"], pr["mergeable"], pr.get("mergeStateStatus"), dec, n_app, n_chg)

    m = find_jira(pr)
    jira = m or "-"
    if m and jira_url:
        jira = link(jira, f"{jira_url.rstrip('/')}/browse/{jira}", tty)

    draft = color("[draft] ", "d", tty) if pr["isDraft"] else ""
    return [
        link(pr["repository"]["nameWithOwner"] + f"#{pr['number']}", pr["url"], tty),
        color(*ci_txt, tty), color(*rv, tty), color(*merge, tty), jira,
        ago(pr["updatedAt"]), draft + pr["title"],
    ], m is not None


def vlen(s):
    s = re.sub(r"\033\]8;;[^\033]*\033\\", "", s)
    return len(re.sub(r"\033\[[0-9;]*m", "", s))


def render(prs, jira_url, tty):
    hdr = ["PR", "CI", "REVIEW", "MERGE", "JIRA", "AGE", "TITLE"]
    rows = [row(p, jira_url, tty)[0] for p in prs]
    widths = [max(vlen(r[i]) for r in [hdr] + rows) for i in range(len(hdr) - 1)]
    lines = ["  ".join(color(h.ljust(w), "bold", tty) for h, w in zip(hdr, widths)) + "  TITLE"]
    for r in rows:
        lines.append("  ".join(c + " " * (w - vlen(c)) for c, w in zip(r, widths)) + "  " + r[-1])
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-i", "--interval", type=int, default=60)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--org")
    ap.add_argument("--no-notify", action="store_true", help="关闭桌面通知(CI 失败/approve/冲突)")
    ap.add_argument("--test-notify", action="store_true", help="发一条测试通知后退出")
    ap.add_argument("--jira-url", default=os.environ.get("JIRA_URL"))
    a = ap.parse_args()
    tty = sys.stdout.isatty()
    if a.test_notify:
        notify("prwatch", "测试通知")
        time.sleep(2)
        return
    prev = None

    while True:
        try:
            prs = fetch(a.org)
            if not a.no_notify:
                prev = check_changes(prs, prev)
            body = render(prs, a.jira_url, tty) if prs else "没有未 merge 的 PR 🎉"
            head = f"Open PRs: {len(prs)}   updated {datetime.now():%H:%M:%S}"
            err = ""
        except Exception as e:  # 网络抖动时保留界面，下轮重试
            body, head, err = "", "", color(f"刷新失败: {e}", "r", tty)
        if tty and not a.once:
            sys.stdout.write("\033[2J\033[H")
        if head:
            print(color(head, "bold", tty) + color(f"   (every {a.interval}s, Ctrl-C 退出)", "d", tty) + "\n")
        print(body or err)
        if err and body:
            print(err)
        if a.once:
            return
        try:
            time.sleep(a.interval)
        except KeyboardInterrupt:
            return


if __name__ == "__main__":
    main()
