#!/usr/bin/env python3
"""Layer 1: pre-flight briefing audit.

Reads a briefing markdown, asks glm-4.6 to surface:
- named entities (companies, products, people)
- numerical claims (with potential dispute angle)
- contested points (with sides)
- missing angles (perspectives a typical sim swarm would not cover)
- weakest single claim (the one most ripe for adversarial challenge)

Writes preflight.json next to --out.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from openai import OpenAI


PROMPT = """你是一名敌对式风险审计员（adversarial red-team auditor）。
下面是一份事件 briefing，你要为后续 swarm-simulation 做"事前审计"。
**只输出严格的 JSON**（不要 markdown fence、不要前言）。

== Briefing ==
{briefing}

== 输出 schema ==
{{
  "named_entities": ["..."],                          // 至少 8 个具名实体（公司、产品、人、地点、产品型号）
  "numerical_claims": [                               // 至少 5 条带数字的声明
    {{
      "claim": "原文里数字声明的简述",
      "source_quote": "briefing 原文最相关的一句（≤80 字）",
      "potential_dispute": "为什么这个数字可能误导/可被挑战，给一句具体反驳"
    }}
  ],
  "contested_points": [                               // 至少 4 个争议点
    {{"topic": "...", "sides": ["..A 立场", "..B 立场"]}}
  ],
  "missing_angles": [                                 // 至少 6 个 briefing 没覆盖但本应被讨论的角度
    "..."
  ],
  "weakest_claim": "整篇 briefing 里最薄弱、最容易被外部质疑的单一声明（一句话）"
}}

要求：
- 每个 numerical_claim.potential_dispute 必须给出**具体**反驳（不是"可能不准"）
- missing_angles 要点出**本可有但缺席的群体/视角**（如监管、中小企业、技术债、海外用户、保险业、SRE 反方等）
- weakest_claim 一定是单一声明，并且能让一个聪明的对手用三句话击穿
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--briefing", required=True)
    ap.add_argument("--out", required=True, help="preflight.json")
    ap.add_argument("--llm-base-url",
                    default=os.environ.get("LLM_BASE_URL",
                                           "http://127.0.0.1:8011/v1"))
    ap.add_argument("--llm-api-key",
                    default=os.environ.get("LLM_API_KEY",
                                           os.environ.get("OPENROUTER_API_KEY",
                                           os.environ.get("ZAI_API_KEY", ""))))
    ap.add_argument("--llm-model",
                    default=os.environ.get("LLM_MODEL_NAME", "glm-4.6"))
    ap.add_argument("--max-tokens", type=int, default=4000)
    args = ap.parse_args()

    briefing = Path(args.briefing).read_text()
    if len(briefing.strip()) < 200:
        print(f"[FAIL] briefing too short ({len(briefing)} chars)")
        sys.exit(2)

    client = OpenAI(api_key=args.llm_api_key, base_url=args.llm_base_url)
    t0 = time.time()
    r = client.chat.completions.create(
        model=args.llm_model,
        messages=[{"role": "user",
                   "content": PROMPT.format(briefing=briefing)}],
        temperature=0.4,
        max_tokens=args.max_tokens,
    )
    elapsed = time.time() - t0
    raw = (r.choices[0].message.content or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        snip = raw[:600]
        print(f"[FAIL] non-JSON response: {e}\n--- first 600 chars ---\n{snip}")
        sys.exit(3)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"[OK] preflight.json -> {out}  ({elapsed:.1f}s, {len(raw)} chars)")
    print(f"     entities={len(data.get('named_entities', []))} "
          f"claims={len(data.get('numerical_claims', []))} "
          f"contested={len(data.get('contested_points', []))} "
          f"missing={len(data.get('missing_angles', []))}")
    print(f"     weakest_claim: {data.get('weakest_claim', '')[:160]}")


if __name__ == "__main__":
    main()
