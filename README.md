# DDD Dev Agent

基于 Claude API 的 DDD 全流程开发 Agent。输入业务需求，自动完成领域设计 → 验收标准 → 代码生成 → 测试编写 → 集成 → 验证的完整流水线。

## 目录

- [快速开始](#快速开始)
- [工作模式](#工作模式)
- [流水线阶段](#流水线阶段)
- [项目配置](#项目配置)
- [目录结构](#目录结构)
- [质量保证机制](#质量保证机制)

---

## 快速开始

### 环境准备

```bash
cd tools/ddd-agent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export ANTHROPIC_API_KEY=sk-ant-...
```

### 运行

```bash
# 新功能开发
python main.py -p profiles/payment-demo.yaml "给商城加一个优惠券功能"

# 断点恢复（从上次中断处继续）
python main.py -p profiles/payment-demo.yaml --resume

# 代码审查（全量）
python main.py -p profiles/payment-demo.yaml --review internal/card

# 代码审查（只看本次改动）
python main.py -p profiles/payment-demo.yaml --review internal/card --diff
python main.py -p profiles/payment-demo.yaml --review internal/card --diff main
```

---

## 工作模式

### 生成模式（默认）

针对新功能/新上下文，从零完成完整 DDD 实现。

```
需求 → 领域设计 → 验收标准 → 代码生成 → 测试 → 集成 main.go → 验证
```

设计阶段和验收标准阶段需要人工确认，其余阶段全自动。

**确认选项：**

| 输入 | 操作 |
|------|------|
| `y` | 确认，进入下一阶段 |
| `n` | 终止，进度已保存 |
| `r` | 重做本阶段 |
| `m` | 提修改意见，Agent 根据反馈重新生成 |

### Review 模式（`--review`）

审查已有上下文代码，发现问题并自动修复。

```
扫描审查（人确认）→ 自动修复 → 验证
```

审查维度：架构合规、目录结构、领域模型质量、端口适配器、应用层、Handler、测试质量、共享类型。

**增量审查（`--diff`）：** 配合 git，只审查本次变更的文件，不报告其他文件的问题。

```bash
--diff           # 审查未提交的改动（git diff HEAD）
--diff HEAD~3    # 审查最近 3 个 commit 引入的改动
--diff main      # 审查相对于 main 分支的所有改动
```

**Review 确认选项：**

| 输入 | 操作 |
|------|------|
| `y` | 开始自动修复 |
| `n` | 仅查看报告，不修复 |
| `m` | 补充审查重点，Agent 更新报告后再确认 |

### 断点恢复（`--resume`）

Agent 每完成一个阶段就保存 checkpoint。网络中断或手动终止后可恢复：

```bash
python main.py -p profiles/payment-demo.yaml --resume
python main.py -p profiles/payment-demo.yaml --resume --session 20260313-143022
```

---

## 流水线阶段

### 生成模式

| # | 阶段 | 说明 | 确认 | 工具 |
|---|------|------|------|------|
| 1 | `design` | 领域设计：统一语言、聚合根、端口、事件、共享类型分析 | **人工** | read, list, search |
| 2 | `acceptance` | 验收标准：分层 AC（domain/app/handler/adapter），编号 AC-1~49 | **人工** | read |
| 3 | `codegen` | 代码生成：按依赖顺序生成全部 .go 文件 | 自动 | read, write, list, search, run |
| 4 | `test` | 测试生成：单元/集成/HTTP 测试，标注 AC 编号 | 自动 | read, write, search |
| 5 | `main_update` | 更新 Composition Root：添加新上下文的 import 和路由 | 自动 | read, write, search |
| 6 | `verify` | 验证：编译 + 测试(-race) + 静态检查 + 架构检查 + AC 覆盖矩阵 | 自动(最多3次重试) | read, write, run, arch, search |

### Review 模式

| # | 阶段 | 说明 | 确认 |
|---|------|------|------|
| 1 | `review_scan` | 全面扫描，输出问题清单（🔴严重/🟡警告/🟢建议） | **人工** |
| 2 | `review_fix` | 按问题清单逐项修复，内层先于外层 | 自动 |
| 3 | `review_verify` | 编译 + 测试 + 架构检查 + 复查修复点 | 自动(最多2次重试) |

---

## 项目配置

每个项目对应一个 YAML 配置文件（见 [profiles/payment-demo.yaml](profiles/payment-demo.yaml)）：

```yaml
name: payment-demo
language: go
project_root: "/path/to/project"
source_root: "internal"

build_command: "go build ./..."
test_command: "go test ./... -v -count=1"

# 架构约束（用于 check_architecture 工具）
rules:
  domain_no_import: ["adapter/", "handler/", "application/", "infra/"]
  application_no_import: ["adapter/", "handler/", "infra/"]

# 参考模板（Agent 生成代码前读取，模仿风格）
templates:
  domain_event: "internal/payment/domain/event/event.go"
  domain_model: "internal/payment/domain/model/transaction.go"
  application:  "internal/payment/application/charge_usecase.go"
  # ...

# 项目约定（追加到每个阶段的 system prompt）
conventions:
  - "包名用单数名词"
  - "金额必须用 Money 值对象"

# 从历史代码学到的模式（可持续追加）
learned_patterns:
  - "identity 上下文的 AuthMiddleware 把 userID 写入 ctx"
```

---

## 目录结构

```
tools/ddd-agent/
├── main.py              # CLI 入口
├── agent.py             # Agent 核心：工具循环 + 流水线编排
├── phases.py            # 阶段定义：system prompt + 工具集 + 配置
├── tools.py             # 工具实现：read/write/list/run/arch/search
├── checkpoint.py        # 断点持久化（JSON）
├── project_profile.py   # 项目配置加载
├── profiles/
│   └── payment-demo.yaml
└── requirements.txt
```

**各文件职责：**

- `agent.py` — `DDDAgent` 类：`run_pipeline()`（生成）、`run_review()`（审查）、`_agent_loop()`（单阶段 Claude 循环）
- `phases.py` — `PhaseConfig` 数据类 + 所有阶段的 system prompt 定义
- `tools.py` — `ToolExecutor`：6 个工具的具体实现（文件读写、命令执行、架构检查、代码搜索）

---

## 质量保证机制

### 验收标准作为质量锚点

验收标准（AC-1~49）在阶段 2 由人确认，然后显式传入阶段 4（测试）和阶段 6（验证）。验证阶段输出覆盖矩阵，确保每条 AC 都有对应测试。

### 命令白名单

`run_command` 工具只允许执行以下前缀的命令，拒绝任意 shell 指令：

```
go build / go test / go vet / go fmt / go mod
golangci-lint / ls / cat / head / tail / wc / find / grep
```

### API 重试

`RateLimitError`（429）和服务端错误（5xx）自动指数退避重试，最多 3 次（5s → 10s → 20s）。

### Token 用量统计

每次运行结束后打印分阶段 token 用量：

```
┌─────────────────────────────────────────┐
│           Token 用量统计                │
├──────────┬──────────┬──────────┬────────┤
│ 阶段     │   输入   │   输出   │ 调用次 │
├──────────┼──────────┼──────────┼────────┤
│ design   │   12,345 │    3,456 │      3 │
│ codegen  │   45,678 │   12,345 │      8 │
│ ...      │          │          │        │
└──────────┴──────────┴──────────┴────────┘
```

### 技术文档自动保存

设计和验收阶段完成后，自动保存技术文档到：

```
<project_root>/docs/plans/YYYY-MM-DD-<需求摘要>-design.md
```

供人工审阅后再确认是否进入代码生成。
