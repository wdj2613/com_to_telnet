#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""查询 GitHub Actions 最近一次运行的状态与产物。

公开仓库免登录即可用；私有仓库需要 token（fine-grained PAT 给 Actions: Read 即可）：

    .venv\\Scripts\\python.exe tools\\check_ci.py
    .venv\\Scripts\\python.exe tools\\check_ci.py --repo 别人的/仓库
    set GITHUB_TOKEN=xxx && .venv\\Scripts\\python.exe tools\\check_ci.py

退出码：0 = 最近一次运行成功；1 = 失败/仍在跑；2 = 查询不到（权限或网络问题）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from comtel.console import setup_console

    setup_console()
except Exception:
    pass

DEFAULT_REPO = "wdj2613/com_to_telnet"
API = "https://api.github.com"


def api(path: str, token: str = ""):
    headers = {
        "User-Agent": "com-telnet-ci-check",
        "Accept": "application/vnd.github+json",
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(API + path, headers=headers)
    with urllib.request.urlopen(request, timeout=25) as response:
        return json.load(response)


def human_size(size: int) -> str:
    if size < 1024:
        return "%d B" % size
    if size < 1024 * 1024:
        return "%.1f KB" % (size / 1024.0)
    return "%.1f MB" % (size / 1048576.0)


def main() -> int:
    parser = argparse.ArgumentParser(description="查看 GitHub Actions 运行状态")
    parser.add_argument("--repo", default=DEFAULT_REPO, help="owner/repo，默认 %s" % DEFAULT_REPO)
    parser.add_argument("--token", default="", help="GitHub token（私有仓库需要）")
    parser.add_argument("--all", action="store_true", help="列出最近 10 次运行")
    args = parser.parse_args()

    token = args.token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    print("仓库: %s%s" % (args.repo, "（带 token）" if token else "（匿名访问）"))

    try:
        data = api("/repos/%s/actions/runs?per_page=%d" % (args.repo, 10 if args.all else 1), token)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            print("查不到这个仓库 —— 要么不存在，要么是私有仓库需要 token。")
            print("公开仓库查询：仓库 Settings → General → 最下方 Danger Zone → Change visibility → Make public")
        elif exc.code in (401, 403):
            print("认证/权限不足（HTTP %d）：token 是否有效？细粒度 token 需要 Actions: Read。" % exc.code)
        else:
            print("查询失败：HTTP %d" % exc.code)
        return 2
    except Exception as exc:
        print("查询失败：%s: %s" % (type(exc).__name__, exc))
        return 2

    runs = data.get("workflow_runs") or []
    if not runs:
        print("还没有任何运行记录：推送代码或在 Actions 页面点 Run workflow 之后再看。")
        return 1

    print("最近运行 %d 条，共 %s 条记录\n" % (len(runs), data.get("total_count")))
    for run in runs:
        mark = {"success": "[成功]", "failure": "[失败]", "cancelled": "[取消]"}.get(
            run.get("conclusion") or "", "[进行中]" if run.get("status") != "completed" else "[未知]"
        )
        print("%s #%s %s" % (mark, run.get("run_number"), run.get("name")))
        print("    分支/标签: %s  提交: %s" % (run.get("head_branch"), (run.get("head_sha") or "")[:7]))
        print("    状态: %s / %s" % (run.get("status"), run.get("conclusion")))
        print("    时间: %s" % run.get("created_at"))
        print("    链接: %s" % run.get("html_url"))

    latest = runs[0]
    run_id = latest.get("id")
    if run_id:
        try:
            jobs = api("/repos/%s/actions/runs/%s/jobs" % (args.repo, run_id), token).get("jobs", [])
            print("\n步骤明细：")
            for job in jobs:
                print("  任务 %s —— %s" % (job.get("name"), job.get("conclusion")))
                for step in job.get("steps", []):
                    print("    %-8s %s" % (step.get("conclusion"), step.get("name")))
        except Exception as exc:
            print("（读取任务明细失败：%s）" % exc)

        try:
            arts = api("/repos/%s/actions/runs/%s/artifacts" % (args.repo, run_id), token).get("artifacts", [])
            print("\n构建产物：")
            if not arts:
                print("  （暂无，可能还在构建中）")
            for art in arts:
                print("  %s  %s%s" % (art.get("name"), human_size(art.get("size_in_bytes", 0)),
                                      "（已过期）" if art.get("expired") else ""))
        except Exception as exc:
            print("（读取产物失败：%s）" % exc)

    return 0 if latest.get("conclusion") == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
