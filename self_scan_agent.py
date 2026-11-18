#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自扫描代码 Agent · 小型 Python 项目原型

用法（目标仓库外运行）:
    python self_scan_agent.py <repo路径> --ack-isolated            # 仅扫描+测试
    python self_scan_agent.py <repo路径> --ack-isolated --apply --rounds 3

安全前提见任务书：必须在隔离环境运行，使用仓库副本。
API: 默认 OpenAI Responses API；可设 OPENAI_BASE_URL 指向兼容端点（如 LM Studio :1234/v1），
     此时自动回退 chat/completions 协议（对本地 LMS 的增强，偏离spec处已在README注明）。
"""
import argparse
import ast
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

MAX_ROUNDS_CAP = 5
MAX_FILES_PER_ROUND = 3
MAX_FILE_READ = 20 * 1024
MAX_CONTEXT = 80 * 1024
TEST_TIMEOUT = 60
API_TIMEOUT = 90

api_stats = {"calls": 0, "fallbacks": 0}


def die(msg, code=1):
    print(f"[FATAL] {msg}")
    sys.exit(code)


def run(cmd, cwd=None, timeout=60, check=False):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError(f"cmd failed ({p.returncode}): {' '.join(cmd)}\n{p.stderr[:500]}")
    return p


def find_git():
    import shutil
    g = shutil.which("git")
    if g:
        return g
    for cand in (r"C:\Users\GAO\Projects\PortableGit\bin\git.exe",
                 r"C:\Program Files\Git\cmd\git.exe"):
        if os.path.exists(cand):
            return cand
    die("找不到git，请安装或加入PATH")


def git(repo, *args, timeout=30):
    return run([find_git(), *args], cwd=repo, timeout=timeout)


# ---------- 环境检查 ----------

def check_git_clean(repo):
    p = git(repo, "status", "--porcelain")
    if p.stdout.strip():
        die(f"Git工作区不干净，先 commit 或 stash:\n{p.stdout[:500]}")
    if not os.path.isdir(os.path.join(repo, ".git")):
        die("目标不是git仓库（无.git目录）")


def check_layout(repo):
    src = os.path.join(repo, "src")
    tests = os.path.join(repo, "tests")
    if not (os.path.isdir(src) and os.path.isdir(tests)):
        die("仅支持 src/ + tests/ 布局")
    return src, tests


# ---------- 扫描 ----------

def scan_py(repo):
    src = os.path.join(repo, "src")
    out = []
    for root, _, files in os.walk(src):
        for f in files:
            if f.endswith(".py"):
                p = os.path.join(root, f)
                rel = os.path.relpath(p, repo).replace("\\", "/")
                size = os.path.getsize(p)
                out.append((p, rel, size))
    return out


def build_context(repo, files, limit=MAX_CONTEXT):
    parts, total = [], 0
    for p, rel, size in sorted(files, key=lambda x: x[1]):
        if size > MAX_FILE_READ:
            parts.append(f"--- {rel} (文件{size}字节，超过20KB上限，跳过) ---")
            continue
        with open(p, encoding="utf-8", errors="replace") as f:
            content = f.read()
        block = f"--- {rel} ---\n{content}\n"
        if total + len(block) > limit:
            parts.append(f"--- {rel} (上下文已达{limit}字符上限，截断) ---")
            break
        parts.append(block)
        total += len(block)
    return "\n".join(parts)


# ---------- 测试 ----------

def run_tests(repo):
    python = os.environ.get("AGENT_PYTHON", sys.executable)
    try:
        p = run([python, "-m", "unittest", "discover", "-s", "tests", "-v"],
                cwd=repo, timeout=TEST_TIMEOUT)
        return {"ok": p.returncode == 0, "stdout": p.stdout, "stderr": p.stderr,
                "rc": p.returncode}
    except subprocess.TimeoutExpired:
        return {"ok": False, "stdout": "", "stderr": f"测试超时({TEST_TIMEOUT}s)", "rc": -1}


# ---------- API ----------

def api_config():
    key = os.getenv("OPENAI_API_KEY")
    model = os.getenv("OPENAI_MODEL")
    base = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    if not key or not model:
        die("--apply 需要 OPENAI_API_KEY 和 OPENAI_MODEL 环境变量")
    return key, model, base


def call_model(key, model, base, system, user):
    """Responses API 优先；404/不支持时回退 chat/completions（本地LMS增强）。"""
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    api_stats["calls"] += 1

    # 1) Responses API
    if base.endswith("/v1"):
        payload = {"model": model, "store": False,
                   "instructions": system, "input": user}
        req = urllib.request.Request(base + "/responses",
                                     data=json.dumps(payload).encode(),
                                     headers={"Authorization": "Bearer " + key,
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=API_TIMEOUT) as r:
                j = json.loads(r.read())
            out = j.get("output", [])
            texts = [c.get("text", "") for x in out if x.get("type") == "message"
                     for c in x.get("content", []) if c.get("type") == "output_text"]
            if texts:
                return "\n".join(texts)
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise

    # 2) chat/completions 回退
    api_stats["fallbacks"] += 1
    payload = {"model": model, "messages": messages, "temperature": 0.2}
    req = urllib.request.Request(base + "/chat/completions",
                                 data=json.dumps(payload).encode(),
                                 headers={"Authorization": "Bearer " + key,
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=API_TIMEOUT) as r:
        j = json.loads(r.read())
    return j["choices"][0]["message"]["content"]


FIX_SYSTEM = """你是代码修复助手。用户给你一个Python项目的测试失败日志和相关源码。
只输出修复方案：对每个需要修改的文件，用如下格式给出完整的修改后代码块（不是diff）：

