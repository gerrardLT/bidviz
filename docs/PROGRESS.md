# bidviz 重构进度账本（新会话 / 换 agent 续跑入口）

> **本文件只保留阶段进度摘要。** 跨会话先运行 `bidviz memory context <项目> --operation <任务> --engine <引擎>`；长期 verified 知识在 `memory/`，项目签认决策在 `projects/<项目>/memory/`。
> 规划全文见 `docs/重构任务规划-V1.3.md`；交付适配见 `docs/HANDOFF.md`。新的踩坑应写成带证据和依赖哈希的 durable memory，不再无限追加本文件 §5。

## 0. 阶段状态总表

| 阶段 | 内容 | 状态 | 签认点 |
|---|---|---|---|
| 0 | 债务清偿（CLI KeyError / py-modules / 产物清理 / vault 笔记 / 文档对齐） | ✅ 完成 | — |
| 1 | 规格与门禁地基（tokens.toml / audit_layout / issues / 视觉回归基线） | ✅ 完成 | ① tokens v1.0 已签（随阶段 2 一并确认） |
| 2 | 样板页 S003 与签认（A3 竖版 / T-B1 组件库 / 隐藏层 / golden page） | ✅ 完成 | ② golden page 已冻结 |
| 3 | 分类接入与 8 类模板分批（classify 子系统 / page-plan 生成器 / 模板批 1–4 / 矢量图型库 / build 切换） | ✅ 完成（待用户签认） | ③ 4 节模板批 → `_签认-S012/S018/S019/S029/S054.pdf` |
| 4 | 生图侧改造（style.md / 任务单字段 / 构图契约 / 参考图 / 文字门禁 / 风格种子 / 预算） | ⬜ 下一步 | ④ 风格种子 |
| 5 | 原图表决策（source-assets / 点名重绘 4 张 / keep 表接入 D/G） | ⬜ | ⑤ 决策表 |
| 6 | Web 全图质量工作台（提示词草稿 / 候选版本 / 自动门禁 / 人工签认 / 发布回滚 / 汇编阻断） | ✅ 完成 | 候选必须批准后发布；关键串全检；未决页阻断正式汇编 |
| 6.1 | 人工局部改字（103 页回填已发布 / 全屏大图精确框选 / 仅合成遮罩 / 人工复核发布） | ✅ 完成（S030 验收样本已发布） | 大图查看器 + 100%/200%/400% + 放大镜 + 手柄/方向键微调；空改自动阻断；OCR 检测列为待办 |
| 6.2 | HTML 混合备选引擎（局部 AI 图 + HTML/CSS 排版 → Chromium 矢量 PDF） | ✅ 可用（备选，engine=html） | 117 页 / 文本覆盖 100% / 0 溢出 / 现有 fullimage 不变 |
| 6.3 | AI 生图压缩版（page-plan 精简内容 + 小节合并 + 正文引号） | ✅ 完成 | 本项目 45–50 页目标（实际 47）；页数目标已改为项目级 `[compact]` 配置，后续项目按需设置 |
| M1 | 多层记忆控制面（TOML durable memory / JSONL episode / SQLite FTS / 失效与晋升） | ✅ 完成 | 11 条跨项目记忆 + 7 条安科签认策略；CLI/Web 可检索、审阅、晋升；policy 不自动签认 |
| M2 | 深度清理（发布存储独立化 / manifest / 候选与历史产物裁剪） | ✅ 完成 | 103 current published、S058 v2 待审保留；删除约 4.35 GiB，零哈希冲突；2026-10-23 用户签认：正式交付物为 47 页压缩版，103 页全图册为初版，提示词基线问题按 superseded 关闭（`decision.anke.canonical-deliverable-compact-47p`） |
| 7 | 整册重跑与交付（A3 竖版整册 / 三道验收关 / 退役归档） | ⬜ | ⑥ final 签发 |

冻结签认账本（sha256）：`projects/anke-35kv/work/frozen.json`。
**改 tokens.toml 必须回到签认点①重签；改 page-plan 的 S003 页必须比对 frozen.json 哈希。**

