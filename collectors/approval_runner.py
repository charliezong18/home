#!/usr/bin/env python3
"""门下画可 → 自动执行器：approvals 台账的唯一消费者。cron 每 10 分钟跑一次；stdout 一行或空。

每轮最多做一件事（全局并发 1）：
  有在办的单（job.autoexec.phase 非 closed）→ 续办它：补派工 / 看 worker 退出没 / 收尾。
  没有 → 取最老的合格 pending 单（kind ∈ kinds 且画可时刻 ≥ autoexec_since；旧积压永不自动跑），
         受每日上限与急停文件约束，开始：
  ① 定范围：只读 claude -p --strict-mcp-config（opus，cwd op），读折文档（合并 sha / 服务 revision）、
     判与「待你拍板」作答 → JSON {executable_steps, gated_steps, repos, acceptance, notes}。
     gated（采购/付款/对外发消息/公开发布/App Store/签署/登录 2FA/不可逆删除）永不自动执行，
     本地关键词再兜一层：可执行步骤里出现 gated 词 → 挪进 gated。无可执行步骤 → blocked + 报告。
  ② 执行：hd.py create --template build --complex --max-turns 60（task_id 由幂等键确定）→ claim 写 task_id
     → 后台启动 worker → hd attach。
  ③ 收尾（worker 退出后下一轮）：读 hd run json → RESULT.zh-CN.md → 呈门下（GitHub：原折 gh pr comment
     + sonnet 小 worker 用 open_folder 开 kind:交付 wait:你读 读物折；服务：该 review 下发 comment）
     → transition done/blocked → approvals sync。permission_denials 非空、测试未过、没给结构化结论 → blocked。

幂等：task_id = 键的哈希；hd create 报「已存在」即复用（dispatch.json 留着派工命令）；pid 文件判断是否已启动；
收尾每一步在 job.autoexec 里打标，崩溃重启从断点续。worker 没退出却进程不在 / 超过 wall clock → blocked。

用法：approval_runner.py [--dry-run] [--autoexec-since ISO] [--config PATH]
配置：$MENXIA_AUTOEXEC_CONFIG，默认 ~/.config/menxia-approvals/autoexec.json（缺失或 enabled=false → 不干活）。
急停：touch ~/.local/state/menxia-approvals/AUTOEXEC_PAUSE（存在即整轮不动，含收尾；在跑的 worker 不杀）。
"""
import argparse
import base64
import datetime as _dt
import fcntl
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import approvals as A  # noqa: E402

HOME = os.path.expanduser("~")
OP = os.path.join(HOME, "Developer/op")
HD = os.path.join(OP, "hermes-dispatch/hd.py")
PYTHON = "/opt/homebrew/bin/python3"
VM_SH = os.path.join(OP, "tools/menxia-vm.sh")
CONFIG_FILE = os.path.expanduser(os.environ.get("MENXIA_AUTOEXEC_CONFIG", "~/.config/menxia-approvals/autoexec.json"))
DEFAULTS = {
    "enabled": False,
    "autoexec_since": None,          # ISO；Hermes 上线时设为启用时刻。没有它一单都不跑
    "kinds": ["设计", "拍板"],
    "daily_limit": 3,
    "wall_clock_hours": 4,
    "scope_timeout_sec": 1200,
    "outputs_dir": os.path.join(OP, "outputs/approvals"),
}
# 照抄 hermes-dispatch/hd.py 的 AUTH_PREFIX：先用交互登录，未登录才 source cron.env 的 setup-token；本文件不读凭据。
AUTH_PREFIX = ('if ! claude auth status 2>/dev/null | grep -q \'"loggedIn": true\'; then '
               '[ -f "$HOME/.config/claude-code/cron.env" ] && { set -a; . "$HOME/.config/claude-code/cron.env"; '
               'set +a; }; fi; unset ANTHROPIC_API_KEY ANTHROPIC_BASE_URL; ')
GATED_CATALOG = ["采购/购买/下单", "付款/支付/转账", "对外发消息/发邮件/发帖/回复他人", "公开发布（上线公开页面、发包、发版）",
                 "App Store / TestFlight 提交", "签署/签字/同意条款", "需要登录/2FA/验证码", "不可逆删除（永久删除、purge、rm -rf、删库删仓）"]
