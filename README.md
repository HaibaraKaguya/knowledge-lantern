# Knowledge Lantern

Knowledge Lantern（知识灯塔）是一个面向企业内部知识场景的本地化问答原型，当前聚焦于财务与部门资料的检索、问答和治理。

项目的核心约束是：

- 权限先于检索：未授权的内容不会进入候选集、模型上下文或返回的证据。
- 证据可追溯：问答结果包含检索词、来源、请求 ID 和耗时信息。
- 动作可审计：资料提交、审核、检索决策和会话都会留下结构化记录。
- 模型无关：通过标准 OpenAI-compatible 接口连接本地或远程模型服务。

## 当前形态

```text
浏览器
  |
  v
FastAPI + 静态前端 (:8000)
  |
  +-- demo IAM / 访问决策 ------ data/iam.db
  +-- 知识检索与证据 ------------ data/knowledge.db
  +-- 会话 ---------------------- data/checkpoints.db
  +-- 审计 ---------------------- data/audit.db
  |
  +-- 模型服务 (OpenAI API) ------ 可配置的 /v1 端点
  +-- 学习 OCR 适配层 ------------ 外部 LearningOCR 项目
```

后端代码位于 `src/`，浏览器界面位于 `static/`。当前服务包含三类主要工作流：

1. 部门知识问答：自动在 `DIRECT` 和 `INTERNAL_RETRIEVAL` 两条路径之间选择。
2. 资料治理：会计提交资料，主管审核后才可以进入检索。
3. 学习记录 OCR：通过适配层调用本机 LearningOCR，保存结构化主记录和独立审计 sidecar。

项目仍是原型和测试平台，不是生产认证系统，也不包含真实公司的制度、财务或经营数据。

## 快速开始

### 1. 安装依赖

项目要求 Python 3.12 或更高版本。首次使用可以执行：

```bash
cd /path/to/knowledge-lantern
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/pip install pytest pytest-asyncio
```

如果需要启用向量检索和 reranker，再安装可选依赖：

```bash
.venv/bin/pip install -e '.[retrieval]'
```

### 2. 配置模型端点

复制示例配置并按需修改：

```bash
cp config.yaml.example config.yaml
```

默认配置提供了一个本机 OpenAI-compatible 端点示例：

```yaml
api_base_url: http://127.0.0.1:8080/v1
model: local-model
```

配置优先级为：环境变量 > `config.yaml` > 内置默认值。可用环境变量包括：
`SRC_API_KEY`、`SRC_API_BASE_URL`、`SRC_MODEL`、`SRC_TEMPERATURE`、`SRC_TIMEOUT` 和
`SRC_MAX_TOKENS`。

### 3. 启动服务

先按所使用的推理服务启动一个 OpenAI-compatible `/v1/chat/completions` 端点，并在 `config.yaml` 或环境变量中配置 `SRC_API_BASE_URL`、`SRC_MODEL` 和可选的 `SRC_API_KEY`。然后启动 FastAPI：

```bash
./run_server.sh
```

服务启动后访问：

- 主工作台：<http://127.0.0.1:8000>
- 学习记录页面：<http://127.0.0.1:8000/static/study/index.html>
- 服务健康检查：<http://127.0.0.1:8000/api/health>
- 问答与检索健康检查：<http://127.0.0.1:8000/api/paper-qa/health>

也可以直接使用 uvicorn 启动：

```bash
PYTHONPATH=src .venv/bin/python -m uvicorn src.server:app --host 0.0.0.0 --port 8000
```

模型服务不可用时，服务和多数测试仍可启动；问答接口会报告模型不可用。知识库检索、权限和资料治理逻辑不依赖模型进程。

## 主要能力

### 部门知识问答

`POST /api/paper-qa/ask` 会由本地模型选择问答路径：

- `INTERNAL_RETRIEVAL`：针对部门制度、流程、财务和内部资料进行授权检索，再根据证据生成回答。
- `DIRECT`：不依赖内部资料的通用问题直接交给本地模型，`sources` 为空。

