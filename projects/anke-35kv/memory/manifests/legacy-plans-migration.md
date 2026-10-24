# 历史实施方案迁移说明

- 人工局部改字方案的有效约束已迁入 `memory/procedures/`，实现与验收见 `docs/局部改字操作手册.md`。
- HTML 混合引擎方案已落地为 `src/bidviz_html.py`、`templates/html/album.css` 与 `docs/PROGRESS.md` §6。
- 原 `.zcode/plans/` 仅为会话草稿，迁移后由 Git 历史与 durable memory 取代。
