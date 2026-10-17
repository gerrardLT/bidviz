#!/usr/bin/env python3
"""bidviz_classify —— 分类编排子系统（V1.3 阶段 3.1）。

四遍编排（对任意文档同格式产出）：
  P0 章级统计（确定性）：节数 / 顶层 Word 表格对象 / 内嵌图片处数
  P1 章内矩阵         ：LLM 按章分批（prompts/classify.md），或 bootstrap 自现有人审 md
  P2 全局 pass        ：跨章节同对象 / 混合小节拆分建议 / 样板建议
  P3 规则 pass        ：原图表处理建议 + 冲突缺项（对接 work/issues.json）

双产物（S1 门控直读直改）：
  work/layout-classification.json   机读决策件
  work/classification.md            人审版（与 docs/layout-md/layout_classification.md 同格式）
  md 改后回写 json：sync_from_md()

门禁（3.1b）validate()：schema 五字段完整（主类/建议图/建议表/组合/制作方式）、
节覆盖与顺序对齐、原起始页单调性、每章抽 2 节人工核对清单。

用法：
    python -m bidviz_classify projects/anke-35kv                     # md bootstrap
    python -m bidviz_classify projects/anke-35kv --p1 llm           # LLM 通道（需凭据）
    python -m bidviz_classify projects/anke-35kv --validate          # 只跑门禁
    python -m bidviz_classify projects/anke-35kv --sync-from-md     # md → json 回写
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "src"))

CLASSES = ("A", "B", "C", "D", "E", "F", "G", "H")
JSON_NAME = "layout-classification.json"
MD_NAME = "classification.md"
GOLDEN_MD = BASE / "docs" / "layout-md" / "layout_classification.md"
PROMPT_TEMPLATE = BASE / "prompts" / "classify.md"

# ---------------------------------------------------------------- 小工具


def parse_class_field(s: str) -> tuple[str, list[str]]:
    """『D＋G、B』→ ("D", ["G","B"])；主类必须 ∈ A–H。"""
    s = (s or "").strip()
    main, _, rest = s.partition("＋")
    main = main.strip()
    if main not in CLASSES:
        raise ValueError(f"主类非法：{s!r}（须为 A–H）")
    aux = [a.strip() for a in re.split(r"[、,，/]", rest) if a.strip()]
    bad = [a for a in aux if a not in CLASSES]
    if bad:
        raise ValueError(f"辅类非法：{bad}（须为 A–H）")
    return main, aux


def _table_rows(lines: list[str], start: int) -> tuple[list[list[str]], int]:
    """从 lines[start] 起解析 markdown 表；返回 (行列表, 下一非表行下标)。"""
    rows: list[list[str]] = []
    i = start
    while i < len(lines):
        ln = lines[i].strip()
        if not ln.startswith("|"):
            break
        cells = [c.strip() for c in ln.strip("|").split("|")]
        if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells):   # 跳过分隔行
            rows.append(cells)
        i += 1
    return rows, i


# ---------------------------------------------------------------- md 解析（同格式）


def parse_classification_md(path: Path) -> dict[str, Any]:
    """解析人审版 classification.md（与 golden 同格式）→ 结构化 dict。

    提取：章级统计表（§三）、57 节矩阵（§四）、每节下一级主题与编排注意（拆分小节）、
    跨章节同对象表与混合小节拆分示例（§五）、点名重绘页码（§六）。
    """
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    out: dict[str, Any] = {
        "p0_chapter_stats": [],
        "sections": [],
        "cross_chapter": [],
        "mixed_split": {},
        "named_redraw_pages": [],
    }
    cur_chapter = ""
    detail: dict[str, dict[str, Any]] = {}      # (章, 节名) → {sub_topics, note}
    cur_detail_key: tuple[str, str] | None = None
    mode = ""            # "" / "stats" / "matrix" / "cross" / "mixed" / "detail"
    part = 0             # §三=3 §四=4 §五=5 §六=6（章矩阵只在 §四）
    section = ""         # §六子节标题

    i = 0
    while i < len(lines):
        ln = lines[i]
        s = ln.strip()

        if s.startswith("## 三、"):
            part, mode = 3, "stats"
        elif s.startswith("## 四、"):
            part, mode = 4, ""
        elif s.startswith("## 五、"):
            part, mode = 5, "cross"
        elif s.startswith("## 六、"):
            part, mode = 6, ""
        elif s.startswith("### 混合小节拆分示例：") and part == 5:
            mode = "mixed"
        elif s.startswith("### ") and part == 4:
            cur_chapter = s[4:].strip()
            mode = "matrix"
        elif s.startswith("#### "):
            mode = ""
        elif re.fullmatch(r"\*\*\d+\.\d+\s+.+?\*\*", s) and part == 4:
            title = s.strip("*").split(" ", 1)[1].strip()
            cur_detail_key = (cur_chapter, title)
            detail.setdefault(cur_detail_key, {"sub_topics": [], "note": "", "mixed": []})
            mode = "detail"
        elif s.startswith("### "):
            section = s[4:].strip()

        if mode == "stats" and s.startswith("|"):
            rows, i = _table_rows(lines, i)
            for r in rows:
                if len(r) >= 5 and r[0] != "原章名":
                    out["p0_chapter_stats"].append({
                        "chapter": r[0], "sections": int(r[1] or 0),
                        "tables": int(r[2] or 0), "images": int(r[3] or 0),
                        "positioning": r[4],
                    })
            continue

        if mode == "matrix" and s.startswith("|"):
            rows, i = _table_rows(lines, i)
            for r in rows:
                if len(r) >= 7 and not r[0].startswith("原节名"):
                    main, aux = parse_class_field(r[2])
                    out["sections"].append({
                        "chapter": cur_chapter, "title": r[0],
                        "source_page": int(re.sub(r"\D", "", r[1]) or 0),
                        "main_class": main, "aux_classes": aux,
                        "suggested_figures": r[3], "suggested_tables": r[4],
                        "combo": r[5], "production": r[6],
                        "content_blocks": [], "note": "",
                    })
            continue

        if mode == "cross" and s.startswith("|"):
            rows, i = _table_rows(lines, i)
            for r in rows:
                if len(r) >= 3 and not r[0].startswith("原文位置"):
                    out["cross_chapter"].append(
                        {"location": r[0], "purpose": r[1], "expression": r[2]})
            continue

        if mode == "mixed" and s.startswith("|"):
            rows, i = _table_rows(lines, i)
            for r in rows:
                if len(r) >= 3 and not r[0].startswith("原文块"):
                    out["mixed_split"].setdefault("blocks", []).append(
                        {"name": r[0], "class": r[1], "slots": r[2]})
            continue

        if mode == "detail" and cur_detail_key:
            m = re.match(r"原文下一级主题：(.+)", s)
            if m:
                topics = [t.strip() for t in m.group(1).split(" / ") if t.strip()]
                detail[cur_detail_key]["sub_topics"] = topics
            m = re.match(r"编排注意：(.+)", s)
            if m:
                detail[cur_detail_key]["note"] = m.group(1).strip()

        # §六 点名重绘页（「第84页/第108页…适合重绘」句）
        if section.startswith("原图表不是全部换成AI图片"):
            for pg in re.findall(r"第(\d{2,3})页", s):
                if int(pg) not in out["named_redraw_pages"]:
                    out["named_redraw_pages"].append(int(pg))
        i += 1

    # 矩阵行 ↔ 拆分详情合并；下一级主题 → content_blocks
    for sec in out["sections"]:
        d = detail.get((sec["chapter"], sec["title"]), {})
        sec["note"] = d.get("note", "")
        sec["content_blocks"] = [{"name": t, "class": "", "slots": "", "note": ""}
                                 for t in d.get("sub_topics", [])]
    # 混合小节拆分示例挂到对应节：示例表是权威细化，替换粗粒度下一级主题
    if out["mixed_split"].get("blocks"):
        ref = parse_mixed_ref(Path(path).read_text(encoding="utf-8"))
        for sec in out["sections"]:
            if sec["title"].startswith(ref.get("title", "\x00")) and sec["chapter"].startswith(ref.get("chapter", "")):
                sec["content_blocks"] = [
                    {"name": b["name"], "class": b["class"], "slots": b["slots"], "note": ""}
                    for b in out["mixed_split"]["blocks"]]
                sec["mixed_split_example"] = True
    return out


def parse_mixed_ref(text: str) -> dict[str, str]:
    """『### 混合小节拆分示例：第四章第三节』→ {"chapter": "第四章", "title": "第三节…"}。"""
    m = re.search(r"###\s*混合小节拆分示例：(第[一二三四五六七八九十]+章)(第[一二三四五六七八九十]+节)",
                  text)
    if not m:
        return {}
    return {"chapter": m.group(1), "title": m.group(2)}


