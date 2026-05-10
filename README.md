# crucible-sim

> **Adversarial swarm simulation orchestrator.** Bolts a 4-layer adversarial harness on top of an existing
> [MiroFish](https://github.com/666ghj/MiroFish) backend (Zep Cloud + OASIS): pre-flight briefing audit,
> synthetic-agent injection, 3-round adversarial interviews, and a triple-pass report (neutral / sharp / gap-audit).

> **对抗式群体仿真编排器**：在 [MiroFish](https://github.com/666ghj/MiroFish) 后端（Zep Cloud + OASIS）之上加挂 4 层
> 对抗式工具链——事前 briefing 审计、合成 agent 注入、三轮对抗式采访、三轮报告（中性/锐利/盲区）。

[English](#english) · [中文](#中文)

---

## English

### Why this exists

[MiroFish](https://github.com/666ghj/MiroFish) (`666ghj`) and
[MiroShark](https://github.com/aaronjmars/MiroShark) (`aaronjmars`) are two existing swarm-simulation
projects that showed multi-agent social discourse can be produced coherently and cheaply.
`crucible-sim` runs on top of MiroFish and is inspired by both.

Out of the box, a swarm tends to **rubber-stamp** the briefing. Real entities pulled from the briefing
repeat each other's framings; nobody challenges the load-bearing numbers; reports synthesize what
the swarm said rather than what the swarm *missed*.

`crucible-sim` does **not** fork the underlying engine. It uses MiroFish's REST API for orchestration,
and after MiroFish's `prepare` step it also edits that run's local `simulation_config.json` and
`twitter_profiles.csv` to inject (or skip) synthetic agents. It adds:

| Layer | What it does |
|---|---|
| **1. Pre-flight audit** | An adversarial red-team LLM scans the briefing, extracts named entities, numerical claims, contested points, and **missing angles**. Locks the *single weakest claim* before the sim starts. |
| **2. Synthetic agent injection** | Generates **0, 3, or 5** *fictional named* characters depending on `--mode` (`mirofish`=0, `default`=3, `miroshark`=5), each with a private prior that explicitly conflicts with at least one real agent. Appended to the run's `simulation_config.json` + `twitter_profiles.csv`, bypassing Zep entity extraction. |
| **3. Three-round adversarial interview** | <ul><li>**R1 self-statement**: tailored question per agent.</li><li>**R2 cross-fire**: every detected disagreement pair gets bilateral pushback questions.</li><li>**R3 weakest-claim challenge**: same question (built from preflight `weakest_claim`) sent to every agent.</li></ul> |
| **4. Triple-pass report** | <ul><li>**Pass A** — neutral synthesis (MiroFish ReACT endpoint, falls back to a direct LLM call if content-filter trips).</li><li>**Pass B** — opinionated critique (sharp framing, must cite specific agents).</li><li>**Pass C** — *gap audit* over preflight `missing_angles` × actual posts × R3 pushback count.</li></ul> |

The output is a static `index.html` (KG, R1/R2/R3 tabs, three reports side-by-side) plus a tarball.
The HTML loads `vis-network` from `unpkg.com` for the KG view; offline use requires vendoring that
JS file or editing `bundle.py`. If a `replay.gif` exists in the output directory, `bundle.py` includes
it; this repo does not generate the GIF in the quickstart.

### Three modes

Selected with `--mode` (env `CRUCIBLE_MODE`):

- **`default`** — 3 synthetic agents (Skeptic + Domain Expert + Stakeholder). Balanced critique. *Recommended.*
- **`mirofish`** — *zero* synthetic agents. Uses MiroFish-generated agents only, but **still** runs through
  crucible's preflight / interview / report layers and **still** patches `time_config` to 24h activity.
  This is a *crucible baseline* with no synthetic injection — not untouched vanilla MiroFish.
- **`miroshark`** — 5 synthetic agents (above + Provocateur + Futurist), higher temperature, more
  vivid character voices. The mode name is a local persona-preset label; it doesn't call or depend on `aaronjmars/MiroShark`.

The architecture is identical across modes. Only the synthetic-agent layer changes.

### LLM provider compatibility

The scripts use the OpenAI Python SDK `chat.completions` API. Use any endpoint that supports
chat completions, your selected model slug, and the requested `max_tokens`. Tested-style configurations
include local `glm-proxy`, OpenRouter, and OpenAI direct.

```bash
# Local glm-proxy (default — your own OpenAI-compatible service on port 8011, NOT part of MiroFish)
export LLM_BASE_URL="http://127.0.0.1:8011/v1"
export LLM_API_KEY="$ZAI_API_KEY"
export LLM_MODEL_NAME="glm-4.7"

# OpenRouter — set every model env var so cross-provider runs don't fall back to built-in z.ai
# slugs (high-reasoning steps default to glm-4.7; Pass A fallback defaults to glm-4.5-air).
export LLM_BASE_URL="https://openrouter.ai/api/v1"
export LLM_API_KEY="$OPENROUTER_API_KEY"
export LLM_MODEL_NAME="anthropic/claude-haiku-4.5"
export LLM_MODEL_B="$LLM_MODEL_NAME"
export LLM_MODEL_C="$LLM_MODEL_NAME"
export LLM_MODEL_A_FALLBACK="$LLM_MODEL_NAME"

# OpenAI direct
export LLM_BASE_URL="https://api.openai.com/v1"
export LLM_API_KEY="$OPENAI_API_KEY"
export LLM_MODEL_NAME="gpt-5.4-mini"
export LLM_MODEL_B="$LLM_MODEL_NAME"
export LLM_MODEL_C="$LLM_MODEL_NAME"
export LLM_MODEL_A_FALLBACK="$LLM_MODEL_NAME"
```

> Model slugs are provider-specific and may change. Check
> [openrouter.ai/models](https://openrouter.ai/models) and
> [platform.openai.com/docs/models](https://platform.openai.com/docs/models)
> for current IDs before copying these examples.

Most scripts also accept `--llm-base-url`, `--llm-api-key`, and `--llm-model` directly.
`twopass_report.py` uses `--llm-model-b` for Pass B and the env var `LLM_MODEL_A_FALLBACK` for the
Pass A fallback path.

**Why glm-4.7 by default:** every reasoning-heavy step in crucible (preflight audit, synthetic-agent
generation, **R1 question generation**, disagreement scan, Pass B critique, Pass C gap audit) defaults
to `glm-4.7` rather than `glm-4.6`. Compared to 4.6, 4.7 costs about +9.1% on input tokens and +10%
on output tokens, while adding a 202k context window and scoring materially higher on the relevant
benchmarks (SWE-bench +6pp, Terminal Bench 2.0 +16.5%, plus AIME 2025 / GPQA / HLE / LiveCodeBench
v6). For the dozen-or-so prompts that hit these steps in one run the cost delta is negligible and
question / report quality is the bottleneck. The Pass A *fallback* (only fires when MiroFish's ReACT
pipeline trips a content filter) stays on `glm-4.5-air` intentionally, since Pass A is a
deliberately neutral synthesis.

### Quickstart

Prerequisites:

- A running **local** MiroFish backend at `http://127.0.0.1:5002`, plus filesystem access to its
  `backend/uploads/simulations/` directory. Remote or containerized MiroFish backends require
  extra setup because crucible needs to edit a prepared simulation directory in place — mount that
  directory and pass `--sim-dir-hint`, or run crucible on the same host as the backend.
  Set `MIROFISH_SIM_ROOT` if autodetection fails.
- An OpenAI-compatible LLM endpoint with your API key.
- Python ≥ 3.11 with `openai` and `requests`. Install `jq` if you use the snippet below
  to extract `simulation_id` from `manifest.json`.

```bash
# Install
git clone https://github.com/wshuyi/crucible-sim.git
cd crucible-sim
pip install openai requests

# Set env (pick one provider — see "LLM provider compatibility" above for OpenRouter / OpenAI direct)
export LLM_BASE_URL="https://openrouter.ai/api/v1"
export LLM_API_KEY="$OPENROUTER_API_KEY"
export LLM_MODEL_NAME="anthropic/claude-haiku-4.5"
export LLM_MODEL_B="$LLM_MODEL_NAME"
export LLM_MODEL_C="$LLM_MODEL_NAME"
export LLM_MODEL_A_FALLBACK="$LLM_MODEL_NAME"

# Run end-to-end on the AWS outage demo briefing
WORK=./out/aws-outage-$(date +%Y%m%d)
mkdir -p $WORK/raw

# Step 1 — pre-flight audit
python crucible/preflight.py \
  --briefing inputs/aws-outage-briefing.md \
  --out $WORK/preflight.json

# Step 2 — sim with synthetic agents (mode = default | mirofish | miroshark)
python crucible/run_pipeline.py \
  --backend http://127.0.0.1:5002 \
  --doc inputs/aws-outage-briefing.md \
  --requirement "Discuss AWS US-East-1 outage on May 8, 2026 — concentration risk, multi-cloud vs multi-region, Polymarket 38% YES, AMZN -1.6% reaction." \
  --out $WORK \
  --preflight $WORK/preflight.json \
  --mode default \
  --max-rounds 5 --platform twitter

# Capture sim_id from manifest.json (writes after Step 2 completes)
SIM_ID=$(jq -r .simulation_id "$WORK/manifest.json")

# Step 3 — disagreement scan
python crucible/disagreement_scan.py \
  --results-dir "$WORK" \
  --out "$WORK/disagreement_pairs.json"

# Step 4 — adversarial interview (R1 + R2 + R3)
python crucible/adversarial_interview.py \
  --backend http://127.0.0.1:5002 \
  --simulation-id "$SIM_ID" \
  --results-dir "$WORK" \
  --preflight "$WORK/preflight.json" \
  --pairs "$WORK/disagreement_pairs.json" \
  --out "$WORK/interviews_r1_r2_r3.json"

# Step 5 — Pass A + B reports
python crucible/twopass_report.py \
  --backend http://127.0.0.1:5002 \
  --simulation-id "$SIM_ID" \
  --results-dir "$WORK" \
  --briefing inputs/aws-outage-briefing.md \
  --interviews "$WORK/interviews_r1_r2_r3.json" \
  --out-a "$WORK/report_pass_A.md" \
  --out-b "$WORK/report_pass_B.md" \
  --passes A,B

# Step 6 — Pass C gap audit
python crucible/gap_audit.py \
  --results-dir "$WORK" \
  --preflight "$WORK/preflight.json" \
  --interviews "$WORK/interviews_r1_r2_r3.json" \
  --out "$WORK/report_pass_C_gap.md"

# Step 7 — bundle (single HTML + tarball named after the out-dir)
python crucible/bundle.py --out "$WORK"
```

### Project layout

```
crucible/
├── preflight.py              # Layer 1: red-team briefing audit
├── synthetic_agents.py       # Layer 2: 0 / 3 / 5 synthetic agents (mode-aware)
├── run_pipeline.py           # Sim orchestrator (ontology→build→prepare→inject→start→pull)
├── disagreement_scan.py      # Layer 3a: stance scan → pairs.json
├── adversarial_interview.py  # Layer 3: 3-round interview (R1/R2/R3)
├── twopass_report.py         # Layer 4a/b: Pass A neutral + Pass B sharp
├── gap_audit.py              # Layer 4c: Pass C missing-angle audit
└── bundle.py                 # 7-section static HTML + tarball
```

### Inspiration & credits

- **[MiroFish (`666ghj/MiroFish`)](https://github.com/666ghj/MiroFish)** — the Zep Cloud + OASIS
  swarm engine that crucible-sim talks to. crucible never modifies MiroFish source code, but it
  does mutate the per-run `simulation_config.json` and `twitter_profiles.csv` after MiroFish's
  `prepare` step.
- **[MiroShark (`aaronjmars/MiroShark`)](https://github.com/aaronjmars/MiroShark)** — a Neo4j-backed
  swarm-simulation project whose Polymarket-flavored replay-GIF / share-card UX inspired the visual
  bundling style here.

### Known limitations

- `MiroFish /api/simulation/<sim_id>/run-status` can stick at `current_round=0` for a whole short run;
  we treat `env-status.twitter_available=false` + DB row plateau as completion.
- MiroFish's `/api/report/generate` is a glm-backed ReACT pipeline that occasionally trips a content
  filter (z-ai code 1301). Pass A automatically falls back to a direct LLM call with a neutral
  framing prompt.
- Synthetic agents are appended *after* MiroFish's `prepare` phase. We bypass Zep entity extraction
  for them, so they don't appear in the knowledge graph.
- The default LLM endpoint assumes you run your own OpenAI-compatible service at
  `http://127.0.0.1:8011/v1` (commonly called "glm-proxy"). This proxy is **not** part of MiroFish;
  configure or replace it with OpenRouter / OpenAI by setting `LLM_BASE_URL`, `LLM_API_KEY`, and
  the model env vars.

### License

This repository is MIT — see [`LICENSE`](LICENSE). It contains no code or assets copied from MiroFish
or MiroShark; if you fork it and vendor any, respect the upstream licenses.

---

## 中文

### 背景

[MiroFish](https://github.com/666ghj/MiroFish)（`666ghj`）和
[MiroShark](https://github.com/aaronjmars/MiroShark)（`aaronjmars`）这两个群体仿真项目证明了
跑一场低成本的多 agent 社交仿真是可行的。`crucible-sim` 在 MiroFish 之上运行，灵感来自这两个项目。

开箱即用时，swarm 倾向于**给 briefing 盖章背书**：从 briefing 抽出来的真实实体彼此抄观点，没人挑战
那些用来撑起结论的关键数字，最后报告综述的也只是 swarm "说过什么"，而不是 swarm "没探索什么"。

`crucible-sim` **不 fork** 底层引擎。它通过 MiroFish 的 REST API 做编排，并在 MiroFish 的 `prepare` 完成后直接修改这次运行的 `simulation_config.json` 和 `twitter_profiles.csv`，加挂 4 层：

| 层 | 作用 |
|---|---|
| **1. 事前审计** | 用 LLM 红队扫 briefing，抽出具名实体、数字声明、争议点和**缺失角度**；锁定整篇里最薄弱的单一声明，作为 R3 信仰挑战的"靶子"。 |
| **2. 合成 agent 注入** | 根据 `--mode` 生成 **0 / 3 / 5** 个**虚构具名**角色（mirofish=0、default=3、miroshark=5），每个都有显式 conflict_with 至少一个真实 agent 的私有 prior。直接 append 到 simulation_config + twitter_profiles.csv，绕过 Zep 实体抽取。 |
| **3. 三轮对抗式采访** | <ul><li>**R1 自我陈述**：每个 agent 一道针对性问题</li><li>**R2 跨方对线**：检出立场分歧的 pair，双向追问"A 说 X / B 说 Y，怎么调和？"</li><li>**R3 信仰挑战**：所有 agent 同一道题——"briefing 里最弱的论断在哪？"</li></ul> |
| **4. 三轮报告** | <ul><li>**Pass A** 中性综述（走 MiroFish 自带 ReACT；遇内容过滤自动 fallback 到直接 LLM 调用）</li><li>**Pass B** 锐利批评（带视角，必须引用 agent 原话）</li><li>**Pass C 盲区审计**：把 preflight 的 missing_angles × posts × R3 反驳次数对账</li></ul> |

最终产出一个静态 HTML（含 KG / R1-R3 tab / 三轮报告并列）+ 一个 tar 包。注意：HTML 的 KG 视图通过 `unpkg.com` 加载 `vis-network`，离线打开时需要把 JS 文件 vendor 进来或修改 `bundle.py`；如果输出目录里有 `replay.gif`，`bundle.py` 会引用它，但仓库的 quickstart 不直接生成这个 GIF。

### 三种模式

通过 `--mode`（或环境变量 `CRUCIBLE_MODE`）选择：

- **`default`**（推荐）—— 3 个合成 agent（Skeptic + Expert + Stakeholder），均衡批评。
- **`mirofish`**—— **零**合成 agent，只用 MiroFish 从 briefing 自动抽取的真实实体；但**仍**会跑 crucible 的 preflight / 采访 / 三轮报告，**仍**会把 `time_config` patch 成 24h 活跃。这是"crucible 不注入合成 agent 的对照基线"，**不是**未经修改的 vanilla MiroFish。
- **`miroshark`**—— 5 个合成 agent（在 default 之上 + Provocateur + Futurist），人物语言更鲜明、温度更高。模式名只是本项目的人设预设别名，与 `aaronjmars/MiroShark` 无依赖关系。

三种模式的底层管线**完全一致**，只有合成 agent 这一层不同。

### LLM 提供商

脚本通过 OpenAI Python SDK 的 `chat.completions` API 调用模型。任何兼容这一 API、支持你选择的模型 slug、且能容纳所需 `max_tokens` 的端点都可以工作；常见配置包括本地 `glm-proxy`、OpenRouter、OpenAI 直连。

```bash
# 本地 glm-proxy（默认；你自己运行的服务，**不**属于 MiroFish）
export LLM_BASE_URL="http://127.0.0.1:8011/v1"
export LLM_API_KEY="$ZAI_API_KEY"
export LLM_MODEL_NAME="glm-4.7"

# OpenRouter — 必须设全模型变量，否则跨 provider 运行时会回退到内置 z.ai slug
# （高推理步骤默认 glm-4.7；Pass A fallback 默认 glm-4.5-air）。
export LLM_BASE_URL="https://openrouter.ai/api/v1"
export LLM_API_KEY="$OPENROUTER_API_KEY"
export LLM_MODEL_NAME="anthropic/claude-haiku-4.5"
export LLM_MODEL_B="$LLM_MODEL_NAME"
export LLM_MODEL_C="$LLM_MODEL_NAME"
export LLM_MODEL_A_FALLBACK="$LLM_MODEL_NAME"

# OpenAI 直连
export LLM_BASE_URL="https://api.openai.com/v1"
export LLM_API_KEY="$OPENAI_API_KEY"
export LLM_MODEL_NAME="gpt-5.4-mini"
export LLM_MODEL_B="$LLM_MODEL_NAME"
export LLM_MODEL_C="$LLM_MODEL_NAME"
export LLM_MODEL_A_FALLBACK="$LLM_MODEL_NAME"
```

> 模型 slug 与提供商相关、且会变化；建议在 [openrouter.ai/models](https://openrouter.ai/models) 与 [platform.openai.com/docs/models](https://platform.openai.com/docs/models) 上确认当前可用 ID 后再使用。

大多数脚本也支持 `--llm-base-url / --llm-api-key / --llm-model` CLI 参数；`twopass_report.py` 的 Pass B 走 `--llm-model-b`，Pass A fallback 读环境变量 `LLM_MODEL_A_FALLBACK`。

**为什么默认 glm-4.7：** crucible 里每一个高推理密度的步骤（preflight 审计、合成 agent 生成、**R1 提问生成**、disagreement scan、Pass B 批评、Pass C 盲区审计）默认都用 `glm-4.7` 而不是 4.6。相比 4.6，4.7 输入 token 约贵 9.1%、输出 token 贵 10%，但上下文扩到 202k，并在 SWE-bench (+6pp)、Terminal Bench 2.0 (+16.5%)、AIME 2025 / GPQA / HLE / LiveCodeBench v6 上明显更强；对单次运行总共十几次调用而言成本差可以忽略，问题/报告质量才是瓶颈。Pass A 的 *fallback*（仅在 MiroFish ReACT 被 1301 内容过滤拦截时触发）仍保留 `glm-4.5-air`——这一档本就是中性综述、不需要旗舰模型。

### 快速开始

中文快速开始与英文 [Quickstart](#quickstart) 使用同一组命令。请先按 LLM provider 段落 export 环境变量，然后照上面 7 个 Step 顺序执行。这里只列举差异性提醒：

- **MiroFish 必须在本机运行**（默认 `http://127.0.0.1:5002`），且 crucible 需要写权限到 MiroFish 的 `backend/uploads/simulations/<sim_id>` 目录。容器/远程部署时请挂载该目录并传 `--sim-dir-hint`，或设置环境变量 `MIROFISH_SIM_ROOT`。
- Step 4 / Step 5 的 `simulation_id` 必须用 `jq -r .simulation_id "$WORK/manifest.json"` 抽取，**不要**直接抄 `<sim_id>` 占位符。
- Python ≥ 3.11，`pip install openai requests`；如果用了 jq 抽取 sim_id，还要装 `jq`。

### 致谢

- **[MiroFish (`666ghj/MiroFish`)](https://github.com/666ghj/MiroFish)** —— Zep Cloud + OASIS 的群体仿真引擎，crucible-sim 通过它的 REST API 编排；不修改其源代码，但会修改单次运行的 `simulation_config.json` / `twitter_profiles.csv`。
- **[MiroShark (`aaronjmars/MiroShark`)](https://github.com/aaronjmars/MiroShark)** —— 基于 Neo4j 的群体仿真项目，其 Polymarket 风的回放 GIF / 分享卡 UX 启发了 crucible 的视觉打包风格。

### 已知限制

- `/api/simulation/<sim_id>/run-status` 在短跑 sim 里有时会一直停在 `current_round=0`；我们用 `env-status.twitter_available=false` + DB 行数 plateau 作为完成信号。
- MiroFish 自带的 ReACT 报告偶尔会被 z-ai 1301 内容过滤拦截；Pass A 会自动 fallback 到中性 prompt 的直接 LLM 调用。
- 合成 agent 在 MiroFish prepare 阶段**之后**注入，绕过了 Zep 实体抽取，因此不出现在知识图谱中。
- 默认 LLM 端点假设你自己起了一个本地 OpenAI 兼容服务（俗称 `glm-proxy`，端口 8011）。这个 proxy **不**是 MiroFish 自带的；如果不想自己起，请用上面的 OpenRouter / OpenAI 配置替换。

### 许可证

本仓库代码遵循 MIT —— 见 [`LICENSE`](LICENSE)。仓库中没有 vendor 自 MiroFish / MiroShark 的代码或资产；如果你 fork 后加入了，请自行遵守对应上游的许可证。
