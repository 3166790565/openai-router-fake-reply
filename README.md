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
| `upstream_model` | 转发时改写为这个模型名；空串 = 保持客户端原值。无 `routes` 时生效 |
| `keywords` | 关键词列表，任一出现在最后一条 user 消息中即命中 |
| `patterns` | 正则整句模板列表，优先于 `keywords` 匹配 |
| `match_max_length` | 消息超过此长度不造假（默认 40） |
| `match_head_chars` | 关键词必须出现在前 N 字内（默认 15） |
| `work_override` | 工作词黑名单，命中任一强制走上游 |
| `replies` | 命中后随机选一条作为回复 |
| `routes` | 掺水候选表，见下文 |
| `first_msg` | Agent 首条加速配置，见下文 |
| `fingerprint.enabled` | 指纹总开关 |
| `fingerprint.apply_fake` | 假回复是否套指纹 |
| `fingerprint.apply_proxy` | 转发响应是否套指纹 |

**规则按列表顺序生效，第一条命中的规则胜出。** 把 `model_match=deepseek` 放在兜底规则之前即可。

### 假回复匹配精度

假回复命中分三层，按优先级：

1. **正则整句模板** `patterns`：`^你是谁[？?]?$` 这种锚定整句的正则，最精准。无效正则自动跳过不影响服务。
2. **关键词** `keywords`：子串匹配，但受三重闸门约束——消息长度 ≤ `match_max_length`、关键词出现在前 `match_head_chars` 字内、不含 `work_override` 黑名单词。
3. **工作词黑名单** `work_override`：命中任一词（如"代码""翻译""角色"）强制走上游，防止正常对话被误判为身份询问。

### 掺水路由 `routes`

按权重随机把请求转发到不同模型，客户端无感知（配合指纹 `model="{model}"` 掩盖）：

```json
"routes": [
  { "weight": 70, "upstream_model": "" },
  { "weight": 20, "upstream_model": "deepseek-chat" },
  { "weight": 10, "upstream_model": "glm-5.3", "supports_multimodal": false, "body_override": {"temperature": 0.6} }
]
```

| 字段 | 说明 |
|---|---|
| `weight` | 权重，相对值，自动归一化 |
| `upstream_model` | 目标模型名；空串 = 保持客户端原模型（不掺水档） |
| `supports_multimodal` | 是否支持多模态；`false` 时含图片的请求自动跳过此候选 |
| `body_override` | 转发前合并进请求体，用于需要固定参数的模型（如 Kimi K2 的 `temperature`） |

多模态请求会从候选中排除 `supports_multimodal=false` 的模型，从剩余候选中重新抽签。全部排除则保持原模型。

### Agent 首条加速 `first_msg`

检测到 Claude Code / Codex 等 Agent 工具的首条预热消息时，用固定快模型转发，不参与掺水抽签：

```json
"first_msg": {
  "enabled": true,
  "fixed_model": "deepseek-v4.1-flash",
  "detect_ua": ["claude-cli", "codex"],
  "detect_system": ["You are Claude Code"],
  "max_user_len": 20
}
```

判定条件（全部满足）：`enabled=true` 且 `fixed_model` 非空 → UA 或 system prompt 任一命中 → 只有一条 user 消息 → 消息长度 ≤ `max_user_len`。命中假回复关键词时仍优先走假回复。

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

## 请求日志

每条 `/v1/chat/completions` 请求自动记录决策链路，存于 `data/logs/requests-YYYY-MM-DD.jsonl`（JSON Lines 按日分文件）：

- **分支**：`fake`（假回复）/ `first_msg`（首条加速）/ `route`（掺水）/ `proxy`（直接转发）
- **假回复细节**：命中方式（`pattern`/`keyword`/`work_override`）、回复内容摘要
- **掺水细节**：实际抽中的模型、权重、因多模态被排除的候选列表
- **通用字段**：用户消息摘要、耗时、状态码、token 用量

日志字段截断（user 100 字、reply 200 字），异步写入不阻塞响应。`log_enabled` 控制总开关，`log_retention_days`（默认 7）控制保留天数，后台任务每小时清理过期文件。WebUI 日志页支持按分支/规则/日期过滤、分页浏览、点击展开详情。

## 配置与部署

- 配置存于 `data/config.json`（挂载卷），原子写入；文件损坏时自动回退默认值并保留 `.bak`。
- 环境变量：`CONFIG_PATH`（默认 `/app/data/config.json`）、`WEBUI_USER` / `WEBUI_PASS`（同时非空时对 WebUI 与 `/api/*` 启用 HTTP Basic 认证）。
- `/v1/*` 永远不受 WebUI 认证影响，客户端调用不受干扰。
- **公网部署务必设置 `WEBUI_USER` / `WEBUI_PASS`。**

### 服务器一键更新（智能重建）

`docker compose up -d` 在镜像已存在时不会重建，只重启旧镜像。项目提供 `deploy.sh`（Linux/Git Bash）与 `deploy.ps1`（Windows PowerShell）：

- 自动 `git fetch` 比对本地与远端 HEAD
- **有更新**：`git pull` + `docker compose up -d --build`（重建镜像，并把当前 git commit 注入镜像 label 与环境变量 `APP_GIT_COMMIT`）
- **无更新**：仅 `docker compose up -d --no-build`（秒级启动，不重建）

```bash
# Linux / Git Bash
./deploy.sh

# Windows PowerShell
.\deploy.ps1
```

部署后可查容器对应的 git 版本：

```bash
docker inspect openai-router --format '{{ index .Config.Labels "git.commit" }}'
# 或进容器
docker exec openai-router env | grep APP_GIT
```

不想用脚本、确定要强制重建时，手动：

```bash
git pull && docker compose up -d --build
```

## 已知边界

- 指纹只作用于 `/v1/chat/completions` 的成功响应（`status < 400`），错误响应与其他接口不改写。
- 转发改写模式会解压再重发响应（不再带 `content-encoding`）。
- 单进程假设（uvicorn 单 worker）；多 worker 需自行加文件锁。
- 本服务不保存任何上游密钥，客户端 `Authorization` 原样透传。
- 日志文件写入为异步追加，极端并发下可能有行间交错（不影响 JSON 解析）。