# ---------------------------------------------------------------- P0 确定性统计


def deterministic_stats(root: Path) -> list[dict[str, Any]]:
    """P0：节数（blocks）/ 表格数（source-tables.json）/ 图片处数（page-media-map）按章。"""
    import bidviz_e1 as e1
    import build_e1 as be
    root = Path(root)
    blocks, pm = be.load_project(root)
    sections = e1.group_sections(blocks)

    tables_by_ch: dict[str, int] = {}
    st = root / "work" / "source-tables.json"
    if st.exists():
        data = json.loads(st.read_text(encoding="utf-8"))
        for ch, v in (data.get("summary", {}).get("by_chapter") or {}).items():
            tables_by_ch[ch] = v.get("tables", 0)

    pages_by_ch: dict[str, set[int]] = {}
    for b in blocks:
        ch = (getattr(b, "section_path", "") or "").split(" / ")[0] or "（总述）"
        pages_by_ch.setdefault(ch, set()).add(getattr(b, "source_page", 0))
    images_by_ch = {ch: sum(len(pm.get(p, [])) for p in ps if p)
                    for ch, ps in pages_by_ch.items()}

    stats = []
    for s in sections:
        ch = (s.chapter or "").split(" / ")[0] or "（总述）"
        if stats and stats[-1]["chapter"] == ch:
            stats[-1]["sections"] += 1
        else:
            stats.append({"chapter": ch, "sections": 1,
                          "tables": tables_by_ch.get(ch, 0),
                          "images": images_by_ch.get(ch, 0), "positioning": ""})
    return stats


