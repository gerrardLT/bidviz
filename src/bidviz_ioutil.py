#!/usr/bin/env python3
"""共享 IO 工具：原子写（tmp + fsync + os.replace）。

工作区 JSON（edits / figure-plan / 各引擎 ledger）与交付 PDF 一律经由这里落盘，
消灭「写一半崩溃 → 文件损坏 → 加载自愈为空」与并发交错写两条事故路径。
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def atomic_write_text(path: str | Path, text: str) -> Path:
    """原子写文本：同目录临时文件 → fsync → os.replace 到目标。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent,
                prefix=f".{path.name}.", suffix=".tmp", delete=False) as fh:
            tmp_name = fh.name
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
        tmp_name = ""
    finally:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
    return path


def atomic_write_json(path: str | Path, data) -> Path:
    return atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def atomic_replace_from(tmp_path: str | Path, final_path: str | Path) -> Path:
    """把 tmp_path 原子挪到 final_path（用于 PDF 等先写临时名再替换的产物）。"""
    tmp_path, final_path = Path(tmp_path), Path(final_path)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    os.replace(tmp_path, final_path)
    return final_path
