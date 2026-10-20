# BidViz 交付与适配清单

> **跨会话续跑 / 换 agent 接手：先读 `docs/PROGRESS.md`**（阶段状态总表、验证命令、下一步任务、踩坑记录）。
> 本文件只描述交付形态与适配条件；重构执行规划见 `docs/重构任务规划-V1.3.md`。

## 推荐交付形式

交付 **Git 仓库或版本压缩包**，内含 Python CLI、本地 Web 编辑器和可选 Skill：

1. CLI 是稳定接口，供人、IDE、agent、CI 调用。
2. Web 全图质量工作台供非技术用户改正文、标题与整页提示词；生图先形成候选，经自动门禁与人工批准后才发布，支持退回、豁免、回滚、DRAFT 和正式汇编阻断。
3. `skill/SKILL.md` 只描述工作流，不能代替实现。
4. 项目数据与核心代码分开；新客户只新增 `projects/<name>/`，不复制代码。

不建议只交付 Skill。不同 harness 对 Skill 格式、工具权限和上下文策略各异，而命令行是共同最小接口。

## 其他 harness / IDE 的最低接入条件

- Windows 10/11、Python 3.11+。
- 输入为 `.doc/.docx` 时，安装 Microsoft Word 和 `pywin32`。
- 输入为文本型 PDF 时，不依赖 Word COM。
- 能执行本地命令：`bidviz ingest/generate/build/serve/status`。
- 在线生图时，提供 OpenAI-compatible `/images/generations` 接口。

VS Code、Cursor、Claude Code、Codex、Kiro、Pi 等均无需专属代码适配；让其读取 `README.md` 或安装对应 Skill，即可调用 CLI。

## 新机器安装

```powershell
cd <repo>
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[windows]"
copy .env.example .env
```

随后编辑 `.env`，勿提交密钥。

## 新项目需要修改的内容

只改 `projects/<name>/bidviz.toml`：

- `project.name`：项目名称
- `project.voltage`：电压等级，可留空
- `project.doc_type`：文档类型
- `project.header`：页眉
- `project.standard`：适用规范
- `image.provider`：供应商别名
- `image.model`：模型名
- `image.size`：请求尺寸
- `image.dir`：主视觉输出目录
- `image.fallback_dirs`：旧图回退目录，可留空

然后把 Word/PDF 放到 `inputs/`，运行 `ingest`。无需改 Python 源码。

## 供应商适配

标准 OpenAI-compatible 接口只需配置 URL、key、模型名。若供应商响应不返回 `data[0].b64_json` 或 `data[0].url`，才需修改 `src/bidviz.py::ImageAPI.generate()`。

密钥来源优先级：

1. `BIDVIZ_BASE_URL` / `BIDVIZ_API_KEY`
2. 项目或仓库 `.env`
3. `%USERPROFILE%\.bidviz\providers.toml`
4. PiDeck `imagegen.json` 兼容回退

## 换文档后必须抽检的部分

以下仍是启发式而非通用语义模型：

- 章/节标题识别
- 关键参数抽取
- 风险句抽取
- 工序排序
- ITP、RACI、Gantt、材料设备表推断

若新文档标题格式不是“第一章 / 第一节”，先调整 `bidviz.toml` 的正则，并抽检 `work/blocks.json` 和编辑器目录树。当前 `bidviz_e1.py` 的分节逻辑尚未完全消费自定义正则；遇到特殊标题体系时需改该函数。

## Word COM 边界

- 仅 Windows + 桌面版 Microsoft Word。
- 服务账户、无桌面会话、Office 弹窗和文档损坏均可能导致 COM 失败。
- 云端部署宜要求用户先上传 PDF，或另接 LibreOffice/headless 转换；当前不应把 Word COM 服务化。

## 当前验收证据（2026-10-16 更新）

> 系统重构进行中，执行规划见 `docs/重构任务规划-V1.3.md`，**阶段进度见 `docs/PROGRESS.md`（阶段 0/1/2 已完成）**。以下为**现状**证据：

- 主产物：`projects/anke-35kv/output/施工组织设计-图册-A3.pdf`＝旧全文版 183 页 / A3 横向 / 29.9MB（**待阶段 7 重跑为 A3 竖版新体系**）
- 新体系 golden page：`output/_样板-S003.pdf`（A3 竖版 T-B1，签认点②已冻结，哈希见 `work/frozen.json`）
- 提炼式对照：`output/_校验-报表式.pdf`＝116 页 / 1803 行提炼内容 0 缺失 / 覆盖率 100%
- 主视觉：`work/visuals-2k-yoshub/` 56 张；S018/S028 回退旧目录
- Web：9 章 58 节；/api/doc、/api/section、/api/hero、/api/build、/api/audit 均通过
- CLI：`bidviz status/build/serve` 通过（阶段 0 已修 cmd_build 审计键 KeyError；pyproject 已补 bidviz_plan/bidviz_layout）
- 旧大体积产物（含 348MB DRAFT）已按 D6 授权清理，output 降至约 71MB

审计记录：`projects/anke-35kv/output/audit-vector.json`。

## 尚未产品化的部分

暂不做，因当前单机交付已够用：

- 多用户账户与权限
- 云存储与任务队列
- 多人编辑冲突
- seed / 参考图锁定与完全可复现生图
- 扫描 PDF OCR
- 原生 DWG/BIM 解析
- 通用化图表语义抽取

有真实多人、云端或多文档模板需求后再加，勿先建平台。

## 迁移清理

`D:\project\bidviz` 已成为主目录。旧 C 盘副本尚未自动删除，以免误删唯一历史资料。确认以下三项后再手工删除：

1. 新 PDF 抽检通过；
2. Web 编辑器可正常保存/重生/重编；
3. D 盘项目已有备份或 Git 远端。
