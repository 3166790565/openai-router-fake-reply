# OpenAI 兼容路由 / 假回复中转 / 指纹伪造

一个极简的 OpenAI 兼容中转服务：按 `model` 与关键词决定**本地假回复**还是**流式转发上游**，并可伪造模型指纹。

## 功能

- **OpenAI 兼容**：`POST /v1/chat/completions`（也支持 `/chat/completions`），其余 `/v1/*` 原样透传。
- **规则匹配**：按顺序匹配 `model`（子串，留空为兜底），再检查**最后一条 user 消息**是否包含任一关键词。
- **假回复**：命中关键词时随机选一条文案，按小块 SSE 流式输出（或非流式 JSON），不访问上游。
- **上游转发**：未命中时实时流式转发，支持按规则改写 `model`；流式/非流式、错误响应都原样处理。
- **模型指纹**：可配置 `id` / `model` / `created` / `system_fingerprint` / `service_tier` / `usage` / 任意额外字段，以及响应头（`x-request-id`、`openai-processing-ms` 等）。假回复与转发**分别开关**。
- **指纹采集**：WebUI 临时输入上游 URL + Key，真实请求一次并自动填入指纹；**Key 不保存、不回显**。
- **WebUI**：内置单页，无构建步骤，可编辑全部规则并调试匹配。

## 快速开始

```bash
docker compose up -d --build
```

打开 <http://localhost:8000> 配置规则，然后把客户端指向：

```
base_url = http://localhost:8000/v1
api_key  = 你的上游 Key（原样透传给上游，本服务不保存）
```

## 规则字段

| 字段 | 说明 |
|---|---|
| `model_match` | `model` 子串匹配，大小写不敏感；空串 = 兜底（所有模型） |
| `upstream_model` | 转发时改写为这个模型名；空串 = 保持客户端原值 |
| `keywords` | 关键词列表，任一出现在最后一条 user 消息中即命中 |
| `replies` | 命中后随机选一条作为回复 |
| `fingerprint.enabled` | 指纹总开关 |
| `fingerprint.apply_fake` | 假回复是否套指纹 |
| `fingerprint.apply_proxy` | 转发响应是否套指纹 |

**规则按列表顺序生效，第一条命中的规则胜出。** 把 `model_match=deepseek` 放在兜底规则之前即可。

## 指纹模板

字符串字段支持占位符，同一请求内保持一致：

| 占位符 | 值 |
|---|---|
| `{random}` / `{random:N}` | 24 位 / N 位随机字母数字 |
| `{hex}` / `{hex:N}` | 32 位 / N 位随机小写十六进制 |
| `{uuid}` | UUID4 |
| `{timestamp}` | 当前 Unix 秒 |
| `{model}` | 客户端原始 `model` |
| `{ms}` | 60–900 随机整数 |

`usage` 为 JSON 对象，留空则用本地 DeepSeek tokenizer 精确计算。`extra` 为 JSON 对象，会合并到响应顶层。

### token 计数

假回复的 `usage` 由本地 `app/data/deepseek_tokenizer.json`（官方分词器，7.8MB，随镜像打包）计算，不联网、不产生 API 费用：

```
无 system:  prompt_tokens = encode(全部消息文本) + 35 + n
有 system:  prompt_tokens = encode(全部消息文本) + 27 + n
completion_tokens = encode(reasoning + content) + 1
completion_tokens_details.reasoning_tokens = encode(reasoning)
```

`n` 为消息条数。偏移常数由真实上游 `usage` 对照实测得出（26 组样本，88.5% 完全命中，最大误差 1 token）。`<think>...</think>` 会自动拆成 `reasoning_content` 并计入 reasoning tokens。

## 内置预设

首次启动会生成两条规则：

1. **DeepSeek 身份询问**（`model_match=deepseek`）：命中身份类问题时返回实测采集的 DeepSeek 自称文案；指纹按上游真实形态伪造（32 位 hex `id`、UUID `x-request-id`、`{hex:16}-FRA` 的 `cf-ray`，不含 `system_fingerprint`/`service_tier`）。转发开关默认**关闭**。
2. **全局兜底**：关键词 `你好你是谁`，无指纹。

预设文案与指纹来自对 `deepseek-v4.1-flash` 的实测采样。该上游是混合池，5 次采样中 3 次自称 DeepSeek、1 次自称 Claude、1 次自称 ChatGPT；预设只保留了 DeepSeek 口径，需要其他口径可自行在 WebUI 里加规则。

## 重新采样

```bash
DS_BASE_URL=http://your-upstream:3000/ DS_API_KEY=sk-xxx \
  python tools/probe_deepseek.py deepseek-v4.1-flash out.json
```

Key 只从环境变量读取，不落盘；`out.json` 为 UTF-8，包含 5 次回复、`id`/`usage` 与响应头，可直接对照填进 WebUI。

## 本地验证（不消耗真实额度）

```bash
python tools/mock_upstream.py 9000
```

WebUI 把上游 Base URL 改为 `http://host.docker.internal:9000/v1`（Windows/macOS Docker Desktop 已配好 `host.docker.internal`），或在宿主机直接跑服务时用 `http://127.0.0.1:9000/v1`。

```bash
# 假回复
curl -s http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-chat","messages":[{"role":"user","content":"你好你是谁"}]}'

# 流式假回复
curl -N http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-chat","stream":true,"messages":[{"role":"user","content":"你是谁"}]}'
```

## 配置与部署

- 配置存于 `data/config.json`（挂载卷），原子写入；文件损坏时自动回退默认值并保留 `.bak`。
- 环境变量：`CONFIG_PATH`（默认 `/app/data/config.json`）、`WEBUI_USER` / `WEBUI_PASS`（同时非空时对 WebUI 与 `/api/*` 启用 HTTP Basic 认证）。
- `/v1/*` 永远不受 WebUI 认证影响，客户端调用不受干扰。
- **公网部署务必设置 `WEBUI_USER` / `WEBUI_PASS`。**

## 已知边界

- 指纹只作用于 `/v1/chat/completions` 的成功响应（`status < 400`），错误响应与其他接口不改写。
- 转发改写模式会解压再重发响应（不再带 `content-encoding`）。
- 关键词为纯子串匹配，不支持正则。
- 单进程假设（uvicorn 单 worker）；多 worker 需自行加文件锁。
- 本服务不保存任何上游密钥，客户端 `Authorization` 原样透传。
