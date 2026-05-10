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

[MiroFish](https://github.com/666ghj/MiroFish) and its predecessor [MiroShark](https://github.com/666ghj/MiroShark) showed that you can drive a multi-agent
"social discourse" simulation cheaply and have it produce coherent posts. But out of the box, the
swarm tends to **rubber-stamp** the briefing. Real entities pulled from the briefing repeat each
other's framings; nobody challenges the load-bearing numbers; reports synthesize what
the swarm said rather than what the swarm *missed*.

`crucible-sim` does **not** fork the underlying engine. It wraps MiroFish over its REST API and adds:

| Layer | What it does |
|---|---|
| **1. Pre-flight audit** | An adversarial red-team LLM scans the briefing, extracts named entities, numerical claims, contested points, and **missing angles**. Locks the *single weakest claim* before the sim starts. |
| **2. Synthetic agent injection** | Generates 3-5 *fictional named* characters (Skeptic / Domain Expert / Stakeholder, optionally + Provocateur / Futurist), each with a private prior that explicitly conflicts with at least one real agent. Appends them to the simulation config + profile CSV — bypassing Zep entity extraction. |
| **3. Three-round adversarial interview** | <ul><li>**R1 self-statement**: tailored question per agent.</li><li>**R2 cross-fire**: every detected disagreement pair gets bilateral pushback questions.</li><li>**R3 weakest-claim challenge**: same question (built from preflight `weakest_claim`) sent to every agent.</li></ul> |
| **4. Triple-pass report** | <ul><li>**Pass A** — neutral synthesis (MiroFish ReACT endpoint, falls back to direct LLM if content-filter trips).</li><li>**Pass B** — opinionated critique (sharp framing, must cite specific agents).</li><li>**Pass C** — *gap audit* over preflight `missing_angles` × actual posts × R3 pushback count.</li></ul> |

The output is a single static HTML bundle (KG, replay GIF, R1/R2/R3 tabs, three reports side-by-side) plus a tarball.

### Three modes

Selected with `--mode` (env `CRUCIBLE_MODE`):

- **`default`** — 3 synthetic agents (Skeptic + Domain Expert + Stakeholder). Balanced critique. *Recommended.*
- **`mirofish`** — *zero* synthetic agents; only entities MiroFish auto-extracts from the briefing. Closest to vanilla MiroFish behavior. Use when you want a clean baseline.
- **`miroshark`** — 5 synthetic agents (above + Provocateur + Futurist). Wilder personas, higher temperature, more vivid character voices. Architecture stays the same as `default` (still Zep + OASIS — no Neo4j swap).

The architecture is identical across modes. Only the synthetic-agent layer changes.

### LLM provider compatibility

Any OpenAI-compatible endpoint works. The default is a local
[`glm-proxy`](https://github.com/666ghj/MiroFish/blob/main/glm-proxy.py) on `http://127.0.0.1:8011/v1`,
but you can point at OpenRouter, OpenAI, or anything else via env vars or CLI flags.

```bash
# Local glm-proxy (default)
export LLM_BASE_URL="http://127.0.0.1:8011/v1"
export LLM_API_KEY="$ZAI_API_KEY"
export LLM_MODEL_NAME="glm-4.6"

# OpenRouter
export LLM_BASE_URL="https://openrouter.ai/api/v1"
export LLM_API_KEY="$OPENROUTER_API_KEY"
export LLM_MODEL_NAME="anthropic/claude-haiku-4.5"

# OpenAI direct
export LLM_BASE_URL="https://api.openai.com/v1"
export LLM_API_KEY="$OPENAI_API_KEY"
export LLM_MODEL_NAME="gpt-5.4-mini"
```

Each script also accepts `--llm-base-url`, `--llm-api-key`, `--llm-model` directly.

### Quickstart

Prerequisites:

- A running MiroFish backend (default `http://127.0.0.1:5002`). See [MiroFish docs](https://github.com/666ghj/MiroFish).
- An OpenAI-compatible LLM endpoint with your API key.
- Python ≥ 3.11 with `openai`, `requests`, `matplotlib`, `pillow` (matplotlib + pillow are only needed for `replay.gif`).

```bash
# Install
git clone https://github.com/<your-handle>/crucible-sim.git
cd crucible-sim
pip install openai requests matplotlib pillow

# Set env (pick one provider; here: OpenRouter)
export LLM_BASE_URL="https://openrouter.ai/api/v1"
export LLM_API_KEY="$OPENROUTER_API_KEY"
export LLM_MODEL_NAME="anthropic/claude-haiku-4.5"

# Run end-to-end on the AWS outage demo briefing
WORK=./out/aws-outage-$(date +%Y%m%d)
mkdir -p $WORK/raw

# Step 1 — pre-flight audit
python crucible/preflight.py \
  --briefing inputs/aws-outage-briefing.md \
  --out $WORK/preflight.json

# Step 2 — sim with synthetic agents (mode=default | mirofish | miroshark)
python crucible/run_pipeline.py \
  --backend http://127.0.0.1:5002 \
  --doc inputs/aws-outage-briefing.md \
  --requirement "Discuss AWS US-East-1 outage on May 8, 2026..." \
  --out $WORK \
  --preflight $WORK/preflight.json \
  --mode default \
  --max-rounds 5 --platform twitter

# Step 3 — disagreement scan
python crucible/disagreement_scan.py \
  --results-dir $WORK \
  --out $WORK/disagreement_pairs.json

# Step 4 — adversarial interview (R1 + R2 + R3)
python crucible/adversarial_interview.py \
  --backend http://127.0.0.1:5002 \
  --simulation-id <sim_id from manifest.json> \
  --results-dir $WORK \
  --preflight $WORK/preflight.json \
  --pairs $WORK/disagreement_pairs.json \
  --out $WORK/interviews_r1_r2_r3.json

# Step 5 — Pass A + B reports
python crucible/twopass_report.py \
  --backend http://127.0.0.1:5002 \
  --simulation-id <sim_id> \
  --results-dir $WORK \
  --briefing inputs/aws-outage-briefing.md \
  --interviews $WORK/interviews_r1_r2_r3.json \
  --out-a $WORK/report_pass_A.md \
  --out-b $WORK/report_pass_B.md \
  --passes A,B

# Step 6 — Pass C gap audit
python crucible/gap_audit.py \
  --results-dir $WORK \
  --preflight $WORK/preflight.json \
  --interviews $WORK/interviews_r1_r2_r3.json \
  --out $WORK/report_pass_C_gap.md

# Step 7 — bundle (single HTML + tarball)
python crucible/bundle.py --out $WORK
```

### Project layout

```
crucible/
├── preflight.py              # Layer 1: red-team briefing audit
├── synthetic_agents.py       # Layer 2: 3 / 0 / 5 synthetic agents (mode-aware)
├── run_pipeline.py           # Sim orchestrator (ontology→build→prepare→inject→start→pull)
├── disagreement_scan.py      # Layer 3a: stance scan → pairs.json
├── adversarial_interview.py  # Layer 3: 3-round interview (R1/R2/R3)
├── twopass_report.py         # Layer 4a/b: Pass A neutral + Pass B sharp
├── gap_audit.py              # Layer 4c: Pass C missing-angle audit
└── bundle.py                 # 7-section static HTML + tarball
```

### Inspiration & credits

This project would not exist without the work of:

- **[MiroFish](https://github.com/666ghj/MiroFish)** — the lighter Zep Cloud–backed swarm engine (REST + OASIS) that crucible-sim talks to. The four-layer design is *additive* — we never modify MiroFish itself.
- **[MiroShark](https://github.com/666ghj/MiroShark)** — MiroFish's earlier sibling, with the Polymarket-flavored replay-GIF / share-card vibe that inspired the visual bundling.

We **do not** fork either project, do not modify their backends, and do not depend on Neo4j (MiroShark's heavier graph backend). Crucible runs on top of MiroFish's existing `/api/*` endpoints.

### Known limitations

- `MiroFish /api/simulation/<sim_id>/run-status` can stick at `current_round=0` for a whole short run; we treat `env-status.twitter_available=false` + DB row plateau as completion.
- MiroFish's `/api/report/generate` is a glm-backed ReACT pipeline that occasionally trips a content filter (z-ai code 1301). Pass A automatically falls back to a direct LLM call with a neutral framing prompt.
- Synthetic agents are appended *after* MiroFish's prepare phase. We bypass Zep entity extraction for them, so they don't appear in the knowledge graph.

### License

MIT — see [`LICENSE`](LICENSE).

---

## 中文

### 背景

[MiroFish](https://github.com/666ghj/MiroFish) 与其前身 [MiroShark](https://github.com/666ghj/MiroShark) 证明了
跑一场低成本的多 agent 社交仿真是可行的，输出帖子也具备一定连贯性。但开箱即用时，
swarm 倾向于**给 briefing 盖章背书**：从 briefing 抽出来的真实实体彼此抄观点，没人挑战
那些用来撑起结论的关键数字，最后报告综述的也只是 swarm "说过什么"，而不是 swarm "没探索什么"。

`crucible-sim` **不 fork** 底层引擎，仅作为编排器调用 MiroFish 现有的 REST API，加挂 4 层：

| 层 | 作用 |
|---|---|
| **1. 事前审计** | 用 LLM 红队扫 briefing，抽出具名实体、数字声明、争议点和**缺失角度**；锁定整篇里最薄弱的单一声明，作为 R3 信仰挑战的"靶子"。 |
| **2. 合成 agent 注入** | 生成 3-5 个**虚构具名**角色（Skeptic / Domain Expert / Stakeholder，可选 + Provocateur / Futurist），每个都有显式 conflict_with 至少一个真实 agent 的私有 prior。直接 append 到 simulation_config + twitter_profiles.csv，绕过 Zep 实体抽取。 |
| **3. 三轮对抗式采访** | <ul><li>**R1 自我陈述**：每个 agent 一道针对性问题</li><li>**R2 跨方对线**：检出立场分歧的 pair，双向追问"A 说 X / B 说 Y，怎么调和？"</li><li>**R3 信仰挑战**：所有 agent 同一道题——"briefing 里最弱的论断在哪？"</li></ul> |
| **4. 三轮报告** | <ul><li>**Pass A** 中性综述（走 MiroFish 自带 ReACT；遇内容过滤自动 fallback 到直接 LLM 调用）</li><li>**Pass B** 锐利批评（带视角，必须引用 agent 原话）</li><li>**Pass C 盲区审计**：把 preflight 的 missing_angles × posts × R3 反驳次数对账</li></ul> |

最终产出一个自洽的静态 HTML（含 KG / Replay GIF / R1-R3 tab / 三轮报告并列）+ 一个 tar 包。

### 三种模式

通过 `--mode`（或环境变量 `CRUCIBLE_MODE`）选择：

- **`default`**（推荐）—— 3 个合成 agent（Skeptic + Expert + Stakeholder），均衡批评。
- **`mirofish`**—— **零**合成 agent，只用 MiroFish 从 briefing 自动抽取的真实实体。最接近原版 MiroFish 行为，适合做对照基线。
- **`miroshark`**—— 5 个合成 agent（在 default 之上 + Provocateur + Futurist），人物语言更鲜明、风格更跳脱、温度更高。**底层架构不变**（仍走 Zep + OASIS，不切换 Neo4j），只是合成层"飘逸一些"。

三种模式的底层管线**完全一致**，只有合成 agent 这一层不同。

### LLM 提供商

任何 OpenAI 兼容的端点都行。默认是本地的 `glm-proxy`（127.0.0.1:8011/v1），但可以无缝切到 OpenRouter、OpenAI 直连等。

```bash
# 本地 glm-proxy（默认）
export LLM_BASE_URL="http://127.0.0.1:8011/v1"
export LLM_API_KEY="$ZAI_API_KEY"
export LLM_MODEL_NAME="glm-4.6"

# OpenRouter
export LLM_BASE_URL="https://openrouter.ai/api/v1"
export LLM_API_KEY="$OPENROUTER_API_KEY"
export LLM_MODEL_NAME="anthropic/claude-haiku-4.5"
```

每个脚本同时支持 CLI 参数 `--llm-base-url` / `--llm-api-key` / `--llm-model`，不强制走环境变量。

### 致谢

本项目的灵感来自：

- **[MiroFish](https://github.com/666ghj/MiroFish)** —— Zep Cloud + OASIS 的轻量级群体仿真引擎，crucible-sim 在它的 REST API 上加挂 4 层工具，**从不修改其后端**。
- **[MiroShark](https://github.com/666ghj/MiroShark)** —— MiroFish 的前身，其 Polymarket 风的回放 GIF / 分享卡风格启发了 crucible 的视觉打包。

crucible-sim **不 fork**、不依赖 Neo4j，是一个纯 Python 编排器。所有"飘逸"的部分都源于合成 agent 和 prompt 工程，与底层引擎解耦。

### 已知限制

- `/api/simulation/<sim_id>/run-status` 在短跑 sim 里有时会一直停在 `current_round=0`；我们用 `env-status.twitter_available=false` + DB 行数 plateau 作为完成信号。
- MiroFish 自带的 ReACT 报告偶尔会被 z-ai 1301 内容过滤拦截；Pass A 会自动 fallback 到中性 prompt 的直接 LLM 调用。
- 合成 agent 在 MiroFish prepare 阶段**之后**注入，绕过了 Zep 实体抽取，因此不出现在知识图谱中。

### 许可证

MIT — 见 [`LICENSE`](LICENSE)。
