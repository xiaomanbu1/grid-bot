"""配置加载."""
import yaml
from pathlib import Path


class Cfg(dict):
    """dict 套壳, 支持 cfg.grid.step_pct 点号访问."""
    def __getattr__(self, k):
        v = self[k]
        return Cfg(v) if isinstance(v, dict) else v


def load_config(path: str = "config.yaml") -> Cfg:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"配置文件不存在: {p.resolve()}")
    with open(p, "r", encoding="utf-8") as f:
        return Cfg(yaml.safe_load(f))
