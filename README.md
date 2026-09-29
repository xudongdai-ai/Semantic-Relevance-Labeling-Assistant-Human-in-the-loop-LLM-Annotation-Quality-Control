# Semantic Relevance Labeling Assistant

Human-in-the-loop semantic relevance annotation and quality-control toolkit.

Python 3.10+ · Human-in-the-loop · AI-assisted · CLI · Zero third-party dependencies

一个用于 `semantic_relevance` 项目的 Python 命令行工具。它读取项目导出 JSON（或在线获取导出），把 Query 的 `llm_grade_final` 整理成本人任务的预览计划，并把一致度不足的任务单独标为需复核。

默认只生成本地预览，不登录、不提交。真正提交必须同时指定 `--submit` 和 `--acknowledge-ai-assisted`。

## 环境

- Python 3.10 或更高版本
- Python 标准库，无第三方依赖
- 在线导出或提交需要与本地路由映射及数据模型兼容的标注服务账号
- 在线操作默认要求 HTTPS；旧服务只提供 HTTP 时，可显式添加 `--allow-insecure-http`，但凭据会以明文传输
- 在线连接需要按你有权访问的服务配置本地路由映射

## 本地导出预览

```bash
python3 semantic_relevance_assistant.py \
  --export ./semantic_relevance_export.json \
  --project-id 123 \
  --email you@example.com
```

按模型一致比例筛选候选处理计划，例如仅保留一致度至少 100% 的任务：

```bash
python3 semantic_relevance_assistant.py \
  --export ./semantic_relevance_export.json \
  --project-id 123 \
  --email you@example.com \
  --min-agreement 1
```

输出 `semantic_relevance_plan.json`，其中 `ready` 是符合筛选条件的任务，`needs_review` 是需要人工复核的任务。计划包含模型候选等级、一致度和待提交答案，提交前请检查。

## 在线导出

公开包不包含服务专属 API 路径。在线导出或提交前，复制 `examples/api_routes.example.json` 为当前目录下的 `annotation_routes.json`，按你有权访问的服务填写路由模板。该本地文件已加入 `.gitignore`。

```bash
python3 semantic_relevance_assistant.py \
  --online-export \
  --base-url https://label.example.com \
  --routes ./annotation_routes.json \
  --project-id 123 \
  --email you@example.com
```

在线导出也只生成本地预览计划。需要登录时，密码会通过终端隐藏提示读取；也可以由安全的启动环境预先提供 `FOLK_LABEL_PASSWORD`，不要把密码直接写进命令或 shell 历史。

## 明确提交

检查预览计划后，显式启用提交和 AI 辅助标注确认：

```bash
python3 semantic_relevance_assistant.py \
  --export ./semantic_relevance_export.json \
  --project-id 123 \
  --email you@example.com \
  --base-url https://label.example.com \
  --routes ./annotation_routes.json \
  --submit \
  --acknowledge-ai-assisted
```

提交时工具会校验登录身份、项目 ID 与类型、本人分配状态、在线 Query ID，并在每条提交后回读结果。已提交的任务默认跳过。`--resubmit-from-log PATH` 仅用于按指定日志重新处理此前已提交的本人任务，请在确认计划后使用。

## 输入数据

导出 JSON 至少需要包含 `project.id`、`project.task_type`、`items[]`、每项的本人 `assignments[]`，以及 `semantic_context.pairs[]` 中的 `pair_id` 和 `llm_grade_final`。`llm_grades` 用于计算模型一致度。可查看 [合成示例](examples/semantic_export.sample.json)；示例数据完全虚构。

更多结构和请求时序见 [架构说明](docs/architecture.md) 与 [集成边界](docs/integration-boundary.md)。

## 测试

测试只使用合成数据，不连接标注服务：

```bash
python3 -m unittest discover -s tests -v
```

## 数据和凭据

- 不要把真实项目导出、预览计划、提交日志或账号密码上传到公开仓库。
- 输出计划可能含歌曲标题、歌词和模型判断；本地 JSON 文件已加入 `.gitignore`。
- 密码只从隐藏提示或指定环境变量读取，不会被写入代码或提交载荷。
- 本仓库不包含真实服务器地址、服务专属路由、个人账号、真实项目 ID、任务数据或逐题答案。
- AI 等级只是辅助结果，需按项目规范人工审查。