GATED_RE = re.compile(
    r"采购|购买|下单|付款|支付|转账|充值|订阅付费|发邮件|发送邮件|回邮件|发消息|发短信|发帖|发推|对外|公开发布|公开上线|"
    r"发布到|App ?Store|TestFlight|提交审核|签署|签字|签约|登录|登入|2FA|两步验证|验证码|永久删除|不可逆|purge|rm -rf|"
    r"删库|删除仓库|\bpurchase\b|\bbuy\b|\bpay(ment)?\b|\bsend (an )?e?mail\b|\bpublish\b|\bsign\b|\blogin\b|\blog in\b",
    re.I)
WORKER_TOOLS = ["Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)", "Bash(git add:*)",
                "Bash(git commit:*)", "Bash(git push -u origin autoexec/:*)", "Bash(git push origin autoexec/:*)",
                "Bash(git fetch:*)", "Bash(git worktree add:*)", "Bash(git -C:*)", "Bash(gh pr create:*)",
                "Bash(gh pr view:*)", "Bash(ls:*)", "Bash(/opt/homebrew/bin/python3 -m pytest:*)",
                "Bash(npm test:*)", "Bash(npm run test:*)", "Bash(npm ci:*)", f"Bash({VM_SH}:*)"]
STATUS_RE = re.compile(r"AUTOEXEC_STATUS:\s*(\{.*\})")
os.environ["PATH"] = os.path.join(HOME, ".local/bin") + ":" + os.environ["PATH"]  # cron 环境里找得到 claude


def now():
    return A.now()


# ---------- 外部调用（全部可注入替身） ----------

