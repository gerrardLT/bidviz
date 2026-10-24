# BidViz

将 Word/PDF《施工组织设计》解析为按章、节组织的工程图册：每节含 AI 技术信息图与矢量图表，正文按类保留或提炼（prose_policy），页上文字均可搜索与反抽审计；网页编辑器支持标题、正文、提示词编辑、单节重生与重新汇编。

> 重构进行中：目标体系（A3 竖版 + 8 类版式 + 门控 run 编排）见 `docs/重构任务规划-V1.3.md`。

## 交付形态

本项目是 **Python CLI + 本地 Web 编辑器 + Skill 入口**，不绑定 Pi、PiDeck、VS Code 或其他 programming harness。任何 IDE/agent 均可调用同一套 CLI；Word 输入转换仅要求 Windows 已安装 Microsoft Word。

```text
bidviz/
├─ src/                         核心解析、生图、图表和 PDF 编译器
├─ web/static/index.html        本地文档编辑器
├─ web/server.py                标准库 HTTP 服务
├─ memory/                      跨项目 verified 记忆与 schema
├─ projects/<project>/memory/   项目级签认决策、策略与证据
├─ projects/<project>/          各项目配置与数据
├─ skill/SKILL.md               agent/IDE 可选工作流说明
├─ docs/reference/              有价值的模型与版式对比资料
├─ bidviz                       未安装时的 CLI 包装入口
└─ pyproject.toml
```

## 环境

- Python 3.11+
- Windows 10/11
- 处理 `.doc/.docx` 时：Microsoft Word + `pywin32`
- 直接处理文本型 PDF 时：无需 Word COM
- 扫描 PDF：当前尚未集成 OCR

## 安装

```powershell
cd D:\project\bidviz
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[windows]"
```

未执行 `pip install -e .` 时，命令均可写成 `python bidviz ...`。

## 生图凭据

推荐把根目录 `.env.example` 复制为 `.env`：

```text
BIDVIZ_BASE_URL=https://api.yoshub.com
BIDVIZ_API_KEY=sk-...
```

也可设系统环境变量，或把多个供应商写入：

```text
%USERPROFILE%\.bidviz\providers.toml
```

格式见 `providers.toml.example`。凭据优先级：环境变量 / `.env` → `~/.bidviz/providers.toml` → PiDeck `imagegen.json` 兼容回退。密钥不进入代码、日志或项目配置。

## 新项目

```powershell
bidviz init demo-35kv --voltage 35kV
# 把 Word/PDF 放入 projects/demo-35kv/inputs/
bidviz ingest demo-35kv
bidviz generate demo-35kv --chapter 第一章
bidviz generate demo-35kv --all
bidviz build demo-35kv
bidviz serve demo-35kv --port 8770
```

浏览器打开 `http://localhost:8770/`。

当前 `anke-35kv` 的 `[layout].engine = "fullimage"`，Web 已升级为全图质量工作台：编辑完整页提示词 → 生成候选 2K → 自动门禁 → 人工批准发布 → 全部页面通过后正式汇编。候选不会直接覆盖已发布图；缺少 current published，或页面存在过期/缺失/阻断状态时，正式 PDF 会被阻断。已发布页可以另有更新候选待审，不影响用 current published 汇编；也可生成带水印 DRAFT。

全图页支持**人工框选局部改字**：点任意页图打开全屏查看器（滚轮缩放、拖动平移、适应/100%/200%/400%）→ 从已发布/候选栏进入全屏编辑器（默认 100% 原像素、5× 放大镜、8 个手柄、方向键/坐标微调）→ 输入替换文字 → 只重绘框内 → 生成新候选 → 人工放大复核后批准发布；框外像素保持不变，框内零变化会被自动阻断。历史 103 页需先执行一次回填（`python -m src.bidviz_local_edit import --root projects/anke-35kv`），操作见 `docs/局部改字操作手册.md`。OCR 乱码自动检测（RapidOCR + 原文比对）列为待办，当前为纯人工审核。

## 常用命令

```powershell
bidviz list
bidviz status anke-35kv
bidviz ingest anke-35kv --source D:\docs\施工组织设计.docx
bidviz generate anke-35kv --chapter 第一章
bidviz generate anke-35kv --all
bidviz build anke-35kv
bidviz audit anke-35kv
bidviz serve anke-35kv
bidviz pages anke-35kv
# 记忆控制面：执行前取适用策略/经验，索引可随时重建
bidviz memory context anke-35kv --operation build --engine fullimage
bidviz memory doctor anke-35kv
# 备选引擎：局部 AI 图 + HTML/CSS 排版（矢量 PDF）
bidviz generate anke-35kv --engine html --all
bidviz build anke-35kv --engine html
bidviz pages anke-35kv --engine html
# AI 生图 45–50 页压缩版（page-plan 精简内容 + 小节合并）
python -m bidviz_fullimage_compact anke-35kv --plan
python -m bidviz_fullimage_compact anke-35kv --generate
python -m bidviz_fullimage_compact anke-35kv --pdf
```

`bidviz pages` 把 PDF 的每页图按**真实 PDF 页码**另存到
`output/page-images/<PDF名>/001_Sxxx_pN.png`，并写 `index.json`/`index.csv`
映射；fullimage 默认用硬链接，不额外占盘，加 `--copy` 才复制文件。

