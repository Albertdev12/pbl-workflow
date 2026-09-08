# -*- coding: utf-8 -*-
"""本地触发 / 观察 GitHub Actions 云端运行（不依赖 gh CLI）。

用法（PowerShell）:
    $env:GH_TOKEN = "<你的GitHub令牌>"      # 需要 Actions: Read and write
    python tools/gh_run.py trigger eod      # 触发一次 eod
    python tools/gh_run.py status           # 查看最近一次运行状态
    python tools/gh_run.py wait             # 阻塞等待最近一次运行结束并打印每一步结论
    python tools/gh_run.py run eod          # 触发并等待（默认最长20分钟）

令牌也可以放到环境变量 PBL_TOKEN；仓库默认 Albertdev12/pbl-workflow，
可用环境变量 PBL_REPO 覆盖（格式 用户名/仓库名）。
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

API = "https://api.github.com"
REPO = os.environ.get("PBL_REPO", "Albertdev12/pbl-workflow")
WORKFLOW = os.environ.get("PBL_WORKFLOW", "schedule.yml")


def _token():
    t = os.environ.get("GH_TOKEN") or os.environ.get("PBL_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not t:
        print("缺少令牌：请设置环境变量 GH_TOKEN（需 Actions: Read and write 权限）")
        sys.exit(2)
    return t


def _req(path, method="GET", payload=None):
    url = path if path.startswith("http") else API + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "Bearer " + _token(),
        "Accept": "application/vnd.github+json",
        "User-Agent": "pbl-runner",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode() or "{}"
            return r.status, (json.loads(body) if body.strip().startswith(("{", "[")) else body)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:400]
    except Exception as e:
        return 0, str(e)[:200]


def trigger(task="eod"):
    st, body = _req(f"/repos/{REPO}/actions/workflows/{WORKFLOW}/dispatches", "POST",
                    {"ref": "main", "inputs": {"task": task}})
    if st == 204:
        print(f"[触发成功] 任务 {task} 已提交，云端约 1-3 分钟后开始")
        return True
    print(f"[触发失败] HTTP {st}: {body}")
    return False


def show_jobs(run_id=None):
    """打印某次运行（默认最近一次）每个步骤的状态，定位卡在哪一步。"""
    if run_id is None:
        run = latest_run()
        run_id = run.get("id") if run else None
    if not run_id:
        print("[查询失败] 没有可用的运行记录")
        return
    st, jobs = _req(f"/repos/{REPO}/actions/runs/{run_id}/jobs")
    if st != 200 or not isinstance(jobs, dict):
        print(f"[查询失败] HTTP {st}: {jobs}")
        return
    for j in jobs.get("jobs", []):
        print(f"作业 {j.get('name')}: {j.get('status')} / {j.get('conclusion')} "
              f"（开始 {j.get('started_at')}）")
        for s in j.get("steps", []):
            print(f"   {s.get('number')}. {s.get('name')}: {s.get('status')} / {s.get('conclusion')}")


def latest_run():
    # 只取本工作流的运行（仓库里还有 GitHub Pages 等其他工作流，避免抓错）
    st, body = _req(f"/repos/{REPO}/actions/workflows/{WORKFLOW}/runs?per_page=10")
    if st != 200 or not isinstance(body, dict):
        print(f"[查询失败] HTTP {st}: {body}")
        return None
    return (body.get("workflow_runs") or [None])[0]


def describe(run):
    if not run:
        return
    print(f"运行 #{run.get('run_number')} id={run.get('id')} 状态={run.get('status')} "
          f"结论={run.get('conclusion')} 提交={str(run.get('head_sha'))[:7]} "
          f"开始={run.get('created_at')}")
    print("日志:", run.get("html_url"))


def wait_run(timeout=1200, poll=20):
    t0 = time.time()
    seen = None
    while time.time() - t0 < timeout:
        run = latest_run()
        if not run:
            return None
        if seen is None:
            seen = run.get("id")
            print(f"[等待] 监控运行 id={seen}")
        if run.get("id") != seen:
            time.sleep(poll)
            continue
        if run.get("status") == "completed":
            describe(run)
            st, jobs = _req(f"/repos/{REPO}/actions/runs/{seen}/jobs")
            if st == 200 and isinstance(jobs, dict):
                for j in jobs.get("jobs", []):
                    print(f"  作业 {j.get('name')}: {j.get('conclusion')}")
                    for s in j.get("steps", []):
                        print(f"    - {s.get('name')}: {s.get('conclusion')}")
            return run
        time.sleep(poll)
    print("[超时] 运行仍在进行，请稍后在 GitHub Actions 页面查看")
    return None


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "trigger":
        trigger(sys.argv[2] if len(sys.argv) > 2 else "eod")
    elif cmd == "status":
        describe(latest_run())
    elif cmd == "wait":
        wait_run()
    elif cmd == "jobs":
        show_jobs(int(sys.argv[2]) if len(sys.argv) > 2 else None)
    elif cmd == "run":
        task = sys.argv[2] if len(sys.argv) > 2 else "eod"
        if trigger(task):
            time.sleep(8)
            wait_run()
    else:
        print(__doc__)
