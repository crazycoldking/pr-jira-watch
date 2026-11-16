#!/usr/bin/env python3
"""prwatch_tui - 交互式终端界面 (curses, 无第三方依赖)，同时负责桌面通知。

  ./prwatch_tui.py [-i 60] [--org my-org] [--jira-url https://xxx.atlassian.net] [--no-notify]

按键:
  ↑/↓ j/k  移动         ←/→ Tab/1-7  切换筛选      空格  勾选/取消
  a        全选当前列表   c  复制链接(已勾选或当前行)  m  复制 Markdown
  o        打开 PR       J  打开 Jira               r  立即刷新
  x        清除勾选       ?  帮助                    q  退出
"""
import argparse
import base64
import curses
import os
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
from datetime import datetime

import prwatch

TABS = [
    ("全部", lambda p: True),
    ("CI失败", lambda p: p["ci"] in ("FAILURE", "ERROR")),
    ("有冲突", lambda p: p["merge"] == "CONFLICTING"),
    ("待Review", lambda p: p["review"] == "REVIEW_REQUIRED" and not p["draft"]),
    ("可合并", lambda p: sev(p) == -1),
    ("Backport", lambda p: p["backport"] is not None),
    ("草稿", lambda p: p["draft"]),
]


JIRA_TAB = len(TABS)  # 之后两个页签是 Jira(assignee 是我) 和 SDET(SDET 字段是我)，不是 PR 筛选
SDET_TAB = JIRA_TAB + 1
N_TABS = len(TABS) + 2


def sev(p):
    """越大越需要关注；-1 表示可合并 (classify.is_ready)。值已由 item_from_pr 预算好。"""
    return p["severity"]


# ---------- 文本宽度(中文占 2 列) ----------
def wcw(s):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def fit(s, w):
    """截断/补齐到恰好 w 列。"""
    out, n = "", 0
    for c in s:
        cw = wcw(c)
        if n + cw > w - (1 if wcw(s) > w else 0):
            break
        out, n = out + c, n + cw
    if wcw(s) > w:
        out += "…"
        n += 1
    return out + " " * max(0, w - n)


