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
import warnings
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
        # 每张规划图可单独换供应商/模型/尺寸（第 1 张与第 2 张不必用同一个生图 API）。
        # 键可以是图序（"1"/"2"）或图型 key（layout_plan 等），图序优先。
        # [image.figure_overrides]
        # 1 = { provider = "grokimg", model = "gpt-image-2-2K/4K", size = "2048x1448" }
        # 2 = { provider = "taomu",   model = "gpt-image-2.5",      size = "1536x1024" }
        "figure_overrides": {},
    },
    "layout": {
        # legacy | v13 | fullimage | html
        "engine": "legacy",
    },
    "html": {
        # auto = 自动探测 Chrome/Edge；也可写可执行文件的绝对路径。
        "browser": "auto",
        "dpi": 200,          # 可选 PNG 页图的渲染分辨率
        "emit_png": False,   # build 时是否同时导出页图
        "table_cell_max": 400,  # HTML 表格单元格最大字数（v13/legacy 仍用 70）
    },
    "compact": {
        # 页数目标是项目级要求：不同项目按招标文件设置；0 = 不限制。
        "min_pages": 0,
        "max_pages": 0,
        # 目标未限制时，按每页字数/每页小节数排版。
        "max_chars_per_page": 2000,
        "max_sections_per_page": 2,
    },
    "plan": {
        # 每节最多画几张图。张数由内容体量与维度决定，不是固定槽位。
        "max_figures": 2,
        # 置 false 则每节只画一张主视觉。
        "second_figure": True,
        # 触发「再补一张图」的阈值，可被项目 toml 覆盖。
        "thresholds": {
            "chars": 2600,    # 本节正文字数
            "items": 4,       # 三级分项（一、二、三…）数
            "param": 8,       # 原文落地的参数句数
            "mat": 10,        # 原文落地的材料设备条数
            "layout": 6,      # 平面布置/运输组织词频
            "time": 6,        # 阶段工期词频
            "org": 4,         # 岗位职责词频
        },
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


# ---------------------------------------------------------------- 引擎

SUPPORTED_ENGINES = ("legacy", "v13", "fullimage", "html")
_ENGINE_ALIASES = {"hybrid": "html"}
_warned_engines: set[str] = set()


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

    @property
    def figure_overrides(self) -> dict:
        """按图序或图型覆盖生图供应商/模型/尺寸。"""
        return self.raw["image"].get("figure_overrides", {}) or {}

    # -- plan --
    @property
    def plan(self) -> dict:
        return self.raw.get("plan", {}) or {}

    @property
    def max_figures(self) -> int:
        return max(1, int(self.plan.get("max_figures", 2)))

    @property
    def second_figure(self) -> bool:
        return bool(self.plan.get("second_figure", True))

    # -- style --
    @property
    def style(self) -> dict:
        return self.raw["style"]

    # -- ingest --
    @property
    def ingest(self) -> dict:
        return self.raw["ingest"]

    # -- layout（V1.3 阶段 3.6：新旧体系切换；legacy 保留至阶段 7 验收后移除） --
    @property
    def layout_engine(self) -> str:
        raw = str(self.raw.get("layout", {}).get("engine", "legacy")).strip().lower()
        engine = _ENGINE_ALIASES.get(raw, raw)
        if engine not in SUPPORTED_ENGINES:
            if raw not in _warned_engines:
                _warned_engines.add(raw)
                warnings.warn(f"未知 layout.engine={raw!r}，按 legacy 处理",
                              RuntimeWarning, stacklevel=2)
            return "legacy"
        return engine

    @property
    def supported_engines(self) -> tuple[str, ...]:
        return SUPPORTED_ENGINES

    # -- html（AI 局部插图 + HTML/CSS 排版） --
    @property
    def html_browser(self) -> str:
        return _env("BIDVIZ_BROWSER") or str(
            self.raw.get("html", {}).get("browser", "auto") or "auto").strip()

    @property
    def html_dpi(self) -> int:
        return int(_env("BIDVIZ_HTML_DPI") or self.raw.get("html", {}).get("dpi", 200))

    @property
    def html_table_cell_max(self) -> int:
        return max(40, int(self.raw.get("html", {}).get("table_cell_max", 400)))

    # -- compact（AI 生图压缩版） --
    @property
    def compact(self) -> dict:
        return self.raw.get("compact", {}) or {}

    @property
    def compact_min_pages(self) -> int:
        return max(0, int(self.compact.get("min_pages", 0)))

    @property
    def compact_max_pages(self) -> int:
        return max(0, int(self.compact.get("max_pages", 0)))

    @property
    def compact_max_chars(self) -> int:
        return max(400, int(self.compact.get("max_chars_per_page", 2000)))

    @property
    def compact_max_sections(self) -> int:
        return max(1, int(self.compact.get("max_sections_per_page", 2)))

    @property
    def html_emit_png(self) -> bool:
        value = self.raw.get("html", {}).get("emit_png", False)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)


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

def provider_credentials(name: str, env_first: bool = True) -> dict:
    """按优先级取供应商凭据：环境变量 → ~/.bidviz/providers.toml → PiDeck imagegen.json。

    环境变量（推荐，给别人用时用这个）：
        BIDVIZ_BASE_URL     例如 https://api.yoshub.com
        BIDVIZ_API_KEY      sk-xxxxx

    env_first=False 用于「显式指定了某个供应商」的场景（如按图位切换生图 API）：
    此时先查 providers.toml / PiDeck 里那个供应商，找不到才回退全局环境变量。
    """
    base = _env("BIDVIZ_BASE_URL")
    key = _env("BIDVIZ_API_KEY")
    if env_first and base and key:
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

    if base and key:
        return {"name": name, "baseUrl": base, "apiKey": key, "referenceMode": "edits"}

    raise RuntimeError(
        f"找不到供应商 {name} 的凭据。请设置环境变量 BIDVIZ_BASE_URL / BIDVIZ_API_KEY，"
        f"或创建 ~/.bidviz/providers.toml。"
    )
