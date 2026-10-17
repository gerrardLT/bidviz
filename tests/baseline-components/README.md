# 视觉回归基线（V1.3 阶段 1.4）

本目录存放**组件固定数据渲染的基线 PNG**。任何组件实现或 tokens.toml 变更导致像素 diff
超过阈值时，`tests/test_components_diff.py` 会失败，从而防止"改一处、崩十处"的隐性回归。

## 用途

- 阶段 1（当前）：仅建立机制 + 自检基线（`demo-rect.png`）。
- 阶段 2：加入 S003 样板页组件基线（KPI 卡 / 编号白片 / chevron / 图标行 / 表头）。
- 阶段 3+：8 类模板每类 1 张基线，作为模板演化的护城河。

## 添加新基线

1. 写一个 `render_xxx(path: Path) -> None` 函数，用**固定输入数据**渲染组件到 PNG。
2. 本地跑一次生成 PNG，肉眼确认后提交到本目录。
3. 在 `test_components_diff.py` 的 `COMPONENT_CASES` 里加一条 `(name, render_fn)`。

## 阈值

- 默认像素 diff 比例阈值：**2%**（`MAX_DIFF_RATIO = 0.02`）。
- 若基线需要更新（例如 tokens 冻结新版），删除对应 PNG 后重跑测试即可自动重生。

## 命名约定

- `demo-*.png`：机制自检用，与真实组件无关。
- `<template-id>-<component>.png`：真实组件基线，例如 `t-b1-kpi-strip.png`。