# ---------------------------------------------------------------- P2 / P3


_STOP = ("工程施工", "方案", "措施", "质量", "安全管理", "管理", "应对", "重难点", "技术",
         "组织", "工作", "施工", "安装", "服务", "计划", "保证", "控制", "实施", "安排",
         "机制", "配置", "流程", "总体", "专项", "工程", "作业", "保障")


def _lcs(a: str, b: str) -> str:
    """最长公共子串（标题对齐用，O(len²) 足够小）。"""
    best = ""
    for i in range(len(a)):
        for j in range(len(b)):
            k = 0
            while i + k < len(a) and j + k < len(b) and a[i + k] == b[j + k]:
                k += 1
            if k > len(best):
                best = a[i:i + k]
    return best


_ORDINAL_RE = re.compile(r"^第[一二三四五六七八九十百\d]{1,4}(章|节|部分|篇)$")


def _tokens(s: str) -> list[str]:
    """标题/主题 → 干净 CJK 词块（≥3 字，去序号词与停用词）。"""
    return [t for t in re.findall(r"[\u4e00-\u9fff]{3,}", s)
            if t not in _STOP and not _ORDINAL_RE.fullmatch(t)]


def global_pass(cls: dict[str, Any]) -> dict[str, Any]:
    """P2：跨章节同对象 / 混合小节拆分建议 / 样板建议（确定性）。"""
    secs = cls["sections"]
    chapters: list[str] = []
    for s in secs:
        if s["chapter"] not in chapters:
            chapters.append(s["chapter"])

    # 跨章节同对象：不同章节的词块包含/长公共子串 ≥4 → 归并到更长词
    objects: dict[str, set[str]] = {}      # 对象 → 出现章节
    locations: dict[str, list[dict[str, str]]] = {}
    for i, a in enumerate(secs):
        toks_a = set()
        for t in [a["title"]] + [c["name"] for c in a["content_blocks"]]:
            toks_a.update(_tokens(t))
        for b in secs[i + 1:]:
            if a["chapter"] == b["chapter"]:
                continue
            toks_b = set()
            for t in [b["title"]] + [c["name"] for c in b["content_blocks"]]:
                toks_b.update(_tokens(t))
            for ta in toks_a:
                for tb in toks_b:
                    if ta in tb or tb in ta:
                        obj = ta if len(ta) >= len(tb) else tb
                    else:
                        obj = _lcs(ta, tb)
                        if len(obj) < 4 or obj in _STOP:
                            continue
                    objects.setdefault(obj, set()).update(
                        (a["chapter"][:6], b["chapter"][:6]))
                    locs = locations.setdefault(obj, [])
                    for s2 in (a, b):
                        if not any(x["title"] == s2["title"] for x in locs):
                            locs.append({"chapter": s2["chapter"][:6], "title": s2["title"]})
    cross = [{"object": k, "sections": locations[k]} for k, chs in objects.items() if len(chs) >= 2]
    # 父子串归并：「有限空间」⊂「有限空间作业」时保留更长、章节并入
    merged = []
    for c in sorted(cross, key=lambda x: -len(x["object"])):
        host = next((m for m in merged if c["object"] in m["object"]), None)
        if host:
            seen = {x["title"] for x in host["sections"]}
            host["sections"].extend(x for x in c["sections"] if x["title"] not in seen)
            continue
        merged.append(c)
    cross = merged
    cross.sort(key=lambda x: (-len(x["sections"]), x["object"]))

    mixed = [{"chapter": s["chapter"], "title": s["title"], "main_class": s["main_class"],
              "blocks": len(s["content_blocks"]),
              "reason": f"{len(s['content_blocks'])} 个下一级主题跨形态，建议逐块选版式"}
             for s in secs if len(s["content_blocks"]) >= 4]

    # 样板建议：每类取首个主类命中（覆盖 8 类版式验证）
    samples: dict[str, dict[str, str]] = {}
    for s in secs:
        c = s["main_class"]
        if c not in samples:
            samples[c] = {"chapter": s["chapter"][:10], "title": s["title"]}
    return {"cross_chapter_objects": cross[:12],
            "mixed_split_suggestions": mixed,
            "sample_suggestions": samples}


