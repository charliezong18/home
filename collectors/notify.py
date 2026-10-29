"""ntfy 推送：status.py / dibao_sched / events 共用这一处。仅标准库。

topic 是访问凭据，不进仓库：从 ~/.config/dibao/config.toml 的 [ntfy] 段读（DIBAO_CONFIG 可改路径）。
  [ntfy]
  topic = "..."          # 必填
  legacy_topic = "..."   # 可选：换 topic 过渡期双发，确认手机已订阅新 topic 后删掉
  server = "https://ntfy.sh"   # 可选
AI_HOME_NTFY_DRYRUN=1 时只打印不发。没配 topic → 记日志、返回 False。
"""
import logging
import os
import urllib.error
import urllib.request

try:
    import tomllib
except ImportError:  # Python < 3.11
    tomllib = None

log = logging.getLogger("dibao.notify")
DEFAULT_SERVER = "https://ntfy.sh"
TIMEOUT = 15


def config_path():
    return os.environ.get("DIBAO_CONFIG") or os.path.expanduser("~/.config/dibao/config.toml")


def load_targets(path=None):
    """返回 [(标签, url)]：先 topic 后 legacy_topic；标签用于日志，不含 topic 本身。"""
    path = path or config_path()
    if not tomllib or not os.path.exists(path):
        return []
    try:
        with open(path, "rb") as f:
            sec = tomllib.load(f).get("ntfy", {})
    except (OSError, ValueError) as e:
        log.warning("读 ntfy 配置失败：%s", e)
        return []
    server = str(sec.get("server") or DEFAULT_SERVER).rstrip("/")
    out = []
    for label in ("topic", "legacy_topic"):
        t = str(sec.get(label) or "").strip()
        if t and all(t != u.rsplit("/", 1)[1] for _, u in out):
            out.append((label, f"{server}/{t}"))
    return out


def deliver(text, *, title, click=None):
    """逐个 topic POST，返回 [(标签, HTTP 状态码或 None)]。dry-run 返回 [("dry-run", None)]。"""
    if os.environ.get("AI_HOME_NTFY_DRYRUN") == "1":
        log.warning("[dry-run] 本应推送 ntfy：%s %s", text, click or "")
        return [("dry-run", None)]
    targets = load_targets()
    if not targets:
        log.warning("ntfy 未配置 topic（%s 的 [ntfy] 段），没发：%s", config_path(), text)
        return []
    headers = {"Title": title}
    if click:
        headers["Click"] = click
    out = []
    for label, url in targets:
        req = urllib.request.Request(url, data=text.encode("utf-8"), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                out.append((label, r.status))
        except urllib.error.HTTPError as e:
            out.append((label, e.code))
        except (OSError, ValueError) as e:
            log.warning("ntfy 推送失败（%s）：%s", label, e)
            out.append((label, None))
    return out


def send(text, *, title, click=None):
    """任一 topic 返回 200（或 dry-run）即算送达。"""
    res = deliver(text, title=title, click=click)
    ok = any(s == 200 for _, s in res) or res == [("dry-run", None)]
    if not ok:
        log.warning("ntfy 没送达：%s", res)
    return ok
