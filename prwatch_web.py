#!/usr/bin/env python3
"""prwatch_web - PR 监测的网页界面 (复用 prwatch.py 的数据获取)。

  ./prwatch_web.py                 # http://127.0.0.1:8765 ，页面默认每 5 分钟自动刷新
  ./prwatch_web.py -p 9000 --org my-org --jira-url https://<your-site>.atlassian.net
"""
import argparse
import base64
import hashlib
import json
import os
import threading
import time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import prwatch

HERE = Path(__file__).parent
LOGO = next(p for p in (HERE / "logo.local.png", HERE / "logo.png") if p.exists())  # logo.local.png: optional private override, gitignored


def favicon_svg():
    """标签页图标: 把 logo 裁成圆形的 SVG(PNG 以 data URI 内嵌，不需要额外文件)。"""
    img = "data:image/png;base64," + base64.b64encode(LOGO.read_bytes()).decode()
    return ('<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 180 180">'
            '<clipPath id="c"><circle cx="90" cy="90" r="90"/></clipPath>'
            f'<image width="180" height="180" clip-path="url(#c)" href="{img}" xlink:href="{img}"/></svg>').encode()
CACHE = {"ts": 0, "data": None, "err": None, "jira": [], "sdet": [], "jprs": {}, "jerr": None}
LOCK = threading.Lock()
# 上一次抓取成功的完整快照: 内存 + 磁盘。页面打开时先拿它立即显示，不必等本次抓取(GitHub+Jira 要好几秒)。
DISK = Path.home() / ".cache" / "prwatch" / "web_cache.json"
SNAP = None


def save_snap(snap):
    global SNAP
    SNAP = snap
    try:
        DISK.parent.mkdir(parents=True, exist_ok=True)
        tmp = DISK.with_suffix(".tmp")
        tmp.write_text(json.dumps(snap))
        os.chmod(tmp, 0o600)  # 含 PR 标题/Jira 摘要，仅自己可读
        os.replace(tmp, DISK)
    except OSError:
        pass


def load_snap():
    """服务启动时读回磁盘快照; 同时填进 CACHE，这样首次抓取失败时也不会退回空列表。"""
    global SNAP
    try:
        s = json.loads(DISK.read_text())
        CACHE.update(data=s["prs"], jira=s["jira"], sdet=s["sdet"], jprs=s["jiraPrs"])
        SNAP = s
    except (OSError, ValueError, KeyError, TypeError):
        SNAP = None


def to_item(pr, jira_url):
    reviews = pr["latestOpinionatedReviews"]["nodes"]
    jira = prwatch.find_jira(pr)
    return {
        "repo": pr["repository"]["nameWithOwner"], "number": pr["number"],
        "title": pr["title"], "url": pr["url"], "draft": pr["isDraft"],
        "branch": pr["headRefName"], "updatedAt": pr["updatedAt"],
        "ci": prwatch.ci_state(pr), "review": pr["reviewDecision"],
        "approvals": sum(r["state"] == "APPROVED" for r in reviews),
        "changes": sum(r["state"] == "CHANGES_REQUESTED" for r in reviews),
        "merge": pr["mergeable"], "mergeState": pr["mergeStateStatus"],
        "backport": prwatch.backport_of(pr), "base": pr["baseRefName"],
        "bot": pr["author"]["login"] != prwatch.me(),
        "jira": jira,
        "jiraUrl": f"{jira_url.rstrip('/')}/browse/{jira}" if jira and jira_url else None,
    }


def get_data(org, jira_url, ttl):
    """服务端缓存，避免多个标签页/频繁刷新打爆 GitHub API。"""
    with LOCK:
        if time.time() - CACHE["ts"] >= ttl:
            try:
                CACHE["data"] = [to_item(p, jira_url) for p in prwatch.fetch(org)]
                CACHE["err"] = None
            except Exception as e:
                CACHE["err"] = str(e)
            try:  # Jira 单独容错，失败不影响 PR 列表
                site = (jira_url or prwatch.jira_site() or "").rstrip("/")
                CACHE["jira"] = [dict(j, url=f"{site}/browse/{j['key']}") for j in prwatch.fetch_jira()]
                CACHE["sdet"] = [dict(j, url=f"{site}/browse/{j['key']}") for j in prwatch.fetch_jira(prwatch.JIRA_JQL_SDET)]
                CACHE["jerr"] = None
                try:  # 关联 PR 取自 Jira 自身的 development 信息(不限作者)
                    CACHE["jprs"] = prwatch.fetch_jira_prs(list({j["key"] for j in CACHE["jira"] + CACHE["sdet"]}))
                except Exception as e:
                    CACHE["jerr"] = "linked-PR lookup failed: " + str(e)[:120]
            except Exception as e:
                CACHE["jerr"] = str(e)
            CACHE["ts"] = time.time()
            if CACHE["data"] is not None:
                save_snap({"prs": CACHE["data"], "error": None, "fetchedAt": CACHE["ts"], "jira": CACHE["jira"],
                           "sdet": CACHE["sdet"], "jiraPrs": CACHE["jprs"], "jiraError": None, "me": prwatch.me()})
        return {"prs": CACHE["data"] or [], "error": CACHE["err"], "fetchedAt": CACHE["ts"],
                "jira": CACHE["jira"], "sdet": CACHE["sdet"], "jiraPrs": CACHE["jprs"], "jiraError": CACHE["jerr"]}