def rule_pass(root: Path, parsed_md: dict[str, Any] | None = None) -> dict[str, Any]:
    """P3：原图表处理建议（source-tables 初判 + 点名重绘页）+ 冲突缺项（issues）。"""
    root = Path(root)
    out: dict[str, Any] = {"original_tables": {}, "original_images": {},
                           "named_redraw_pages": [], "issues_ref": ""}
    st = root / "work" / "source-tables.json"
    if st.exists():
        data = json.loads(st.read_text(encoding="utf-8"))
        summ = data.get("summary", {})
        out["original_tables"] = {
            "total": summ.get("tables", 0), "keep": summ.get("keep", 0),
            "redraw": summ.get("redraw", 0),
            "redraw_items": [{"index": t["index"], "section_id": t.get("section_id"),
                              "reason": t.get("reason")}
                             for t in data.get("tables", [])
                             if t.get("initial_decision") == "redraw"],
            "note": "初判仅供建议；最终决策在阶段 5 source-assets.json（签认点⑤）",
        }
    pm = root / "work" / "page-media-map.json"
    if pm.exists():
        pmm = json.loads(pm.read_text(encoding="utf-8"))
        out["original_images"] = {"total_places": sum(len(v) for v in pmm.values()),
                                  "note": "60 处内嵌图片逐项 keep/redraw/replace 决策在阶段 5"}
    if parsed_md:
        out["named_redraw_pages"] = parsed_md.get("named_redraw_pages", [])
    iss = root / "work" / "issues.json"
    if iss.exists():
        try:
            doc = json.loads(iss.read_text(encoding="utf-8"))
            out["issues_ref"] = f"work/issues.json（open {doc.get('summary', {}).get('open', '?')} 条）"
        except (OSError, json.JSONDecodeError):
            out["issues_ref"] = "work/issues.json 存在但解析失败"
    else:
        out["issues_ref"] = "未生成（python -m bidviz_issues <root>）"
    return out


# ---------------------------------------------------------------- P1 LLM 通道


