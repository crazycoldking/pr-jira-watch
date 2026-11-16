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

数据抓取与业务规则都在 prwatch_core 包 (./prwatch/)，本文件只做 CLI 渲染。
"""
import argparse
import os
import sys
import time
from datetime import datetime

from prwatch import classify, github, textutil as T
from prwatch.notify import desktop, check_pr_changes

# 列宽上限与配色: MERGE 列的状态文案/颜色来自 classify.merge_cell
CI_CELLS = {"SUCCESS": ("✔ pass", "g"), "FAILURE": ("✖ fail", "r"), "ERROR": ("✖ error", "r"),
            "PENDING": ("● running", "y"), "EXPECTED": ("● waiting", "y"), "NONE": ("- none", "d")}

ANSI = {"g": "\033[32m", "r": "\033[31m", "y": "\033[33m", "b": "\033[34m",
        "d": "\033[2m", "bold": "\033[1m", "x": "\033[0m"}


def color(s, c, on):
    return f"{ANSI[c]}{s}{ANSI['x']}" if on else s


def review_cell(item):
    """(text, color) for the REVIEW column, mirroring the web UI's rv()."""
    n_app, n_chg = item["approvals"], item["changes"]
    dec = item["review"]
    if dec == "APPROVED":
        return f"✔ approved({n_app})", "g"
    if dec == "CHANGES_REQUESTED":
        return f"✖ changes({n_chg})", "r"
    if dec == "REVIEW_REQUIRED":
        return "… need review" + (f" ({n_app} ok)" if n_app else ""), "y"
    return (f"✔ {n_app} ok", "g") if n_app else ("- none", "d")


def row(item, jira_url, tty):
    ci_txt = CI_CELLS.get(item["ci"], (item["ci"], "d"))
    rv_txt = review_cell(item)
    merge = classify.merge_cell(item["draft"], item["merge"], item["mergeState"],
                                item["review"], item["approvals"], item["changes"])
    jira = item["jira"] or "-"
    if item["jira"] and jira_url:
        jira = T.osc8(jira, item["jiraUrl"], tty)
    draft = color("[draft] ", "d", tty) if item["draft"] else ""
    return [
        T.osc8(f"{item['repo']}#{item['number']}", item["url"], tty),
        color(*ci_txt, tty), color(*rv_txt, tty), color(*merge, tty), jira,
        T.ago(item["updatedAt"]), draft + item["title"],
    ]


def render(items, jira_url, tty):
    hdr = ["PR", "CI", "REVIEW", "MERGE", "JIRA", "AGE", "TITLE"]
    rows = [row(p, jira_url, tty) for p in items]
    widths = [max(T.vlen(r[i]) for r in [hdr] + rows) for i in range(len(hdr) - 1)]
    lines = ["  ".join(color(h.ljust(w), "bold", tty) for h, w in zip(hdr, widths)) + "  TITLE"]
    for r in rows:
        lines.append("  ".join(c + " " * (w - T.vlen(c)) for c, w in zip(r, widths)) + "  " + r[-1])
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
        desktop("prwatch", "测试通知")
        time.sleep(2)
        return
    prev = None

    while True:
        try:
            raw = github.fetch_mine(a.org)
            if not a.no_notify:
                prev = check_pr_changes(raw, prev)
            items = classify.sort_items(
                [classify.item_from_pr(p, a.jira_url, github.me()) for p in raw])
            body = render(items, a.jira_url, tty) if items else "没有未 merge 的 PR 🎉"
            head = f"Open PRs: {len(items)}   updated {datetime.now():%H:%M:%S}"
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
