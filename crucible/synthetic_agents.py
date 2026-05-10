#!/usr/bin/env python3
"""Layer 2: synthetic agent generation + injection.

Reads preflight.json + real agent_configs (after MiroFish's prepare step) and
asks an LLM to produce 3 named synthetic agents (Skeptic / Domain Expert /
Personal Stakeholder), each with a private_prior conflicting with at least
one real agent.

Then mutates simulation_config.json (append agent_configs[]) and
twitter_profiles.csv (append rows) IN PLACE so the next /api/simulation/start
loads 11 agents instead of 8.

Returns the synthetic agents dict for downstream bookkeeping.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import uuid
from pathlib import Path

from openai import OpenAI


SYNTH_PROMPT_DEFAULT = """你要为一场 swarm-simulation 注入 3 个虚构具名角色，让模拟从"应声虫"变成"对抗式辩论"。

== 真实 agent 池（不要重复其立场）==
{real_agents_brief}

== Briefing 摘要 ==
{briefing}

== Pre-flight 审计的关键洞察 ==
- 最薄弱的声明（weakest_claim）：{weakest_claim}
- 缺失的视角（missing_angles）：
{missing_angles}
- 争议点：
{contested}

== 任务 ==
**严格输出 JSON 数组**，包含 3 个对象，分别为：
1. **SKEPTIC**: 专门攻击 weakest_claim 的批判者
2. **DOMAIN_EXPERT**: 一位具名领域专家（如 SRE、合规律师、云架构师），覆盖至少一个 missing_angle
3. **STAKEHOLDER**: 一个有切身利益的具名个人（如受影响小企业主、丢钱的散户、政府工作人员）

每个对象 schema：
{{
  "role": "SKEPTIC | DOMAIN_EXPERT | STAKEHOLDER",
  "name": "全名（英文姓名 + 简短身份后缀，如 'Sarah Chen, AWS SRE 7y'）",
  "username": "twitter handle 风格小写下划线 ≤30 字符",
  "entity_type": "Person",
  "stance": "skeptic | expert | stakeholder",
  "sentiment_bias": <float -0.8..0.8>,
  "activity_level": <float 0.6..0.9>,
  "posts_per_hour": <int 3..6>,
  "comments_per_hour": <int 4..8>,
  "influence_weight": <float 0.8..2.0>,
  "private_prior": "一句话的私有立场（具体到事实级反驳，不要空话）",
  "conflicts_with": ["real agent 池里至少 1 个名字"],
  "description": "120-220 字中文 bio：身份、专业背景、为什么对此事件有强观点、如何说话",
  "user_char": "60-120 字中文：性格画像 + 语言风格 + 标志性表达"
}}

要求：
- 三个角色合起来必须覆盖至少 2 个 missing_angle
- private_prior 必须是**具体反驳**：例如不写"他不同意 38%"，而写"38% 在 Polymarket 上低于 5 万 USDC 流动性的 bin，无法构成市场共识"
- 每个 conflicts_with 至少给一个具体真实 agent 名（不能写 "all"）
- 只输出 JSON 数组、不要 markdown fence、不要前言
"""


SYNTH_PROMPT_MIROSHARK = """你要为一场 swarm-simulation 注入 5 个**风格鲜明、思想跳脱**的虚构具名角色，让模拟从"会议室辩论"变成"街头剧场"。

== 真实 agent 池（不要复制其立场）==
{real_agents_brief}

== Briefing 摘要 ==
{briefing}

== Pre-flight 审计的关键洞察 ==
- 最薄弱的声明（weakest_claim）：{weakest_claim}
- 缺失的视角（missing_angles）：
{missing_angles}
- 争议点：
{contested}

== 任务 ==
**严格输出 JSON 数组**，包含 5 个对象，**每个角色必须有鲜明的语言风格和不可被取代的世界观**：
1. **SKEPTIC**: 用犀利数据撕开 weakest_claim 的批判者
2. **DOMAIN_EXPERT**: 一位具名领域专家，必须覆盖至少 1 个 missing_angle
3. **STAKEHOLDER**: 一个有切身利益的具名个人，带具体损失数字
4. **PROVOCATEUR**: 一个故意唱反调的喷子型角色（媒体红人 / 财经评论员 / 退休教授），愿意为流量得罪所有人
5. **FUTURIST**: 一个把眼前事件投射到 5-10 年后的脑洞玩家（科幻作家 / 长期主义投资人 / 末日论者），用看似离谱但有逻辑的预测刺激其他 agent

