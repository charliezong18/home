"""邸报早晚推送（早报 08:00 / 晚报 21:30，America/Los_Angeles）：内容组装 + 脱敏 + 时间窗口。纯函数，不发送。

发送与去重走 events.Notifier.push：键 `digest-<kind>-<日期>`（带日期，pushed.jsonl 保留 7 天），
同一天每类最多一条；重启 / 补跑读回账本不重发。错过时间点 GRACE 秒内补发，超过就跳过。

敏感项：折带受保护的 `签:` / `proj:` 标签，或任何条目标题含金额 → 锁屏只显示「N 件私密事项」，不显示标题和折号。
"""
import datetime as dt
import re
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Los_Angeles")
SLOTS = {"morning": (8, 0), "evening": (21, 30)}
GRACE = 30 * 60
SOON_DAYS = 3
MAX_TITLES = 3
TITLE_MAX = 24
DEFAULT_QIAN = frozenset({"身份", "健康", "钱"})     # 通用缺省；其余受保护 label 只在本机 config.toml [digest] 里设
PROTECTED_QIAN = set(DEFAULT_QIAN)
PROTECTED_PROJ = set()
MONEY_RE = re.compile(r"[$¥￥€£]\s?\d|\d[\d,.]*\s?(?:元|块|美元|美金|刀|万|[kK]\b|USD|usd|RMB|rmb)"
                      r"|(?<![A-Za-z])(?:USD|RMB|CNY|HKD|EUR|usd|rmb|cny|hkd|eur)\s?\d"     # 货币在前：USD 300
                      r"|[零一二两三四五六七八九十百千万亿]+\s?(?:块|元|美元|美金|刀)"   # 中文数字：三千块
                      r"|\d{1,3}(?:,\d{3})+(?!\d)")                                 # 无单位千分位：1,200
STALE_H = 3                      # home.json 生成时间超过这么久 = 数据过期，早晚报不下「空日」结论
CORE_SOURCES = ("menxia", "dashboard")
STALE_FAILS = 3
STALE = {"morning": "邸报数据过期，早报暂缺（{why}）", "evening": "邸报数据过期，晚报暂缺（{why}）"}
# App（menxia-ios HomeLink）目前只认 menxia://home（邸报）/ ?id= / ?pr=，没有跳「待审」tab 的深链；晚报暂时也跳邸报。
URLS = {"morning": "menxia://home", "evening": "menxia://home?tab=open"}  # 晚报点开进「待审」（menxia-ios #36 起支持）
TITLES = {"morning": "早报", "evening": "晚报"}
EMPTY = {"morning": "今天没有到期和待拍", "evening": "今天没有新进展，明天暂无安排"}
WEEK = "一二三四五六日"


def now_la(now=None):
    """now：带时区 datetime / epoch 秒 / None（现在）→ 洛杉矶时间。"""
    if now is None:
        return dt.datetime.now(TZ)
    if isinstance(now, (int, float)):
        return dt.datetime.fromtimestamp(now, TZ)
    return now.astimezone(TZ)


def slot_day(kind, now=None):
    """现在落在 kind 的 [时间点, 时间点 + GRACE) 里 → 当天日期（YYYY-MM-DD）；否则 None（未到或已错过）。"""
    t = now_la(now)
    h, m = SLOTS[kind]
    start = t.replace(hour=h, minute=m, second=0, microsecond=0)
    return t.date().isoformat() if start <= t < start + dt.timedelta(seconds=GRACE) else None


def key(kind, day):
    return f"digest-{kind}-{day}"


def la_date(iso):
    """ISO 时间戳（含 Z）→ 洛杉矶日期串；解析不了返回 ''。"""
    try:
        return dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone(TZ).date().isoformat()
    except ValueError:
        return ""


# ---------- 脱敏 ----------
def configure(digest_cfg=None):
    """config.toml [digest]：protected_qian / protected_proj（列表，缺省空）→ 在通用缺省之上追加。返回生效集合。"""
    c = digest_cfg or {}
    PROTECTED_QIAN.clear()
    PROTECTED_QIAN.update(DEFAULT_QIAN, (str(x).lower() for x in c.get("protected_qian") or []))
    PROTECTED_PROJ.clear()
    PROTECTED_PROJ.update(str(x).lower() for x in c.get("protected_proj") or [])
    return set(PROTECTED_QIAN), set(PROTECTED_PROJ)


