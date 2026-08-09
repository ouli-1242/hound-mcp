<div align="center">

# 🐕 Hound

**让 AI 代理访问互联网。$0、两条命令、无需密钥。**

抓取 · 爬取 · 绕过反爬 · 读取 PDF（含扫描件）· 搜索网页

[MIT](LICENSE)

</div>

---

## 快速开始

```bash
git clone https://github.com/ouli-1242/hound-mcp.git
cd hound-mcp
pip install -e .[all]                    # 完整版：抓取 + 爬取 + 搜索 + PDF + OCR
playwright install chromium              # 反检测浏览器引擎
```

然后在任何 MCP 客户端中指向 `hound` 命令即可。无需参数、无需密钥、无需环境变量。

```bash
hound -v          # 查看版本 + 更新状态
hound -u          # 更新到最新版
```

---

## 8 个工具

| 工具 | 功能 |
|------|------|
| `smart_fetch` | 抓取任意 URL。HTTP 优先，被拦截时自动升级到反检测浏览器。支持批量、PDF（OCR）、`focus` 聚焦、`actions` 页面交互、`schema` 结构化提取（含 auto 模式）、分页。 |
| `smart_crawl` | 同域最佳优先爬取。Sitemap 模式、`search` 关键词过滤、内容自适应提取、时间 + token 预算控制。 |
| `smart_search` | 本地无密钥搜索。多后端并行、神经重排序、跨引擎共识、六信号排名。`fetch_content=true` 自动抓取 top3 全文。 |
| `screenshot` | 页面截图（多模态代理专用）。 |
| `parse` | 本地文件解析（.html/.docx/.xlsx/.csv → Markdown）。 |
| `feed_fetch` | 批量抓取 RSS/Atom feed 最新条目（更新日志 / 发布说明 / 博客追踪）。 |
| `resolve_url` | 解析 URL 最终地址（跟随重定向、不下载页面体）。 |
| `cache_clear` | 清除抓取缓存。 |

---

## 本地无密钥搜索

无需 API key、无需账户。`smart_search` 在本机并行运行无密钥后端，合并、去重、用本地 ONNX 交叉编码器排序。

- **无密钥后端**：bing（国内网可用）、duckduckgo、brave、yahoo、yandex（默认池；可加量 wikipedia、grokipedia；`engines=` 最多 9 个）
- **神经重排序**：`ms-marco-MiniLM-L-6-v2`，本地运行，$0
- **六信号排名**：共识 + 域名声誉 + 答案信号 + 标题相关 + URL 相关 + 来源类型
- **跨引擎共识**：多个独立索引返回的 URL 获得排名加成
- **来源分类**：每条结果标注 `source_type`（docs/paper/repo/forum/blog/news）
- **搜索+全文**：`fetch_content=true` 自动抓取 top3 结果内容（可配合 `fetch_schema` 做结构化提取）
- **熔断器**：被限速的引擎自动冷却 60 秒；状态跨重启持久化
- **过滤器**：`site`、`exclude_sites`、`location`、`language`、`freshness`、`page`
- **代理**：设置 `HOUND_SEARCH_PROXY`（也自动读取 `HTTPS_PROXY`/`ALL_PROXY`）

---

## 抓取 & 反爬

`smart_fetch` 先尝试 HTTP（~1 秒）。如果被拦截或检测到 JS 空壳页面，自动升级到 **Patchright** 隐身浏览器并求解 Cloudflare 验证。

- **TCP 预检**：2 秒连接检测；connection_refused/dns_failure 直接跳过两层（节省 30-60 秒）
- **Archive.org 回退**：所有层都失败时，尝试 Wayback Machine 最近快照
- **自适应超时**：按域名 EMA 追踪延迟，慢站自动延长超时
- **隐身引擎**：系统 Chrome TLS 指纹、JS 补丁（webdriver=undefined、canvas 噪声）、人类行为模拟、CF Turnstile 求解器
- **聚焦提取**：`focus="..."` 只返回 BM25 相关段落（减少 80%+ 上下文），标题下内容 1.5x 加成 + 表格/代码强制保留
- **结构化提取**：`schema={...}` CSS 选择器 + JSON-LD + 正则；`schema={"type":"auto"}` 自动检测表格/列表/元数据
- **批量结构化**：`urls=[...], schema={...}` 一次调用对多个 URL 并行提取统一 schema
- **页面交互**：`actions=[{click:...},{fill:...},{scroll:...}]`
- **智能缓存**：SQLite WAL 模式，坏内容永不缓存
- **分页**：超过 40KB 的内容自动分块，返回 `next_offset`

---

## 爬取

`smart_crawl` 按**最佳优先**顺序遍历同域链接（聚焦相关性 + 内容可能性评分）。

