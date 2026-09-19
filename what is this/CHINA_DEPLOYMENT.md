# 在中国部署 OMA 平台的 Web 工具

## 问题

在中国部署 OMA 平台时,web_search 和 web_fetch 工具面临以下问题:

1. **web_search**: DuckDuckGo 被 GFW 封锁,无法访问
2. **web_fetch**: 
   - Wikipedia 等国外网站被 GFW 封锁
   - Jina Reader (r.jina.ai) 也被 GFW 封锁

## 解决方案

### 方案 1: Tavily API (web_search) ✅ 已实现

Tavily 是一个搜索 API 服务,在中国可以访问。

**配置**:
```bash
# 在 .env 文件中添加
TAVILY_API_KEY=tvly-your-key-here
```

获取 API key: https://tavily.com/

**工作原理**:
- 当设置了 `TAVILY_API_KEY` 时,自动使用 Tavily 作为 web_search 后端
- 修改了 `harness/oma_adapter/web_search/runtime.py` 中的 `resolve_search_backend()` 函数

### 方案 2: 远程 Markdown 服务 (web_fetch) ✅ 已实现

在海外服务器上部署一个 Markdown 转换服务,国内 OMA 平台调用该服务来抓取和转换网页。

**架构**:
```
[OMA Platform - China] --> [Markdown Service - Overseas] --> [Wikipedia/Other Sites]
```

**部署步骤**:

1. **在海外服务器上部署 Markdown 服务**:
```bash
# 在海外服务器上
git clone <oma-platform-repo>
cd oma-platform/scripts
pip install fastapi uvicorn httpx
python markdown_service.py
```

服务默认监听 8899 端口。

2. **配置 OMA 平台使用远程服务**:
```bash
# 在 .env 文件中添加
MARKDOWN_SERVICE_URL=http://your-overseas-server.com:8899
```

**工作原理**:
- web_fetch 首先尝试调用远程 Markdown 服务
- 如果失败,尝试 Jina Reader
- 如果还是失败,使用直接 httpx 访问
- 最后使用 curl fallback

**Markdown 服务功能**:
- 接收 URL 参数
- 抓取网页内容
- 使用正则表达式将 HTML 转换为 Markdown
- 返回 Markdown 文本

### 方案 3: Jina Reader API (web_fetch) ❌ 在中国被封锁

Jina Reader 是一个网页转 Markdown 的 API 服务,但在中国被 GFW 封锁。

**配置** (仅适用于海外服务器):
```bash
# 在 .env 文件中添加
JINA_API_KEY=jina-your-key-here
```

获取 API key: https://jina.ai/reader

## 优先级

web_fetch 会按以下顺序尝试:

1. **远程 Markdown 服务** (如果配置了 `MARKDOWN_SERVICE_URL`)
2. **Jina Reader API** (如果配置了 `JINA_API_KEY`)
3. **直接 httpx 访问** + markdownify 转换
4. **curl fallback** (返回原始 HTML)

## 测试

运行测试脚本验证配置:

```bash
# 测试 Tavily web_search
python test_tavily.py

# 测试 Jina Reader (仅海外服务器)
python test_jina_api.py

# 测试远程 Markdown 服务
python test_markdown_service.py
```

## 总结

- ✅ **web_search**: 使用 Tavily API,在中国工作正常
- ⚠️ **web_fetch**: 需要在海外服务器部署 Markdown 服务
- ❌ **Jina Reader**: 在中国被封锁,仅适用于海外部署

## 替代方案

如果不想部署海外服务,可以考虑:

1. **使用代理服务**: 配置 HTTP/HTTPS 代理访问被封锁的网站
2. **接受限制**: web_search (Tavily) 可以工作,web_fetch 可能无法访问某些网站
3. **使用国内镜像**: 对于 Wikipedia,可以使用国内镜像站点

## 未来改进

- [ ] 支持多个 Markdown 服务后端(故障转移)
- [ ] 添加缓存层减少重复请求
- [ ] 支持更多网页提取策略(Readability, Trafilatura 等)
- [ ] 集成国内可访问的搜索 API(百度、搜狗等)
