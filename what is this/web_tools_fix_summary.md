# Web 工具修复总结

## 问题背景

在中国部署 OMA 平台时,web_search 和 web_fetch 工具无法正常工作:
- **web_search**: DuckDuckGo 被 GFW 封锁
- **web_fetch**: Wikipedia 等国外网站被 GFW 封锁,Jina Reader 也被封锁

## 已完成的修复

### 1. ✅ Tavily web_search (已实现并测试)

**修改文件**:
- `oma-platform/harness/oma_adapter/web_search/runtime.py`
  - 修改 `resolve_search_backend()` 函数
  - 当设置了 `TAVILY_API_KEY` 时自动使用 Tavily 后端

**配置**:
```bash
# 在 oma-platform/.env 中添加
TAVILY_API_KEY=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
```

**测试结果**:
- ✅ web_search 正常工作
- ✅ 返回搜索结果(标题、URL、摘要)
- ✅ 可以在中国访问

### 2. ✅ 远程 Markdown 服务 (已实现,待部署)

**新增文件**:
- `oma-platform/scripts/markdown_service.py` - Markdown 转换服务
- `oma-platform/scripts/start-markdown-service.sh` - 启动脚本
- `oma-platform/harness/oma_adapter/web_fetch/core.py` - 添加远程服务支持
  - 新增 `_remote_markdown_service_fetch()` 函数
  - 修改 `_fetch_to_markdown()` 函数添加远程服务优先级

**架构**:
```
[OMA - 中国] --HTTP POST--> [Markdown Service - 海外] --HTTP GET--> [Wikipedia等]
```

**部署步骤**:

1. **在海外服务器部署服务**:
```bash
# 在海外服务器上
cd oma-platform/scripts
./start-markdown-service.sh
```

2. **配置 OMA 使用远程服务**:
```bash
# 在 oma-platform/.env 中添加
MARKDOWN_SERVICE_URL=http://your-overseas-server.com:8899
```

**测试**:
```bash
# 测试远程服务
python test_markdown_service.py
```

### 3. ⚠️ Jina Reader (已实现,但在中国被封锁)

**修改文件**:
- `oma-platform/harness/oma_adapter/web_fetch/core.py`
  - 新增 `_jina_reader_fetch()` 函数
  - 添加了调试日志

**配置**:
```bash
# 在 oma-platform/.env 中添加
JINA_API_KEY=jina_91aea80b9adb44d3b95f9218c7a8ec9dTKitm7fV85nJHP-NuVEwNldJwVxh
```

**状态**: 
- ❌ r.jina.ai 在中国被 GFW 封锁
- ✅ 适用于海外服务器部署

## web_fetch 优先级

现在 web_fetch 会按以下顺序尝试:

1. **远程 Markdown 服务** (如果配置了 `MARKDOWN_SERVICE_URL`)
2. **Jina Reader API** (如果配置了 `JINA_API_KEY`,但在中国被封锁)
3. **直接 httpx 访问** + markdownify 转换
4. **curl fallback** (返回原始 HTML)

## 重启服务

修改代码后需要重启 harness:

```bash
# 杀掉旧进程
lsof -ti:8090 | xargs kill -9

# 重启 harness
cd oma-platform
./start-harness.sh
```

## 测试结果

### GAIA Benchmark 测试 (L1 第1题)

**修复前**:
- Defects: `tool_web_fetch_error`, `tool_web_search_error`, `timeout`, `no_answer_extracted`
- web_search 返回 "Error:"
- web_fetch 返回 "[NOTE: markdown extraction unavailable...]"

**修复后** (仅 Tavily):
- Defects: `timeout`, `no_answer_extracted`
- ✅ web_search 正常工作 (Tavily)
- ❌ web_fetch 仍然失败 (需要远程服务)

## 下一步

### 立即可用
- ✅ web_search 使用 Tavily API,在中国工作正常
- ✅ Console UI 可以访问 http://127.0.0.1:8787/

### 需要部署
- ⚠️ 在海外服务器部署 Markdown 服务
- ⚠️ 配置 `MARKDOWN_SERVICE_URL` 环境变量
- ⚠️ 测试 web_fetch 功能

### 文档
- 📄 `CHINA_DEPLOYMENT.md` - 详细的部署指南
- 📄 `test_markdown_service.py` - 测试脚本
- 📄 `oma-platform/scripts/markdown_service.py` - Markdown 服务代码

## 文件清单

**修改的文件**:
1. `oma-platform/.env` - 添加 API keys
2. `oma-platform/harness/oma_adapter/web_search/runtime.py` - Tavily 支持
3. `oma-platform/harness/oma_adapter/web_fetch/core.py` - 远程服务和 Jina 支持

**新增的文件**:
1. `oma-platform/scripts/markdown_service.py` - Markdown 转换服务
2. `oma-platform/scripts/start-markdown-service.sh` - 启动脚本
3. `CHINA_DEPLOYMENT.md` - 部署指南
4. `test_markdown_service.py` - 测试脚本
5. `test_jina_api.py` - Jina 测试脚本
6. `web_tools_fix_summary.md` - 本文档

## 技术细节

### Tavily 集成

**代码位置**: `oma-platform/harness/oma_adapter/web_search/runtime.py`

```python
def resolve_search_backend(agent: AgentSnapshot) -> str:
    """Mirror harness/tools.ts: Tavily type overrides DDG when present.

    Also use Tavily if TAVILY_API_KEY is set (for China deployment where
    DuckDuckGo is blocked by GFW).
    """
    if "web_search_tavily" in _tool_types(agent):
        return "tavily"
    # Fallback: if TAVILY_API_KEY is set, prefer Tavily over DDG
    if os.environ.get("TAVILY_API_KEY"):
        return "tavily"
    return "ddg"
```

### 远程 Markdown 服务集成

**代码位置**: `oma-platform/harness/oma_adapter/web_fetch/core.py`

```python
async def _remote_markdown_service_fetch(url: str, cap: int) -> str | None:
    """Fetch URL via remote Markdown conversion service."""
    service_url = os.environ.get("MARKDOWN_SERVICE_URL")
    if not service_url:
        return None

    endpoint = f"{service_url.rstrip('/')}/convert"
    payload = {"url": url, "max_length": cap}
    
    async with httpx.AsyncClient(...) as client:
        response = await client.post(endpoint, json=payload)
        return response.text
```

## 总结

✅ **已完成**: Tavily web_search 在中国工作正常
⚠️ **待部署**: 远程 Markdown 服务需要在海外服务器部署
❌ **不可用**: Jina Reader 在中国被封锁

**当前状态**: web_search 可用,web_fetch 需要部署远程服务后才能正常工作。