- **内容自适应**：文章 → trafilatura 主内容；列表 → 结构化链接列表；JS 空壳 → 诚实报告
- **Sitemap 模式**：`sitemap=true` 一次请求映射整站
- **URL 搜索**：`search="关键词"` 在发现的 URL 中按关键词过滤（配合 `discover_only=true` 快速定位大站特定页面）
- **浏览器自动升级**：首页是 JS 空壳时，后续页面自动使用隐身浏览器
- **瞬态错误重试**：timeout/connection_reset 的页面自动重试一次
- **预算控制**：`max_pages`、`max_depth`、`max_total_chars`、`deadline_ms`

---

## PDF + OCR

- 结构化 Markdown（表格、目录、`pages='1-5'` 子集提取）
- CID 损坏自动 OCR（学术论文中字体子集缺失的情况）
- 扫描件/纯图片 PDF 自动 OCR（rapidocr，纯 pip）
- `quality_score`（0-1）+ 加密 PDF 支持 `password` 参数

---

## 对比

| | **Hound** | Crawl4AI | Jina Reader | Firecrawl（免费版） |
|---|---|---|---|---|
| **价格** | $0 永久 | $0 | 免费但限速 | $0 自托管 / 1K 免费 |
| **本地运行** | 是 | 是 | 否 | 需要 Docker+Redis |
| **网页搜索** | 有（多后端） | **无** | 有 | **无** |
| **反爬** | 内置 | 有限 | 无 | 默认无 |
| **PDF + OCR** | 有 | 部分 | 有 | 云端付费 |
| **Agent 信号** | 有 | 无 | 无 | 无 |
| **MCP 原生** | 是 | 社区版 | 是 | 需自己搭 |

---

## 安装

> 本项目为本地自制项目，未发布到 PyPI，请从源码安装。

### 完整安装（含反爬浏览器）

```bash
git clone https://github.com/ouli-1242/hound-mcp.git
cd hound-mcp

# 普通安装（代码复制到 site-packages，源码目录可随意移动）
pip install .[all]
playwright install chromium      # 下载反检测浏览器引擎（~150MB）

# 开发模式（editable，改代码立即生效，源码目录不可移动）
pip install -e .[all]
playwright install chromium
```

> **浏览器依赖说明**：`[all]` 包含 patchright + playwright + browserforge，
> `playwright install chromium` 下载的浏览器用于 `smart_fetch` 的反爬升级层
> （JS 渲染 / Cloudflare 求解 / 截图）。**不装浏览器**时 hound 仍可工作——
> 走纯 HTTP 抓取 + 搜索，遇到 JS 壳/反爬页面会失败并提示（优雅降级）。

### 仅 HTTP + 搜索（无浏览器，精简）

```bash
pip install .
```

### 卸载

```bash
pip uninstall hound-mcp

# 可选：删除浏览器依赖（~150MB）
pip uninstall patchright playwright browserforge
playwright uninstall chromium

# 可选：删除运行时缓存 + 修复脚本
rd /s /q %USERPROFILE%\.hound        # Windows
rm -rf ~/.hound                       # Linux/Mac
```

### 环境变量

| 变量 | 用途 |
|------|------|
| `HOUND_SEARCH_PROXY` | 搜索引擎代理（也自动读取 `HTTPS_PROXY`/`ALL_PROXY`） |
| `HOUND_BROWSER_IDLE_TIMEOUT` | 浏览器空闲关闭时间（默认 300 秒，`0` = 永不关闭） |

### MCP 配置

在 Claude Code / Cursor / OpenCode 的 MCP 配置中添加：

```json
{ "mcpServers": { "hound": { "command": "hound" } } }
```

---

## 已知限制

| 限制 | 处理方式 |
|------|----------|
| DataDome / Akamai / 交互式 Turnstile | 无法绕过。`next_action` 会提示换源。 |
| 搜索引擎限速 | 多样性仲裁 + 熔断器兜底；重度使用设 `HOUND_SEARCH_PROXY`。 |
| 国内网（无 VPN）搜索 | 默认池含 bing / yandex（国内可达）；duckduckgo / brave / yahoo 需 VPN。 |
| 域名 DNS 解析内网复查 | 默认关闭（DNS 污染环境会误伤）；需严格 SSRF 保护时设 `HOUND_SSRF_DNS_RECHECK=1`。 |
| Bright Data SERP | 默认启用（随代码内置密钥，仅个人自用）；`HOUND_BRIGHTDATA_API_KEY=` 置空可禁用。 |
| 需要登录的网站 | 不支持（不在设计范围内）。 |
| YouTube | 只能获取少量文本。 |

---

**MIT 协议**