class Deps:
    def __init__(self):
        self.gh_json = A.gh_json
        self.service_http = A.service_http
        self.gh_edit = None  # None → approvals.run_cmd（真 gh pr edit / gh label create）

    def run(self, cmd, timeout=120, cwd=None):
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        return r.returncode, r.stdout, r.stderr

    def scope(self, prompt, prompt_file, timeout):
        """只读定范围：返回 claude -p --output-format json 的 stdout。"""
        with open(prompt_file, "w", encoding="utf-8") as f:
            f.write(prompt)
        argv = ["claude", "-p", "--strict-mcp-config", "--model", "opus", "--output-format", "json",
                "--max-turns", "12", "--tools", "Read,Grep,Glob", "--allowedTools", "Read", "Grep", "Glob", "--"]
        cmd = AUTH_PREFIX + f"cd {shlex.quote(OP)} && {shlex.join(argv)} \"$(cat {shlex.quote(prompt_file)})\" < /dev/null"
        rc, out, err = self.run(["bash", "-c", cmd], timeout=timeout)
        if rc != 0 and not out.strip():
            raise RuntimeError(f"定范围 claude rc={rc}: {err.strip()[-300:]}")
        return out

    def hd(self, args):
        return self.run([PYTHON, HD, *args], timeout=120)

    def launch(self, command, log_file):
        with open(log_file, "ab") as lf:
            p = subprocess.Popen(["bash", "-c", command], stdin=subprocess.DEVNULL, stdout=lf, stderr=lf,
                                 start_new_session=True)
        return p.pid

    def pid_alive(self, pid):
        try:
            os.kill(int(pid), 0)
            return True
        except (OSError, ValueError, TypeError):
            return False

    def gh(self, args):
        rc, out, err = self.run(["gh", *args], timeout=60)
        if rc != 0:
            raise RuntimeError(f"gh {' '.join(args[:3])} rc={rc}: {err.strip()[:200]}")
        return out

    def service_text(self, path, token):
        import urllib.request
        req = urllib.request.Request(A.SERVICE_BASE + path, headers={"Authorization": f"Bearer {token}",
                                                                     "User-Agent": "menxia-approvals"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read().decode("utf-8", "replace")


# ---------- 配置 / 领域小工具 ----------

def load_config(path=None, since_override=None):
    cfg = dict(DEFAULTS)
    path = path or CONFIG_FILE
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cfg.update(json.load(f))
    if since_override:
        cfg["autoexec_since"] = since_override
    return cfg


def pause_file():
    return os.path.join(A.STATE_DIR, "AUTOEXEC_PAUSE")


def slug(key):
    """charliezong18/review#212@<sha> → review-212-<sha12>；menxia-service#rv_x@dc_y → menxia-service-rv_x-dc_y。"""
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", key.split("/")[-1] if key.startswith("charliezong18/") else key)
    head, _, tail = s.rpartition("-")
    if len(tail) == 40:
        s = f"{head}-{tail[:12]}"
    return s.strip("-")[:80]


def task_id(key, prefix="ax"):
    h = hashlib.sha1(key.encode()).hexdigest()[:10]
    return f"{prefix}-{slug(key)[:40].rstrip('-.')}-{h}"


def approved_at(job):
    return A.parse(job.get("merged_at") or job["history"][0]["at"])


def link(job):
    if job.get("source") == A.SERVICE:
        return job.get("web_url") or f"{A.SERVICE_BASE}/reviews/{job.get('review_id')}"
    return f"https://menxia-reader.github.io/menxia/?pr={job['pr']}"


def ax(job):
    return job.get("autoexec") or {}


def ineligible_reason(job, cfg):
    """None = 可以自动跑；否则返回中文原因。"""
    if job["state"] != "pending":
        return f"状态 {job['state']}"
    if ax(job):
        return "已被执行器接过"
    kind = A.kind_of(job.get("labels_at_merge") or [])
    if kind not in cfg["kinds"]:
        return f"kind={kind} 不在 {cfg['kinds']}"
    if not cfg.get("autoexec_since"):
        return "未配置 autoexec_since"
    if approved_at(job) < A.parse(cfg["autoexec_since"]):
        return f"旧积压（画可 {job.get('merged_at')} < autoexec_since {cfg['autoexec_since']}）"
    return None


def started_today(jobs):
    today = now().astimezone().date()
    return sum(1 for j in jobs.values()
               if ax(j).get("started_at") and A.parse(ax(j)["started_at"]).astimezone().date() == today)


def active_job(jobs):
    act = [j for j in jobs.values() if ax(j).get("phase") not in (None, "closed")]
    return sorted(act, key=lambda j: ax(j)["started_at"])[0] if act else None


def read_ledger():
    path, _ = A.paths()
    if not os.path.exists(path):
        return {"jobs": {}}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def patch(key, **kw):
    with A.ledger() as data:
        data["jobs"][key].setdefault("autoexec", {}).update(kw)
        A.queue_event(data, f"autoexec {key} {sorted(kw)}")
        return dict(data["jobs"][key])


def work_dir(key):
    d = os.path.join(A.STATE_DIR, "autoexec", slug(key))
    os.makedirs(d, exist_ok=True)
    return d


def load_json(path):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return None


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ---------- ① 定范围 ----------

def fetch_docs(job, deps, limit=150_000):
    """折的文档原文：GitHub 取 PR 改动的 .md 在 merge_sha 处的内容；服务取批准时 revision 的文档。"""
    docs, total = [], 0
    if job.get("source") == A.SERVICE:
        tok, why = A.service_token()
        if not tok:
            raise RuntimeError(f"服务来源读文档失败：{why}")
        rid = job["review_id"]
        rv = deps.service_http("GET", f"/api/v1/reviews/{rid}", tok) or {}
        rv = rv.get("review", rv)
        cr = rv.get("current_revision") or {}
        no = job.get("revision_no") or cr.get("revision_no")
        for d in cr.get("documents") or cr.get("files") or []:
            p = d.get("path") if isinstance(d, dict) else d
            if p and p.endswith(".md"):
                docs.append((p, deps.service_text(
                    f"/api/v1/reviews/{rid}/revisions/{no}/content/{urllib.parse.quote(p)}", tok)))
    else:
        files = A.gh_pages(f"repos/{A.REPO}/pulls/{job['pr']}/files", deps.gh_json)
        for f in files:
            if f.get("status") == "removed" or not f["filename"].endswith(".md"):
                continue
            c = deps.gh_json(f"repos/{A.REPO}/contents/{urllib.parse.quote(f['filename'])}?ref={job['merge_sha']}")
            docs.append((f["filename"], base64.b64decode(c.get("content") or "").decode("utf-8", "replace")))
    out = []
    for p, text in docs:
        text = text[: max(0, limit - total)]
        total += len(text)
        out.append((p, text))
    return out


def scope_prompt(job, docs):
    decs = "\n\n".join(f"[{d.get('where')} {d.get('at')}]\n{d.get('body')}" for d in job.get("decisions") or []) or "（无）"
    body = "\n\n".join(f"===== 文档 {p} =====\n{t}" for p, t in docs) or "（未取到文档）"
    return f"""你是只读的「定范围」审阅员。Charlie 在门下画可（批准）了下面这份折。你只读、不改任何东西、不调用工具以外的手段。
任务：根据折的文档原文、Charlie 的判（原文）与「待你拍板」作答，列出 Charlie 明确批准、且可以在本机由 agent 完成的下一步。

硬规则：
- 只列 Charlie 明确批准的事；文档里的建议、备选、未拍板的问题不算批准。拿不准就不列进 executable_steps，写进 notes。
- 下列事项永远放进 gated_steps，不得放进 executable_steps：{'；'.join(GATED_CATALOG)}。
- executable_steps 每条一句话、可验证；repos 写本机仓库名或路径；acceptance 写验收标准。

只输出一个 JSON 对象（不要别的文字）：
{{"executable_steps": ["..."], "gated_steps": ["..."], "repos": ["..."], "acceptance": ["..."], "notes": "..."}}

## 折
标题：{job.get('title')}
链接：{link(job)}
幂等键：{job['key']}

## 待你拍板（折原文）
{job.get('pending_decisions') or '（无）'}

## Charlie 的判 / 作答（原文）
{decs}

## 文档原文
{body}
"""


def parse_scope(text):
    """claude 输出 → 规范化的范围 dict；可执行步骤里撞 gated 词的挪进 gated。解析不了 → ValueError。"""
    try:
        outer = json.loads(text)
        if isinstance(outer, list):
            outer = next((r for r in reversed(outer) if isinstance(r, dict) and r.get("type") == "result"), {})
        if isinstance(outer, dict) and "result" in outer and "executable_steps" not in outer:
            text = str(outer.get("result") or "")
    except ValueError:
        pass
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    raw = m.group(1) if m else text[text.find("{"): text.rfind("}") + 1]
    try:
        obj = json.loads(raw)
    except ValueError as e:
        raise ValueError(f"定范围输出不是 JSON：{text[:200]!r}") from e
    if not isinstance(obj, dict):
        raise ValueError("定范围输出不是 JSON 对象")
    as_list = lambda v: [str(x).strip() for x in (v if isinstance(v, list) else [v] if v else []) if str(x).strip()]
    exe, gated = [], as_list(obj.get("gated_steps"))
    for s in as_list(obj.get("executable_steps")):
        if GATED_RE.search(s):
            gated.append(f"{s}（执行器识别为 gated，从可执行步骤挪出）")
        else:
            exe.append(s)
    return {"executable_steps": exe, "gated_steps": gated, "repos": as_list(obj.get("repos")),
            "acceptance": as_list(obj.get("acceptance")), "notes": str(obj.get("notes") or "").strip()}


# ---------- ② 执行 ----------

def worker_brief(job, scope, cfg):
    s = slug(job["key"])
    decs = "\n\n".join(f"[{d.get('where')} {d.get('at')}]\n{d.get('body')}" for d in job.get("decisions") or []) or "（无）"
    num = lambda xs: "\n".join(f"{i + 1}. {x}" for i, x in enumerate(xs)) or "（无）"
    return f"""门下画可自动执行。折：{job.get('title')}（{link(job)}），幂等键 {job['key']}。

## 批准原文（Charlie 的判 / 作答，原文）
{decs}

## 待你拍板（折原文）
{job.get('pending_decisions') or '（无）'}

## 授权的可执行步骤（只做这些，别的不做）
{num(scope['executable_steps'])}

涉及仓库：{', '.join(scope['repos']) or '（未指明）'}

## gated 清单——遇到即停，写进报告「没做的」，永不执行
{num(GATED_CATALOG + scope['gated_steps'])}

## 验收标准
{num(scope['acceptance'])}

## 工作方式（硬规则）
- 代码改动：每单独立分支 `autoexec/{s}`，worktree 建在 `~/Developer/worktrees/<repo>/{s}`（git worktree add），在 worktree 里改；push 后 `gh pr create`，**不合并、不 force push、不动 main**。
- iOS 不在主机编译/跑模拟器：只用 `{VM_SH}`。
- 非代码交付写到 `{cfg['outputs_dir']}/{s}/`。
- 第 50 回合起停止新改动，先 push 再交付报告。
- 报告最后一行必须是一行机器可读结论（JSON 单行）：
  AUTOEXEC_STATUS: {{"verdict": "done 或 blocked", "tests_passed": true/false/null（无代码改动填 null）, "prs": ["PR 链接"], "summary": "一句话结论", "needs_charlie": ["需要 Charlie 做的"]}}
"""


def ensure_dispatch(job, scope, cfg, deps):
    """hd create（幂等：task_id 固定，已存在则用 dispatch.json）。返回 dispatch dict。"""
    d = work_dir(job["key"])
    path = os.path.join(d, "dispatch.json")
    cached = load_json(path)
    if cached:
        return cached
    tid = task_id(job["key"])
    args = ["create", "--template", "build", "--complex", "--max-turns", "60", "--task-id", tid,
            "--question", f"门下画可自动执行：{job.get('title')}", "--context", worker_brief(job, scope, cfg),
            "--cwd", OP, "--add-dir", os.path.join(HOME, "Developer")]
    for t in WORKER_TOOLS:
        args += ["--allowed-tool", t]
    rc, out, err = deps.hd(args)
    if rc == 3:
        raise RuntimeError(f"hd 任务 {tid} 已存在但 dispatch.json 丢失，无法安全续派")
    if rc != 0:
        raise RuntimeError(f"hd create rc={rc}: {err.strip()[:300]}")
    res = json.loads(out)
    disp = dict(res["dispatch"], task_id=tid)
    save_json(path, disp)
    return disp


def launch(job, disp, deps):
    """启动 worker（pid 文件存在 = 已启动，不重复启动）。"""
    d = work_dir(job["key"])
    pid_file = os.path.join(d, "worker.pid")
    if os.path.exists(pid_file):
        return int(open(pid_file).read().strip() or 0)
    pid = deps.launch(disp["command"], os.path.join(d, "worker.log"))
    with open(pid_file, "w") as f:
        f.write(str(pid))
    deps.hd(["attach", "--task-id", disp["task_id"], "--process-id", str(pid), "--event-id", f"{disp['task_id']}-attach"])
    return pid


def start(job, cfg, deps):
    key = job["key"]
    d = work_dir(key)
    if not ax(job).get("started_at"):
        job = patch(key, phase="scoping", started_at=A.iso(now()))
    scope = load_json(os.path.join(d, "scope.json"))
    if scope is None:
        try:
            docs = fetch_docs(job, deps)
            raw = deps.scope(scope_prompt(job, docs), os.path.join(d, "scope.prompt.txt"), cfg["scope_timeout_sec"])
            scope = parse_scope(raw)
        except (ValueError, RuntimeError, subprocess.TimeoutExpired) as e:
            scope = {"executable_steps": [], "gated_steps": [], "repos": [], "acceptance": [],
                     "notes": f"定范围失败：{e}"}
        save_json(os.path.join(d, "scope.json"), scope)
    if not scope["executable_steps"]:
        reason = "定范围没有找到 Charlie 明确批准、可本机完成的步骤" + (f"；{scope['notes']}" if scope["notes"] else "")
        patch(key, phase="finishing", outcome={"ok": False, "reason": reason[:1500], "summary": reason[:200],
                                               "result": "", "prs": [], "needs_charlie": scope["gated_steps"]})
        return finish(read_ledger()["jobs"][key], cfg, deps)
    return dispatch_and_launch(job, scope, cfg, deps)


def dispatch_and_launch(job, scope, cfg, deps):
    key = job["key"]
    disp = ensure_dispatch(job, scope, cfg, deps)
    if read_ledger()["jobs"][key]["state"] == "pending":
        A.transition(key, "executing", by="autoexec", event_id=f"claim-{disp['task_id']}", task_id=disp["task_id"],
                     owner_session=job.get("owner_session") or "autoexec",
                     authorized_scope="\n".join(scope["executable_steps"]))
    patch(key, phase="dispatched", task_id=disp["task_id"])
    pid = launch(job, disp, deps)
    patch(key, phase="launched", pid=pid, launched_at=A.iso(now()))
    return f"门下自动执行：开始 {job.get('title')}（{disp['task_id']}）{link(job)}"


# ---------- ③ 收尾 ----------

def evaluate_run(disp):
    """读 hd run json → outcome。denials / 非 success / 测试未过 / 无 AUTOEXEC_STATUS → ok=False。"""
    exit_code = None
    try:
        exit_code = int(open(disp["exit_file"]).read().strip())
    except (OSError, ValueError):
        pass
    try:
        res = json.load(open(disp["output_json"], encoding="utf-8"))
    except (OSError, ValueError) as e:
        return {"ok": False, "reason": f"run json 读不了：{e}", "summary": "worker 输出不可读", "result": "",
                "prs": [], "needs_charlie": []}
    if isinstance(res, list):
        res = next((r for r in reversed(res) if isinstance(r, dict) and r.get("type") == "result"), {})
    text = str(res.get("result") or "")
    m = None
    for m in STATUS_RE.finditer(text):
        pass
    st = {}
    if m:
        try:
            st = json.loads(m.group(1))
        except ValueError:
            st = {}
    base = {"result": text, "subtype": res.get("subtype"), "exit_code": exit_code,
            "denials": res.get("permission_denials") or [], "prs": st.get("prs") or [],
            "summary": str(st.get("summary") or "").strip(), "needs_charlie": st.get("needs_charlie") or []}
    if base["denials"]:
        why = f"权限被拒 {len(base['denials'])} 次：" + json.dumps(base["denials"], ensure_ascii=False)[:500]
    elif res.get("subtype") != "success" or res.get("is_error") or exit_code not in (None, 0):
        why = f"worker 未正常结束（subtype={res.get('subtype')} is_error={res.get('is_error')} exit={exit_code}）"
    elif not st:
        why = "worker 报告没有 AUTOEXEC_STATUS 结构化结论"
    elif st.get("tests_passed") is False:
        why = "测试未过（worker 自报 tests_passed=false）"
    elif st.get("verdict") != "done":
        why = f"worker 自报 {st.get('verdict')}"
    else:
        why = None
    return dict(base, ok=why is None, reason=why, summary=base["summary"] or (why or "完成"))


def check_worker(job, cfg, deps):
    key, a = job["key"], ax(job)
    disp = load_json(os.path.join(work_dir(key), "dispatch.json"))
    if not disp:
        patch(key, phase="finishing", outcome={"ok": False, "reason": "dispatch.json 丢失，无法确认 worker",
                                               "summary": "派工记录丢失", "result": "", "prs": [], "needs_charlie": []})
        return finish(read_ledger()["jobs"][key], cfg, deps)
    if os.path.exists(disp["exit_file"]):
        patch(key, phase="finishing", outcome=evaluate_run(disp))
        return finish(read_ledger()["jobs"][key], cfg, deps)
    elapsed = now() - A.parse(a.get("launched_at") or a["started_at"])
    if deps.pid_alive(a.get("pid")) and elapsed < _dt.timedelta(hours=cfg["wall_clock_hours"]):
        return ""
    why = ("worker 进程已不在且没有退出记录" if not deps.pid_alive(a.get("pid"))
           else f"超过 wall clock {cfg['wall_clock_hours']} 小时仍未结束")
    patch(key, phase="finishing", outcome={"ok": False, "reason": why, "summary": why, "result": "", "prs": [],
                                           "needs_charlie": ["看一下 worker 日志：" + os.path.join(work_dir(key), "worker.log")]})
    return finish(read_ledger()["jobs"][key], cfg, deps)


def report_path(job, cfg):
    return os.path.join(cfg["outputs_dir"], slug(job["key"]), "RESULT.zh-CN.md")


def render_report(job, scope, o):
    lst = lambda xs: "\n".join(f"- {x}" for x in xs) or "- （无）"
    state = "完成" if o["ok"] else "阻塞"
    return f"""# 结果报告：{job.get('title')}

- 状态：**{state}**
- 来源：{link(job)}
- 幂等键：`{job['key']}`
- 执行任务：{ax(job).get('task_id') or '（未派工）'}
- 生成时间：{A.iso(now())}

## 一句话结论
{o['summary']}{'' if o['ok'] else f'（阻塞原因：{o["reason"]}）'}

## 做了什么
授权的可执行步骤：
{lst(scope.get('executable_steps') or [])}

## 验证证据（worker 报告原文）
```
{o.get('result') or '（无 worker 输出）'}
```

## PR / 提交
{lst(o.get('prs') or [])}

## 没做的与 gated 步骤
{lst(scope.get('gated_steps') or [])}
{('备注：' + scope['notes']) if scope.get('notes') else ''}

## 需要 Charlie 做的
{lst(o.get('needs_charlie') or [])}
"""


def publish(job, cfg, deps, rpath):
    """呈门下。每步打标，崩溃重启不重发。"""
    key, a, o = job["key"], ax(job), ax(job)["outcome"]
    head = f"**回话** 自动执行{'完成' if o['ok'] else '阻塞'}：{o['summary']}"
    body = f"{head}\n\n- 任务：{a.get('task_id') or '（未派工）'}\n- 结果报告：`{rpath}`\n" + \
           "".join(f"- PR：{p}\n" for p in o.get("prs") or []) + \
           ("" if o["ok"] else f"- 阻塞原因：{o['reason']}\n")
    if job.get("source") == A.SERVICE:
        if not a.get("service_comment"):
            tok, why = A.service_token()
            if not tok:
                raise RuntimeError(f"服务评论发不出：{why}")
            deps.service_http("POST", f"/api/v1/reviews/{job['review_id']}/comments", tok, {"body": body})
            patch(key, service_comment=A.iso(now()))
        return
    if not a.get("gh_comment"):
        deps.gh(["pr", "comment", str(job["pr"]), "-R", A.REPO, "--body", body])
        patch(key, gh_comment=A.iso(now()))
    if not a.get("folder_task"):
        proj = [l for l in job.get("labels_at_merge") or [] if l.startswith("proj:")]
        tid = task_id(key, "axpub")
        q = (f"把自动执行结果报告呈门下读物折：读 {rpath}，用 mcp__menxia__open_folder 开一份 kind:交付、wait:你读 的读物折"
             f"（label 另加 {', '.join(proj) or '与原折相同的 proj:*（原折没有就不加）'}），标题「结果：{job.get('title')}」，"
             f"正文首段链接原折 {link(job)}。按 review-loop skill 的呈折格式（中英双语）。只呈折，不做别的。")
        rc, out, err = deps.hd(["create", "--template", "build", "--model", "sonnet", "--max-turns", "20",
                                "--task-id", tid, "--question", q, "--cwd", OP, "--add-dir", cfg["outputs_dir"],
                                "--allowed-tool", "mcp__menxia__open_folder"])
        if rc not in (0, 3):
            raise RuntimeError(f"读物折 worker 建不了 rc={rc}: {err.strip()[:200]}")
        if rc == 0:
            disp = json.loads(out)["dispatch"]
            pid = deps.launch(disp["command"], os.path.join(work_dir(key), "publish.log"))
            deps.hd(["attach", "--task-id", tid, "--process-id", str(pid), "--event-id", f"{tid}-attach"])
        patch(key, folder_task=tid)


def finish(job, cfg, deps):
    key, o = job["key"], ax(job)["outcome"]
    scope = load_json(os.path.join(work_dir(key), "scope.json")) or {}
    rpath = report_path(job, cfg)
    if not ax(job).get("report"):
        os.makedirs(os.path.dirname(rpath), exist_ok=True)
        with open(rpath, "w", encoding="utf-8") as f:
            f.write(render_report(job, scope, o))
        job = patch(key, report=rpath)
    publish(job, cfg, deps, rpath)
    st = read_ledger()["jobs"][key]["state"]
    if o["ok"]:
        if st == "executing":
            A.transition(key, "verifying", by="autoexec", event_id=f"verify-{key}")
            st = "verifying"
        if st == "verifying":
            A.transition(key, "done", evidence=f"{o['summary']}；报告 {rpath}；PR {o.get('prs')}", by="autoexec",
                         event_id=f"done-{key}")
    elif st in ("pending", "executing", "verifying"):
        A.transition(key, "blocked", reason=o["reason"], evidence=f"报告 {rpath}", by="autoexec",
                     event_id=f"blocked-{key}")
    try:
        A.sync(key, deps.gh_json, deps.gh_edit)
    except Exception as e:  # noqa: BLE001 — label 回写失败不影响收尾，下次手动 approvals.py sync 补
        patch(key, sync_error=str(e)[:200])
    patch(key, phase="closed", closed_at=A.iso(now()))
    return f"门下自动执行：{'完成' if o['ok'] else '卡住'} {job.get('title')}——{o['summary']} {link(job)}"


# ---------- 一轮 ----------

def run_once(cfg, deps=None, dry_run=False):
    deps = deps or Deps()
    if dry_run:
        return plan(cfg)
    if not cfg.get("enabled") or os.path.exists(pause_file()):
        return ""
    os.makedirs(A.STATE_DIR, exist_ok=True)
    with open(os.path.join(A.STATE_DIR, "autoexec.lock"), "w") as lk:
        try:
            fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return ""  # 上一轮还在跑（并发 1）
        jobs = read_ledger()["jobs"]
        cur = active_job(jobs)
        try:
            if cur:
                a = ax(cur)
                if a["phase"] == "finishing":
                    return finish(cur, cfg, deps)
                if a["phase"] == "launched":
                    return check_worker(cur, cfg, deps)
                scope = load_json(os.path.join(work_dir(cur["key"]), "scope.json"))
                return start(cur, cfg, deps) if scope is None or a["phase"] == "scoping" else \
                    dispatch_and_launch(cur, scope, cfg, deps)
            if started_today(jobs) >= cfg["daily_limit"]:
                return ""
            cands = sorted((j for j in jobs.values() if ineligible_reason(j, cfg) is None), key=approved_at)
            return start(cands[0], cfg, deps) if cands else ""
        except (Exception, SystemExit) as e:  # noqa: BLE001 — 留在原 phase，下一轮续（transition 非法会 SystemExit）
            return f"门下自动执行：本轮出错，下轮重试（{type(e).__name__}: {str(e)[:200]}）"


def plan(cfg):
    """--dry-run：只读台账，打印「会」怎么处理；不调 claude/hd/gh，不写任何状态。"""
    jobs = read_ledger()["jobs"]
    out = [f"[dry-run] enabled={cfg.get('enabled')} autoexec_since={cfg.get('autoexec_since')} kinds={cfg['kinds']} "
           f"daily_limit={cfg['daily_limit']} 今日已开始={started_today(jobs)} "
           f"急停={'是' if os.path.exists(pause_file()) else '否'} 台账 jobs={len(jobs)}"]
    if not cfg.get("enabled"):
        out.append("[dry-run] 注意：enabled=false，真跑时本轮什么都不做（下面按 enabled=true 推演）")
    cur = active_job(jobs)
    if cur:
        out.append(f"[dry-run] 有在办单 {cur['key']} phase={ax(cur)['phase']} → 本轮只续办它，不开新单")
    budget = max(0, cfg["daily_limit"] - started_today(jobs))
    picked = False
    for j in sorted(jobs.values(), key=approved_at):
        why = ineligible_reason(j, cfg)
        title = (j.get("title") or "")[:40]
        if why:
            out.append(f"  跳过 {j['key'][:60]}「{title}」：{why}")
        elif cur or picked or not budget:
            out.append(f"  合格但本轮不处理 {j['key'][:60]}「{title}」：{'有在办单' if cur else '每日上限' if not budget else '并发 1，排队'}")
        else:
            picked = True
            out.append(f"  会开始 {j['key'][:60]}「{title}」：定范围（只读 opus）→ hd create {task_id(j['key'])} → claim → 后台 worker")
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--autoexec-since")
    ap.add_argument("--config")
    a = ap.parse_args(argv)
    line = run_once(load_config(a.config, a.autoexec_since), dry_run=a.dry_run)
    if line:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
