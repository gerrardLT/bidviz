---
name: bid-visual-pdf
description: 将招投标施工组织设计的 Word/PDF 转成可编辑、可溯源的 A3 横向工程图册。适用于施工组织设计可视化、标书配图、施工工艺信息图、按节生成主视觉、工程图表、完整正文 PDF、局部重生与重新汇编。
compatibility: Python 3.11+；Word 输入需要 Windows、Microsoft Word 与 pywin32；在线生图需要 OpenAI-compatible images/generations 接口。
---

# BidViz 施工组织设计图册

代码库根目录执行 CLI。先读 `README.md`，再运行：

```powershell
bidviz list
bidviz status <project>
bidviz ingest <project>
bidviz generate <project> --chapter 第一章
bidviz build <project>
bidviz serve <project>
```

## 工作流

1. 用 `bidviz init <project>` 建项目。
2. 将正式 Word/PDF 放入 `projects/<project>/inputs/`。
3. 在 `bidviz.toml` 填项目名、电压等级、规范、模型和图片目录。
4. 运行 `bidviz ingest <project>`，检查章、节及文本块。
5. **先按章或单节出样，不得未经用户确认直接全量生图。**
6. 样张确认后运行 `bidviz generate <project> --all`。
7. 用 `bidviz serve <project>` 编辑标题、正文和提示词；单节重生。
8. 运行 `bidviz build <project>`；检查 PDF 覆盖率和工程内容。

## 安全与事实边界

- API key 只来自 `.env`、环境变量、`~/.bidviz/providers.toml` 或 PiDeck 兼容回退；不得写入代码和项目配置。
- 原始 `blocks.json` 不修改；用户编辑存入 `work/edits.json`。
- 图片只承担技术可视化和结构化摘要；完整正文由矢量文字页承载。
- 无 CAD/BIM/现场照片时，图只称通用工程示意，不暗示真实坐标或现场。
- 图中文字、参数、工艺必须来自原文；不能验证时宁缺勿编。
- 模型生成成功不等于工程正确，终稿须由工程师逐页复核。

## 交付

推荐交付整个 Git 仓库或版本压缩包，而非只复制 Skill。Skill 仅教 agent 如何调用；CLI/Web 才是确定性实现。其他 harness/IDE 只需能运行 Python 命令，不需适配专属插件。