`--engine html` 是备选路线：AI 只生成 figure-plan 的 1–2 张局部插图，正文/表格/
页眉页脚/页码全部由 HTML/CSS 排版，Playwright 驱动 Chromium 打印成矢量 PDF。
需要先在 `.venv` 安装可选依赖：`pip install "playwright==1.55.0"`；本机已缓存
Chromium 时无需再下载。该路线使用 `page-plan.json` 的精简内容，文字量少于
fullimage 全图路线；输出统一带 `-html` 后缀，不覆盖现有产物。

`bidviz_fullimage_compact` 是 **AI 生图压缩版**：仍由 AI 生成整页 A3 设计稿，但按
项目配置的每页字数/小节数合并。页数目标是**项目级要求**，写在项目 `bidviz.toml`：

```toml
[compact]
min_pages = 45              # 0 = 不要求下限
max_pages = 50              # 0 = 不限页数
max_chars_per_page = 2000
max_sections_per_page = 2
```

本项目按招标要求设 45–50 页，实际 47 页（46 内容页 + 封面）。其他项目按各自
要求改这里；也可用 `--min-pages/--max-pages` 临时覆盖。提示词中的正文用「」
包住，并在汇编时写入隐藏文字层，兼顾逐字约束与 Ctrl+F/审计。

压缩版强制 **2048×2896**：生图返回低于 2048×2800 会删除重试，不接受低清图；
默认走 api7900。汇编时顶部 64pt、底部 32pt 由矢量页眉页脚覆盖（章名/项目全称/
三道斜线/横线/页码），因此各页页眉页脚位置固定，不再跟随模型自由绘制。
页内表格已清除“逐字取自原文”“表头/行/表：”等编辑说明，并对工序/序号列做
1..N 连续重编号；成品里不保留任何生成/校核痕迹。

`generate` 默认跳过已有图片；加 `--force` 才重生。Web 单节重生始终写入项目配置的主视觉目录。

## 多层记忆控制面

BidViz 将长期知识与运行状态分开：仓库级 verified procedure/knowledge 位于 `memory/`，项目签认 decision/policy 位于 `projects/<name>/memory/`；每次 CLI/Web 任务的 JSONL 事件位于 `work/runs/`，SQLite FTS 索引位于 `work/memory-index-*.sqlite3`，后两者均可删除重建。

执行前用 `bidviz memory context` 按项目、引擎、操作、节和页取最小上下文。代码、配置、提示词或原文哈希变化时，依赖它们的记忆自动转为 stale 并退出默认检索。系统只自动提出 candidate；`policy/decision` 必须显式人工签认，记忆不会自动修改原文、工程参数、费用授权或发布权限。完整格式与晋升规则见 `memory/README.md`。

## 项目配置

每个项目由 `projects/<name>/bidviz.toml` 控制：

```toml
[project]
name = "项目名称"
voltage = "35kV"
doc_type = "施工组织设计"
header = "项目名称 · 施工组织设计"
standard = "适用验收标准"

[image]
provider = "yoshub"
model = "gpt-image-2-2K/4K"
size = "2048x1448"
dir = "work/visuals"

[layout]
engine = "fullimage"   # legacy | v13 | fullimage | html

[html]
browser = "auto"       # 可选 Chrome/Edge 可执行文件路径
dpi = 200              # --png 页图分辨率
emit_png = false
table_cell_max = 400   # HTML 表格单元格最大字数
```

项目名、电压等级、页眉、规范文字、供应商、模型、图片尺寸和输出目录均不再写死于代码。

## 数据与输出

```text
projects/<name>/
├─ bidviz.toml
├─ inputs/                      原始 Word/PDF
├─ work/
│  ├─ blocks.json              原文结构化块（不被编辑器修改）
│  ├─ pages.json
│  ├─ edits.json               非破坏式编辑
│  └─ visuals*/Sxxx.png        每节主视觉
└─ output/
   ├─ 施工组织设计-图册-A3.pdf
   ├─ page-images/<PDF名>/         按真实页码导出的页图 001_…png + index.json/csv
   ├─ 施工组织设计-图册-A3-html.pdf   HTML 混合引擎产物
   └─ _imgcache/                   汇编时生成的 JPEG 缓存
```

HTML 引擎中间件：

```text
projects/<name>/work/
├─ illustrations/Sxxx.png, Sxxx__2.png   局部 AI 插图
├─ illustrations-ledger.json             插图台账
├─ html/album.html                      汇编 HTML
└─ html-pages.json                      PDF 页号 → 节/块/表行 清单
```

PDF 中 AI 主视觉为栅格图片；标题、图表和完整正文为矢量文字，可搜索、复制和反抽审计。`edits.json` 中的标题及正文会进入下一次 build；提示词用于下一次单节生图。

## 现有安科项目

```powershell
bidviz status anke-35kv
bidviz serve anke-35kv
bidviz build anke-35kv
```

项目数据位于 `projects/anke-35kv/`。当前 yoshub 目录已有 56/58 张，缺 `S018`、`S028`；因其他视觉目录有回退图，不妨碍完整 PDF 汇编。

## 已知边界

- Word COM 仅适用于安装了 Microsoft Word 的 Windows。
- 章/节识别与参数、风险、工序、ITP 抽取仍为启发式；换文档后须抽检。
- AI 图片不可完全复现，也不能替代工程师对工艺、参数与图中文字的复核。
- 当前为单机单用户工具；未加入账户、权限、云存储或多人冲突处理。
- `build` 输出“图版页 + 内容报表页”，正文按类保留或提炼（prose_policy），页数随内容块与页数预算（≤3 页/节）变化。
