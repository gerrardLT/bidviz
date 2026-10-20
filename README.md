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

当前 `anke-35kv` 的 `[layout].engine = "fullimage"`，Web 已升级为全图质量工作台：编辑完整页提示词 → 生成候选 2K → 自动门禁 → 人工批准发布 → 全部页面通过后正式汇编。候选不会直接覆盖已发布图；存在待审、过期、缺失或阻断页面时，正式 PDF 会被阻断，可另生成带水印 DRAFT。

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
```

`generate` 默认跳过已有图片；加 `--force` 才重生。Web 单节重生始终写入项目配置的主视觉目录。

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
   └─ 施工组织设计-图册-A3.pdf
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