def has_money(title):
    return bool(MONEY_RE.search(title or ""))


def fold_private(f):
    qian = {str(q).lower() for q in f.get("qian") or []}
    proj = {str(p).lower() for p in f.get("proj") or []}
    return bool(qian & PROTECTED_QIAN or proj & PROTECTED_PROJ or has_money(f.get("title")))


def item_private(it):
    """due[] / projects[] 条目：显式 private: true（life.jsonl 可带）或标题含金额。事件推送与早晚报共用。"""
    return bool(it.get("private") is True or has_money(it.get("title")))


def stale_reason(home, now=None):
    """home.json 缺失 / 过旧 / 主要源连续失败 → 原因文本（锁屏可见，不含标题）；数据可信返回 None。"""
    if not home:
        return "home.json 缺失"
    try:
        gen = dt.datetime.fromisoformat(str(home.get("generated")).replace("Z", "+00:00"))
    except ValueError:
        return "home.json 无生成时间"
    age = (now_la(now) - gen).total_seconds() / 3600
    if age > STALE_H:
        return f"{int(age)} 小时未更新"
    bad = [s for s in CORE_SOURCES if int(((home.get("sources") or {}).get(s) or {}).get("consecutive_failures") or 0)
           >= STALE_FAILS]
    return f"{'、'.join(bad)} 采集连续失败" if bad else None


def short(s):
    s = (s or "").strip() or "（无标题）"
    return s if len(s) <= TITLE_MAX else s[:TITLE_MAX] + "…"


def listing(items, fmt=lambda it: short(it["title"])):
    """[{title, private}] → 「A、B 等 5 件」；私密的只计数，不列标题。"""
    pub = [it for it in items if not it["private"]]
    priv = len(items) - len(pub)
    parts = [fmt(it) for it in pub[:MAX_TITLES]]
    more = len(pub) - len(parts)
    s = "、".join(parts)
    if more:
        s += f" 等 {len(pub)} 件"
    if priv:
        s += ("，另 " if s else "") + f"{priv} 件私密事项"
    return s


def fold_label(f):
    return "1 件私密事项" if f["private"] else f"#{f['n']} {short(f['title'])}"


# ---------- 取数 ----------
def folds(home):
    """folders.oldest 各组 → 去重的折列表（含 private）。oldest 每组最多 20 条，够推送用。"""
    seen, out = set(), []
    for group in ((home.get("folders") or {}).get("oldest") or {}).values():
        for f in group or []:
            if f.get("n") in seen:
                continue
            seen.add(f.get("n"))
            out.append({**f, "private": fold_private(f)})
    return out


def dated_items(home):
    """due[].date + projects[].deadline + 折的 deadline → [{id, title, date, private}]，按日期升序。"""
    out = []
    for d in home.get("due") or []:
        if d.get("date"):
            out.append({"id": d.get("id"), "title": d.get("title", ""), "date": d["date"], "private": item_private(d)})
    for p in home.get("projects") or []:
        if p.get("deadline"):
            out.append({"id": p.get("id"), "title": p.get("title", ""), "date": str(p["deadline"])[:10],
                        "private": item_private(p)})
    for f in folds(home):
        if f.get("deadline"):
            out.append({"id": f"#{f['n']}", "title": f.get("title", ""), "date": f["deadline"], "private": f["private"]})
    return sorted(out, key=lambda x: x["date"])


def you_pai(home):
    fo = home.get("folders") or {}
    n = int((fo.get("counts") or {}).get("你拍") or 0)
    lst = (fo.get("oldest") or {}).get("你拍") or []
    oldest = {**lst[0], "private": fold_private(lst[0])} if lst else None
    return n, oldest


def todos_done_today(records, today):
    """todo-actions.jsonl 记录 → 今天（洛杉矶）经邸报勾完成、且没在今天撤销的任务数。"""
    first, finals = {}, []
    for r in records or []:
        if not isinstance(r, dict) or not r.get("id"):
            continue
        if r.get("op") and r["id"] not in first:
            first[r["id"]] = r
        elif r.get("state") == "done" and not r.get("op"):
            finals.append(r)
    last = {}
    for r in sorted(finals, key=lambda x: str(x.get("ts", ""))):
        a = first.get(r["id"])
        if a and la_date(r.get("ts")) == today:
            last[a.get("task_id")] = a.get("op")
    return sum(1 for op in last.values() if op == "complete")


