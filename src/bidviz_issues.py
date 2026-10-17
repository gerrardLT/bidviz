#!/usr/bin/env python3
"""bidviz_issues —— issues.json schema + 自动初筛规则 + build 门禁。

对应 V1.3 阶段 1.3：
- 规则初筛：数值无单位（PARAM_RE 反向）/ 同主题跨段数值不一致 / "约/左右" + 关键承诺 /
  总工期不可推日期。
- Gate：open 状态存在 → S5 阻断 final 或加 DRAFT 水印；waive 必须留痕（决断人+时间）。
- 与旧 bidviz.detect_issues（Claim 层）解耦，直接消费 blocks.json。

用法::

    python -m bidviz_issues <project_root>            # 扫描并落盘 work/issues.json
    python -m bidviz_issues <project_root> --report   # 只打印摘要，不写盘
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = "1.0"

# 状态：open（待决断）/ confirmed（已确认属实）/ waived（已豁免，必须留痕）
STATUS_OPEN = "open"
STATUS_CONFIRMED = "confirmed"
STATUS_WAIVED = "waived"
VALID_STATUS = {STATUS_OPEN, STATUS_CONFIRMED, STATUS_WAIVED}

# 类型（与规划一致，可扩展）
TYPE_MISSING_UNIT = "missing-unit"
TYPE_CONFLICT = "conflict"
TYPE_AMBIGUOUS = "ambiguous"
TYPE_UNFALSIFIABLE_DATE = "unfalsifiable-date"
TYPE_NO_BASIS = "no-basis"


# ---------------------------------------------------------------- dataclass


@dataclass
class Location:
    """issue 命中的原文位置；至少填 section_path 或 block_id 之一。"""

    section_path: str = ""
    block_id: str = ""
    source_page: int | None = None
    chapter: str = ""
    section_id: str = ""


@dataclass
class Issue:
    """一条 issue；id 由 rule+excerpt 哈希生成，可稳定复现。"""

    id: str
    rule: str                       # 触发规则名（missing-unit / conflict / ambiguous / ...）
    type: str                       # 与 rule 同义，用于门禁分类
    excerpt: str                    # 原文摘录（≤120 字，含命中片段）
    suggestion: str                 # 处理建议
    locations: list[Location] = field(default_factory=list)
    status: str = STATUS_OPEN
    decided_by: str = ""
    decided_at: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)   # 规则自定义证据

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["locations"] = [asdict(x) if isinstance(x, Location) else x for x in self.locations]
        return d


@dataclass
class IssuesDoc:
    """work/issues.json 顶层结构。"""

    version: str = SCHEMA_VERSION
    generated_at: str = ""
    project: str = ""
    issues: list[Issue] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "generated_at": self.generated_at,
            "project": self.project,
            "summary": summarize(self.issues),
            "issues": [x.to_dict() for x in self.issues],
        }


# ---------------------------------------------------------------- id / helpers


def _short_id(rule: str, key: str) -> str:
    """由 rule + key（excerpt/主题）生成短哈希 id，可稳定复现。"""
    h = hashlib.sha1(f"{rule}::{key}".encode("utf-8")).hexdigest()[:8]
    return f"ISS-{rule[:3].upper()}-{h}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _excerpt(text: str, hit_start: int, hit_end: int, span: int = 40) -> str:
    """围绕命中区间截一段可读摘录（≤ span*2 + hit 长度）。"""
    lo = max(0, hit_start - span)
    hi = min(len(text), hit_end + span)
    prefix = "…" if lo > 0 else ""
    suffix = "…" if hi < len(text) else ""
    return prefix + text[lo:hi] + suffix


def _blocks_from_project(root: Path) -> list[dict[str, Any]]:
    """读 work/blocks.json → list[dict]；不存在返回空。"""
    p = root / "work" / "blocks.json"
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return raw if isinstance(raw, list) else []


# ---------------------------------------------------------------- 规则 1：数值无单位

# 命中模式：数字 + 尺寸语素（厚/长/宽/高/深/距/径/直/方/度/跨/坡）但缺具体单位
# 例："60 厚" / "300 长" / "2.5 深" —— 缺 mm/cm/m 等单位。
# 右 lookahead 只拒绝数字与字母（允许中文结构助词"的/内/以上"等）。
_BARE_DIM_NUM = re.compile(
    r"(?P<num>\d+(?:\.\d+)?)\s*(?P<dim>厚|长|宽|高|深|距|径|直|方|度|跨|坡)(?![0-9a-zA-Z])"
)
# 明确带单位的正向模式（用于反查是否漏单位）
_UNIT_TOKENS = ("mm", "cm", "m", "km", "m²", "m³", "㎡", "㎥", "kg", "t", "吨", "度", "°", "%", "‰")


def _has_unit_near(text: str, pos: int, window: int = 6) -> bool:
    """命中点右侧 window 字符内是否出现明确单位 token（避免误伤"60 厚度控制"）。"""
    tail = text[pos:pos + window]
    return any(tok in tail for tok in _UNIT_TOKENS)


def rule_missing_unit(blocks: Iterable[dict[str, Any]]) -> list[Issue]:
    """扫描 blocks，命中"数字+尺寸语素但缺单位"→ 一条 issue 每处。"""
    issues: list[Issue] = []
    for b in blocks:
        text = str(b.get("text", ""))
        if not text:
            continue
        for m in _BARE_DIM_NUM.finditer(text):
            if _has_unit_near(text, m.end()):
                continue
            num = m.group("num")
            dim = m.group("dim")
            excerpt = _excerpt(text, m.start(), m.end())
            key = f"{num}{dim}|{b.get('block_id', '')}"
            issues.append(Issue(
                id=_short_id("missing-unit", key),
                rule="missing-unit",
                type=TYPE_MISSING_UNIT,
                excerpt=excerpt,
                suggestion=f"数值 {num}{dim} 缺明确单位（应为 {num} mm/{num} cm/{num} m 之一），请核对原文补充。",
                locations=[Location(
                    section_path=b.get("section_path", ""),
                    block_id=b.get("block_id", ""),
                    source_page=b.get("source_page"),
                )],
                evidence={"num": num, "dim": dim},
            ))
    return issues


# ---------------------------------------------------------------- 规则 2：同主题跨段数值不一致

# 主题锚点：包含"复杂故障/复杂问题/复杂性故障/一般故障/一般性问题"等承诺分级 + 时限
_TOPIC_TIME_RE = re.compile(
    r"(?P<topic>复杂(?:性)?(?:故障|问题)|一般(?:性)?(?:故障|问题)|突发故障|应急)"
    r"[^。，,；;]{0,20}?(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>小时|分钟|天|日历天)"
)


def _normalize_topic(topic: str) -> str:
    """将同义分级归一：复杂性故障/复杂性问题/复杂问题 → 复杂故障；一般性故障 → 一般故障。"""
    if topic.startswith("复杂"):
        return "复杂故障"
    if topic.startswith("一般"):
        return "一般故障"
    return topic


def rule_conflict_topic_time(blocks: Iterable[dict[str, Any]]) -> list[Issue]:
    """同 topic（归一化后：复杂故障 / 一般故障 / …）跨块时限数值不一致 → 汇总为一条 conflict。"""
    # normalized_topic → {(num, unit): [locations]}
    hits: dict[str, dict[tuple[str, str], list[Location]]] = defaultdict(lambda: defaultdict(list))
    excerpts: dict[str, dict[tuple[str, str], str]] = defaultdict(dict)
    raw_topic_of: dict[tuple[str, str, str], str] = {}   # (norm, num, unit) → raw topic
    for b in blocks:
        text = str(b.get("text", ""))
        if not text:
            continue
        for m in _TOPIC_TIME_RE.finditer(text):
            raw_topic = m.group("topic")
            topic = _normalize_topic(raw_topic)
            num = m.group("num")
            unit = m.group("unit")
            loc = Location(
                section_path=b.get("section_path", ""),
                block_id=b.get("block_id", ""),
                source_page=b.get("source_page"),
            )
            hits[topic][(num, unit)].append(loc)
            raw_topic_of[(topic, num, unit)] = raw_topic
            if (num, unit) not in excerpts[topic]:
                excerpts[topic][(num, unit)] = _excerpt(text, m.start(), m.end())

    issues: list[Issue] = []
    for topic, variants in hits.items():
        if len(variants) < 2:
            continue
        # 冲突：同 topic 出现 ≥2 种 (num, unit) 组合
        all_locs = [loc for lst in variants.values() for loc in lst]
        detail = {f"{n}{u}": len(lst) for (n, u), lst in variants.items()}
        raw_variants = sorted({raw_topic_of[(topic, n, u)] for (n, u) in variants})
        excerpt_summary = " ｜ ".join(
            f"{raw_topic_of[(topic, n, u)]} {n}{u}：{excerpts[topic][(n, u)]}"
            for (n, u) in variants
        )
        issues.append(Issue(
            id=_short_id("conflict", f"topic-time::{topic}"),
            rule="conflict",
            type=TYPE_CONFLICT,
            excerpt=f"【{topic}】处置时限在原文出现多种口径：{excerpt_summary}",
            suggestion=(
                f"主题「{topic}」处置时限存在 {len(variants)} 种口径（{detail}）；"
                f"原文用词：{raw_variants}。请业务决断统一。"
            ),
            locations=all_locs,
            evidence={"topic": topic, "variants": detail, "raw_topics": raw_variants},
        ))
    return issues


# ---------------------------------------------------------------- 规则 3："约/左右" + 关键承诺

_APPROX_RE = re.compile(r"(?P<approx>约|大约|大致|左右|大概|接近)")
_PROMISE_RE = re.compile(
    r"(?P<promise>总工期|日历天|验收送电|完成处置|完成修复|抵达现场|小时内完成|天内完成|天内追回)"
)


def rule_ambiguous_promise(blocks: Iterable[dict[str, Any]]) -> list[Issue]:
    """同句内出现"约/左右" + 关键承诺词 → 一条 ambiguous。"""
    issues: list[Issue] = []
    for b in blocks:
        text = str(b.get("text", ""))
        if not text:
            continue
        # 按中文句号/分号切句
        for sent in re.split(r"[。；;！!？?\n]", text):
            if not sent.strip():
                continue
            am = _APPROX_RE.search(sent)
            pm = _PROMISE_RE.search(sent)
            if not (am and pm):
                continue
            # 只关心二者共现且承诺词在 approx 附近（±30 字）
            if abs(am.start() - pm.start()) > 30:
                continue
            key = f"{am.group('approx')}|{pm.group('promise')}|{b.get('block_id', '')}"
            issues.append(Issue(
                id=_short_id("ambiguous", key),
                rule="ambiguous",
                type=TYPE_AMBIGUOUS,
                excerpt=sent.strip()[:120],
                suggestion=f"关键承诺「{pm.group('promise')}」使用模糊限定「{am.group('approx')}」，请核实是否可量化。",
                locations=[Location(
                    section_path=b.get("section_path", ""),
                    block_id=b.get("block_id", ""),
                    source_page=b.get("source_page"),
                )],
                evidence={"approx": am.group("approx"), "promise": pm.group("promise")},
            ))
    return issues


# ---------------------------------------------------------------- 规则 4：总工期不可推日期

_DURATION_RE = re.compile(r"(?P<num>\d+)\s*(?P<unit>日历天|工期日|工作日)(?![气内])")
# 具体日历日期锚点：YYYY 年 MM 月 [DD 日] 或 MM 月 DD 日；排除社保/规范引用日期，
# 只有与 duration 同段共现时才算有效锚点。
_DATE_ANCHOR_RE = re.compile(
    r"(?P<date>\d{4}\s*年\s*\d{1,2}\s*月(?:\s*\d{1,2}\s*日)?|\d{1,2}\s*月\s*\d{1,2}\s*日)"
)


def rule_unfalsifiable_date(blocks: Iterable[dict[str, Any]]) -> list[Issue]:
    """全文含"X 日历天/工作日"总工期口径，但没有任何一段同时给出具体日历日期锚点 →
    无法推出交付日期，命中一条 no-basis（归入 unfalsifiable-date 类型）。

    判定：对每个含 duration 的 block，检查同 block 内是否出现 _DATE_ANCHOR_RE；
    只要有一个 block 同时含二者，即视为"有依据"，不报。
    """
    durations: list[tuple[str, Location, str]] = []
    has_anchor_with_duration = False
    for b in blocks:
        text = str(b.get("text", ""))
        if not text:
            continue
        dur_hits = list(_DURATION_RE.finditer(text))
        if not dur_hits:
            continue
        anchor_hits = list(_DATE_ANCHOR_RE.finditer(text))
        if anchor_hits:
            has_anchor_with_duration = True
        for m in dur_hits:
            durations.append((
                f"{m.group('num')}{m.group('unit')}",
                Location(
                    section_path=b.get("section_path", ""),
                    block_id=b.get("block_id", ""),
                    source_page=b.get("source_page"),
                ),
                _excerpt(text, m.start(), m.end()),
            ))

    if not durations or has_anchor_with_duration:
        return []

    uniq_labels = sorted({d[0] for d in durations})
    return [Issue(
        id=_short_id("unfalsifiable-date", "|".join(uniq_labels)),
        rule="unfalsifiable-date",
        type=TYPE_UNFALSIFIABLE_DATE,
        excerpt=(
            f"原文出现总工期口径 {uniq_labels}（共 {len(durations)} 处），"
            f"但没有任何一段同时给出具体的日历日期锚点（YYYY 年 MM 月 [DD 日] 或 MM 月 DD 日）。"
        ),
        suggestion=(
            "总工期缺具体起始日历日期锚点，无法推出交付日期与横道图；"
            "请补入合同签订日/开工日的具体日历日期，或明确以招标文件给定日期为准。"
        ),
        locations=[d[1] for d in durations[:8]],
        evidence={"labels": uniq_labels, "hit_count": len(durations)},
    )]


# ---------------------------------------------------------------- 汇总与门禁

RULES = (
    rule_missing_unit,
    rule_conflict_topic_time,
    rule_ambiguous_promise,
    rule_unfalsifiable_date,
)


def scan(root: Path) -> IssuesDoc:
    """扫描项目所有规则，返回 IssuesDoc（未落盘）。"""
    blocks = _blocks_from_project(root)
    all_issues: list[Issue] = []
    for rule in RULES:
        all_issues.extend(rule(blocks))
    # 稳定排序：type → id
    all_issues.sort(key=lambda x: (x.type, x.id))
    return IssuesDoc(
        version=SCHEMA_VERSION,
        generated_at=_now_iso(),
        project=root.name,
        issues=all_issues,
    )


def summarize(issues: Iterable[Issue]) -> dict[str, Any]:
    """按 type/status 汇总计数，供 CLI/门禁快速判断。"""
    by_type: dict[str, int] = defaultdict(int)
    by_status: dict[str, int] = defaultdict(int)
    for x in issues:
        by_type[x.type] += 1
        by_status[x.status] += 1
    return {
        "total": sum(by_type.values()),
        "by_type": dict(by_type),
        "by_status": dict(by_status),
        "open_blocking": by_status.get(STATUS_OPEN, 0),
    }


def gate_check(doc: dict[str, Any]) -> tuple[bool, str]:
    """门禁：open>0 且非 waived → 阻断 final（或加 DRAFT 水印）。

    返回 (passed, reason)；passed=True 表示可以进入 final。
    """
    issues = doc.get("issues", []) if isinstance(doc, dict) else []
    open_items = [x for x in issues if x.get("status") == STATUS_OPEN]
    if not open_items:
        return True, "no open issues"
    types = sorted({x.get("type", "?") for x in open_items})
    return False, f"{len(open_items)} open issue(s): types={types}"


# ---------------------------------------------------------------- 持久化


def issues_path(root: Path) -> Path:
    return root / "work" / "issues.json"


def save(root: Path, doc: IssuesDoc) -> Path:
    p = issues_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def load(root: Path) -> dict[str, Any]:
    """加载已存在的 issues.json；缺失返回空结构。"""
    p = issues_path(root)
    if not p.exists():
        return {"version": SCHEMA_VERSION, "issues": [], "summary": summarize([])}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": SCHEMA_VERSION, "issues": [], "summary": summarize([])}
    return raw if isinstance(raw, dict) else {"version": SCHEMA_VERSION, "issues": []}


def merge_status(existing: dict[str, Any], fresh: IssuesDoc) -> IssuesDoc:
    """把已存在的 issues.json 里的 status/decided_by/decided_at 合并到新扫描结果。

    规则：按 id 匹配；如已存在且非 open，保留人工决断状态；否则用 fresh 默认（open）。
    """
    old_by_id = {x.get("id"): x for x in existing.get("issues", []) if isinstance(x, dict)}
    for iss in fresh.issues:
        old = old_by_id.get(iss.id)
        if not old:
            continue
        status = old.get("status", STATUS_OPEN)
        if status in VALID_STATUS and status != STATUS_OPEN:
            iss.status = status
            iss.decided_by = old.get("decided_by", "") or iss.decided_by
            iss.decided_at = old.get("decided_at", "") or iss.decided_at
    return fresh


# ---------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="bidviz_issues", description="扫描 blocks.json 生成 issues.json")
    ap.add_argument("project", help="项目根目录（含 work/blocks.json）")
    ap.add_argument("--report", action="store_true", help="只打印摘要，不写盘")
    ap.add_argument("--json", action="store_true", help="以 JSON 打印完整结果（供管道消费）")
    args = ap.parse_args(argv)

    root = Path(args.project).resolve()
    if not (root / "work" / "blocks.json").exists():
        print(f"[ERR] 缺少 work/blocks.json：{root}", file=sys.stderr)
        return 2

    doc = scan(root)
    existing = load(root)
    doc = merge_status(existing, doc)

    if args.json:
        print(json.dumps(doc.to_dict(), ensure_ascii=False, indent=2))
        return 0

    summary = summarize(doc.issues)
    print(f"[OK] 扫描 {root.name}：共 {summary['total']} 条 issue")
    print(f"     按类型：{summary['by_type']}")
    print(f"     按状态：{summary['by_status']}")
    passed, reason = gate_check(doc.to_dict())
    print(f"     门禁　：{'PASS' if passed else 'BLOCK'}（{reason}）")

    if args.report:
        return 0 if passed else 1

    out = save(root, doc)
    print(f"[OK] 已写入 {out}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
