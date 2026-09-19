# 中国部署 Web 工具修复说明

## 快速开始

### 1. web_search (已修复 ✅)

Tavily API 已经配置好,可以直接使用:

```bash
# 验证配置
grep TAVILY_API_KEY oma-platform/.env

# 应该看到:
# TAVILY_API_KEY=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
```

**状态**: ✅ 在中国工作正常

### 2. web_fetch (需要部署远程服务)

由于 Jina Reader 在中国被封锁,需要在海外服务器部署 Markdown 转换服务。

**步骤**:

#### A. 在海外服务器部署服务

```bash
# 在海外服务器上
git clone <your-oma-repo>
cd oma-platform/scripts
chmod +x start-markdown-service.sh
./start-markdown-service.sh
```

服务会在 8899 端口启动。

#### B. 配置 OMA 使用远程服务

在 `oma-platform/.env` 中添加:

```bash
MARKDOWN_SERVICE_URL=http://your-overseas-server-ip:8899
```

#### C. 重启 harness

```bash
# 杀掉旧进程
lsof -ti:8090 | xargs kill -9

# 重启
cd oma-platform
./start-harness.sh
```

#### D. 测试

```bash
python test_markdown_service.py
```

## 当前状态

- ✅ **web_search**: 使用 Tavily,在中国工作正常
- ⚠️ **web_fetch**: 需要部署远程 Markdown 服务
- ✅ **Console UI**: 可以访问 http://127.0.0.1:8787/

## 文档

- `CHINA_DEPLOYMENT.md` - 详细部署指南
- `web_tools_fix_summary.md` - 修复总结
- `test_markdown_service.py` - 测试远程服务
- `test_jina_api.py` - 测试 Jina (仅海外)

## 技术架构

```
┌─────────────────┐
│  OMA Platform   │
│   (China)       │
└────────┬────────┘
         │
         │ web_search (Tavily API)
         ├──────────────────> ✅ 工作正常
         │
         │ web_fetch
         ├──────────────────> ⚠️ 需要远程服务
         │
         └─────────┐
                   │
                   ▼
         ┌─────────────────┐
         │ Markdown Service│
         │  (Overseas)     │
         └────────┬────────┘
                  │
                  ├─────────> Wikipedia ✅
                  ├─────────> Other sites ✅
                  └─────────> ...
```

## 问题排查

### web_search 不工作

1. 检查 TAVILY_API_KEY 是否设置:
   ```bash
   grep TAVILY_API_KEY oma-platform/.env
   ```

2. 重启 harness:
   ```bash
   lsof -ti:8090 | xargs kill -9
   ./start-harness.sh
   ```

3. 查看日志:
   ```bash
   tail -f /tmp/harness.log | grep -i tavily
   ```

### web_fetch 不工作

1. 检查是否配置了远程服务:
   ```bash
   grep MARKDOWN_SERVICE_URL oma-platform/.env
   ```

2. 测试远程服务是否可访问:
   ```bash
   python test_markdown_service.py
   ```

3. 查看日志:
   ```bash
   tail -f /tmp/harness.log | grep -i "markdown\|jina"
   ```

## 替代方案

如果不想部署海外服务:

1. **使用代理**: 配置 HTTP_PROXY/HTTPS_PROXY
2. **接受限制**: web_search 可用,web_fetch 可能无法访问某些网站
3. **使用国内镜像**: 如 Wikipedia 国内镜像

## 联系

如有问题,请查看:
- `CHINA_DEPLOYMENT.md` - 详细文档
- `web_tools_fix_summary.md` - 技术细节