检索默认使用 SQLite FTS5。配置和本地模型都可用时，还可以组合：

- BGE-M3 向量检索；
- GraphRAG 图谱扩展；
- BGE reranker；
- 加权 RRF 融合。

这些组件是可选的。组件缺失或索引未构建时会安全降级为 FTS5，健康接口会如实报告实际启用状态。

### 资料提交与审核

资料工作流为：

```text
accountant 提交 -> pending_review -> finance_supervisor 发布或拒绝 -> active / rejected
```

未发布的资料不参与问答检索。服务端会检查角色、资料分类上限、状态和提交人与审核人关系；提交人不能审核自己的资料。

### 学习记录与 OCR

`static/study/` 是一个独立的移动优先学习记录界面，其中记录库和部分检索内容仍是本地演示数据。

「识别」流程通过 `/api/study/ocr/*` 接入外部 LearningOCR 项目：

- 图片只允许写入 `data/study_ocr/uploads/`；
- 上传路径按 SHA-256 内容寻址，同一内容只保存一份；
- 业务记录 ID 不使用文件名或 SHA-256；
- 主记录和审计 sidecar 分开保存；
- OCR 失败时标记为 `failed`，不会伪造识别结果；
- `mock` 模式不需要模型，`real` 模式依赖外部项目及其模型环境。

默认外部项目目录为 `~/LearningOCR/ocr-vlm-pipeline`，可通过以下环境变量覆盖：

| 变量 | 默认值 | 作用 |
| --- | --- | --- |
| `LEARNINGOCR_ROOT` | `~/LearningOCR/ocr-vlm-pipeline` | 外部项目根目录 |
| `LEARNINGOCR_PYTHON` | `<root>/.venv/bin/python` | 外部项目 Python 解释器 |
| `LEARNINGOCR_TIMEOUT` | `300` | 单次 OCR 超时时间，单位为秒 |

## Demo 身份与权限

当前没有真实登录系统，前端通过 `user_id` 选择演示身份；服务端从 `data/iam.db` 读取权限，不信任客户端提交的角色或 scope。

| 身份 | 能力 |
| --- | --- |
| `employee` | 只能检索公开知识 |
| `accountant` | 可检索公开知识和内部财务知识；可提交资料，不能审核资料 |
| `finance_supervisor` | 可检索公开、内部和受限财务知识；可审核其他人提交的资料 |

分类等级为 `public < internal < restricted`。受限资料目前固定在 `finance-ledger / finance` 范围内。

## 测试期种子知识

仓库包含用于检索、权限和图谱测试的合成/整理语料：

| 类别 | 条目数 |
| --- | ---: |
| 经营分析 | 220 |
| 财务合规 | 220 |
| 人力资源 | 220 |
| 项目与运营 | 220 |
| 行政 | 220 |
| 合计 | 1100 |

这些内容不是任何真实公司的数据，也不应被当作实际制度或财务口径使用。种子内容的出处字段为：

```text
origin = seeded_synthetic_or_curated
license = project-generated-synthetic-no-third-party
is_real_company_data = false
seed_label = 测试期种子知识
```

如需重建种子库：

```bash
PYTHONPATH=src .venv/bin/python scripts/build_demo_knowledge.py \
  --db data/knowledge.db
```

默认是幂等的增量覆盖。`--force` 会删除目标数据库后重建，可能丢失同库中的其他资料，使用前应先备份。

重建后可以运行迁移和审计：

```bash
PYTHONPATH=src .venv/bin/python scripts/migrate_knowledge_metadata.py \
  --db data/knowledge.db

PYTHONPATH=src .venv/bin/python scripts/audit_knowledge_seed.py \
  --db data/knowledge.db
```

向量索引是可选的，也可以从知识库重新生成：

```bash
PYTHONPATH=src .venv/bin/python scripts/build_knowledge_indices.py \
  --db data/knowledge.db
```

## 数据文件