> **进行中（2026-10-17）：签认点①② 重签认迭代（T-B1 v2 样图同构）。**
> 用户比对样板图后要求精准复刻 → 已产出 tokens **v1.1**（margin 72→40、主蓝 #1f4e79→#004890、
> 新增 chapter_title/kpi_icon_r/skyline 等键——来源=样图程序化量测+视觉解析）+ **T-B1 v2**
> （页眉线夹层/章大标题渐变线/节标题条/圆环 KPI 卡/深蓝圆片标注+图签/表题行/天际线页脚）。
> S003 重渲染 contact sheet 相似度 ~90%（剩余差距=AI 主图画风，属阶段 4 风格种子）。
> v2 golden 像素哈希：`6e1f29e5…`（dpi100）。整册 114 页 / 乱度 0 超标 / 覆盖率 100% 复验通过。
> **frozen.json 仍是 v1.0/v1 哈希——待用户签认 contact sheet 后更新（①② 重签）。**

## 1. 新会话启动顺序（必读文件）

1. `bidviz memory context anke-35kv --operation <任务> --engine <引擎>` —— 最小适用策略、程序和知识
2. `docs/PROGRESS.md`（本文件）—— 阶段状态与下一步
3. `docs/重构任务规划-V1.3.md` —— 决策 D1–D13、阶段任务明细、门禁总表、§7 run orchestrator 规格
4. `projects/anke-35kv/work/frozen.json` —— 已冻结件哈希（勿擅改已签认件）
5. `docs/layout-md/tokens.toml` —— 视觉 tokens（组件只引用 token 名，禁硬编码颜色/字号）
6. `docs/layout-md/layout_classification.md` —— 57 节人工分类（阶段 3 classify 子系统的 golden 来源）
7. 按需：`src/bidviz_templates.py`（T-B1 组件库）、`src/build_sample_s003.py`（样板渲染入口）

## 2. 状态验证命令（接手后先跑，全绿才开工）

```powershell
cd d:\project\bidviz
.\.venv\Scripts\python.exe -m unittest discover -s tests        # 期望 Ran 181 tests OK
.\.venv\Scripts\python.exe -m src.bidviz_cli memory doctor anke-35kv
.\.venv\Scripts\python.exe tests/test_smoke.py                  # 旧体系 legacy 冒烟
.\.venv\Scripts\python.exe src/build_sample_s003.py             # 重渲染 golden page（应确定性复现）
.\.venv\Scripts\python.exe -m src.bidviz_cli issues anke-35kv --report   # issues 门禁现状
.\.venv\Scripts\python.exe -m src.bidviz_classify anke-35kv --validate   # 分类门禁（阶段 3+）
.\.venv\Scripts\python.exe src/build_v13.py                     # v13 整册（114 页/覆盖率100%/乱度0超标）
```

注意两点：
- `issues --report` 存在 open issue 时**退出码为 1（设计如此）**：当前 7 条 open 是预期状态（阶段 3 起在 S2 门控消费），不要误判为环境异常。
- golden page 复现校验**以像素哈希为准**：重渲染后取 100dpi 像素 sha256，应等于 frozen.json 的
  `pixel_sha256_dpi100`（实测 `13974cc5…`）；PDF 字节哈希含 reportlab 时间戳，**不可复现，仅供参考**。
  乱度门禁应与 frozen.json gates 一致（fill 21 / colors 4 / x0 26 / 覆盖 100%）。

## 3. 下一步任务（阶段 4 领取处）

阶段 3 已完成（2026-10-17，签认点③产物已出，**待用户签认后进阶段 4**）。按 `docs/重构任务规划-V1.3.md` §阶段 4 执行：

1. **4.1 `prompts/style.md`** 全局风格契约：三语汇映射 8 类（C→等轴测/剖面三维；B→三维施工示意；H→真实感场景；D→不生图）；色板服从 tokens；负面清单（无页面级文字）。
2. **4.2 figure-plan 任务单字段扩展** + subject 白名单过滤（flow/org/timeline/param 拒收转矢量）。
3. **4.3 构图契约 + callouts 锚点数据**；任务单 `text_spec` 声明图内标注文字（D9）。
4. **4.4 参考图机制**：`page-media-map.json` 索引化接入任务单 refs。
5. **4.5 生图门禁**：尺寸校验（D7）+ VLM 回读 diff（D10）→ 重试 3 次 → 拒收转人工（D11）；隐藏层随 build 写入。
6. **4.6 风格种子**：每类题材 1 张签认（**签认点④**）。
7. **4.7 生图预算（D13）**：token-cost 实测价估算 + 超 20% 告警。

