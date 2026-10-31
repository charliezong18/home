"""测试用 git 夹具（不是测试本身，文件名不以 test_ 开头）：tmp 目录里的本地裸仓库当 remote，完全离线。

- isolated_env(tmp)：返回要打进 os.environ 的补丁，屏蔽全局 / 系统 git 配置（签名、代理、insteadOf 等），
  并给出固定的 user.name / email，测试结果不依赖跑测试那台机器的 ~/.gitconfig。
- make_remote(tmp)：建 bare 仓（分支 home-data）并推一个初始提交，返回 remote 路径。
- clone(remote, dest)：克隆 home-data 分支。
"""
import os
import subprocess

BRANCH = "home-data"


def sh(*cmd, cwd=None, check=True):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError(f"{' '.join(cmd)} -> rc={r.returncode}\nstdout={r.stdout}\nstderr={r.stderr}")
    return r.stdout


def isolated_env(tmp):
    cfg = os.path.join(tmp, "gitconfig")
    with open(cfg, "w") as f:
        f.write("[user]\n\tname = t\n\temail = t@example.com\n"
                "[commit]\n\tgpgsign = false\n[tag]\n\tgpgsign = false\n"
                f"[init]\n\tdefaultBranch = {BRANCH}\n")
    return {"GIT_CONFIG_GLOBAL": cfg, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}


def make_remote(tmp, name="remote.git"):
    remote = os.path.join(tmp, name)
    sh("git", "init", "-q", "--bare", "-b", BRANCH, remote)
    seed = os.path.join(tmp, "_seed")
    sh("git", "init", "-q", "-b", BRANCH, seed)
    with open(os.path.join(seed, "README.md"), "w") as f:
        f.write("fake home-data\n")
    sh("git", "-C", seed, "add", "README.md")
    sh("git", "-C", seed, "commit", "-q", "-m", "init")
    sh("git", "-C", seed, "remote", "add", "origin", remote)
    sh("git", "-C", seed, "push", "-q", "origin", f"HEAD:{BRANCH}")
    return remote


def clone(remote, dest):
    sh("git", "clone", "-q", "-b", BRANCH, remote, dest)
    return dest


def commit_file(repo, rel, text, message, append=False, push=True):
    """在 repo 里写/追加一个文件 → commit →（可选）push，模拟网页 / iOS / 另一台机器的写入（先拉最新，像网页先 GET sha）。"""
    if push:
        sh("git", "-C", repo, "pull", "-q", "--rebase", "origin", BRANCH)
    with open(os.path.join(repo, rel), "a" if append else "w", encoding="utf-8") as f:
        f.write(text)
    sh("git", "-C", repo, "add", "--", rel)
    sh("git", "-C", repo, "commit", "-q", "-m", message)
    if push:
        sh("git", "-C", repo, "push", "-q", "origin", f"HEAD:{BRANCH}")


def remote_show(remote, rel):
    r = subprocess.run(["git", "-C", remote, "show", f"{BRANCH}:{rel}"], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def log_subjects(repo, ref="HEAD"):
    return sh("git", "-C", repo, "log", "--format=%s", ref).splitlines()


def rev(repo, ref="HEAD"):
    return sh("git", "-C", repo, "rev-parse", ref).strip()
