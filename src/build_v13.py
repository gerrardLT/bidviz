#!/usr/bin/env python3
"""build_v13 —— V1.3 新体系编译器：page-plan → A3 竖版图册 PDF。

职责（阶段 3.6）：
- 读 work/page-plan.json（S003 为签认点②冻结件，逐字节沿用）；
- 每节按 template_id 用 bidviz_templates 装配；表溢出续页（表头重复）；
  ≤3 页/节硬约束（D4），超出记 overflow（不静默丢弃正文——留在 plan 的 overflow 清单）；
- 两遍编译：首遍探页数，次遍写准 total_pages；
- 编后门禁：audit_layout（乱度）+ issues（告警）。

与旧体系隔离（踩坑 #11）：本模块**必须与 legacy build 分进程运行**——
import 时即 bv.set_page('A3','portrait')，旧体系若在同进程后 import 会拿到竖版快照。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "src"))

import bidviz_templates as tpl
import bidviz_vector as bv
from bidviz_templates import (Callout, FigureSlot, KPI, PagePlan, ProseBlock)
from bidviz_vector import Sheet

MM = bv.MM

# 本模块 import 即切竖版（新体系画布）；见模块 docstring 的进程隔离约定
bv.set_page("A3", "portrait")

MAX_PAGES = 3          # D4：每节 ≤3 页


def _page_plan_from_dict(raw: dict, root: Path, page_no: int, total_pages: int) -> PagePlan:
    kpis = [KPI(**k) for k in raw.get("kpis", [])]
    fig_raw = raw.get("figure")
    figure = None
    if fig_raw:
        img = fig_raw.get("image")
        img_path = (root / img) if img else None
        callouts = [Callout(no=str(c["no"]), text=c.get("text", ""),
                            anchor=tuple(c.get("anchor", (0.5, 0.5))))
                    for c in fig_raw.get("callouts", [])]
        figure = FigureSlot(image=img_path, callouts=callouts,
                            caption=fig_raw.get("caption", ""),
                            text_spec=fig_raw.get("text_spec", []),
                            fit=fig_raw.get("fit", "contain"),
                            crop_top=float(fig_raw.get("crop_top", 0.0)),
                            cover_anchor=tuple(fig_raw.get("cover_anchor", (0.5, 0.5))))
    prose = [ProseBlock(text=p["text"], kind=p.get("kind", "body"))
             for p in raw.get("prose", [])]
    it = raw.get("icon_table", {})
    return PagePlan(
        chapter=raw.get("title", {}).get("chapter", ""),
        section=raw.get("title", {}).get("section", ""),
        page_no=page_no,
        total_pages=total_pages,
        template_id=raw.get("template_id", "T-B1"),
        kpis=kpis,
        figure=figure,
        prose=prose,
        icon_table_headers=it.get("headers", []),
        icon_table_rows=it.get("rows", []),
        tables=raw.get("tables", []),
        vector=raw.get("vector"),
        footer_note=raw.get("footer_note", ""),
    )


def _remaining_tables(page_raw: dict, tk: dict, content_w: float) -> list[dict]:
    """首页装不下的表（除首页模板自己消费的第一张外）。"""
    tables = list(page_raw.get("tables", []))
    tid = page_raw.get("template_id", "T-B1")
    first_consumed = tid in ("T-A1", "T-B1", "T-C1", "T-H1") and page_raw.get("figure") is not None
    # A/C/H/B 首页底部只放第一张表；矢量先行模板（E/F/G/D）首页能放几张贴几张，
    # 但为确定性起见：非图模板首页也只放第一张，其余全部续页。
    return tables[1:] if (first_consumed or tables) else []


def build_v13(root: Path, out_pdf: Path, limit: int | None = None) -> dict[str, Any]:
    """编译新体系图册；返回 {pages, sections, overflow_sections}。

    两遍编译：首遍数出每节实际页数（探针 PDF），次遍把节内 total_pages 写准。
    """
    import bidviz_plan as plan_mod
    root = Path(root)
    pp = plan_mod.load_page_plan(root)
    if not pp:
        pp = plan_mod.build_page_plan(root)
    tk = tpl.load_tokens()
    sids = sorted(pp["sections"].keys())
    if limit:
        sids = sids[:limit]

    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    probe = out_pdf.with_name(out_pdf.stem + ".pass1.pdf")
    counts = _count_pages(root, probe, pp, sids, tk)
    probe.unlink(missing_ok=True)
    return _render(root, out_pdf, pp, sids, tk, counts)


def _count_pages(root: Path, out_pdf: Path, pp: dict, sids: list[str], tk: dict) -> dict[str, int]:
    """首遍：渲染到探针 PDF，回读每节页数（按节起始页顺序累积）。"""
    sheet = Sheet(out_pdf)
    counts: dict[str, int] = {}
    for sid in sids:
        pages_raw = pp["sections"][sid].get("pages", [])
        if not pages_raw:
            continue
        n = 1
        page1 = pages_raw[0]
        plan1 = _page_plan_from_dict(page1, root, 1, 1)
        tpl.compose_page(sheet, tk, plan1, template=page1.get("template_id", "T-B1"))
        sheet.c.showPage()
        rest = _remaining_tables(page1, tk, sheet.w)
        page_no = 1
        while rest and page_no < MAX_PAGES:
            page_no += 1
            plan_c = _page_plan_from_dict(page1, root, page_no, 1)
            plan_c.kpis, plan_c.figure, plan_c.vector, plan_c.prose = [], None, None, []
            rest = tpl.compose_content_page(sheet, tk, plan_c, rest)
            sheet.c.showPage()
            n += 1
        counts[sid] = n
    sheet.c.save()
    return counts


def _render(root: Path, out_pdf: Path, pp: dict, sids: list[str], tk: dict,
            counts: dict[str, int] | None = None) -> dict[str, Any]:
    """正式遍：节内页码写准（N / total per 节）。"""
    counts = counts or {}
    sheet = Sheet(out_pdf)
    overflow_sections: list[str] = []
    n_pages = 0
    for sid in sids:
        entry = pp["sections"][sid]
        pages_raw = entry.get("pages", [])
        overflow = any(p.get("overflow") for p in pages_raw)
        page1 = pages_raw[0] if pages_raw else None
        if page1 is None:
            continue

        # ---- 首页 ----
        total = counts.get(sid, 1)
        plan1 = _page_plan_from_dict(page1, root, 1, total)
        tpl.compose_page(sheet, tk, plan1, template=page1.get("template_id", "T-B1"))
        sheet.c.showPage()
        n_pages += 1

        # ---- 续页：剩余表按 ≤3 页预算 ----
        rest = _remaining_tables(page1, tk, sheet.w)
        # 首页模板未含 tables 字段的老条目（如冻结 S003 的 icon_table）无续页
        page_no = 1
        while rest and page_no < MAX_PAGES:
            page_no += 1
            plan_c = _page_plan_from_dict(page1, root, page_no, total)
            plan_c.kpis = []                      # 续页不再放 KPI
            plan_c.figure = None
            plan_c.vector = None
            plan_c.prose = []
            rest = tpl.compose_content_page(sheet, tk, plan_c, rest)
            sheet.c.showPage()
            n_pages += 1
        if rest:
            overflow = True
        if overflow:
            overflow_sections.append(sid)

    sheet.c.save()
    return {"pages": n_pages, "sections": len(sids), "overflow_sections": overflow_sections}


def run_post_gates(root: Path, out_pdf: Path) -> dict[str, Any]:
    """编后门禁：乱度（audit_layout）+ issues（告警）。报告写入 output/。"""
    gates: dict[str, Any] = {}
    try:
        import audit_layout
        tokens = BASE / "docs" / "layout-md" / "tokens.toml"
        rep_path = out_pdf.parent / "audit-layout-v13.json"
        rep = audit_layout.audit_pdf(out_pdf, tokens, rep_path)
        gates["layout"] = {
            "pages": len(rep["pages"]),
            "failed_pages": rep["failed_pages"],
            "passed": rep["overall_passed"],
        }
        failed = rep["failed_pages"]
        print(f"[gate] audit_layout（v13）：{len(rep['pages'])} 页，超标 {len(failed)} 页"
              + (f" → {failed[:10]}{'…' if len(failed) > 10 else ''}" if failed else ""))
    except Exception as exc:   # pragma: no cover
        gates["layout"] = {"error": str(exc)}
        print(f"[gate] audit_layout 跳过：{exc}")
    try:
        import bidviz_issues
        doc = bidviz_issues.load(root)
        passed, reason = bidviz_issues.gate_check(doc)
        gates["issues"] = {"passed": passed, "reason": reason}
        print(f"[gate] issues：{'PASS' if passed else 'BLOCK'}（{reason}）")
    except Exception as exc:   # pragma: no cover
        gates["issues"] = {"error": str(exc)}
    return gates


def audit_v13_coverage(out_pdf: Path, root: Path) -> dict[str, Any]:
    """v13 覆盖率（口径：**计划排入的表行零缺失**）。

    与 legacy audit_coverage（全部 curate 行）不同：v13 按 D4 页数预算是提炼式编排，
    审计对象 = page-plan 中各节 tables 实际计划的行；hidden 层（text_spec）一并校验
    可搜索性（D10）。
    """
    import fitz
    import re as _re

    def norm(s: str) -> str:
        return _re.sub(r"\s+", "", s)

    import bidviz_plan as plan_mod
    pp = plan_mod.load_page_plan(root) or {}
    doc = fitz.open(out_pdf)
    whole = norm("".join(page.get_text() for page in doc))
    doc.close()

    checked = missing = 0
    miss_list: list[dict] = []
    for sid, entry in pp.get("sections", {}).items():
        for page in entry.get("pages", []):
            for t in page.get("tables", []):
                for row in t.get("rows", []):
                    for cell in row:
                        n = norm(str(cell))
                        if len(n) < 4:
                            continue
                        checked += 1
                        if n not in whole:
                            missing += 1
                            if len(miss_list) < 12:
                                miss_list.append({"section": sid, "text": str(cell)[:80]})
            fig = page.get("figure") or {}
            for s in fig.get("text_spec", []):
                n = norm(str(s))
                if len(n) < 2:
                    continue
                checked += 1
                if n not in whole:
                    missing += 1
                    if len(miss_list) < 12:
                        miss_list.append({"section": sid, "text": f"[hidden] {s}"})
    # 续页实际渲染的表在 plan 里只有一份定义，续行由渲染器按 max_h 截断；
    # 超出 3 页预算的行记录在 overflow，此处不计缺失（已显式留痕）。
    return {"rows_checked": checked, "rows_missing": missing,
            "coverage_pct": round(100 * (checked - missing) / checked, 2) if checked else 100.0,
            "missing_sample": miss_list}


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="V1.3 新体系编译（page-plan → A3 竖版）")
    ap.add_argument("--root", default="projects/anke-35kv")
    ap.add_argument("--out", default=None)
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    root = (BASE / args.root).resolve() if not Path(args.root).is_absolute() else Path(args.root)
    out = Path(args.out) if args.out else root / "output" / "施工组织设计-图册-A3-v13.pdf"

    t0 = time.time()
    stat = build_v13(root, out, limit=args.limit)
    print(f"[OK] v13 编译：{stat['sections']} 节 / {stat['pages']} 页 / "
          f"溢出节 {len(stat['overflow_sections'])} → {out}"
          f"（{out.stat().st_size / 1024 / 1024:.1f} MB，{time.time() - t0:.1f}s）")
    run_post_gates(root, out)
    cov = audit_v13_coverage(out, root)
    (out.parent / "audit-v13.json").write_text(
        json.dumps({"build": stat, "coverage": cov}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(f"[OK] v13 覆盖率 {cov['coverage_pct']}%（缺失 {cov['rows_missing']}/{cov['rows_checked']} 行计划内容）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