FILE: <相对路径，如 src/calc.py>
```python
<完整的新文件内容>
```

规则：
- 只修改已有 src/**/*.py 文件，最多3个文件
- 不创建新文件，不修改测试
- 不输出任何其他解释、前言或结语
- 如果无法修复，输出一行 IMPOSSIBLE: <原因>"""


def parse_fixes(reply):
    """解析模型回复中的 FILE: + ```python 代码块。"""
    fixes = {}
    blocks = re.split(r"FILE:\s*", reply)
    for b in blocks[1:]:
        m = re.match(r"([^\n`]+)\s*\n+```python\s*\n(.*?)```", b, re.S)
        if m:
            path = m.group(1).strip().replace("\\", "/")
            code = m.group(2)
            fixes[path] = code
    impossible = re.search(r"IMPOSSIBLE:\s*(.+)", reply)
    return fixes, (impossible.group(1).strip() if impossible else None)


def safe_rel_path(repo, rel):
    """路径校验：必须在 src/ 下，是 .py，不越界。"""
    rel = rel.replace("\\", "/").lstrip("/")
    if not rel.startswith("src/") or not rel.endswith(".py"):
        return None
    full = os.path.normpath(os.path.join(repo, rel))
    src_root = os.path.normpath(os.path.join(repo, "src"))
    if not full.startswith(src_root + os.sep):
        return None
    return full


def syntax_ok(code):
    try:
        ast.parse(code)
        return True
    except SyntaxError:
        return False


# ---------- 修复轮 ----------

