#!/usr/bin/env bash
# 启动 PR & JIRA Watch：交互式终端 TUI(负责桌面通知) + 网页版，已在运行的会话会跳过。
cd "$(dirname "$(readlink -f "$0")")" || exit 1
JIRA_URL_ARG="${JIRA_URL:+--jira-url $JIRA_URL}"  # empty: the site is read from `acli jira auth status`
tmux has-session -t =prwatch 2>/dev/null     || tmux new -d -s prwatch     "./prwatch_tui.py -i 60 $JIRA_URL_ARG"
tmux has-session -t =prwatch-web 2>/dev/null || tmux new -d -s prwatch-web "./prwatch_web.py $JIRA_URL_ARG"
