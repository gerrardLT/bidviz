# AGENTS.md — bidviz 仓库工作约定

> 面向在本仓库工作的 AI 代理。项目背景、管线、命令详见 Obsidian 笔记（见下），本文件只收录**跨会话必须遵守**的环境与流程约定。

## 一、Obsidian Vault：当前项目配置

| 项 | 值 |
|---|---|
| Vault（唯一） | `D:\工作`（配置目录 `.obsidian\`；全局注册表 `%APPDATA%\obsidian\obsidian.json`，单 vault `open:true`、`"cli":true`） |
| REST API | `https://127.0.0.1:27124`（Local REST API 插件；**Obsidian 必须在运行**，否则 MCP 全部不可用） |
| 组织策略 | **单 vault + 项目文件夹**：`项目/<项目名>/`。禁止为单个项目另建 vault——wikilink 自动重命名、backlinks、全局搜索、Bases 只在单一 vault 内生效 |
| 本项目笔记 | `项目/bidviz/bidviz 总览.md`（索引/MOC）、`项目/bidviz/报表式排版规范.md`（版面红线） |
| 项目附件 | `项目/bidviz/附件/`（图版页示例.png、报表页示例.png = 成品第 3/4 页渲染）。**不要用 vault 默认附件位置（vault 根）** |
| 大文件红线 | 成品 PDF（24MB 级）等大文件**不进 vault**；笔记中用 `file:///d:/project/bidviz/...` 链接引用 |

## 二、笔记编写规范

- 正文朴素简洁；**必须带轻量 frontmatter**：`tags`（列表）、`项目`、`更新`（YYYY-MM-DD），可加 `仓库`。目的：`search_query` 按 tags/项目筛选、`.base` 聚合视图。
- 内链用 `[[Note]]`（不要对 vault 内笔记用 markdown 链接）；嵌入用 `![[名称.png|宽度]]`。
- 新建项目时同步建 `项目/<名>/` 文件夹与一篇 `<名> 总览.md`。

## 三、Agent 操作 Obsidian 的标准姿势

1. **主通道 = MCP `obsidian`（20 个工具）**。`obsidian` CLI 不可依赖：`Get-Command obsidian` 解析到 `D:\obsidian\Obsidian.com`（Electron 控制台桩，非独立 CLI；注册表虽有 `"cli":true`，未实测通过前不用）。
2. **读**：先 `vault_get_document_map`（标题树 + 块 ID + version 哈希）→ `vault_read` 带 `targetType/target/scope` 定向读；图片/PDF 用 `vault_read_binary`。
3. **写**：新文件 `vault_write`（自动建父目录、覆盖不警告，慎用）；**改已有笔记一律** `vault_patch` + `ifMatch=<version>`：
   - heading 的 `content` 层级是相对的（一个 `#` = 目标的直接子级），别自己数 `#`；
   - 重复标题/重复块 ID 的 marker 后缀（不可打印字符）必须从 document map **原样复制**，不能手打；
   - `within` 续写已有块（如 `\n- 新项` 追加列表）；`rejectIfContentPreexists:true` 让 append 幂等可重试。
4. **传附件**（`vault_get_upload_url` 流程，目标=精确文件路径而非目录）：

   ```powershell
   # 1) vault_get_upload_url { "path": "项目/bidviz/附件/<名>.png" } → 签名一次性 URL
   # 2) PUT（-k：localhost 自签证书；URL 必须整体加引号，含 & 符号）
   curl.exe -sk -X PUT -H "Content-Type: image/png" --data-binary "@<本地绝对路径>" "<签名URL>"
   # 3) 验证：vault_list 确认落盘 + Get-Item 字节数比对
   ```

   URL 一次性（成功即作废）、会过期、PUT 自动建父目录、覆盖同名不警告。
5. **查**：全文用 `search_simple`；结构化用 `search_query`（JsonLogic：`glob`/`regexp`/`missing`/`and`/`or`/`in`/比较运算）。示例——验本项目笔记的 wikilink 是否解析：
   `search_query {"and":[{"glob":["项目/bidviz/*",{"var":"path"}]},{"var":"links"}]}`
6. **联动**：写完用 `open_file`（`newLeaf:true` 开新面板）让用户直接看结果；事件订阅 `events_get_listener_url`（SSE；监听 frontmatter 变化用 `metadataCache changed`，别用 `vault modify`——后者触发时元数据还没重读）。
7. **写后必验**：`vault_list` 确认落盘 / 字节比对 / links 查询。走 MCP 写入后 tags/backlinks **立即可查**；绕过 MCP 直写 `D:\工作` 文件系统会导致 links/搜索延迟重扫——批量生成可直写，单篇编辑走 MCP。