应用运行时使用 `data/` 下的 SQLite 数据库和报告文件：

| 文件 | 用途 |
| --- | --- |
| `iam.db` | 演示身份、角色和访问范围 |
| `knowledge.db` | 文档、chunk、FTS5 索引和图谱数据 |
| `checkpoints.db` | 会话和问答轮次 |
| `audit.db` | 检索、授权和资料动作审计 |
| `study_ocr/index.db` | OCR 任务元数据 |
| `study_ocr/uploads/` | 受控上传图片 |
| `study_ocr/results/` | OCR 主记录和审计 sidecar |
| `vector/` | 可再生的向量索引和构建报告 |

服务日志默认为 `data/knowledge-lantern.log`；模型服务的日志由具体 provider 自行管理。

## API 概览

### 服务与问答

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/api/health` | 进程健康检查 |
| `GET` | `/api/paper-qa/health` | 知识库、模型、检索组件、会话和审计状态 |
| `GET` | `/api/paper-qa/identities` | 查看演示身份 |
| `POST` | `/api/paper-qa/ask` | 自动路由问答 |
| `POST` | `/api/paper-qa/conversations` | 创建会话 |
| `GET` | `/api/paper-qa/conversations` | 列出用户自己的会话 |
| `GET` | `/api/paper-qa/conversations/{id}` | 读取会话和轮次 |

### 资料和 OCR

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/api/knowledge/documents` | 查询资料元数据 |
| `POST` | `/api/knowledge/ingest` | 提交资料进入待审核区 |
| `POST` | `/api/knowledge/documents/{id}/publish` | 发布资料 |
| `POST` | `/api/knowledge/documents/{id}/reject` | 拒绝资料 |
| `GET` | `/api/study/ocr/health` | 查询 OCR 能力状态 |
| `POST` | `/api/study/ocr/uploads` | 上传图片 |
| `POST` | `/api/study/ocr/jobs` | 提交 OCR 任务 |
| `GET` | `/api/study/ocr/jobs` | 查询 OCR 任务元数据 |
| `GET` | `/api/study/ocr/jobs/{id}` | 查询结构化 OCR 结果 |

常见错误语义：未知身份返回 `401`，权限或动作不允许返回 `403`，资源不存在返回 `404`，输入无效返回 `400`，状态冲突返回 `409`。证据不足使用业务状态 `insufficient_evidence`，不等同于权限拒绝。

## 项目结构

```text
src/
  server.py          FastAPI 应用和 HTTP 路由
  access/            身份、角色、授权上下文和访问决策
  knowledge/         文档存储、元数据、FTS、向量和图谱检索
  paper_qa/          问答路由、检索、模型调用、会话和审计
  study/             学习记录和 OCR 适配层
  config.py          模型服务配置读取
static/              主工作台和学习记录前端
scripts/             种子构建、迁移、审计和索引脚本
tests/               后端、权限、检索、OCR 和前端契约测试
data/                本地数据库、索引、报告和运行时文件
run_server.sh        通用 FastAPI 启动脚本
```

## 测试

运行完整测试集：

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/ -q
```

测试覆盖访问控制、知识库构建与迁移、FTS/混合检索、问答服务、会话归属、资料审核、API 契约、前端契约以及 OCR 路径安全和身份规则。

## 当前边界

- 演示身份不是认证系统，生产使用前必须接入真实身份认证和组织权限系统。
- 种子知识只用于测试，不能作为真实制度、财务数据或业务决策依据。
- 向量检索、图谱和 reranker 依赖本地模型与索引，缺失时会降级，不会自动下载权重。
- 学习页面的一部分记录库、图表和检索结果仍是前端演示数据。
- 业务模式的部门过滤和真实业务看板数据尚未接入；当前后端主路径仍是部门知识问答。
- `data/` 和 `work/` 中的运行时文件应根据部署方式决定是否纳入版本控制，真实资料不应直接提交到仓库。

## License

见 [LICENSE](LICENSE)。