def apply_round(repo, files, test_result, key, model, base, rnd):
    print(f"\n[ROUND {rnd}] 请求模型修复...")
    context = build_context(repo, files)
    user = (f"测试失败日志:\n```\n{test_result['stderr'][-3000:] or test_result['stdout'][-3000:]}\n```\n\n"
            f"相关源码:\n{context}\n\n请修复使全部测试通过。")
    reply = call_model(key, model, base, FIX_SYSTEM, user)

    fixes, impossible = parse_fixes(reply)
    if impossible:
        print(f"[ROUND {rnd}] 模型认为无法修复: {impossible}")
        return False
    if not fixes:
        print(f"[ROUND {rnd}] 未能解析出修复方案")
        return False

    fixes = dict(list(fixes.items())[:MAX_FILES_PER_ROUND])
    applied = []
    for rel, code in fixes.items():
        full = safe_rel_path(repo, rel)
        if full is None:
            print(f"  跳过非法路径: {rel}")
            continue
        if not syntax_ok(code):
            print(f"  跳过语法错误: {rel}")
            continue
        with open(full, "w", encoding="utf-8") as f:
            f.write(code)
        applied.append(rel)
        print(f"  已写入: {rel} ({len(code)}字符)")

    if not applied:
        print(f"[ROUND {rnd}] 无有效修改")
        return False

    print(f"[ROUND {rnd}] 重新运行测试...")
    result = run_tests(repo)
    if result["ok"]:
        print(f"[ROUND {rnd}] ✅ 测试通过！")
        return True
    print(f"[ROUND {rnd}] ❌ 测试仍失败，回滚本轮写入的文件...")
    # 回滚：恢复到本轮修改前状态（git checkout 这些文件）
    if applied:
        git(repo, "checkout", "--", *applied)
        print(f"  已回滚: {', '.join(applied)}")
    return False


# ---------- 报告 ----------

def write_report(repo, report_dir_name, data):
    sibling = os.path.join(os.path.dirname(repo.rstrip("\\/")), report_dir_name)
    os.makedirs(sibling, exist_ok=True)
    path = os.path.join(sibling, f"report-{time.strftime('%Y%m%d-%H%M%S')}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False, indent=1))
    print(f"[报告] {path}")
    return path


# ---------- 主流程 ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("--ack-isolated", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--report-dir", default=None)
    args = ap.parse_args()

    if not args.ack_isolated:
        die("必须加 --ack-isolated 确认在隔离环境运行（安全前提，见任务书）")
    args.rounds = min(args.rounds, MAX_ROUNDS_CAP)
    repo = os.path.abspath(args.repo)
    if not os.path.isdir(repo):
        die(f"仓库不存在: {repo}")

    check_git_clean(repo)
    src, tests = check_layout(repo)
    files = scan_py(repo)
    print(f"[SCAN] {len(files)}个源文件, 共{sum(s for _, _, s in files)}字节")

    print("[TEST] 运行unittest...")
    result = run_tests(repo)
    print(f"[TEST] {'PASS' if result['ok'] else 'FAIL'}")
    if result["ok"]:
        print("无失败测试，无需修复。收工。")
        return

    report_dir = args.report_dir or (os.path.basename(repo) + "-agent-report")

    if not args.apply:
        print("\n[提示] 有失败测试。加 --apply 进入修复模式（需要 OPENAI_API_KEY/OPENAI_MODEL）。")
        write_report(repo, report_dir, {"mode": "scan", "result": result.__dict__ if hasattr(result,'__dict__') else result})
        return

    key, model, base = api_config()
    print(f"[API] model={model} base={base}")

    fixed = False
    history = []
    for rnd in range(1, args.rounds + 1):
        ok = apply_round(repo, files, result, key, model, base, rnd)
        result = run_tests(repo)
        history.append({"round": rnd, "pass": result["ok"]})
        if result["ok"]:
            fixed = True
            break

    diff = git(repo, "diff").stdout if fixed else ""
    data = {"fixed": fixed, "rounds": args.rounds, "history": history,
            "api_calls": api_stats["calls"], "fallbacks": api_stats["fallbacks"],
            "diff": diff[-5000:]}
    write_report(repo, report_dir, data)
    print(f"\n[结果] {'修复成功' if fixed else '未能在轮次内修复'} | API调用{api_stats['calls']}次")

    if fixed:
        print("\n=== DIFF（未提交，供人工审查）===")
        print(diff[:3000])


if __name__ == "__main__":
    main()