def _chat_completion(prompt: str, provider: str, model: str, timeout: int = 300) -> str:
    """OpenAI-compatible /chat/completions；走 bidviz._safe_open 统一安全出口。"""
    import urllib.request
    import bidviz
    from bidviz_config import provider_credentials
    cred = provider_credentials(provider)
    body = {"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0}
    req = urllib.request.Request(
        cred["baseUrl"].rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {cred['apiKey']}", "Content-Type": "application/json"},
        method="POST")
    with bidviz._safe_open(req, timeout) as resp:
        payload = json.load(resp)
    return payload["choices"][0]["message"]["content"]


def _parse_llm_json(content: str) -> dict[str, Any]:
    """剥掉代码围栏后解析 JSON；失败给可读错误。"""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.S)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM 返回不是合法 JSON：{exc}；开头={content[:120]!r}") from None


def llm_chapter_matrix(chapter: str, stats: dict[str, Any], section_inputs: list[dict[str, Any]],
                       provider: str, model: str) -> list[dict[str, Any]]:
    """P1 单章 LLM 分类：prompts/classify.md 模板 → /chat/completions → sections。"""
    tpl = PROMPT_TEMPLATE.read_text(encoding="utf-8")
    stats_line = f"节数 {stats['sections']} / 表格 {stats['tables']} / 图片处数 {stats['images']}"
    secs_line = "\n".join(
        f"- {s['title']}\n  下一级主题：{' / '.join(s['sub_topics']) or '（无）'}\n  摘录：{s['excerpt']}"
        for s in section_inputs)
    prompt = (tpl.replace("{{CHAPTER}}", chapter)
               .replace("{{STATS}}", stats_line)
               .replace("{{SECTIONS}}", secs_line))
    data = _parse_llm_json(_chat_completion(prompt, provider, model))
    rows = data.get("sections", [])
    for r in rows:
        parse_class_field(r.get("main_class", "") + ("＋" + "、".join(r.get("aux_classes", []))
                                                     if r.get("aux_classes") else ""))
    return rows


def _section_inputs(root: Path, chapter: str) -> list[dict[str, Any]]:
    """拼 LLM 输入的节清单（标题/三级主题/正文摘录 120 字）。"""
    import bidviz_e1 as e1
    import build_e1 as be
    blocks, _pm = be.load_project(Path(root))
    sections = e1.group_sections(blocks)
    out = []
    for s in sections:
        ch = (s.chapter or "").split(" / ")[0]
        if ch != chapter:
            continue
        subs = [b.text for b in s.blocks
                if getattr(b, "kind", "") == "heading" and re.match(r"^[一二三四五六七八九十]+、", b.text)]
        body = next((b.text for b in s.blocks if getattr(b, "kind", "") != "heading"), "")
        out.append({"title": s.title, "sub_topics": subs[:8], "excerpt": body[:120]})
    return out


# ---------------------------------------------------------------- 主编排


def _blocks_sections(root: Path) -> list[tuple[str, str, str, int]]:
    """[(S 编号, 章, 节名, blocks 首块 source_page)]，文档顺序。"""
    import bidviz_e1 as e1
    import build_e1 as be
    blocks, _pm = be.load_project(Path(root))
    out = []
    for s in e1.group_sections(blocks):
        ch = (s.chapter or "").split(" / ")[0]
        out.append((s.section_id, ch, s.title, s.blocks[0].source_page if s.blocks else 0))
    return out


