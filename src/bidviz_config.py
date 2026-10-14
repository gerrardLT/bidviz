"""bidviz_config —— 项目配置加载。

设计原则：
  * 代码里不出现客户名、电压等级、模型名、密钥。
  * 每个项目一个 bidviz.toml，放在项目根目录。
  * API 密钥优先读环境变量，其次读 ~/.bidviz/providers.toml，最后回退 PiDeck。

用法：
    from bidviz_config import load, get
    load(Path("projects/anke-35kv"))     # 显式加载某个项目
    get().header_text                    # 读配置
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------- 默认值

DEFAULTS: dict = {
    "project": {
        "name": "未命名项目",
        "doc_type": "施工组织设计",
        "voltage": "",
        "standard": "",
        "header": "",          # 留空则由 name + doc_type 自动拼
        "client": "",
    },
    "paths": {
        "inputs": "inputs",
        "work": "work",
        "output": "output",
        "images": "work/visuals",
    },
    "page": {
        "size": "A3",
        "orientation": "landscape",
        "margin_mm": 12.0,
    },
    "image": {
        "provider": "gptimage2",
        "model": "gpt-image-2",
        "size": "2048x1448",
        "dir": "work/visuals",
        "fallback_dirs": [],
        "concurrency": 1,
        "retries": 3,
        "pause": 1.5,
    },
    "style": {
        "palette": "power_grid_cyan_blue",
        "header_bg": "#13334c",
        "dark": "#0c2333",
        "accent": "#00897b",
        "cyan": "#26a69a",
        "border": "#c1d0d9",
        "text": "#1a2c38",
        "muted": "#5a6e7c",
        "warn": "#e65100",
        "danger_bg": "#8a2a2a",
    },
    "ingest": {
        "chapter_pattern": r"^第[一二三四五六七八九十]+章",
        "section_pattern": r"^第[一二三四五六七八九十]+节",
        "min_chars": 220,
    },
}


# ---------------------------------------------------------------- 工具

def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _load_env_file(path: Path) -> None:
    """读取简单 KEY=VALUE；不覆盖进程中已设置的环境变量。"""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


# ---------------------------------------------------------------- Config

@dataclass
class Config:
    root: Path
    raw: dict = field(default_factory=dict)

    # -- project --
    @property
    def name(self) -> str:
        return self.raw["project"]["name"]

    @property
    def doc_type(self) -> str:
        return self.raw["project"]["doc_type"]

    @property
    def voltage(self) -> str:
        return self.raw["project"]["voltage"]

    @property
    def header_text(self) -> str:
        h = self.raw["project"].get("header")
        if h:
            return h
        bits = [self.name]
        if self.doc_type:
            bits.append(self.doc_type)
        return " · ".join(bits)

    @property
    def prompt_subject(self) -> str:
        """提示词里对这份文档的完整称呼。"""
        voltage = f" {self.voltage} 电力接入工程" if self.voltage else ""
        return f"{self.name}{voltage}{self.doc_type}"

    @property
    def standard_text(self) -> str:
        return self.raw["project"].get("standard") or \
            f"执行 {self.voltage} 施工质量验收规范通用评定标准"

    # -- paths --
    def path(self, key: str) -> Path:
        return (self.root / self.raw["paths"][key]).resolve()

    @property
    def inputs(self) -> Path:
        return self.path("inputs")

    @property
    def work(self) -> Path:
        return self.path("work")

    @property
    def output(self) -> Path:
        return self.path("output")

    @property
    def image_dir(self) -> Path:
        return (self.root / self.raw["image"]["dir"]).resolve()

    @property
    def image_dirs(self) -> list[Path]:
        dirs = [self.image_dir]
        for value in self.raw["image"].get("fallback_dirs", []):
            path = (self.root / value).resolve()
            if path not in dirs:
                dirs.append(path)
        return dirs

    # -- image --
    @property
    def provider(self) -> str:
        return _env("BIDVIZ_PROVIDER") or self.raw["image"]["provider"]

    @property
    def model(self) -> str:
        return _env("BIDVIZ_MODEL") or self.raw["image"]["model"]

    @property
    def image_size(self) -> str:
        return _env("BIDVIZ_SIZE") or self.raw["image"]["size"]

    @property
    def retries(self) -> int:
        return int(_env("BIDVIZ_RETRIES") or self.raw["image"]["retries"])

    @property
    def pause(self) -> float:
        return float(_env("BIDVIZ_PAUSE") or self.raw["image"]["pause"])

    # -- style --
    @property
    def style(self) -> dict:
        return self.raw["style"]

    # -- ingest --
    @property
    def ingest(self) -> dict:
        return self.raw["ingest"]


# ---------------------------------------------------------------- 加载

_current: Config | None = None


def load(root: str | Path, toml_name: str = "bidviz.toml") -> Config:
    """加载项目配置。root 下没有 bidviz.toml 也能跑，用默认值。"""
    global _current
    root = Path(root).resolve()
    _load_env_file(Path(__file__).resolve().parent.parent / ".env")
    _load_env_file(root / ".env")
    f = root / toml_name
    over = {}
    if f.exists():
        with f.open("rb") as fh:
            over = tomllib.load(fh)
    _current = Config(root=root, raw=_deep_merge(DEFAULTS, over))
    return _current


def get() -> Config:
    """取当前配置；未加载时用当前工作目录兜底。"""
    global _current
    if _current is None:
        _current = load(Path.cwd())
    return _current


def reset() -> None:
    global _current
    _current = None


# ---------------------------------------------------------------- 供应商凭据

def provider_credentials(name: str) -> dict:
    """按优先级取供应商凭据：环境变量 → ~/.bidviz/providers.toml → PiDeck imagegen.json。

    环境变量（推荐，给别人用时用这个）：
        BIDVIZ_BASE_URL     例如 https://api.yoshub.com
        BIDVIZ_API_KEY      sk-xxxxx
    """
    base = _env("BIDVIZ_BASE_URL")
    key = _env("BIDVIZ_API_KEY")
    if base and key:
        return {"name": name, "baseUrl": base, "apiKey": key, "referenceMode": "edits"}

    # 独立配置文件
    cfgf = Path.home() / ".bidviz" / "providers.toml"
    if cfgf.exists():
        try:
            with cfgf.open("rb") as fh:
                data = tomllib.load(fh)
            p = (data.get("providers") or {}).get(name)
            if p and p.get("baseUrl") and p.get("apiKey"):
                return {"name": name, **p}
        except Exception:
            pass

    # PiDeck 回退（本机开发用）
    pideck = Path(_env("APPDATA", str(Path.home() / "AppData" / "Roaming"))) / "PiDeck" / "imagegen.json"
    if pideck.exists():
        import json
        data = json.loads(pideck.read_text(encoding="utf-8"))
        provs = data.get("providers", [])
        active = data.get("activeProviderId")
        hit = next((p for p in provs if p.get("name") == name), None) \
            or next((p for p in provs if p.get("id") == active), None)
        if hit:
            return {
                "name": hit.get("name", name),
                "baseUrl": hit["baseUrl"],
                "apiKey": hit["apiKey"],
                "referenceMode": hit.get("referenceMode", "edits"),
            }

    raise RuntimeError(
        f"找不到供应商 {name} 的凭据。请设置环境变量 BIDVIZ_BASE_URL / BIDVIZ_API_KEY，"
        f"或创建 ~/.bidviz/providers.toml。"
    )
