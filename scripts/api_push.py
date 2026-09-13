# -*- coding: utf-8 -*-
"""走 GitHub REST API 推送本地提交（用于 github.com:443 被阻断、只有 api.github.com 可达的网络）。

原理：不依赖 git 协议，而是用 Contents/Trees API 直接构造一次提交：
  1) 取远端 main 的最新 commit 与它引用的 tree（递归，一次请求拿到全部 blob sha）；
  2) 对本地相对该 tree 有差异的文件：内容 sha 与远端一致就复用原 blob，否则新建 blob；
  3) 用新 tree + 父提交 = 远端 main 创建提交，再把 refs/heads/main 指过去。

安全：Token 只从环境变量 GH_PUSH_TOKEN 或文件读取，绝不写入任何文件、绝不打印；
      用完不落盘。仓库名从 git remote 解析，默认 Albertdev12/pbl-workflow。

用法：
    GH_PUSH_TOKEN=... python scripts/api_push.py            # 先干跑（只比对，不提交）
    GH_PUSH_TOKEN=... python scripts/api_push.py --apply    # 真正提交并更新 main
"""
import argparse
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API = "https://api.github.com"


def sh(*args):
    # core.quotepath=false：否则中文路径会被转义成 \345\256\... 导致 API 404
    return subprocess.run(["git", "-c", "core.quotepath=false", *args], cwd=BASE,
                          capture_output=True, text=True, check=True).stdout.strip()


def repo_slug():
    url = sh("remote", "get-url", "origin")
    slug = url.rstrip("/").split(":")[-1] if url.startswith("git@") else url.rstrip("/").split("github.com/")[-1]
    return slug[:-4] if slug.endswith(".git") else slug


def token():
    t = (os.environ.get("GH_PUSH_TOKEN") or "").strip()
    if t:
        return t
    for p in (os.path.join(BASE, ".push_token"), os.path.join(BASE, "..", ".push_token")):
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                return f.read().strip()
    raise SystemExit("未找到令牌：请设置 GH_PUSH_TOKEN 或写入 workflow/.push_token")


def call(method, path, tok, payload=None, tries=3, timeout=90):
    url = path if path.startswith("http") else API + path
    data = json.dumps(payload).encode() if payload is not None else None
    for i in range(tries):
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {tok}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "pbl-api-push",
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read().decode("utf-8", "replace")
                return r.status, (json.loads(body) if body.strip() else {})
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            # 401/403/404 多数是"权限不足"或"路径不存在"，由调用方按状态码决策，不在底层直接退出
            if e.code in (401, 403, 404):
                return e.code, {"message": detail}
            if i == tries - 1:
                raise
            time.sleep(2 * (i + 1))
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(2 * (i + 1))
    return 0, {}