def header(kind, day):
    d = dt.date.fromisoformat(day)
    return f"{TITLES[kind]} · {d.month}/{d.day} 周{WEEK[d.weekday()]}"


# ---------- 组装 ----------
def morning(home, day):
    today = dt.date.fromisoformat(day)
    soon_end = (today + dt.timedelta(days=SOON_DAYS)).isoformat()
    items = dated_items(home)
    due_today = [it for it in items if it["date"] == day]
    soon = [it for it in items if day < it["date"] <= soon_end]
    overdue = [it for it in items if it["date"] < day]
    n_pai, oldest = you_pai(home)
    td = home.get("todos") or {}
    tc = td.get("counts") or {}
    t_today, t_over = int(tc.get("today") or 0), int(tc.get("overdue") or 0)
    lines = []
    if due_today:
        lines.append(f"今天到期 {len(due_today)}：{listing(due_today)}")
    if soon:
        md = lambda it: f"{int(it['date'][5:7])}/{int(it['date'][8:10])} {short(it['title'])}"  # noqa: E731
        lines.append(f"3 天内到期 {len(soon)}：{listing(soon, md)}")
    if n_pai:
        if oldest:
            age = (today - dt.date.fromisoformat(la_date(oldest.get("created")) or day)).days
            lines.append(f"待你拍 {n_pai} 折，最老 {fold_label(oldest)}（{age} 天）")
        else:
            lines.append(f"待你拍 {n_pai} 折")
    if overdue:
        lines.append(f"过期待处理 {len(overdue)}：{listing(overdue)}")
    if t_today or t_over:
        lines.append(f"今日待办：今天到期 {t_today}，已过期 {t_over}")
    return lines


def evening(home, day, done_todos=0):
    tomorrow = (dt.date.fromisoformat(day) + dt.timedelta(days=1)).isoformat()
    merged = [{**f, "private": fold_private(f)} for f in (home.get("folders") or {}).get("merged_recent") or []
              if la_date(f.get("merged_at")) == day]
    fo = home.get("folders") or {}
    if "recent_open" in fo:                       # 每组最新 20 折（oldest 只有最旧 20，组大时会漏掉今天新到的）
        recent = [{**f, "private": fold_private(f)} for f in fo.get("recent_open") or []]
    else:                                         # 旧版 home.json 兜底
        recent = folds(home)
    new = [f for f in recent if la_date(f.get("created")) == day]
    lines = []
    prog = []
    if merged:
        prog.append(f"画可 {len(merged)} 折（{listing(merged)}）")
    if done_todos:
        prog.append(f"完成待办 {done_todos} 件")
    if prog:
        lines.append("今天进展：" + "，".join(prog))
    if new:
        lines.append(f"今天新到 {len(new)} 折：{listing(new)}")
    first = next((it for it in dated_items(home) if it["date"] == tomorrow), None)
    _, oldest = you_pai(home)
    if first:
        lines.append("明天第一件事：" + ("1 件私密事项到期" if first["private"] else f"{short(first['title'])} 到期"))
    elif oldest:
        lines.append(f"明天第一件事：拍 {fold_label(oldest)}")
    return lines


def build(kind, home, day, done_todos=0, stale=None):
    """→ {key, title, body, url, empty, stale, lines}。没内容只发一行 EMPTY；stale（stale_reason 的结果）非空时
    只发一行「数据过期」，不下「空日」结论。"""
    home = home or {}
    if stale:
        lines, body = [], STALE[kind].format(why=stale)
    else:
        lines = morning(home, day) if kind == "morning" else evening(home, day, done_todos)
        body = "\n".join(lines) if lines else EMPTY[kind]
    return {"kind": kind, "day": day, "key": key(kind, day), "title": header(kind, day),
            "body": body, "url": URLS[kind], "empty": not lines and not stale, "stale": stale, "lines": lines}