def build(root: Path, p1: str = "md", source_md: Path | None = None,
          provider: str = "gptimage2", model: str = "glm-4.7") -> dict[str, Any]:
    """四遍编排主入口；返回 layout-classification dict（并落盘双产物）。"""
    root = Path(root)
    sec_index = _blocks_sections(root)
    p0 = deterministic_stats(root)

    sections: list[dict[str, Any]] = []
    if p1 == "md":
        parsed = parse_classification_md(source_md or GOLDEN_MD)
        md_secs = {(s["chapter"], s["title"]): s for s in parsed["sections"]}
        for sid, ch, title, page in sec_index:
            hit = md_secs.get((ch, title))
            if not hit:
                continue
            sec = dict(hit)
            sec["section_id"] = sid
            sec["blocks_source_page"] = page
            sections.append(sec)
        p2_source = parsed
    elif p1 == "llm":
        p2_source = None
        for st in p0:
            if st["chapter"].startswith("（"):
                continue
            rows = llm_chapter_matrix(st["chapter"], st,
                                      _section_inputs(root, st["chapter"]), provider, model)
            for sid, ch, title, page in sec_index:
                if ch != st["chapter"]:
                    continue
                hit = next((r for r in rows if r.get("title", "").strip() == title), None)
                if not hit:
                    continue
                main, aux = parse_class_field(
                    hit["main_class"] + ("＋" + "、".join(hit.get("aux_classes", []))
                                         if hit.get("aux_classes") else ""))
                sections.append({
                    "section_id": sid, "chapter": ch, "title": title,
                    "source_page": page, "blocks_source_page": page,
                    "main_class": main, "aux_classes": aux,
                    "suggested_figures": hit.get("suggested_figures", ""),
                    "suggested_tables": hit.get("suggested_tables", ""),
                    "combo": hit.get("combo", ""), "production": hit.get("production", ""),
                    "content_blocks": hit.get("content_blocks", []),
                    "note": hit.get("note", ""),
                })
    else:
        raise SystemExit(f"未知 P1 模式：{p1}（md | llm）")

    cls = {
        "schema": "bidviz-layout-classification/1.0",
        "version": f"v{datetime.now().strftime('%Y%m%d.%H%M')}",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "p1_mode": p1,
        "p0_chapter_stats": p0,
        "sections": sections,
        "p2_global": global_pass({"sections": sections}),
        "p3_rules": rule_pass(root, p2_source),
    }
    save(root, cls)
    return cls


def json_path(root: Path) -> Path:
    return Path(root) / "work" / JSON_NAME


def md_path(root: Path) -> Path:
    return Path(root) / "work" / MD_NAME