# ---------- 剪贴板 / 打开链接 ----------
def copy_text(text):
    try:
        if shutil.which("powershell.exe"):  # WSL: 用 base64 传输，避免编码/BOM 问题(clip.exe 会留下 BOM)
            b64 = base64.b64encode(text.encode()).decode()
            subprocess.run(["powershell.exe", "-NoProfile", "-Command",
                            f"Set-Clipboard -Value ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{b64}')))"],
                           check=True, timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"], ["pbcopy"]):
            if shutil.which(cmd[0]):
                subprocess.run(cmd, input=text.encode(), check=True, timeout=5)
                return True
    except (OSError, subprocess.SubprocessError):
        pass
    # 兜底: OSC52 (终端/tmux 支持时有效)
    sys.stdout.write("\033]52;c;" + base64.b64encode(text.encode()).decode() + "\a")
    sys.stdout.flush()
    return True


def open_url(url):
    try:
        if shutil.which("powershell.exe"):
            cmd = ["powershell.exe", "-NoProfile", "-Command", f"Start-Process '{url}'"]
        else:
            cmd = [shutil.which("xdg-open") or "open", url]
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


# ---------- 应用 ----------
class App:
    def __init__(self, a):
        self.a = a
        self.items, self.err, self.fetched = [], None, 0.0
        self.prev = None
        self.jprev = None
        self.lock = threading.Lock()
        self.loading = False
        self.tab, self.cur, self.top = 0, 0, 0
        self.picked = set()
        self.msg, self.msg_until = "", 0.0
        self.next_at = 0.0
        self.show_help = False
        self.jira, self.sdet, self.jira_err = [], [], None
        self.jprs = {}  # Jira key -> Jira 开发信息里关联的 PR(不限作者)

    # 后台刷新，界面不卡
    def refresh(self):
        with self.lock:
            if self.loading:
                return
            self.loading = True

        def work():
            try:
                raw = prwatch.fetch(self.a.org)
                if not self.a.no_notify:
                    self.prev = prwatch.check_changes(raw, self.prev)
                items = [prwatch.item_from_pr(p, self.a.jira_url, prwatch.me()) for p in raw]
                with self.lock:
                    self.items, self.err, self.fetched = items, None, time.time()
            except Exception as e:
                with self.lock:
                    self.err = str(e)
            try:  # Jira 单独容错，失败不影响 PR 列表
                base = self.a.jira_url.rstrip("/")
                mine = [dict(j, url=f"{base}/browse/{j['key']}") for j in prwatch.fetch_jira(prwatch.config.JQL_ACTIVE)]
                sdet = [dict(j, url=f"{base}/browse/{j['key']}") for j in prwatch.fetch_jira(prwatch.config.JQL_SDET)]
                with self.lock:
                    self.jira, self.sdet, self.jira_err = mine, sdet, None
                try:  # 关联 PR 取自 Jira 自身的 development 信息；失败时退回到"我自己的 PR 按 Jira 号匹配"
                    jprs = prwatch.fetch_jira_prs(list({j["key"] for j in mine + sdet}))
                    with self.lock:
                        self.jprs = jprs
                except Exception as e:
                    with self.lock:
                        self.jira_err = "关联 PR 获取失败: " + str(e)[:120]
                if not self.a.no_notify:
                    self.jprev = prwatch.check_jira_changes(list({j["key"]: j for j in mine + sdet}.values()), self.jprev)
            except Exception as e:
                with self.lock:
                    self.jira_err = str(e)
            finally:
                with self.lock:
                    self.loading = False
                    self.next_at = time.time() + self.a.interval
        threading.Thread(target=work, daemon=True).start()

    def view(self):
        with self.lock:
            items = list(self.items)
            jira = list(self.sdet if self.tab == SDET_TAB else self.jira)
        if self.tab >= JIRA_TAB:
            return items, jira
        f = TABS[self.tab][1]
        rows = [p for p in items if f(p)]
        rows.sort(key=lambda p: p["updatedAt"], reverse=True)
        rows.sort(key=lambda p: -sev(p))  # 稳定排序: 先按紧急度，同级按最近更新
        return items, rows

    def flash(self, m):
        self.msg, self.msg_until = m, time.time() + 3

    def targets(self, rows):
        sel = [p for p in rows if p["url"] in self.picked]
        if sel:
            return sel
        return [rows[self.cur]] if rows else []

    # ---------- 绘制 ----------
    def draw(self, s):
        H, W = s.getmaxyx()
        items, rows = self.view()
        self.picked &= {p["url"] for p in items} | {j["url"] for j in self.jira + self.sdet}
        self.cur = max(0, min(self.cur, len(rows) - 1))
        s.erase()
        if H < 9 or W < 50:
            s.addstr(0, 0, "终端太小"[: W - 1])
            return

        # 标题行
        state = "刷新中…" if self.loading else (f"更新于 {datetime.fromtimestamp(self.fetched):%H:%M:%S}" if self.fetched else "加载中…")
        left = f" PR & JIRA Watch  {len(items)} PR · {len(self.jira)} Jira · {len(self.sdet)} SDET"
        right = f"{state}  下次 {max(0, int(self.next_at - time.time()))}s "
        put(s, 0, 0, fit(left, W - wcw(right)) + right, curses.color_pair(5) | curses.A_BOLD, W)

        # 页签分两行: 第 1 行 PR 筛选；第 2 行 Jira 分组(各自颜色)
        with self.lock:
            all_prs = list(self.items)
        # PR 页签按语义配色: 失败红 / 冲突黄 / 待 review 蓝 / 可合并绿 / 其余默认(紫、青留给 Jira 两个页签)
        pr_cols = [0, 2, 3, 8, 1, 0, 0]
        tabs = [(name, sum(1 for p in all_prs if f(p)), curses.color_pair(c) if c else 0)
                for (name, f), c in zip(TABS, pr_cols)] + [
            ("我负责", len(self.jira), curses.color_pair(7)), ("我是SDET", len(self.sdet), curses.color_pair(6))]
        for row, lo, hi, prefix in ((1, 0, JIRA_TAB, " PR   "), (2, JIRA_TAB, N_TABS, " Jira ")):
            put(s, row, 0, prefix, curses.A_BOLD | curses.A_DIM | (curses.color_pair(7) if row == 2 else 0), W)
            x = len(prefix)
            for i in range(lo, hi):
                name, n, col = tabs[i]
                label = f" {i + 1} {name} {n} "
                if i == self.tab:
                    attr = col | curses.A_REVERSE | curses.A_BOLD
                else:  # 数量为 0 的页签调暗，有内容的才醒目
                    attr = col | (curses.A_BOLD if n else curses.A_DIM)
                if x + wcw(label) >= W:
                    break
                put(s, row, x, label, attr, W)
                x += wcw(label) + 1

        err = (self.jira_err and "Jira 刷新失败: " + self.jira_err) if self.tab >= JIRA_TAB else (self.err and "刷新失败: " + self.err)
        if err:
            put(s, 3, 1, fit(err.replace("\n", " "), W - 2), curses.color_pair(2), W)

        is_jira = self.tab >= JIRA_TAB
        if is_jira:
            by_key = {}
            for p in items:
                if p["jira"]:
                    by_key.setdefault(p["jira"], []).append(p)
            w_k = max([3] + [wcw(j["key"]) for j in rows])
            w_st = min(16, max([6] + [wcw(j["status"]) for j in rows]))
            w_pri, w_ty, w_pr_n = 8, 8, 9
            hdr = "  " + " ".join(fit(h, w) for h, w in (("KEY", w_k), ("STATUS", w_st), ("PRIORITY", w_pri),
                                                        ("TYPE", w_ty), ("PRS", w_pr_n))) + " SUMMARY"
        else:
            show_bp = any(p["backport"] is not None for p in rows)
            w_pr = min(44, max([len("PR")] + [wcw(f"{p['repo']}#{p['number']}") for p in rows]))
            w_ci, w_rv, w_mg, w_jira, w_age = 9, 14, 8, 9, 4
            w_bp = max([8] + [wcw(bp_text(p)) for p in rows]) if show_bp else 0
            hdr = "  " + " ".join(fit(h, w) for h, w in (("PR", w_pr), ("CI", w_ci), ("REVIEW", w_rv), ("MERGE", w_mg),
                                                        ("JIRA", w_jira), ("AGE", w_age)))
            hdr += (" " + fit("BACKPORT", w_bp) if show_bp else "") + " TITLE"
        put(s, 4, 0, fit(hdr, W), curses.A_BOLD | curses.A_UNDERLINE, W)

        # 列表
        body_h = H - 5 - 3
        if self.cur < self.top:
            self.top = self.cur
        if self.cur >= self.top + body_h:
            self.top = self.cur - body_h + 1
        if not rows:
            put(s, 6, 2, ("没有 SDET 是我的未完成 Jira 🎉" if self.tab == SDET_TAB else "没有 In Progress 的 Jira 🎉") if is_jira else "没有符合条件的 PR 🎉", curses.A_DIM, W)
        for i in range(body_h):
            idx = self.top + i
            if idx >= len(rows):
                break
            p, y = rows[idx], 5 + i
            base = curses.A_REVERSE if idx == self.cur else 0
            put(s, y, 0, " " * W, base, W)
            picked = p["url"] in self.picked
            put(s, y, 0, ("◉" if picked else "○") + " ", base | (curses.color_pair(6) | curses.A_BOLD if picked else 0), W)
            x = 2
            if is_jira:
                linked = self.jprs.get(p["key"], by_key.get(p["key"], []))
                st = p["status"].upper()
                st_c = (curses.A_DIM if p.get("category") == "To Do" else curses.color_pair(2) if "BLOCK" in st
                        else curses.color_pair(3) if "REVIEW" in st else curses.color_pair(6))
                pr_t, pr_c = pr_summary(linked)
                parts = [(p["key"], w_k, curses.A_BOLD), (p["status"], w_st, st_c), (p["priority"], w_pri, 0),
                         (p["type"], w_ty, curses.A_DIM), (pr_t, w_pr_n, pr_c)]
                tail = p["summary"]
            else:
                ci_t, ci_c = ci_cell(p)
                rv_t, rv_c = rv_cell(p)
                mg_t, mg_c = mg_cell(p)
                parts = [(f"{p['repo']}#{p['number']}", w_pr, 0), (ci_t, w_ci, ci_c), (rv_t, w_rv, rv_c),
                         (mg_t, w_mg, mg_c), (p["jira"] or "-", w_jira, 0 if p["jira"] else curses.A_DIM),
                         (ago(p["updatedAt"]), w_age, curses.A_DIM)]
                if show_bp:
                    parts.append((bp_text(p), w_bp, curses.color_pair(6) if p["backport"] is not None else 0))
                tail = ("[draft] " if p["draft"] else "") + p["title"]
            for text, w, attr in parts:
                put(s, y, x, fit(text, w), base | attr, W)
                x += w + 1
            put(s, y, x, fit(tail, W - x), base | (curses.A_DIM if not is_jira and p["draft"] else 0), W)

        # 当前行详情
        det = ""
        if rows:
            p = rows[self.cur]
            if is_jira:
                linked = self.jprs.get(p["key"], by_key.get(p["key"], []))
                det = " 关联 PR: " + (", ".join(link_text(l) for l in linked) if linked else "无 (Jira 里没有关联的 PR)")
            else:
                det = f" {p['branch']} → {p['base']}   {p['title']}"
        put(s, H - 3, 0, fit(det, W), curses.A_DIM, W)

        # 底栏
        if time.time() < self.msg_until:
            put(s, H - 2, 0, fit(" " + self.msg, W), curses.color_pair(1) | curses.A_BOLD, W)
        else:
            sel = f" 已选 {len(self.picked)}" if self.picked else ""
            put(s, H - 2, 0, fit(sel, W), curses.color_pair(6) | curses.A_BOLD, W)
        keys = " ↑↓移动 空格勾选 a全选 c复制链接 m复制MD o打开 J打开Jira r刷新 ←→/1-9切换 ?帮助 q退出"
        put(s, H - 1, 0, fit(keys, W), curses.color_pair(5), W)

        if self.show_help:
            self.draw_help(s, H, W)
        s.refresh()

    def draw_help(self, s, H, W):
        lines = ["按键帮助", "", "↑/↓ j/k   移动        PgUp/PgDn 翻页   g/G 顶部/底部",
                 "←/→ Tab   切换筛选    1-9 直接选页签 (8=Jira 9=SDET)", "空格      勾选/取消   a 全选当前列表   x 清除勾选",
                 "c  复制链接(已勾选，否则当前行)   m  复制 Markdown", "o  浏览器打开 PR   J  打开 Jira",
                 "r  立即刷新   q  退出", "", "红色=CI失败/冲突，● =运行中，◉ =已勾选", "按任意键关闭"]
        w = min(W - 2, max(wcw(l) for l in lines) + 4)
        h = len(lines) + 2
        y0, x0 = max(0, (H - h) // 2), max(0, (W - w) // 2)
        for i in range(h):
            put(s, y0 + i, x0, " " * w, curses.A_REVERSE, W)
        for i, l in enumerate(lines):
            put(s, y0 + 1 + i, x0 + 2, l, curses.A_REVERSE | (curses.A_BOLD if i == 0 else 0), W)

    # ---------- 输入 ----------
    def key(self, k, rows, H):
        if self.show_help:
            self.show_help = False
            return True
        page = max(1, H - 9)
        if k in (ord("q"), 27):
            return False
        elif k in (curses.KEY_DOWN, ord("j")):
            self.cur += 1
        elif k in (curses.KEY_UP, ord("k")):
            self.cur -= 1
        elif k == curses.KEY_NPAGE:
            self.cur += page
        elif k == curses.KEY_PPAGE:
            self.cur -= page
        elif k == ord("g"):
            self.cur = 0
        elif k == ord("G"):
            self.cur = len(rows) - 1
        elif k in (curses.KEY_RIGHT, 9, ord("l")):
            self.tab, self.cur = (self.tab + 1) % N_TABS, 0
        elif k in (curses.KEY_LEFT, curses.KEY_BTAB, ord("h")):
            self.tab, self.cur = (self.tab - 1) % N_TABS, 0
        elif ord("1") <= k <= ord("0") + N_TABS:
            self.tab, self.cur = k - ord("1"), 0
        elif k == ord(" ") and rows:
            u = rows[self.cur]["url"]
            self.picked ^= {u}
            self.cur += 1
        elif k == ord("a"):
            urls = {p["url"] for p in rows}
            self.picked = self.picked - urls if urls <= self.picked else self.picked | urls
        elif k == ord("x"):
            self.picked.clear()
        elif k in (ord("c"), ord("m")) and rows:
            t = self.targets(rows)
            text = "\n".join(p["url"] for p in t) if k == ord("c") else \
                "\n".join(f"- [{p['key']} {p['summary']}]({p['url']})" if "key" in p else
                          f"- [{p['repo']}#{p['number']} {p['title']}]({p['url']})" for p in t)
            copy_text(text)
            self.flash(f"已复制 {len(t)} 个{'链接' if k == ord('c') else ' Markdown'}")
        elif k == ord("o") and rows:  # Jira 页签里 o 打开的就是 Jira 工单
            for p in self.targets(rows):
                open_url(p["url"])
        elif k == ord("J") and rows:
            urls = [p.get("jiraUrl") or p["url"] if "key" in p else p["jiraUrl"] for p in self.targets(rows)]
            urls = [u for u in urls if u]
            if urls:
                for u in dict.fromkeys(urls):
                    open_url(u)
            else:
                self.flash("没有可打开的 Jira 链接 (需要 --jira-url)")
        elif k == ord("r"):
            self.refresh()
        elif k == ord("?"):
            self.show_help = True
        self.cur = max(0, min(self.cur, len(rows) - 1))
        return True


def put(s, y, x, text, attr, W):
    try:
        if x < W:
            s.addstr(y, x, text, attr)
    except curses.error:
        pass  # 写到右下角会抛异常，忽略


def link_text(l):
    """详情行里单个关联 PR: repo#N + 状态(merged / CI 符号，冲突加 ⚠)。"""
    st = l.get("state", "OPEN")
    tag = {"MERGED": "merged", "DECLINED": "closed", "CLOSED": "closed"}.get(st) or ci_cell(l)[0].split()[0]
    return f"{l['repo'].split('/')[-1]}#{l['number']} {tag}" + (" ⚠" if l.get("merge") == "CONFLICTING" else "")


def pr_summary(linked):
    """Jira 关联 PR 概览: 总数 + 仍 open 的 PR 的 CI 汇总(✖ 有失败 / ● 运行中 / ✔ 全部通过)；全 merged 则显示 merged。"""
    if not linked:
        return "-", curses.A_DIM
    opn = [p for p in linked if p.get("state", "OPEN") == "OPEN"]
    if not opn:
        return f"{len(linked)} merged", curses.color_pair(1) | curses.A_DIM
    if any(p["ci"] in ("FAILURE", "ERROR") for p in opn):
        return f"{len(linked)} PR ✖", curses.color_pair(2)
    if all(p["ci"] == "SUCCESS" for p in opn):
        return f"{len(linked)} PR ✔", curses.color_pair(1)
    return f"{len(linked)} PR ●", curses.color_pair(3)


def ago(iso):
    return prwatch.ago(iso)


def bp_text(p):
    if p["backport"] is None:
        return ""
    return f"→{p['base']}" + (f" ←#{p['backport']}" if p["backport"] else "")


def ci_cell(p):
    return {"SUCCESS": ("✔ pass", curses.color_pair(1)), "FAILURE": ("✖ fail", curses.color_pair(2)),
            "ERROR": ("✖ error", curses.color_pair(2)), "PENDING": ("● running", curses.color_pair(3)),
            "EXPECTED": ("● waiting", curses.color_pair(3))}.get(p["ci"], ("- none", curses.A_DIM))


def rv_cell(p):
    r = p["review"]
    if r == "APPROVED":
        return f"✔ approved({p['approvals']})", curses.color_pair(1)
    if r == "CHANGES_REQUESTED":
        return f"✖ changes({p['changes']})", curses.color_pair(2)
    if r == "REVIEW_REQUIRED":
        return "… review" + (f" ({p['approvals']}ok)" if p["approvals"] else ""), curses.color_pair(3)
    return (f"✔ {p['approvals']} ok", curses.color_pair(1)) if p["approvals"] else ("- none", curses.A_DIM)


def mg_cell(p):
    t, k = prwatch.merge_cell(p["draft"], p["merge"], p.get("mergeState"), p["review"], p["approvals"], p.get("changes", 0))
    return t, {"g": curses.color_pair(1), "r": curses.color_pair(2), "y": curses.color_pair(3), "d": curses.A_DIM}[k]


def run(stdscr, a):
    curses.curs_set(0)
    curses.use_default_colors()
    for pair, c in ((1, curses.COLOR_GREEN), (2, curses.COLOR_RED), (3, curses.COLOR_YELLOW), (6, curses.COLOR_CYAN), (7, curses.COLOR_MAGENTA), (8, curses.COLOR_BLUE)):
        curses.init_pair(pair, c, -1)
    curses.init_pair(5, curses.COLOR_BLACK, curses.COLOR_WHITE)
    stdscr.timeout(500)
    stdscr.keypad(True)
    app = App(a)
    app.refresh()
    while True:
        if time.time() >= app.next_at and app.next_at and not app.loading:
            app.refresh()
        app.draw(stdscr)
        k = stdscr.getch()
        if k == -1 or k == curses.KEY_RESIZE:
            continue
        _, rows = app.view()
        if not app.key(k, rows, stdscr.getmaxyx()[0]):
            return


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-i", "--interval", type=int, default=60)
    ap.add_argument("--org")
    ap.add_argument("--no-notify", action="store_true")
    ap.add_argument("--jira-url", default=os.environ.get("JIRA_URL"))
    a = ap.parse_args()
    a.jira_url = a.jira_url or prwatch.jira_site() or ""
    os.environ.setdefault("ESCDELAY", "25")
    curses.wrapper(run, a)


if __name__ == "__main__":
    main()
