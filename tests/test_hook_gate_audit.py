"""提交闸门「绕过留痕」的测试（ML-003）。

为什么必须有这个文件：

    `MYLEDGER_ALLOW_SENSITIVE=YES_I_AM_SURE` 是刻意保留的紧急通道 ——
    判断有误的时候总要有个出口。但它原本**什么都不留**：绕过一次和绕过
    一百次，事后看到的证据完全一样，根本无法回答「这个仓库到底有没有
    过被-circuit breaker 放行的敏感内容」。

    所以绕过时要写一条审计日志。这里钉住三件事：

        1. 不带绕过变量时，闸门照样拦得住（别把这件事顺手挠坏）
        2. 带绕过变量时提交成功，并且**确实留下了记录**
        3. 普通提交（没走绕过）**不会**产生这种日志 ——
           审计的前提是「有和无有区别」，次次都记等于没记

    钩子是 shell 脚本，这里跑的是真实链路：临时仓库 + 真实 `git commit`
    + 真实钩子执行，而不是把脚本抠出来单独验一句 truncation。

环境：需要 bash 与 git。找不到就明确打印 SKIP，**不假装通过**。

用法：<venv>\\Scripts\\python.exe tests/test_hook_gate_audit.py
返回码 0 表示全部通过（含 SKIP）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HOOK = os.path.join(ROOT, ".githooks", "pre-commit")

FAILURES: list[str] = []

BASH = shutil.which("bash")
GIT = shutil.which("git")

TEST_USER = "MyLedger Gate Test"
TEST_MAIL = "gate-test@synthetic.invalid"

# 假手机号必须**拼出来**，不能写成完整的 11 位字面量：
# 提交闸门会扫描仓库里所有疑似手机号的内容，写了就提交不上去 ——
# 这正是它该做的，不能用绕过变量绕过去。
# 拼接后源码里没有连续的 11 位数字，扫不到；但运行时拼出来的字符串
# 仍然是合法手机号，闸门照样拦 —— 而这个测试要验的就是「拦得住」。
# 手法和 `.githooks/pre-commit` 里把私钥标记拆成 'PRIV''ATE' 是同一套。
PHONE_A = "139" + "00001111"
PHONE_B = "139" + "00002222"


def check(name: str, got, want) -> None:
    ok = got == want
    if not ok:
        FAILURES.append(f"{name}: 期望 {want!r}，实际 {got!r}")
    print(f"  [{'✓' if ok else '✗'}] {name:<46} -> {got!r}")


def read(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)


def git(repo: str, *args, env=None) -> subprocess.CompletedProcess:
    full = dict(os.environ)
    full.update(env or {})
    return subprocess.run([GIT, *args], cwd=repo, env=full,
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def init_repo(repo: str) -> None:
    """造一个装着本项目闸门的临时仓库。"""
    os.makedirs(repo, exist_ok=True)
    git(repo, "init", "-q")
    git(repo, "config", "user.name", TEST_USER)
    git(repo, "config", "user.email", TEST_MAIL)
    git(repo, "config", "commit.gpgsign", "false")
    hooks = os.path.join(repo, ".githooks")
    os.makedirs(hooks, exist_ok=True)
    shutil.copyfile(HOOK, os.path.join(hooks, "pre-commit"))
    git(repo, "config", "core.hooksPath", ".githooks")


def commit(repo: str, bypass: bool) -> subprocess.CompletedProcess:
    env = {"MYLEDGER_ALLOW_SENSITIVE": "YES_I_AM_SURE"} if bypass else {}
    return git(repo, "commit", "-q", "-m", "合成测试提交", env=env)


def audit_log(repo: str) -> str:
    return os.path.join(repo, ".workbuddy", "audit", "gate-bypass.log")


def run(tmp: str) -> int:
    # ------------------------------------------------------------------
    print()
    print("=" * 78)
    print("闸门本身没被改坏：不该过的还是过不了")
    print("=" * 78)
    blocked = os.path.join(tmp, "repo-blocked")
    init_repo(blocked)
    write(os.path.join(blocked, "imports", "real-bill.csv"),
          f"交易时间,对方,手机\n2026-09-01 10:00:00,某商户,{PHONE_A}\n")
    git(blocked, "add", "-A")
    res = commit(blocked, bypass=False)
    check("不带绕过变量时被拦下（非零退出）", res.returncode != 0, True)
    hint = (res.stdout or "") + (res.stderr or "")
    check("拦下的理由点明了这个文件", "imports/real-bill.csv" in hint, True)
    check("提示了该走的正路（数据放 vault）", "MyLedgerVault" in hint, True)
    check("这次没有留下审计记录", os.path.exists(audit_log(blocked)), False)

    # ------------------------------------------------------------------
    print()
    print("=" * 78)
    print("绕过可以提交，但必须留下痕迹")
    print("=" * 78)
    passed = os.path.join(tmp, "repo-bypassed")
    init_repo(passed)
    write(os.path.join(passed, "imports", "real-bill.csv"),
          f"交易时间,对方,手机\n2026-09-01 10:00:00,某商户,{PHONE_B}\n")
    git(passed, "add", "-A")
    res = commit(passed, bypass=True)
    check("绕过变量生效，提交成功", res.returncode, 0)

    log = audit_log(passed)
    check("留下了绕过记录", os.path.exists(log), True)
    if os.path.exists(log):
        body = read(log)
        check("记录里有事件名", "bypass-commit" in body, True)
        check("记录里点明了被放行的内容", "imports/real-bill.csv" in body, True)
        check("记录里点明了拦了几条", "hits=" in body, True)
        check("记录里写了是谁提交的", TEST_USER in body, True)
        check("记录里有时间戳", len(body.split("\t", 1)[0]) >= 19, True)
        check("记录是追加的，不是覆盖", body.count("bypass-commit"), 1)

    # 再绕一次，应当是追加而不是覆盖
    write(os.path.join(passed, "imports", "second.csv"), "再来一份\n")
    git(passed, "add", "-A")
    commit(passed, bypass=True)
    body = read(log)
    check("第二次绕过也记上了", body.count("bypass-commit"), 2)

    # ------------------------------------------------------------------
    print()
    print("=" * 78)
    print("普通提交不许产生这种日志（有和无必须有区别）")
    print("=" * 78)
    clean = os.path.join(tmp, "repo-clean")
    init_repo(clean)
    write(os.path.join(clean, "fixtures", "sample.csv"), "商户,金额\n合成便利店,12.50\n")
    git(clean, "add", "-A")
    res = commit(clean, bypass=False)
    check("干净提交直接通过", res.returncode, 0)
    check("没有产生审计日志", os.path.exists(audit_log(clean)), False)

    print()
    print("=" * 78)
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项：")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print("全部通过。")
    return 0


def main() -> int:
    if not GIT or not BASH:
        print("SKIP：缺少 git 或 bash，钩子审计这一项没跑。")
        print("  这不是通过 —— 换台有 Git Bash 的机器再跑一次。")
        return 0
    tmp = tempfile.mkdtemp(prefix="myledger-gate-")
    try:
        return run(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