def save(root: Path, cls: dict[str, Any]) -> None:
    json_path(root).parent.mkdir(parents=True, exist_ok=True)
    json_path(root).write_text(json.dumps(cls, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path(root).write_text(render_md(cls), encoding="utf-8")


def load(root: Path) -> dict[str, Any] | None:
    p = json_path(root)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def sync_from_md(root: Path) -> dict[str, Any]:
    """S1 门控回写：人审改 work/classification.md → 回写 json（保留 p0/p2/p3 重算）。"""
    root = Path(root)
    parsed = parse_classification_md(md_path(root))
    old = load(root) or {"sections": []}
    md_secs = {(s["chapter"], s["title"]): s for s in parsed["sections"]}
    merged = []
    for sec in old["sections"]:
        hit = md_secs.get((sec["chapter"], sec["title"]))
        if hit:
            sec = {**sec, **{k: hit[k] for k in
                             ("main_class", "aux_classes", "suggested_figures", "suggested_tables",
                              "combo", "production", "content_blocks", "note", "source_page")}}
        sec["content_blocks"] = [
            {**b, "class": b.get("class", ""), "slots": b.get("slots", "")}
            for b in sec.get("content_blocks", [])]
        merged.append(sec)
    cls = {**old, "sections": merged,
           "p2_global": global_pass({"sections": merged}),
           "p3_rules": rule_pass(root, parsed),
           "version": f"v{datetime.now().strftime('%Y%m%d.%H%M')}-md",
           "generated_at": datetime.now().isoformat(timespec="seconds")}
    save(root, cls)
    return cls


# ---------------------------------------------------------------- md 渲染（同格式）

_STATIC_HEAD = """# {title}：章节分类与图文表编排方案

## 结论

保留原有章节目录和正文阅读顺序，按内容块的信息形态选择8类版式。专业主题决定画什么，
信息形态决定怎么排；同一节可以有一个主类、多个辅类，不强行套用一个模板。

本清单页码为解析版本的起始页。章节、标题和内容主题来自原文；分类名称、建议图表和
版式属于新增设计建议，不代表原文已提供这些图。本成果用于编排规划，不替代工程技术审核。
"""

_STATIC_MID = """## 二、每个内容块使用的分类维度

| 维度 | 标注内容 | 对编排的作用 |
|---|---|---|
| 专业主题 | 供货、土建、电气、质量、安全、协调、调试、运维等，可多选 | 决定插图对象与素材归属 |
| 信息形态 | A至H，主类在前、辅类在后 | 决定图形结构和页面模板 |
| 原文形态与密度 | 连续段落、逐条要求、现有表格；短、中、长 | 决定能否左右分栏、是否通栏或跨页 |
| 素材制作方式 | AI概念示意、依据资料的矢量图、真实资料原件 | 区分可以生成的插图与必须精确绘制的图表 |
| 保真与审核要求 | 参数敏感、程序敏感、服务承诺、资料边界 | 防止制图时补数值、串错流程、扩大承诺 |
| 素材复用主题 | 电缆、沟井、排管、接头、有限空间、验收等 | 同对象复用底图，但按章节目的更换标注 |
"""


def render_md(cls: dict[str, Any]) -> str:
    """从 json 渲染人审同格式 md（parser 可回读，round-trip 一致）。"""
    name = (cls.get("project") or {}).get("name") or "项目"
    lines: list[str] = [_STATIC_HEAD.format(title=name), _STATIC_MID]

    lines.append("## 三、原文总体结构与编排定位")
    lines.append("| 原章名 | 节数 | 顶层Word表格对象 | 内嵌图片处数 | 编排定位 |")
    lines.append("|---|---:|---:|---:|---|")
    for st in cls.get("p0_chapter_stats", []):
        lines.append(f"| {st['chapter']} | {st['sections']} | {st['tables']} | "
                     f"{st['images']} | {st.get('positioning') or '—'} |")
    lines.append("")

    lines.append("## 四、分类与编排矩阵")
    chapters: list[str] = []
    for s in cls["sections"]:
        if s["chapter"] not in chapters:
            chapters.append(s["chapter"])
    for ci, ch in enumerate(chapters, 1):
        lines.append(f"\n### {ch}\n")
        lines.append("| 原节名 | 原起始页 | 主类＋辅类 | 建议图形 | 建议表格 | 图文表组合 | 图像制作方式 |")
        lines.append("|---|---:|---|---|---|---|---|")
        sec_no = 0
        for s in cls["sections"]:
            if s["chapter"] != ch:
                continue
            sec_no += 1
            aux = "、".join(s["aux_classes"])
            cls_field = s["main_class"] + (f"＋{aux}" if aux else "")
            lines.append(f"| {s['title']} | {s['source_page']} | {cls_field} | "
                         f"{s['suggested_figures']} | {s['suggested_tables']} | "
                         f"{s['combo']} | {s['production']} |")
        lines.append("\n#### 本章内容拆分与注意事项\n")
        sec_no = 0
        for s in cls["sections"]:
            if s["chapter"] != ch:
                continue
            sec_no += 1
            lines.append(f"\n**{ci}.{sec_no} {s['title']}**\n")
            if s["content_blocks"]:
                lines.append("原文下一级主题：" + " / ".join(b["name"] for b in s["content_blocks"]))
            if s.get("note"):
                lines.append(("" if not s["content_blocks"] else "\n") + f"编排注意：{s['note']}")
            lines.append("")

    lines.append("\n## 五、同一对象跨章节采用不同表达\n")
    cross = cls.get("p2_global", {}).get("cross_chapter_objects", [])
    if cross:
        lines.append("| 对象 | 出现节 |")
        lines.append("|---|---|")
        for c in cross:
            locs = "；".join(f"{x['chapter']} {x['title']}" for x in c["sections"])
            lines.append(f"| {c['object']} | {locs} |")
    mixed = [s for s in cls["sections"] if s.get("mixed_split_example")]
    for s in mixed:
        lines.append(f"\n### 混合小节拆分示例：{s['chapter'][:3]}{s['title'][:3]}\n")
        lines.append("| 原文块 | 分类 | 最适合的呈现 |")
        lines.append("|---|---|---|")
        for b in s["content_blocks"]:
            lines.append(f"| {b['name']} | {b.get('class') or '—'} | {b.get('slots') or '—'} |")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- 门禁（3.1b）


def validate(root: Path, cls: dict[str, Any] | None = None) -> tuple[bool, list[str], list[str]]:
    """schema 五字段 / 节覆盖与顺序 / 起始页单调性；返回 (ok, problems, warnings)。"""
    cls = cls or load(root)
    problems: list[str] = []
    warnings: list[str] = []
    if not cls:
        return False, ["layout-classification.json 不存在"], []

    sec_index = _blocks_sections(Path(root))
    by_sid = {s["section_id"]: s for s in cls["sections"]}
    real = [(sid, ch, t) for sid, ch, t, _p in sec_index if not t.startswith("封面") and "总述" not in t]

    for sid, ch, title in real:
        s = by_sid.get(sid)
        if not s:
            problems.append(f"{sid}（{title}）无分类条目")
            continue
        for field, label in (("main_class", "主类"), ("suggested_figures", "建议图"),
                             ("suggested_tables", "建议表"), ("combo", "组合"),
                             ("production", "制作方式")):
            if not str(s.get(field) or "").strip():
                problems.append(f"{sid} 五字段缺失：{label}")
        if s.get("main_class") not in CLASSES:
            problems.append(f"{sid} 主类非法：{s.get('main_class')!r}")
        bad_aux = [a for a in s.get("aux_classes", []) if a not in CLASSES]
        if bad_aux:
            problems.append(f"{sid} 辅类非法：{bad_aux}")

    extra = set(by_sid) - {sid for sid, _, _ in real}
    if extra:
        problems.append(f"多余条目（blocks 无对应节）：{sorted(extra)}")

    pages = [s["source_page"] for s in cls["sections"] if s.get("source_page")]
    if pages != sorted(pages):
        problems.append("原起始页非单调递增（与节顺序不一致）")
    if len(cls["sections"]) != len(real):
        warnings.append(f"条目 {len(cls['sections'])} vs 实节 {len(real)}（总述节不计）")
    return (not problems), problems, warnings


def sample_checklist(cls: dict[str, Any]) -> list[dict[str, str]]:
    """每章抽 2 节人工核对主类（确定性取样：首节 + 中位节）。"""
    out = []
    chapters: list[str] = []
    for s in cls["sections"]:
        if s["chapter"] not in chapters:
            chapters.append(s["chapter"])
    for ch in chapters:
        secs = [s for s in cls["sections"] if s["chapter"] == ch]
        idx = {0, len(secs) // 2} if len(secs) > 1 else {0}
        for k in sorted(idx):
            s = secs[k]
            out.append({"section_id": s["section_id"], "title": s["title"],
                        "main_class": s["main_class"], "chapter": ch[:12]})
    return out


# ---------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="分类编排子系统（P0–P3 四遍 + 双产物）")
    ap.add_argument("root", nargs="?", default="projects/anke-35kv")
    ap.add_argument("--p1", choices=["md", "llm"], default="md",
                    help="P1 通道：md=解析现有人审 md（bootstrap）；llm=按章调 LLM")
    ap.add_argument("--source-md", default=None, help="P1=md 时的来源 md（默认 golden）")
    ap.add_argument("--provider", default="gptimage2", help="P1=llm 时的供应商别名")
    ap.add_argument("--model", default="glm-4.7", help="P1=llm 时的 chat 模型")
    ap.add_argument("--validate", action="store_true", help="只跑门禁不重建")
    ap.add_argument("--sync-from-md", action="store_true", help="md → json 回写（S1 编辑后）")
    ap.add_argument("--samples", action="store_true", help="打印每章抽 2 节核对清单")
    args = ap.parse_args(argv)

    root = Path(args.root)
    if not root.is_absolute():
        root = (BASE / root).resolve()

    if args.validate:
        ok, problems, warnings = validate(root)
        for p in problems:
            print(f"  [FAIL] {p}")
        for w in warnings:
            print(f"  [warn] {w}")
        print(f"门禁：{'PASS' if ok else 'BLOCK'}（{len(problems)} 项问题）")
        return 0 if ok else 1

    if args.sync_from_md:
        cls = sync_from_md(root)
    else:
        cls = build(root, p1=args.p1, source_md=Path(args.source_md) if args.source_md else None,
                    provider=args.provider, model=args.model)

    print(f"[OK] 分类编排：{len(cls['sections'])} 节 → {json_path(root)}")
    print(f"     P2 跨章同对象 {len(cls['p2_global']['cross_chapter_objects'])} 组 / "
          f"混合拆分建议 {len(cls['p2_global']['mixed_split_suggestions'])} 节")
    print(f"     人审版 {md_path(root)}")
    if args.samples:
        for s in sample_checklist(cls):
            print(f"  抽检 {s['section_id']} [{s['main_class']}] {s['title']}")
    ok, problems, _ = validate(root, cls)
    print(f"     门禁：{'PASS' if ok else 'BLOCK ' + '; '.join(problems[:5])}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
