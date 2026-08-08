# modelbench

> 一个**对推理模型友好（reasoning-aware）**的 LLM 性能 benchmark，统一走 **OpenAI Responses API**。

在任何 Responses 兼容端点上——官方直连 API、托管套餐、路由器/网关——用**同一份负载**测量 **TTFT、解码 TPS、端到端延迟、可靠性、prompt 缓存效果、reasoning effort 档位**，因此结果可横向对比。

[English](README.md) | 中文

## 为什么做这个

市面上多数 LLM 测速工具只报一个"tokens/sec"和一个"TTFT"。但这对**推理模型**（DeepSeek、Kimi、GLM、Doubao……）会失效——它们在给出可见答案前，会先输出一大段隐藏的**思考（thinking）**流。朴素的 benchmark 要么：

- 把思考 token 也算成输出（**虚高 TPS**），要么
- 把 TTFT 测到第一个**思考** token（**掩盖真实的"看到答案"耗时**），要么
- 在提供方流式不回传 `usage` 时直接失灵（**DeepSeek 官方流式就不回 usage**）。

modelbench 用三个测量设计解决这些问题：

1. **chunk 计时解码 TPS** —— `正文token / (末正文chunk − 首正文chunk)`。解码窗口从第一个**可见** token 起算，天然不受 usage 缺失和思考长度波动影响。
2. **TTFT 拆两档** —— **首响应 TTFT**（开始思考）vs **首正文 TTFT**（开始给答案）。对 Agent，后者才是用户真实体感。
3. **reasoning effort 档位扫描** —— 每个模型跑 `low / medium / high` 三档，量化"思考更久"的延迟代价（并验证解码 TPS 几乎不变——代价全在 TTFT）。

每次跑完还会做**数据质量审计**（截断、空响应、token 估算回退、方差），让你在引用数字前就知道它可不可信。

## 指标

| 指标 | 定义 |
|---|---|
| 首响应 TTFT | 到首个输出事件（推理模型=首个思考 token） |
| 首正文 TTFT | 到首个**可见答案** token |
| **解码 TPS** | 正文token / (末−首正文chunk) —— 纯解码速率 |
| 端到端 TPS | 正文token / 总时长 —— 最接近体感 |
| E2E 延迟 | 请求 → `response.completed` |
| 工具调用延迟 | 到首个 `function_call` item（agent 负载） |
| 工具调用合法性 | 产生**可解析** function_call 的 agent 调用占比 |
| Prompt 缓存 | `cached_tokens` + 相同前缀重复请求的首响应 TTFT 降幅 |

## 安装

```bash
git clone <本仓库> && cd modelbench
uv sync
```

## 配置

```bash
cp config.example.yaml config.yaml   # 填你的 endpoint + 模型 id
cp .env.example .env                 # 填你的 API key
```

`config.yaml` 和 `.env` 已被 git 忽略。key 一律从环境变量读，绝不入库。

## 使用

```bash
# 先探测端点/模型可用性（便宜）
uv run python -m modelbench.cli probe

# 全量 benchmark（S/M/L/XL + agent + cache 负载），并出报告
uv run python -m modelbench.cli run --report

# 子集快速冒烟
uv run python -m modelbench.cli run --endpoints hosted-plan --models glm-5.2 --cases S,M --repeats 3 --report

# reasoning effort 档位扫描（low/medium/high）
uv run python -m modelbench.cli effort --case M --efforts low,medium,high

# 从已有结果文件出报告
uv run python -m modelbench.cli report --results results/raw_<ts>.jsonl --effort results/effort.jsonl

# 审计结果文件的数据质量
uv run python -m modelbench.audit results/raw_<ts>.jsonl
```

## 负载档位

| case | 输入 | 输出 | 用途 |
|---|---|---|---|
| S | ~50 tok | 200 | 交互式 |
| M | ~1k | 500 | 典型 Agent 单步 |
| L | ~4k | 1000 | 长上下文 |
| XL | ~8k | 2000 | 压长上下文上限 |
| agent | ~2k + 工具 | 1 次 function call | 工具调用延迟 + 合法性 |
| cache | ~6k 静态前缀 × N 次 | 200 | prompt 缓存效果 |

## 产物

`results/` 下有 `raw_<ts>.jsonl`（逐请求明细）、`report_<ts>.md`（汇总表 + 分析）和 PNG 图。真实报告样例见 **[examples/](examples/)**。

## 方法论与注意事项

- 同一（模型， case）内**串行**请求；不同模型**并发**（打的是不同后端）。每个 endpoint 有信号量限制并发，避免触发限流——否则限流会被误记成 TTFT 虚高。
- 推理模型可能把整个输出预算耗在思考上导致正文为空；modelbench 会加输出余量，并拆分正文/思考 token，让这种情况**可见而非静默**。
- 单一"综合速度分"会误导（一个模型可能吞吐第一但要 50s 才开始作答）。报告因此把**吞吐优先**和**响应优先**分开评分。
- 结果是**某一时刻**的快照，且受提供方侧波动影响。**方法可复用，数字会过时**——请把本仓库当工具用，把 examples 里的数字当带日期的样例看。

## License

MIT
