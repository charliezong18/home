"""配置：内置缺省 < ~/.config/dibao/config.toml < 环境变量 DIBAO_*。"""
import json
import os
from dataclasses import dataclass, field

from apply import DEFAULT_RECOLLECT, DEFAULT_WORKER

try:
    import tomllib
except ImportError:  # Python < 3.11
    tomllib = None

HOME = os.path.expanduser("~")
CONFIG_PATH = os.environ.get("DIBAO_CONFIG", os.path.join(HOME, ".config/dibao/config.toml"))


@dataclass
class Config:
    port: int = 8766
    sync_down_secs: int = 60
    push_debounce_secs: int = 60
    collect_secs: int = 1800
    worker_timeout: int = 30
    log_file: str = ""                       # 空 = 打 stderr；install 出来的 config 指向 ~/Library/Logs/dibao/api.log
    worker_cmd: list = field(default_factory=lambda: list(DEFAULT_WORKER))
    recollect_cmd: list = field(default_factory=lambda: list(DEFAULT_RECOLLECT))


def load(path=None):
    cfg = Config()
    path = path or CONFIG_PATH
    if tomllib and os.path.exists(path):
        with open(path, "rb") as f:
            for k, v in tomllib.load(f).get("dibao", {}).items():
                if hasattr(cfg, k):
                    setattr(cfg, k, v)
    env = os.environ
    for key, attr in (("DIBAO_PORT", "port"), ("DIBAO_SYNC_SECS", "sync_down_secs"),
                      ("DIBAO_PUSH_DEBOUNCE", "push_debounce_secs"), ("DIBAO_COLLECT_SECS", "collect_secs"),
("DIBAO_WORKER_TIMEOUT", "worker_timeout")):
        if env.get(key):
            setattr(cfg, attr, int(env[key]))
    if env.get("DIBAO_LOG_FILE"):
        cfg.log_file = env["DIBAO_LOG_FILE"]
    for key, attr in (("DIBAO_WORKER", "worker_cmd"), ("DIBAO_RECOLLECT", "recollect_cmd")):
        if env.get(key):
            setattr(cfg, attr, json.loads(env[key]))   # JSON 数组，测试用
    cfg.log_file = os.path.expanduser(cfg.log_file)
    return cfg
