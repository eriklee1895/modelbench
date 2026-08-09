# 方法论（Methodology）

[English](metrics.md) | [中文](metrics_cn.md)

modelbench 怎么测、为什么这么测。本文讲"为什么"，用法见 [README](../README_CN.md)，完整样例见 [examples/](../examples/)。

## 这个工具解决什么问题

朴素的 TPS/TTFT benchmark 在**推理模型**（DeepSeek、Kimi、GLM、Doubao……）上会失效。这类模型在可见答案前会先输出一大段隐藏的**思考（thinking）**流，导致三个测量陷阱：

1. **把思考 token 算成输出，虚高 TPS**——模型显得快，其实你数的是用户看不到的 token。
2. **TTFT 测到第一个"思考"token，掩盖真实的"看到答案"耗时**——"首 token 0.3s"听着好，但答案 40s 后才开始。
3. **流式不回传 `usage` 时，token 计数直接失灵**——比如 DeepSeek 官方 Responses 端点流式不回 usage，任何依赖它的指标都会静默失败。

modelbench 就是围绕这些现实设计的。

## 核心指标

### 解码 TPS（权威速度数字）

```
decode_tps = 正文token / (末正文chunk时刻 − 首正文chunk时刻)
```

- token 只算**正文（可见答案）**，思考 token 排除。
- 解码窗口从第一个**可见正文 chunk** 起算，因此思考时间天然不在分母里。
- 计时来自 **chunk 到达时间戳**，不依赖 `usage`——所以提供方流式不回 usage 也能测准。

这让解码 TPS 成为跨提供方最稳定、可对比的数字，回答的是"模型开始作答后，吐字有多快"。

### 首响应 TTFT vs 首正文 TTFT

我们分开记录**两个** TTFT：

| 指标 | 止于 | 含义 |
|---|---|---|
| 首响应 TTFT | 首个输出事件（推理模型=首个思考 token） | 模型已启动；排队+prefill+首token |
| **首正文 TTFT** | 首个**可见答案** token | 用户真正开始看到答案的时刻 |

非推理模型两者相同；推理模型可能差几十秒（思考长度），后者才是用户体感。

> 对推理模型只报一个"TTFT"会误导——看 Agent 体验看首正文 TTFT，看后端排队看首响应 TTFT。

### 端到端 TPS（体感速度）

```
e2e_tps = 正文token / 总请求时长
```

从请求到 `response.completed` 的总时长。它是最接近体感的单一数字，因为含首 token 延迟。一个模型可能解码 TPS 极高但端到端 TPS 很差（如果它作答前思考 50s，比如 GLM-5.2）。

### E2E 延迟

请求 → `response.completed` 的墙钟时间，适合 SLA 式对比。

### Prompt 缓存效果

`cache` 负载把同一个 ~6k token 前缀连发 10 次，记录：
- API 回传的 `cached_tokens`（证明确实命中缓存），
- 首次（冷）vs 最优（热）的首响应 TTFT，
- 降幅百分比。

我们测的所有提供方都支持前缀缓存，降幅依模型从 ~10% 到 ~80% 不等。

### Reasoning effort 档位

`effort` 命令让每个模型跑 `low / medium / high` 三档（外加提供方默认档）。一个关键实证发现：

> **思考量和首正文 TTFT 随档位显著上升，但解码 TPS 几乎不变。** "思考更努力"的代价全在延迟，不在解码速度。而且很多模型上 `default ≈ high`——真正省时间的是 `low`。

注意：部分提供方（GLM、某些套餐上的 MiniMax）会**静默忽略** `reasoning.effort` 参数。档位扫描通过对比各档思考 token 数把这一点暴露出来。

### 工具调用延迟与合法性（agent 负载）

`agent` 负载发一个工具丰富的 prompt，测到首个 `function_call` item 的耗时。关键是**合法性**——function call 的参数必须可解析：返回 200 但没产生调用算失败，不算成功。这能抓住"宣称支持工具但不可靠触发"的模型（我们发现 DeepSeek 官方端点在思考模式下拒绝强制 `tool_choice`，就是一例）。

## 怎么保证数字可信

### 数据质量审计器

`modelbench.audit` 按（模型，case）标记：

- **可靠性**——成功率；低于 ~90% 意味着延迟数字采自有偏子集。
- **空响应**——`incomplete:length` 表示模型把整个输出预算耗在思考上、正文为空（激进推理模型常见）。这对生产是真实故障模式，不是测量误差。
- **截断**——输出撞上 `max_output_tokens`，说明 TPS 窗口异常。
- **token 估算回退**——`usage` 缺失时用 tiktoken 计数；审计会标记，让你知道哪些数字是估算。
- **TTFT 方差**——高变异系数通常是思考长度随机（不是测量误差）；与首响应 TTFT 的 CV 对比，可区分排队噪声和思考长度噪声。

### 实验控制

- **同一（模型，case）内串行**——repeats 一个接一个，不争抢同一后端。
- **不同模型间并发**——不同模型打不同后端，安全；每个 endpoint 有信号量限并发，避免触发限流（否则会表现为 TTFT 虚高）。
- **每格 10 次 repeats + 1 次 warmup**——报中位数，同时保留 p95 和标准差。
- **`temperature=0`** + 固定输出预算，让输出长度跨模型可比。

## 已知的坑（我们踩过，你不用再踩）

- **DeepSeek 官方流式不回 `usage`。** 解码 TPS 回退到 tiktoken 分块计数——这正是 chunk 计时存在的原因。
- **DeepSeek 思考模式拒绝 `tool_choice: "required"`。** 我们在那里降级为 `auto`；在 agent prompt 上仍能触发 function call。
- **"思考狂"模型可能把整个输出预算耗在思考上。** 我们加输出余量（`content_budget + 16384`，上限 32768）并拆分正文/思考 token，让问题可见。`doubao-seed-2.1-turbo` 是最严重的——随机 200–8000+ 思考 token，偶发正文为 0。
- **effort 档位在不同提供方含义不同。** `high` 的思考长度差异巨大；务必读思考 token 列，别只看档位标签。
- **共享网关在长上下文负载下会退化。** 我们观测到某个 OpenAI 兼容路由器在 8k+ 前缀的 cache 扫描中 TTFT 从 ~2s 涨到 ~15s——这是网关的真实特性，不是模型的。
- **同模型、不同托管路径，TPS 差异显著。** 我们实测 DeepSeek-V4-Flash 和 MiniMax-M3 官方直连的解码 TPS 比托管/套餐路径快 ~27–33%。值得为你自己的提供方实测。

## 怎么读报告

别用单一综合分给模型排名。一个模型可能吞吐第一但响应垫底：

- **吞吐优先**（80% 解码TPS + 20% 首正文TTFT）→ 长文生成 / 批量任务。
- **响应优先**（80% 首正文TTFT + 20% 解码TPS）→ 交互式 Agent，首字延迟主导。

报告会同时给出两个分数和两列底层数据，按你的工作负载选对应的列。

## 范围与局限

- 结果是**某一时刻**的快照——提供方每周都在改模型。**方法可复用，数字会过时**（见 `examples/`）。
- 只测 OpenAI **Responses API**。Chat Completions 端点行为可能不同。
- 网络来自跑 benchmark 的主机；绝对 TTFT 含该主机的延迟。**重点看相对对比。**