class Handler(BaseHTTPRequestHandler):
    org = jira_url = None
    ttl = 15

    def do_GET(self):
        if self.path.startswith("/api/prs") and "cached=1" in self.path:
            # 不抓取、不加锁，立即返回上次快照(没有则 empty)，让页面打开即有内容
            body, ctype = json.dumps(dict(SNAP or {"empty": True}, build=BUILD)).encode(), "application/json"
        elif self.path.startswith("/api/prs"):
            body, ctype = json.dumps(dict(get_data(self.org, self.jira_url, self.ttl), build=BUILD, me=prwatch.me())).encode(), "application/json"
        elif self.path in ("/favicon.ico", "/logo.png"):  # 方形原图: 页眉(CSS 裁圆)、apple-touch-icon、老式 /favicon.ico 请求
            body, ctype = LOGO.read_bytes(), "image/png"
        elif self.path in ("/logo.svg", "/favicon.svg"):  # 标签页图标(圆形)
            body, ctype = favicon_svg(), "image/svg+xml"
        elif self.path in ("/", "/index.html"):
            body, ctype = PAGE.encode(), "text/html; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-cache" if self.path.startswith(("/favicon", "/logo")) else "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


PAGE = r"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PR &amp; JIRA Watch</title>
<link rel="icon" type="image/svg+xml" href="/logo.svg">
<link rel="apple-touch-icon" href="/logo.png">
<style>
:root{color-scheme:dark;--orange:#ff9e4a;--jira:#b48eff;--sdet:#2ee6d6;--bg:#090d0a;--card:#0e1410;--fg:#cfd8cf;--mute:#6f8072;--line:#1e2b22;
--ok:#3ddc84;--okb:rgba(61,220,132,.09);--bad:#ff5c57;--badb:rgba(255,92,87,.10);--warn:#f3b43f;--warnb:rgba(243,180,63,.10);--gray:#7d8c80;--grayb:rgba(125,140,128,.10);--acc:#5ad4ff;
--mono:ui-monospace,"Cascadia Code","JetBrains Mono","Fira Code",SFMono-Regular,Menlo,Consolas,monospace}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:13px/1.55 var(--mono)}
body::after{content:"";position:fixed;inset:0;pointer-events:none;z-index:50;background:repeating-linear-gradient(0deg,rgba(255,255,255,.018) 0 1px,transparent 1px 3px)} /* 淡淡的 CRT 扫描线 */
::selection{background:rgba(61,220,132,.3)}
.wrap{max-width:1600px;margin:0 auto;padding:20px 16px}
header{position:sticky;top:0;z-index:5;display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:-20px -16px 16px;padding:12px 16px;background:var(--bg);border-bottom:1px solid var(--line)}
.hd{flex:1;min-width:0}
h1{font-size:17px;line-height:1.2;margin:0;font-weight:700;color:var(--ok);text-shadow:0 0 10px rgba(61,220,132,.45)}
h1::before{content:"$ ";color:var(--mute);text-shadow:none}
h1::after{content:"▌";margin-left:2px;animation:blink 1.1s steps(1) infinite}
@keyframes blink{50%{opacity:0}}
.sub{color:var(--mute);font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sub::before{content:"# "}
.pill{display:inline-flex;align-items:center;gap:7px;background:var(--card);border:1px solid var(--line);border-radius:3px;padding:3px 11px;font-size:12px;color:var(--mute);white-space:nowrap}
.pill i{width:8px;height:8px;border-radius:50%;background:var(--ok);box-shadow:0 0 6px var(--ok)}
.pill.load i{background:var(--warn);box-shadow:0 0 6px var(--warn);animation:pulse 1s infinite}.pill.err i{background:var(--bad);box-shadow:0 0 6px var(--bad)}.pill.err{color:var(--bad)}
@keyframes pulse{50%{opacity:.25}}
@media(prefers-reduced-motion:reduce){h1::after,.pill.load i,#rf.spin span,.logo{animation:none}}
#rf.spin span{display:inline-block;animation:rot 1s linear infinite}@keyframes rot{to{transform:rotate(360deg)}}
.logo{width:36px;height:36px;border-radius:50%;object-fit:cover;opacity:.6;box-shadow:0 0 6px -2px rgba(205,255,0,.25);animation:heartbeat 1.6s ease-in-out infinite}
@keyframes heartbeat{ /* 心跳: 咚咚—(停顿)，缩放 + 光晕同步 */
0%{transform:scale(1);box-shadow:0 0 6px -2px rgba(205,255,0,.25)}
14%{transform:scale(1.14);box-shadow:0 0 18px 2px rgba(205,255,0,.6)}
28%{transform:scale(1);box-shadow:0 0 8px -2px rgba(205,255,0,.3)}
42%{transform:scale(1.09);box-shadow:0 0 14px 1px rgba(205,255,0,.45)}
70%,100%{transform:scale(1);box-shadow:0 0 6px -2px rgba(205,255,0,.25)}}
.meta{color:var(--mute);font-size:12px}
button,select{background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:3px;padding:5px 10px;font:inherit;cursor:pointer;transition:border-color .15s,color .15s}
select option{background:var(--card);color:var(--fg)}
button:hover,select:hover{border-color:var(--ok);color:var(--ok)}
.stat.k-all{border-left:4px solid var(--acc)}.stat.k-fail{border-left:4px solid var(--bad)}.stat.k-conf{border-left:4px solid var(--orange)}
.stat.k-wait{border-left:4px solid var(--warn)}.stat.k-ready{border-left:4px solid var(--ok)}.stat.k-bp,.stat.k-draft{border-left:4px solid var(--line)}
.stat.k-fail.on{outline-color:var(--bad)}.stat.k-conf.on{outline-color:var(--orange)}.stat.k-wait.on{outline-color:var(--warn)}.stat.k-ready.on{outline-color:var(--ok)}
.stat.k-fail b{color:var(--bad)}.stat.k-conf b{color:var(--orange)}.stat.k-wait b{color:var(--warn)}.stat.k-ready b{color:var(--ok)}
.stat.k-jira{border-left:4px solid var(--jira)}.stat.k-sdet{border-left:4px solid var(--sdet)}
.stat.k-jira.on{outline-color:var(--jira)}.stat.k-sdet.on{outline-color:var(--sdet)}
.stat.k-jira b{color:var(--jira)}.stat.k-sdet b{color:var(--sdet)}
.statrow{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px}.statrow>.stats{display:contents}
.statrow>.stats2>.stat:first-child{margin-left:14px} /* PR 组与 Jira 组之间留一点间隔 */
.stat{display:flex;align-items:baseline;gap:8px;background:var(--card);border:1px solid var(--line);border-radius:3px;padding:5px 12px;cursor:pointer;white-space:nowrap;transition:border-color .15s,background .15s}
.stat:hover{border-color:#2f4a38;background:#101912}
.stat.on{outline:1px solid var(--acc);background:rgba(90,212,255,.06)}
.stat b{font-size:16px;line-height:1.2;text-shadow:0 0 8px currentColor}.stat span{color:var(--mute);font-size:12px}
.list{display:flex;flex-direction:column;gap:6px}
.pr{display:grid;grid-template-columns:4px 1fr auto;align-items:center;background:var(--card);border:1px solid var(--line);border-radius:4px;overflow:hidden;transition:border-color .15s,background .15s}
.pr:hover{border-color:#2f4a38;background:#101912}
.bar{align-self:stretch;background:var(--gray)}.bar.ok{background:var(--ok)}.bar.bad{background:var(--bad)}.bar.warn{background:var(--warn)}
.main{display:flex;align-items:center;gap:12px;padding:8px 12px;min-width:0} /* 一行: 引用 | 标题(自适应) | 徽章 | 关联 PR */
.top{color:var(--mute);font-size:12px;flex:none;max-width:230px;min-width:0}
.top span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.title{font-weight:600;font-size:13px;color:#e4efe4;text-decoration:none;flex:1 1 0;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.title:hover{color:var(--ok);text-shadow:0 0 8px rgba(61,220,132,.4)}
.badges{display:flex;gap:6px;flex:none}
.b{padding:0 8px;border-radius:3px;border:1px solid rgba(125,140,128,.28);font-size:11.5px;font-weight:500;background:var(--grayb);color:var(--gray);text-decoration:none;white-space:nowrap}
.b.ok{background:var(--okb);color:var(--ok);border-color:rgba(61,220,132,.35)}.b.bad{background:var(--badb);color:var(--bad);border-color:rgba(255,92,87,.4)}.b.warn{background:var(--warnb);color:var(--warn);border-color:rgba(243,180,63,.4)}
a.b:hover{filter:brightness(1.3)}
.top{display:flex;align-items:center;gap:6px}
.cp{background:none;border:0;padding:2px;margin:0;color:var(--mute);border-radius:3px;line-height:0;opacity:.6}
.cp:hover{opacity:1;background:var(--grayb);color:var(--ok);border-color:transparent}.cp.done{color:var(--ok);opacity:1}
.jl{flex:0 1 auto;min-width:0;max-width:34%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--mute);font-size:12px}.jl a{color:var(--acc);text-decoration:none}.jl a:hover{text-decoration:underline}
.age{padding:0 14px 0 0;color:var(--mute);font-size:12px;white-space:nowrap}
.err{background:var(--badb);color:var(--bad);border:1px solid rgba(255,92,87,.4);padding:8px 12px;border-radius:4px;margin-bottom:12px}
.err::before{content:"ERR ";font-weight:700}
.hint{color:var(--mute);font-size:12px;margin:-4px 0 10px}.hint::before{content:"# "}
.empty{text-align:center;color:var(--mute);padding:40px}
@media(max-width:1100px){.main{flex-wrap:wrap;row-gap:4px}.title{flex:1 1 55%}.jl{max-width:100%;flex-basis:100%}} /* 窄屏: 徽章折到第二行 */
@media(max-width:600px){.pr{grid-template-columns:4px 1fr}.age{display:none}}
</style></head><body><div class="wrap">
<header><img class="logo" src="/logo.png" alt=""><div class="hd"><h1>pr-jira-watch</h1><div class="sub" id="sub">…</div></div>
<span class="pill load" id="pill"><i></i><span id="meta">…</span></span>
<select id="iv"><option value="30">30s</option><option value="60">60s</option><option value="300" selected>5m</option><option value="600">10m</option><option value="1800">30m</option></select>
<button id="lang" title="中文 / English">EN</button><button id="rf"><span>↻</span> <em id="rft" style="font-style:normal"></em></button></header>
<div id="err"></div><div class="statrow"><div class="stats" id="stats"></div><div class="stats stats2" id="stats2"></div></div><div class="hint" id="hint"></div><div class="list" id="list"></div>
</div>
<script>
const CP='<svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor"><path d="M0 6.75C0 5.78.78 5 1.75 5h1.5a.75.75 0 0 1 0 1.5h-1.5a.25.25 0 0 0-.25.25v7.5c0 .14.11.25.25.25h7.5a.25.25 0 0 0 .25-.25v-1.5a.75.75 0 0 1 1.5 0v1.5A1.75 1.75 0 0 1 9.25 16h-7.5A1.75 1.75 0 0 1 0 14.25Z"/><path d="M5 1.75C5 .78 5.78 0 6.75 0h7.5C15.22 0 16 .78 16 1.75v7.5A1.75 1.75 0 0 1 14.25 11h-7.5A1.75 1.75 0 0 1 5 9.25Zm1.75-.25a.25.25 0 0 0-.25.25v7.5c0 .14.11.25.25.25h7.5a.25.25 0 0 0 .25-.25v-7.5a.25.25 0 0 0-.25-.25Z"/></svg>',
 OK='<svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor"><path d="M13.78 4.22a.75.75 0 0 1 0 1.06l-7.25 7.25a.75.75 0 0 1-1.06 0L2.22 9.28a.75.75 0 0 1 1.06-1.06L6 10.94l6.72-6.72a.75.75 0 0 1 1.06 0Z"/></svg>';
const BUILD="__BUILD__";
let jira=[],sdet=[],jprs={},jiraErr=null,data=[],filter="all",timer,fetchedAt=0,lastFail=new Set();
const $=id=>document.getElementById(id);
const esc=s=>String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function ago(t){let s=Math.max(0,Date.now()/1000-t/1000|0);for(const[u,n]of[["d",86400],["h",3600],["m",60]])if(s>=n)return(s/n|0)+u;return s+"s"}
const I18N={
zh:{loading:"加载中…",refresh:"刷新",refreshNow:"立即刷新",switchTo:"EN",
 ciOk:"✔ CI 通过",ciFail:"✖ CI 失败",ciError:"✖ CI 错误",ciPending:"● CI 运行中",ciExpected:"● CI 等待",ciNone:"CI 无",
 rvChanges:n=>`✖ 要求修改 (${n})`,rvRequired:"… 待 Review",rvNone:"无 Review",
 mgConflict:"⚠ 有冲突",mgDraft:"草稿，不可合并",mgReady:"可合并",mgBehind:"落后于 base，需更新",mgBlocked:"未满足合并条件",
 mgUnstable:"有检查未通过",mgHooks:"待合并检查",mgClean:"无冲突，但缺 Approve",mgUnknown:"合并状态未知",
 tab_all:"未合并 PR",tab_fail:"CI 失败 PR",tab_conf:"有冲突 PR",tab_wait:"等 Review 的 PR",tab_ready:"可合并 PR",tab_bp:"Backport PR",tab_draft:"草稿 PR",tab_jira:"我负责的 Jira",tab_sdet:"我是 SDET 的 Jira",
 tabd_all:"你名下所有还没 merge 的 PR（含草稿）",tabd_fail:"CI 检查失败或出错的 PR，需要修复后重跑",tabd_conf:"和目标分支有冲突的 PR，需要 rebase 或解决冲突",
 tabd_wait:"已就绪、正在等 reviewer 审核的 PR（不含草稿）",tabd_ready:"GitHub 判定可 merge（无冲突、必需检查全过、不落后 base）且已有人 Approve 的 PR",
 tabd_bp:"向 release 分支回合代码的 backport PR",tabd_draft:"还没准备好 review 的 Draft PR",tabd_jira:"Assignee 是你、状态为 In Progress 的 Jira",tabd_sdet:"SDET 字段是你、尚未完成的 Jira",
 noJira:"无 Jira",origPr:n=>`原 PR #${n}`,botMade:"bot 创建",copyPr:"复制 PR 链接",copyJira:"复制 Jira 链接",copied:"已复制",
 updatedAgo:a=>`${a} 前更新`,emptyPr:"没有符合条件的 PR",emptySdet:"没有 SDET 是我的未完成 Jira",emptyJira:"没有 In Progress 的 Jira",
 withDraft:n=>`（含 ${n} 草稿）`,noLinked:"无关联 PR",nMerged:n=>`${n} PR 已 merge`,nDraft:n=>`${n} PR 草稿`,conflictTag:"⚠冲突",linkedPrs:"关联 PR: ",
 metaLoad:"刷新中…",metaErr:"连接失败，重试中…",metaOk:(t,n)=>`更新于 ${t} · ${n}s 后刷新`,metaStale:a=>`刷新中…（当前显示 ${a} 前的数据）`,
 jiraFail:"Jira 刷新失败: ",refreshFail:"刷新失败: ",connFail:"无法连接服务: "},
en:{loading:"Loading…",refresh:"Refresh",refreshNow:"Refresh now",switchTo:"中文",
 ciOk:"✔ CI passed",ciFail:"✖ CI failed",ciError:"✖ CI error",ciPending:"● CI running",ciExpected:"● CI waiting",ciNone:"CI none",
 rvChanges:n=>`✖ Changes requested (${n})`,rvRequired:"… Review needed",rvNone:"No review",
 mgConflict:"⚠ Conflict",mgDraft:"Draft, not mergeable",mgReady:"Mergeable",mgBehind:"Behind base, update needed",mgBlocked:"Merge requirements not met",
 mgUnstable:"Some checks failing",mgHooks:"Pre-merge checks pending",mgClean:"No conflict, needs approval",mgUnknown:"Merge state unknown",
 tab_all:"Open PRs",tab_fail:"CI-fail PRs",tab_conf:"Conflict PRs",tab_wait:"Needs-review PRs",tab_ready:"Mergeable PRs",tab_bp:"Backport PRs",tab_draft:"Draft PRs",tab_jira:"My Jira",tab_sdet:"My SDET Jira",
 tabd_all:"All your PRs that are not merged yet (drafts included)",tabd_fail:"PRs whose CI failed or errored; fix and re-run",tabd_conf:"PRs with conflicts against the base branch; rebase or resolve",
 tabd_wait:"Ready PRs waiting for a reviewer (drafts excluded)",tabd_ready:"GitHub says mergeable (no conflict, required checks pass, not behind base) and someone approved",
 tabd_bp:"Backport PRs that bring changes to release branches",tabd_draft:"Draft PRs that are not ready for review",tabd_jira:"Jira issues assigned to you that are In Progress",tabd_sdet:"Jira issues where you are the SDET and that are not done",
 noJira:"No Jira",origPr:n=>`Original PR #${n}`,botMade:"Created by bot",copyPr:"Copy PR link",copyJira:"Copy Jira link",copied:"Copied",
 updatedAgo:a=>`updated ${a} ago`,emptyPr:"No matching PRs",emptySdet:"No unfinished Jira where I'm SDET",emptyJira:"No In Progress Jira",
 withDraft:n=>` (incl. ${n} draft)`,noLinked:"No linked PR",nMerged:n=>`${n} PR merged`,nDraft:n=>`${n} PR draft`,conflictTag:"⚠conflict",linkedPrs:"Linked PRs: ",
 metaLoad:"Refreshing…",metaErr:"Connection failed, retrying…",metaOk:(t,n)=>`Updated ${t} · refresh in ${n}s`,metaStale:a=>`Refreshing… (showing data from ${a} ago)`,
 jiraFail:"Jira refresh failed: ",refreshFail:"Refresh failed: ",connFail:"Cannot reach server: "}};
let lang="zh";try{const v=localStorage.getItem("prwatch.lang");if(v==="zh"||v==="en")lang=v}catch(_){}
const L=(k,...a)=>{const v=I18N[lang][k];return typeof v==="function"?v(...a):v};
let listErr=null,connErr=null;
function setErr(){const e=connErr?L("connFail")+connErr:(filter==="jira"||filter==="sdet")?(jiraErr&&L("jiraFail")+jiraErr):(listErr&&L("refreshFail")+listErr);$("err").innerHTML=e?`<div class="err">${esc(e)}</div>`:""}
function applyLang(){document.documentElement.lang=lang;$("lang").textContent=L("switchTo");$("rf").title=L("refreshNow");$("rft").textContent=L("refresh")}
function ci(p){return({SUCCESS:[L("ciOk"),"ok"],FAILURE:[L("ciFail"),"bad"],ERROR:[L("ciError"),"bad"],PENDING:[L("ciPending"),"warn"],EXPECTED:[L("ciExpected"),"warn"]})[p.ci]||[L("ciNone"),"gray"]}
function rv(p){
 if(p.review==="APPROVED")return[`✔ Approved (${p.approvals})`,"ok"];
 if(p.review==="CHANGES_REQUESTED")return[L("rvChanges",p.changes),"bad"];
 if(p.review==="REVIEW_REQUIRED")return[L("rvRequired")+(p.approvals?` (${p.approvals} ok)`:""),"warn"];
 return[p.approvals?`✔ ${p.approvals} ok`:L("rvNone"),"gray"]}
function ready(p){ // GitHub 认为可以 merge(CLEAN: 无冲突、必需检查全过、不落后 base) 且确实有人 approve、没人要求修改
 return !p.draft&&p.mergeState==="CLEAN"&&p.review!=="CHANGES_REQUESTED"&&(p.review==="APPROVED"||(p.approvals>0&&!p.changes))}
function mg(p){ // 只有 ready 才显示绿色“可合并”，其余显示具体卡在哪
 if(p.merge==="CONFLICTING"||p.mergeState==="DIRTY")return[L("mgConflict"),"bad"];
 if(p.draft)return[L("mgDraft"),"gray"];
 if(ready(p))return[L("mgReady"),"ok"];
 return({BEHIND:[L("mgBehind"),"warn"],BLOCKED:[L("mgBlocked"),"warn"],UNSTABLE:[L("mgUnstable"),"warn"],
  HAS_HOOKS:[L("mgHooks"),"warn"],CLEAN:[L("mgClean"),"gray"]})[p.mergeState]||[L("mgUnknown"),"gray"]}
function sev(p){ // 越大越需要关注
 if(p.draft)return 0; let s=1;
 if(["FAILURE","ERROR"].includes(p.ci))s+=4; if(p.merge==="CONFLICTING")s+=3; if(p.review==="CHANGES_REQUESTED")s+=2;
 if(ready(p))s=-1; // 可以 merge 了
 return s}
const tabs=[["all",()=>true],["fail",p=>["FAILURE","ERROR"].includes(p.ci)],["conf",p=>p.merge==="CONFLICTING"],
 ["wait",p=>p.review==="REVIEW_REQUIRED"&&!p.draft],["ready",p=>sev(p)===-1],["bp",p=>p.backport!==null],["draft",p=>p.draft],
 ["jira",()=>false],["sdet",()=>false]];
function render(){
 const card=([k,f])=>`<div class="stat k-${k} ${filter===k?"on":""}" data-k="${k}" title="${esc(L("tabd_"+k))}"><b>${k==="jira"?jira.length:k==="sdet"?sdet.length:data.filter(f).length}</b><span>${L("tab_"+k)}</span></div>`;
 $("stats").innerHTML=tabs.filter(t=>t[0]!=="jira"&&t[0]!=="sdet").map(card).join("");
 $("stats2").innerHTML=tabs.filter(t=>t[0]==="jira"||t[0]==="sdet").map(card).join("");
 $("hint").textContent=L("tabd_"+filter);
 if(filter==="jira"||filter==="sdet")return renderJira();
 const f=tabs.find(t=>t[0]===filter)[1];
 const rows=data.filter(f).sort((a,b)=>sev(b)-sev(a)||b.updatedAt.localeCompare(a.updatedAt));
 $("list").innerHTML=rows.length?rows.map(p=>{
  const c=ci(p),r=rv(p),m=mg(p),s=sev(p);
  const bar=s===-1?"ok":s>=4?"bad":s>=1?"warn":"";
  const jira=p.jira?(p.jiraUrl?`<a class="b" href="${esc(p.jiraUrl)}" target="_blank">◆ ${esc(p.jira)}</a>`:`<span class="b">◆ ${esc(p.jira)}</span>`):`<span class="b">${L("noJira")}</span>`;
  const bp=p.backport===null?"":`<span class="b">↩ backport → ${esc(p.base)}</span>`+(p.backport?`<a class="b" href="https://github.com/${esc(p.repo)}/pull/${p.backport}" target="_blank">${L("origPr",p.backport)}</a>`:"")+(p.bot?`<span class="b">${L("botMade")}</span>`:"");
  return`<div class="pr"><div class="bar ${bar}"></div><div class="main">
   <div class="top"><span title="${esc(p.repo)} · ${esc(p.branch)}">${esc(p.repo.split("/").pop())}#${p.number}</span><button class="cp" data-url="${esc(p.url)}" title="${L("copyPr")}" aria-label="${L("copyPr")}">${CP}</button></div>
   <a class="title" href="${esc(p.url)}" target="_blank" title="${esc(p.title)}">${p.draft?"[Draft] ":""}${esc(p.title)}</a>
   <div class="badges"><span class="b ${c[1]}">${c[0]}</span><span class="b ${r[1]}">${esc(r[0])}</span><span class="b ${m[1]}">${m[0]}</span>${jira}${bp}</div>
  </div><div class="age">${L("updatedAgo",ago(Date.parse(p.updatedAt)))}</div></div>`}).join(""):`<div class="empty"># ${L("emptyPr")}</div>`}
function setTitle(){ // 标签页窄，最重要的放最前: ✖CI失败 ⚠冲突 ✔可merge · 总数
 const n=(f)=>data.filter(f).length,fail=n(p=>["FAILURE","ERROR"].includes(p.ci)),conf=n(p=>p.merge==="CONFLICTING"),ready=n(p=>sev(p)===-1);
 const flags=[fail&&`✖${fail}`,conf&&`⚠${conf}`,ready&&`✔${ready}`].filter(Boolean).join(" ");
 document.title=(flags?flags+" · ":"")+`PR & JIRA Watch · ${data.length} PR`+(jira.length?` · ${jira.length} Jira`:"")+(sdet.length?` · ${sdet.length} SDET`:"")}
function stColor(s,cat){s=s.toUpperCase();return cat==="To Do"?"":s.includes("BLOCK")?"bad":s.includes("REVIEW")?"warn":"ok"}
function renderJira(){
 const rows=filter==="sdet"?sdet:jira;
 $("list").innerHTML=rows.length?rows.map(j=>{
  const linked=j.key in jprs?jprs[j.key]:data.filter(p=>p.jira===j.key); // 优先用 Jira 自身关联的 PR
  const opn=linked.filter(p=>(p.state||"OPEN")==="OPEN"),live=opn.filter(p=>!p.draft); // 草稿算 open，但不参与 CI 汇总
  const fail=live.some(p=>["FAILURE","ERROR"].includes(p.ci)),allok=live.length&&live.every(p=>p.ci==="SUCCESS");
  const nd=opn.length-live.length,dn=nd?L("withDraft",nd):"";
  const prb=!linked.length?`<span class="b">${L("noLinked")}</span>`:!opn.length?`<span class="b ok">${L("nMerged",linked.length)}</span>`:!live.length?`<span class="b">${L("nDraft",linked.length)}</span>`:`<span class="b ${fail?"bad":allok?"ok":"warn"}">${linked.length} PR ${fail?"✖":allok?"✔":"●"}${dn}</span>`;
  const links=linked.map(p=>{const st=p.state||"OPEN",c=ci(p),tag=st==="MERGED"?["merged","ok"]:st==="OPEN"?(p.draft?["draft","gray"]:[c[0].split(" ")[0],c[1]]):["closed","gray"];
   return`<a href="${esc(p.url)}" target="_blank">${esc(p.repo.split("/").pop())}#${p.number}</a> <span style="color:var(--${tag[1]})">${tag[0]}</span>${p.merge==="CONFLICTING"?` <span style="color:var(--bad)">${L("conflictTag")}</span>`:""}`}).join(" · ");
  const bar=fail?"bad":"";
  return`<div class="pr"><div class="bar ${bar||"warn"}"></div><div class="main">
   <div class="top"><span>${esc(j.key)} · ${esc(j.type)}</span><button class="cp" data-url="${esc(j.url)}" title="${L("copyJira")}" aria-label="${L("copyJira")}">${CP}</button></div>
   <a class="title" href="${esc(j.url)}" target="_blank" title="${esc(j.summary)}">${esc(j.summary)}</a>
   <div class="badges"><span class="b ${stColor(j.status,j.category)}">${esc(j.status)}</span><span class="b">${esc(j.priority)}</span>${prb}</div>
   ${links?`<div class="jl">${L("linkedPrs")}${links}</div>`:""}</div><div class="age"></div></div>`}).join(""):`<div class="empty"># ${L(filter==="sdet"?"emptySdet":"emptyJira")}</div>`}
let nextAt=0,state="load",me="";
function tick(){ // 头部状态: 更新时间 + 下次刷新倒计时，刷新中/失败有不同颜色
 const p=$("pill");p.className="pill"+(state==="load"?" load":state==="err"?" err":"");
 $("meta").textContent=state==="load"?L("metaLoad"):state==="err"?L("metaErr"):L("metaOk",new Date(fetchedAt*1000).toLocaleTimeString(),Math.max(0,Math.round((nextAt-Date.now())/1000)));
 if(state==="load"&&fetchedAt)$("meta").textContent=L("metaStale",ago(fetchedAt*1000));
 $("rf").classList.toggle("spin",state==="load");
 $("sub").textContent=(me?`@${me} · `:"")+`${data.length} PR · ${jira.length} Jira · ${sdet.length} SDET`}
// 上次的数据: 浏览器 localStorage(秒开) + 服务端快照(换浏览器/服务重启后也有)，先显示它们，再等真正的刷新
const LS="prwatch.snap";
function apply(j){me=j.me||me;data=j.prs||[];jira=j.jira||[];sdet=j.sdet||[];jprs=j.jiraPrs||{};jiraErr=j.jiraError;fetchedAt=j.fetchedAt||0}
function paint(){setTitle();render();tick()}
function save(j){try{localStorage.setItem(LS,JSON.stringify({build:BUILD,prs:j.prs,jira:j.jira,sdet:j.sdet,jiraPrs:j.jiraPrs,fetchedAt:j.fetchedAt,me:j.me}))}catch(_){}}
function restore(){try{const j=JSON.parse(localStorage.getItem(LS));if(j&&j.build===BUILD&&Array.isArray(j.prs)){apply(j);paint()}}catch(_){}}
async function preload(){try{const j=await (await fetch("/api/prs?cached=1")).json();
 if(!j.empty&&j.build===BUILD&&(j.fetchedAt||0)>fetchedAt){apply(j);paint()}}catch(_){}}
async function load(){
 state="load";tick();
 try{const j=await (await fetch("/api/prs")).json();if(j.build&&j.build!==BUILD){location.reload();return}state="ok";connErr=null;apply(j);listErr=j.error;setErr();
  const failing=new Set(data.filter(p=>["FAILURE","ERROR"].includes(p.ci)).map(p=>p.url));
  lastFail=failing;if(!j.error)save(j);
  paint()}catch(e){state="err";connErr=e.message;tick();setErr()}
 clearTimeout(timer);nextAt=Date.now()+$("iv").value*1000;timer=setTimeout(load,$("iv").value*1000);tick()}
$("list").onclick=async e=>{const b=e.target.closest(".cp");if(!b)return;const u=b.dataset.url;
 try{await navigator.clipboard.writeText(u)}catch(_){const t=document.createElement("textarea");t.value=u;document.body.appendChild(t);t.select();document.execCommand("copy");t.remove()}
 b.classList.add("done");b.innerHTML=OK;b.title=L("copied");setTimeout(()=>{b.classList.remove("done");b.innerHTML=CP;b.title=L(filter==="jira"||filter==="sdet"?"copyJira":"copyPr")},1500)};
$("stats").onclick=$("stats2").onclick=e=>{const s=e.target.closest(".stat");if(s){filter=s.dataset.k;setErr();render()}};
$("lang").onclick=()=>{lang=lang==="zh"?"en":"zh";try{localStorage.setItem("prwatch.lang",lang)}catch(_){}applyLang();setErr();paint()};
$("rf").onclick=load;$("iv").onchange=()=>{try{localStorage.setItem("prwatch.iv",$("iv").value)}catch(_){}load()};setInterval(()=>{if(data.length)render()},30000);setInterval(tick,1000);
try{const v=localStorage.getItem("prwatch.iv");if([...$("iv").options].some(o=>o.value===v))$("iv").value=v}catch(_){} // 记住上次选的刷新间隔
applyLang();restore();preload();load();
</script></body></html>"""


# 页面版本号: 服务端代码更新后，已打开的旧页面在下一次轮询时会自动重载
BUILD = hashlib.md5(PAGE.encode()).hexdigest()[:8]
PAGE = PAGE.replace("__BUILD__", BUILD)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-p", "--port", type=int, default=8765)
    ap.add_argument("--org")
    ap.add_argument("--jira-url", default=os.environ.get("JIRA_URL"))
    a = ap.parse_args()
    Handler.org, Handler.jira_url = a.org, a.jira_url
    load_snap()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    print(f"PR & JIRA Watch: http://127.0.0.1:{a.port}  (Ctrl-C 退出)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