每个对象 schema：
{{
  "role": "SKEPTIC | DOMAIN_EXPERT | STAKEHOLDER | PROVOCATEUR | FUTURIST",
  "name": "全名（英文姓名 + 简短身份后缀）",
  "username": "twitter handle 风格小写下划线 ≤30 字符",
  "entity_type": "Person",
  "stance": "skeptic | expert | stakeholder | provocateur | futurist",
  "sentiment_bias": <float -1.0..1.0>,
  "activity_level": <float 0.7..1.0>,
  "posts_per_hour": <int 4..8>,
  "comments_per_hour": <int 5..10>,
  "influence_weight": <float 0.9..2.5>,
  "private_prior": "一句话的私有立场（必须有具体反例 / 数字 / 历史类比）",
  "conflicts_with": ["真实 agent 池里至少 1 个名字"],
  "description": "150-280 字中文 bio：身份 + 履历 + 个人事故 + 为什么对此事件如此偏激",
  "user_char": "80-160 字中文：语言风格（含 1-2 句标志性口头禅）+ 思维怪癖"
}}

风格要求：
- 5 个角色合起来必须覆盖至少 3 个 missing_angle
- PROVOCATEUR 必须有可被引用的尖锐金句
- FUTURIST 的 private_prior 应当是非线性预测（如 "5 年内 Polymarket 上的 AWS 概率合约会被 SEC 强制下架，我已经在押反向"）
- 不要写成新闻通讯稿；让每个角色像剧中人物
- 只输出 JSON 数组、不要 markdown fence、不要前言
"""


def _patch_time_config_24h(cfg, *, agents_per_hour):
    """Collapse all hour-buckets to fully active so OASIS doesn't stall on
    off-peak rounds. Used for any mode (default/mirofish/miroshark)."""
    tc = cfg.setdefault("time_config", {})
    tc["agents_per_hour_min"] = agents_per_hour
    tc["agents_per_hour_max"] = agents_per_hour
    tc["peak_hours"] = list(range(24))
    tc["off_peak_hours"] = []
    tc["morning_hours"] = []
    tc["work_hours"] = []
    tc["peak_activity_multiplier"] = 1.0
    tc["off_peak_activity_multiplier"] = 1.0
    tc["morning_activity_multiplier"] = 1.0
    tc["work_activity_multiplier"] = 1.0
    return tc


def patch_24h_only(sim_dir: Path, *, agents_per_hour=None):
    """For mirofish mode: patch time_config without injecting synthetic agents."""
    cfg_path = sim_dir / "simulation_config.json"
    cfg = json.loads(cfg_path.read_text())
    n = agents_per_hour or len(cfg.get("agent_configs", []))
    _patch_time_config_24h(cfg, agents_per_hour=n)
    cfg_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
    return n


def _real_brief(real_agents):
    lines = []
    for a in real_agents:
        lines.append(f"- {a.get('entity_name')} ({a.get('entity_type')}, "
                     f"stance={a.get('stance')}, "
                     f"sentiment={a.get('sentiment_bias')})")
    return "\n".join(lines)


def generate_synthetic(client, model, *, briefing, preflight, real_agents,
                      mode="default", max_tokens=4500):
    """mode: 'default' → 3 synth (SKEPTIC + EXPERT + STAKEHOLDER, balanced critique)
              'miroshark' → 5 synth (above + PROVOCATEUR + FUTURIST, wilder personas)
              'mirofish' → 0 synth (caller should not invoke this)
    """
    if mode == "mirofish":
        return []
    prompt_template = SYNTH_PROMPT_MIROSHARK if mode == "miroshark" else SYNTH_PROMPT_DEFAULT
    expected_count = 5 if mode == "miroshark" else 3
    prompt = prompt_template.format(
        real_agents_brief=_real_brief(real_agents),
        briefing=briefing[:2400],
        weakest_claim=preflight.get("weakest_claim", ""),
        missing_angles="\n".join(f"  - {a}" for a in preflight.get("missing_angles", [])),
        contested="\n".join(
            f"  - {c.get('topic')}: {' / '.join(c.get('sides', []))}"
            for c in preflight.get("contested_points", [])
        ),
    )
    temperature = 0.85 if mode == "miroshark" else 0.7
    r = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=temperature,
        max_tokens=max_tokens if mode != "miroshark" else 6500,
    )
    raw = (r.choices[0].message.content or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    arr = json.loads(raw)
    if not isinstance(arr, list) or len(arr) < expected_count:
        raise ValueError(
            f"expected JSON array of {expected_count}, got: {type(arr)} "
            f"len={len(arr) if hasattr(arr, '__len__') else 'n/a'}"
        )
    return arr[:expected_count]


def inject(sim_dir: Path, synthetic_agents: list[dict], *,
           start_agent_id: int):
    """Append synthetic_agents into simulation_config.json + twitter_profiles.csv.

    Mutates files in place. Returns list of full agent_config dicts as written.
    """
    cfg_path = sim_dir / "simulation_config.json"
    csv_path = sim_dir / "twitter_profiles.csv"
    cfg = json.loads(cfg_path.read_text())
    real_count = len(cfg.get("agent_configs", []))
    if start_agent_id != real_count:
        raise RuntimeError(
            f"start_agent_id={start_agent_id} but agent_configs has {real_count} entries"
        )

    written = []
    csv_rows = []
    for i, sa in enumerate(synthetic_agents):
        aid = start_agent_id + i
        entity_uuid = str(uuid.uuid4())
        entity_name = sa.get("name") or f"synthetic_{aid}"
        active_hours = list(range(24))
        cfg_entry = {
            "agent_id": aid,
            "entity_uuid": entity_uuid,
            "entity_name": entity_name,
            "entity_type": sa.get("entity_type", "Person"),
            "activity_level": float(sa.get("activity_level", 0.75)),
            "posts_per_hour": int(sa.get("posts_per_hour", 4)),
            "comments_per_hour": int(sa.get("comments_per_hour", 6)),
            "active_hours": active_hours,
            "response_delay_min": 2,
            "response_delay_max": 12,
            "sentiment_bias": float(sa.get("sentiment_bias", 0.0)),
            "stance": sa.get("stance", "skeptic"),
            "influence_weight": float(sa.get("influence_weight", 1.0)),
            # extra metadata, MiroFish ignores unknown keys
            "synthetic_role": sa.get("role"),
            "private_prior": sa.get("private_prior", ""),
            "conflicts_with": sa.get("conflicts_with", []),
        }
        cfg["agent_configs"].append(cfg_entry)
        written.append(cfg_entry)
        csv_rows.append({
            "user_id": aid,
            "name": entity_name,
            "username": (sa.get("username") or f"synthetic_{aid}")[:30],
            "user_char": (sa.get("user_char") or "")[:600],
            "description": (sa.get("description") or "")[:1200],
        })

    # Bump time_config to total agent count
    new_count = real_count + len(synthetic_agents)
    _patch_time_config_24h(cfg, agents_per_hour=new_count)
    cfg_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))

    # Append CSV rows
    with csv_path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f,
            fieldnames=["user_id", "name", "username", "user_char", "description"])
        for row in csv_rows:
            w.writerow(row)

    return written


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--briefing", required=True)
    ap.add_argument("--preflight", required=True)
    ap.add_argument("--sim-dir", required=True,
                    help="Path to MiroFish backend uploads/simulations/<sim_id>")
    ap.add_argument("--out", required=True, help="synthetic_agents.json")
    ap.add_argument("--mode",
                    choices=["default", "mirofish", "miroshark"],
                    default=os.environ.get("CRUCIBLE_MODE", "default"))
    ap.add_argument("--llm-base-url",
                    default=os.environ.get("LLM_BASE_URL",
                                           "http://127.0.0.1:8011/v1"))
    ap.add_argument("--llm-api-key",
                    default=os.environ.get("LLM_API_KEY",
                                           os.environ.get("OPENROUTER_API_KEY",
                                           os.environ.get("ZAI_API_KEY", ""))))
    ap.add_argument("--llm-model",
                    default=os.environ.get("LLM_MODEL_NAME", "glm-4.6"))
    args = ap.parse_args()

    briefing = Path(args.briefing).read_text()
    preflight = json.loads(Path(args.preflight).read_text())
    sim_dir = Path(args.sim_dir)
    cfg = json.loads((sim_dir / "simulation_config.json").read_text())
    real_agents = cfg.get("agent_configs", [])
    print(f"[OK] sim_dir has {len(real_agents)} real agents (mode={args.mode})")

    if args.mode == "mirofish":
        # No synthetic injection — but still apply the 24h time_config patch
        # so OASIS doesn't stall on off-peak rounds.
        n = patch_24h_only(sim_dir)
        Path(args.out).write_text(json.dumps(
            {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
             "mode": "mirofish",
             "model": args.llm_model,
             "synthetic_agents": [],
             "written_configs": []},
            ensure_ascii=False, indent=2))
        print("[OK] mirofish mode: 0 synthetic injected. "
              f"sim retains {n} real agents (24h activity patched).")
        return

    client = OpenAI(api_key=args.llm_api_key, base_url=args.llm_base_url)
    t0 = time.time()
    synth = generate_synthetic(client, args.llm_model,
                               briefing=briefing, preflight=preflight,
                               real_agents=real_agents,
                               mode=args.mode)
    print(f"[OK] LLM generated {len(synth)} synthetic agents in {time.time()-t0:.1f}s")
    for s in synth:
        print(f"  - [{s.get('role')}] {s.get('name')[:60]}  "
              f"conflicts_with={s.get('conflicts_with')}")

    written = inject(sim_dir, synth, start_agent_id=len(real_agents))
    Path(args.out).write_text(json.dumps(
        {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
         "mode": args.mode,
         "model": args.llm_model,
         "synthetic_agents": synth,
         "written_configs": written},
        ensure_ascii=False, indent=2))
    print(f"[OK] injected. simulation_config.json now has "
          f"{len(real_agents) + len(written)} agent_configs")
    print(f"[OK] wrote {args.out}")


if __name__ == "__main__":
    main()