阶段 3 遗留（阶段 4/5 顺带）：
- S001（封面及总述）在 page-plan 走 fallback A 类——分类决策件仍保持 57 真节口径；
- E/F 类矢量图型数据较粗（三级标题/岗位句），阶段 4.2 任务单字段细化后可增强；
- KPI 抽取对 B 类节命中率低（S012 仅 1 条）——4.2 任务单字段+4.5 VLM 回读后复评。

阶段 6.1 已完成（2026-10-21）：103 页既有全图已回填为已发布候选；Web 支持框选局部改字；
S030_p1“开控→开挖”已走完“候选→人工复核→批准发布”，DRAFT PDF 复验通过。

2026-10-21 局部改字交互与合成升级：
- 普通看图改为全屏查看器（滚轮缩放、拖动平移、适应/100%/200%/400%），点击任意页图即可打开；
- 局部改字改为全屏编辑器，默认 100% 原像素，带 5× 放大镜、8 个手柄、框内拖动、方向键 1px/Shift 10px、坐标输入；
- 合成改为“补丁按模型原始坐标贴回 + 补丁边缘羽化 + 用模型返回图的背景环带做色彩对齐”；
- 新增 `inside-change` 门禁：框内零变化直接 block，变化过少 warn；
- 同一页的生成/改字加页级锁，避免并发分配同一候选版本并互相覆盖；
- 从“已发布”栏改字时，审计正确标注 `base_kind=published`。

**OCR 乱码检测（待办，不阻塞当前人工流程）**
- 目标：RapidOCR 全页识别 + 与原文模糊比对，只产出“可疑框 + 期望字符”，不自动改；
- 关键结论：OCR 置信度不可靠（真实错字“开控”置信度 0.99）；Gemini/VLM 会按语义把“开控”读成“开挖”，不能做检测或验证；
- 必须人工看 3–5x 放大图确认后再进局部改字；RapidOCR 仅作为可选 extra，不加入默认依赖；
- 真实样例已迁入 `projects/anke-35kv/memory/evidence/local-edit/`（zoom_kai、verify_v5_*）；其结论见 durable memory `know.fullimage.ocr-confidence-not-proof`。

## 4. 验收证据存档

| 阶段 | 证据 |
|---|---|
| 0 | 16 tests OK；build 冒烟 116 页/24.1MB/100% 覆盖；output 500MB→71MB |
| 1 | 20 tests OK；issues 初筛命中 7 条（60厚缺单位×2 / 复杂故障 1h vs 8h / 60日历天无日期锚点 / 模糊承诺×3）；`output/audit-layout-baseline.json`（报表式 116 页超标 70） |
| 2 | 29 tests OK（+9 竖版断言）；golden page 三门禁全过（layout pass / hidden 4/4 / coverage 100%）；`output/_样板-S003.pdf` + contact sheet；**像素级可复现**（两次重渲染 + golden 三者 100dpi 像素 sha256 一致 `13974cc5…`） |
| 3 | 45 tests OK（+16 classify golden/门禁/LLM mock）；3.0 原表初判 47 表（章分布 12/8/7/4/11/1/3/1 与人工 md 逐章一致，keep 35 / redraw 12）；3.1 分类子系统双产物（57 节门禁 PASS / P2 跨章同对象 12 组含电缆头×3 / golden 集：四章三节五块、三章四节三块 / md↔json round-trip 一致）；3.3 page-plan 58 节 97 预算页 0 溢出（S003 冻结件逐字节保留，golden 像素哈希 MATCH）；3.4/3.5 8 类模板 + 4 矢量图型 + 5 组件基线（视觉抽检合格）；3.6 build_v13 整册 **114 页 / 9.5MB / 13s，乱度门禁 0 超标，v13 覆盖率 100%（0/1919），S012 隐藏层 4/4 可搜索**；legacy 冒烟不受影响；签认点③产物：`output/_签认-S012/S018/S019/S029/S054.pdf` |
| 6.1 | 回填 103/103 页为 published（0 missing/stale/blocked，`can_build_final=true`）；S030_p1 局部改字 v0005（“开控”→“开挖”）框外像素 `max=0/changed=0`，批准发布后 `published/S030_p1.png` 与 v0005 字节一致；操作手册 `docs/局部改字操作手册.md` |
| M1/M2 | **180 tests OK**；TOML durable memory + JSONL run events + SQLite FTS 已接 CLI/Web；human-signed 全内容验签（含签认人/有效性/证据/依赖）、跨进程 ledger/episode/record 锁、发布/回滚失败与进程硬崩恢复、stale 提示词阻断、同源 POST 门禁、正式 gate 强制 published SHA 校验均有回归测试；局部改字候选豁免整页 prompt 比对但保持基线 stale 语义；103 个 current published 独立于候选；历史候选裁剪后 S030 next=v6、S058 next=v3；S058 v2 待审保留；清理 manifest/result 位于 `projects/anke-35kv/memory/manifests/`；work 约 2.9GB→1.17GB，output 约 2.48GB→0.29GB |