def push_via_contents(slug, tok, remote_sha, local_sha, changed, apply, cache_path):
    """逐文件走 Contents API 推送（Tree API 被 Token 拒绝时的退路）。

    每写一个文件就是一次提交（GitHub 只给"单文件提交"这一个写入口），因此会产生多个提交；
    对账本/成果这类小文件完全够用，且不会丢内容。已存在的路径必须带 sha，新路径不能带 sha。
    """
    # 递归收集远端文件 sha（Contents API 不递归，这里按需查询单个文件）
    ok, fail, skip = 0, [], 0
    for i, path in enumerate(changed, 1):
        try:
            blob = sh("rev-parse", f"{local_sha}:{path}")
        except subprocess.CalledProcessError:
            # 本地删除 → 远端也删
            st, cur = call("GET", f"/repos/{slug}/contents/{urllib.parse.quote(path)}?ref=main", tok)
            if st == 200 and apply:
                st2, res = call("DELETE", f"/repos/{slug}/contents/{urllib.parse.quote(path)}", tok,
                                {"message": f"删除 {path}", "sha": cur["sha"]})
                ok += 1 if st2 in (200, 201) else 0
                if st2 not in (200, 201):
                    fail.append(f"{path}: HTTP {st2} {res}")
            continue
        if cache_path.get(path) == blob:
            skip += 1
            continue
        raw = subprocess.run(["git", "show", f"{local_sha}:{path}"], cwd=BASE,
                             capture_output=True).stdout
        if not apply:
            ok += 1
            continue
        st, cur = call("GET", f"/repos/{slug}/contents/{urllib.parse.quote(path)}?ref=main", tok)
        payload = {"message": f"同步 {path}（2026-09-13 修正数据源后重建）",
                   "content": base64.b64encode(raw).decode()}
        if st == 200 and isinstance(cur, dict) and cur.get("sha"):
            payload["sha"] = cur["sha"]
        st2, res = call("PUT", f"/repos/{slug}/contents/{urllib.parse.quote(path)}", tok, payload,
                        timeout=180)
        if st2 in (200, 201):
            ok += 1
            cache_path[path] = blob
            try:
                json.dump(cache_path, open(os.path.join(BASE, "data", "_api_push_cache.json"), "w",
                                           encoding="utf-8"), ensure_ascii=False)
            except Exception:
                pass
            if ok % 20 == 0:
                print(f"[api-push]   已推送 {ok} 个文件…")
        else:
            fail.append(f"{path}: HTTP {st2} {str(res)[:80]}")
    print(f"[api-push] Contents 通道：成功 {ok} 个，跳过（已推送）{skip} 个"
          + (f"，失败 {len(fail)}" if fail else ""))
    for f in fail[:5]:
        print("   ! ", f)
    return ok, fail


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正提交（默认只干跑）")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--via", choices=["auto", "tree", "contents"], default="auto",
                    help="auto=先试 Tree API，被拒则退逐文件 Contents API")
    args = ap.parse_args()
    tok = token()
    slug = repo_slug()
    print(f"[api-push] 仓库 {slug}，分支 {args.branch}")

    # 远端最新提交
    st, ref = call("GET", f"/repos/{slug}/git/ref/heads/{args.branch}", tok)
    if st != 200:
        raise SystemExit(f"读取远端分支失败：HTTP {st} {ref}")
    remote_sha = ref["object"]["sha"]
    st, rcommit = call("GET", f"/repos/{slug}/git/commits/{remote_sha}", tok)
    if st != 200:
        raise SystemExit(f"读取远端提交失败：HTTP {st} {rcommit}")
    base_tree = rcommit["tree"]["sha"]
    print(f"[api-push] 远端 {args.branch} = {remote_sha[:8]}")

    local_sha = sh("rev-parse", "HEAD")
    r = subprocess.run(["git", "merge-base", "--is-ancestor", remote_sha, local_sha], cwd=BASE)
    if r.returncode != 0:
        raise SystemExit(f"远端 {remote_sha[:8]} 不是本地 HEAD {local_sha[:8]} 的祖先——"
                         "请先 rebase 到 origin/main（git rebase origin/main）再推送")

    # 远端 tree（递归）
    st, rtree = call("GET", f"/repos/{slug}/git/trees/{base_tree}?recursive=1", tok)
    if st != 200:
        raise SystemExit(f"读取远端 tree 失败：HTTP {st} {rtree}")
    remote_blobs = {e["path"]: e["sha"] for e in rtree.get("tree", []) if e["type"] == "blob"}
    print(f"[api-push] 远端 tree 含 {len(remote_blobs)} 个文件")

    # 上传缓存：git 的 blob sha 是内容寻址的，本地对象 sha 就是内容指纹。
    # 记录"某路径的某内容已上传过"，脚本重跑（比如创建 tree 失败后重试）就不用重复上传。
    cache_path = os.path.join(BASE, "data", "_api_push_cache.json")
    uploaded = {}
    if os.path.exists(cache_path):
        try:
            uploaded = json.load(open(cache_path, encoding="utf-8"))
        except Exception:
            uploaded = {}

    # 本地相对远端有差异的文件（用本地 object id 与远端 blob sha 直接比对）
    changed = sh("diff", "--name-only", remote_sha, local_sha).splitlines()
    print(f"[api-push] 需要上传/更新的文件：{len(changed)} 个")

    # 明确指定走 Contents 通道时不必再建 blob（那一步只是为了拼 tree）
    if args.via == "contents":
        print("[api-push] 使用逐文件 Contents API 通道")
        if not args.apply:
            print("[api-push] 干跑结束（未做任何改动）。加 --apply 执行提交。")
            return
        push_via_contents(slug, tok, remote_sha, local_sha, changed, args.apply, uploaded)
        return

    entries, created, reused, failed, skipped = [], 0, 0, [], 0
    for path in changed:
        try:
            local_blob = sh("rev-parse", f"{local_sha}:{path}")
        except subprocess.CalledProcessError:
            continue  # 本地没有 = 删除
        if remote_blobs.get(path) == local_blob:
            reused += 1
            continue
        # 上一轮可能已经把 blob 建好了（tree 创建失败可重入）：同内容直接复用，避免重复上传
        if uploaded.get(path) == local_blob:
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": local_blob})
            skipped += 1
            continue
        raw = subprocess.run(["git", "show", f"{local_sha}:{path}"], cwd=BASE,
                             capture_output=True).stdout
        if not args.apply:
            created += 1
            continue
        try:
            st_b, res = call("POST", f"/repos/{slug}/git/blobs", tok,
                             {"content": base64.b64encode(raw).decode(), "encoding": "base64"},
                             timeout=180)
            if st_b not in (200, 201):
                raise RuntimeError(f"HTTP {st_b} {str(res)[:80]}")
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": res["sha"]})
            uploaded[path] = local_blob
            try:
                json.dump(uploaded, open(cache_path, "w", encoding="utf-8"), ensure_ascii=False)
            except Exception:
                pass
            created += 1
            if created % 25 == 0:
                print(f"[api-push]   已上传 {created} 个…")
        except Exception as e:
            failed.append(f"{path}: {str(e)[:80]}")
    print(f"[api-push] 复用远端 blob {reused} 个；新建 {created} 个"
          + (f"；复用本轮已上传 {skipped} 个" if skipped else "")
          + (f"；失败 {len(failed)}" if failed else ""))
    for f in failed[:5]:
        print("   ! ", f)
    if failed:
        raise SystemExit("存在上传失败，未提交（可重试）")
    if not args.apply:
        print("[api-push] 干跑结束（未做任何改动）。加 --apply 执行提交。")
        return

    try:
        st_t, tree = call("POST", f"/repos/{slug}/git/trees", tok,
                          {"base_tree": base_tree, "tree": entries}, timeout=180)
        if st_t not in (200, 201):
            print(f"[api-push] 创建 tree 失败：HTTP {st_t} {str(tree)[:90]}")
            raise SystemExit("tree-failed")
    except SystemExit as e:
        if args.via == "contents":
            raise
        print(f"[api-push] 创建 tree 被拒（{str(e)[:90]}）→ 改用逐文件 Contents API 推送")
        push_via_contents(slug, tok, remote_sha, local_sha, changed, args.apply, uploaded)
        return
    msg = sh("log", "-1", "--pretty=%B")
    st_c, commit = call("POST", f"/repos/{slug}/git/commits", tok,
                        {"message": msg, "tree": tree["sha"], "parents": [remote_sha]}, timeout=120)
    if st_c not in (200, 201):
        raise SystemExit(f"创建提交失败：HTTP {st_c} {str(commit)[:120]}")
    st_r, res_ref = call("PATCH", f"/repos/{slug}/git/refs/heads/{args.branch}", tok,
                         {"sha": commit["sha"]})
    if st_r != 200:
        raise SystemExit(f"更新分支失败：HTTP {st_r} {str(res_ref)[:120]}")
    print(f"[api-push] 已推送：{remote_sha[:8]} → {commit['sha'][:8]}")
    print("[api-push] 本地分支指针已与远端一致（如需可执行 git fetch 更新 origin/main）")


if __name__ == "__main__":
    main()