## 5. 踩坑记录（新 agent 必读，避免重犯）

1. **PDF Tr 状态泄漏**：reportlab TextObject 的 `setTextRenderMode(3)` 会残留在内容流，使其后所有文本隐形且填充色重置为黑。hidden_text 必须 `saveState/restoreState` 包裹（见 `bidviz_vector.hidden_text`）。
2. **wrap 参数序**：`bv.wrap(text, size, max_w)`——第二参是字号不是宽度，写反会每行一字。
3. **fitz span 字段**：text('dict') 的 span 用 `bbox`/`origin`，没有 `c` 键；空 spans（image block）要跳过否则 `min()` 崩。
4. **Sheet.image cover**：必须"放大→按 anchor 裁切"，只 resize 不 crop 会拉伸变形；旧素材标题白带用 `crop_top` 切。
5. **x0 门禁语义**：左边界带数对多栏组件版式=栏数（设计决策），阈值 28 系 T-B1 实测 26 标定（±3pt 聚类后）；散乱约束主要靠 fill/colors。改阈值须在 tokens.toml 注释留标定来源。
6. **load_project 签名**：`build_e1.load_project` 保持 2-tuple；issues 走 `load_issues(root)`，勿再往返回值里塞第三元素（4 处调用方会崩）。
7. **Windows 跨盘**：tempfile 在 C 盘、项目在 D 盘，`Path.replace` 跨盘失败，用 `shutil.copy`。
8. **字体_glyph**：msyh 不支持 ◷◎▦ 等几何符（渲染为 \x00 豆腐块）；图标用 CJK 单字或 ①②③。
9. **内容审计口径**：页上文字 ⊆ 原文且可定位；提炼行必须是**原文逐字前缀**，改写句会 MISS（图标表行曾犯）。
10. **py-modules 约定**：新增 src/*.py 必须同步 `pyproject.toml` py-modules，否则安装态 import 失败。
11. **同进程禁混新旧画布**：`build_e1` 在 import 时快照 `bv.PAGE_W/PAGE_H`（模块级）。若同进程先调 `bv.set_page('A3','portrait')`（如 render_s003）再 import/build 旧体系，旧体系会拿到竖版快照而错乱。样板渲染与旧体系 build 必须分进程跑；测试里 set_page 后必须 finally 还原 landscape。
12. **gitignore 白名单顺序**：gitignore 最后匹配获胜。`projects/*/work|output/*` 忽略后，决策件/golden 产物白名单的 `!` 规则必须放在 `_*.png`/`_*.pdf` 之后才生效；新增白名单条目时注意位置（2026-10-16 曾因此漏入库）。
13. **T-B1 双表通道**：阶段 2 的 T-B1 只画 `icon_table_*` 字段；阶段 3 起 page-plan 的表走 `tables` 字段。`compose_page_tb1` 必须保留 elif 回退（无 icon_table 时画 tables[0]），否则 13 个 T-B1 节首页第一张表整表丢失（2026-10-17 两次复发：整册覆盖率 94.8%→100% 的根因）。
14. **估算与实画同口径**：`data_table_height`（page-plan 预算）与 `Sheet.table`（渲染）必须同字号/行距/列宽算法；改任一侧须同步另一侧，否则预算页数与实际页数漂移。
15. **fitz get_text 只取页内**：画到 mediabox 外的文本（表溢出页底继续画）不会被 get_text 提取——渲染器必须用 max_h 截断并把剩余行转续页（T-B1 v2 的 `_tb1_remainder` 机制），不要依赖"画了就在"。
16. **模板重构先跑覆盖率**：改 compose 函数后必须立刻 `python src/build_v13.py` 看 v13 覆盖率是否仍 100%——v2 重构曾两次静默丢内容（#13 elif 丢失、#15 表画到页脚外），门禁和乱度审计都不查内容缺失，只有覆盖率查。
17. **edits 会重绘整页**：api7900 `/images/edits` 对整页 mask 返回整张画布，diff bbox=全图；裁剪 mask 时遮罩外返回黑色。**永远只合成遮罩区域**，不能整页替换。
18. **局部改字裁剪最小 1024×512**：实测 500×90 直接 400；返回尺寸可能是请求的 1.125 倍，必须 resize 回裁剪尺寸再合成。
19. **遮罩内还有黑边**：模型有时在 mask 内再留一圈黑色/白色边，不能直接按 mask 粘贴；用非黑内容外接框（`_patch_bbox`）再内缩 2px 去掉模型画的细边框。
20. **深底白字不能只用 multiply**：白字/深底与白底深字要统一用“遮罩内侧环带中位数 + 背景扁平化”做色彩对齐；单用 multiply 会把白字变黑。
21. **回填哈希必须同口径**：回填、Web 候选、局部改字统一走 `bidviz_fullimage.page_context()`；否则 `ensure_page` 会把 103 页全部标 stale，正式汇编被阻断。
22. **版本竞态**：`next_candidate_version` 在生成前分配，注册可能被并发占用；局部改字任务在注册冲突时要换下一个版本并保留已生成图片，不能直接丢 job。Web 端已加“页级锁”串行同一页的生成/改字。
23. **补丁按原坐标贴回**：`_patch_bbox` 的结果必须按裁剪窗偏移贴回原图，不能重新居中到用户框；重定心会让文字整体位移、基线漂移，尤其深底白字（bbox 只含字形）时最明显。
24. **羽化要作用在补丁边缘**：只羽化遮罩外沿（用户框外 3–6px）不会处理模型补丁自身边缘，框内会留下硬矩形接缝；必须在贴补丁时对补丁 alpha 做腐蚀/高斯羽化。
25. **色彩对齐的环带必须取模型输出**：拿原图环带做统计，`original/edited` 是同一份像素，LUT 退化成恒等+中位数压平，等于没校色。应从 `raw` 的“补丁外、遮罩内”环带取样；补丁占满遮罩时退回补丁内沿，并跳过近黑环带。

## 6. HTML 混合备选引擎（engine=html）

- 入口：`bidviz generate <项目> --engine html --all`（局部插图）→ `bidviz build <项目> --engine html`（汇编）→ `bidviz pages <项目> --engine html`（可选 PNG）。
- 结构：`work/page-plan.json` 的精简内容 + `work/figure-plan.json` 每节 1–2 张局部图；文字/表格/页眉页脚/页码全部 HTML/CSS，Playwright 驱动 Chromium 输出矢量 PDF。
- 产物：`output/施工组织设计-图册-A3-html.pdf`；中间件 `work/html/album.html`、`work/html-pages.json`、`work/illustrations/`、`work/illustrations-ledger.json`。
- 依赖：`pip install "playwright==1.55.0"`；本机已有 Chromium 缓存时无需再下载。`bidviz.toml [html]` 可配 browser/dpi/emit_png/table_cell_max。
- 当前验证：58 节 / 117 页 / 文本覆盖 100% / 0 溢出 / 无空白页；现有 fullimage、v13 产物不受影响。
- 限制：page-plan 是精简策展，文字量少于 fullimage 全图路线；page-plan 里的表格策展质量（如“材料与设备”左列）仍需继续优化；插图缺失时 PDF 不占位，只跳过并在构建结果的 `missing_figures` 里报告。

## 7. 给新会话的首条提示词模板

> 读取 `d:\project\bidviz\docs\PROGRESS.md` 并按其 §1 顺序加载上下文；跑 §2 验证命令确认状态全绿；
> 然后从 §3 领取阶段 3 任务，按 `docs/重构任务规划-V1.3.md` 的阶段 3 明细执行。
> 约束：不修改 frozen.json 已签认件；组件只引用 tokens.toml；新增 src 模块同步 py-modules；
> 每完成一个子任务更新 PROGRESS.md §0/§4；遇到 §5 踩坑场景先读对应记录。
