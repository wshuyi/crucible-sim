#!/usr/bin/env python3
"""Layer 4a/b: Pass A (neutral, MiroFish endpoint) + Pass B (sharp perspective).

Pass A:  POST /api/report/generate  (force_regenerate=True), poll, dump markdown.
Pass B:  custom prompt to glm-4.6, fed posts + R2 cross-fire + R3 weakest.

Usage:
  python twopass_report.py --backend ... --simulation-id ... --results-dir ... \
    --interviews ... --briefing ... --pass A B
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests
from openai import OpenAI


def _unwrap(d):
    if isinstance(d, dict) and "data" in d:
        return d["data"]
    return d


def _load_body(p):
    blob = json.loads(Path(p).read_text())
    body = blob.get("body") if isinstance(blob, dict) else blob
    return body or {}


PASS_A_FALLBACK_PROMPT = """You are a neutral analyst writing a synthesis report on a multi-agent simulation. Write in Chinese (中文), neutral / observational tone, NO opinion-pushing, NO sensational framing.

== A) Briefing (background) ==
{briefing}

== B) Simulation: posts by agent ==
{posts_block}

Output as Markdown (~3500 字) with this structure:

## 一、模拟概览
- 简述参与方数量、平台、轮次、时长。

## 二、主要议题
- 列出 4-5 个被讨论的核心议题，每个议题一句话总结各方立场。

## 三、关键观点摘录
- 每个议题节选 2-3 个有代表性的发言，标注 @agent_name。

## 四、议题分布与情感倾向
- 估算各议题的覆盖度（哪些被频繁提及/被冷处理）。
- 估算整体情绪（中性/负面/正面），用一段话陈述。

## 五、模拟结论
- 一段中立总结，不下结论"应该多云"或"该监管"，只描述分歧未消解的部分。

注意：保持事实陈述风格，不要给出立场倾向；引用使用 `@agent_name` 标注。
"""


def run_pass_a(backend, sim_id, out_path, raw_dir, *,
               briefing=None, llm_base_url=None, llm_api_key=None,
               fallback_model="glm-4.5-air", timeout=2400):
    """Try MiroFish's /api/report/generate first. On content-filter or failure,
    fall back to a direct OpenAI-compatible call (works with glm-proxy or
    OpenRouter — pass llm_base_url + llm_api_key)."""
    s = requests.Session()
    body = s.post(f"{backend}/api/report/generate",
                  json={"simulation_id": sim_id, "force_regenerate": True},
                  timeout=300).json()
    failure_reason = None
    if not body.get("success", True) and body.get("error"):
        failure_reason = body.get("error")
    task_id = (body.get("data") or {}).get("task_id") or body.get("task_id")
    md = None
    if not failure_reason and task_id:
        print(f"  report task_id={task_id}")
        start = time.time()
        while True:
            st = s.post(f"{backend}/api/report/generate/status",
                        json={"task_id": task_id}, timeout=120).json()
            d = st.get("data") or st
            status = d.get("status")
            if status in ("completed", "done", "ready"):
                break
            if status in ("failed", "error"):
                failure_reason = d.get("error") or "status=failed"
                break
            if time.time() - start > timeout:
                failure_reason = "timeout"
                break
            snap = {k: d.get(k) for k in ("status", "progress", "message", "phase") if k in d}
            print(f"  [pass-A] {json.dumps(snap, ensure_ascii=False)}", flush=True)
            time.sleep(15)
        if not failure_reason:
            rep = s.get(f"{backend}/api/report/by-simulation/{sim_id}", timeout=120).json()
            rep_data = (rep.get("data") or rep)
            rid = rep_data.get("report_id") or rep_data.get("id")
            if rid:
                full = s.get(f"{backend}/api/report/{rid}", timeout=120).json()
                full_data = full.get("data") or full
                md = (full_data.get("markdown_content") or full_data.get("content") or "")
                if raw_dir:
                    (Path(raw_dir) / "art_report.json").write_text(
                        json.dumps(full_data, ensure_ascii=False, indent=2))
                    sec = s.get(f"{backend}/api/report/{rid}/sections", timeout=120).json()
                    (Path(raw_dir) / "art_report_sections.json").write_text(
                        json.dumps(sec, ensure_ascii=False, indent=2))
            else:
                failure_reason = "no report_id from /by-simulation"

    if md:
        Path(out_path).write_text(md)
        print(f"[OK] Pass A (MiroFish endpoint) wrote {out_path} ({len(md)} chars)")
        return md

    # Fallback: direct LLM call
    print(f"  [pass-A] MiroFish endpoint unusable ({failure_reason}); "
          f"falling back to direct {fallback_model} synthesis")
    if not (briefing and llm_base_url and llm_api_key):
        print("[FAIL] Pass A fallback needs --briefing, llm_base_url, llm_api_key")
        return None
    profiles = _load_body(Path(raw_dir) / "art_profiles.json")
    profiles = profiles.get("data", {}).get("profiles") or profiles.get("profiles") or []
    posts_body = _load_body(Path(raw_dir) / "art_posts.json")
    posts = posts_body.get("data", {}).get("posts") or posts_body.get("posts") or []
    posts_block = aggregate_posts(profiles, posts)
    prompt = PASS_A_FALLBACK_PROMPT.format(
        briefing=briefing[:2200], posts_block=posts_block[:8000])
    client = OpenAI(api_key=llm_api_key, base_url=llm_base_url)
    t0 = time.time()
    r = client.chat.completions.create(
        model=fallback_model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.4, max_tokens=10000,
    )
    md = (r.choices[0].message.content or "").strip()
    if md.startswith("```"):
        md = md.strip("`")
        if md.startswith("markdown"):
            md = md[8:]
        md = md.strip()
    Path(out_path).write_text(md)
    print(f"[OK] Pass A (fallback) wrote {out_path} ({len(md)} chars in {time.time()-t0:.1f}s)")
    return md


PASS_B_PROMPT = """你是 The Atlantic / The Information 风格的特稿记者。下面给你：
A) 一份事件 briefing
B) 一场 swarm-simulation 的核心发帖
C) 跨方对线（cross-fire）采访结果
D) 信仰挑战（weakest-claim challenge）采访结果

任务：写一篇**带明确视角**的批评性长文（约 4500-5500 中文字符），主张：
> "围绕本次事件的主流叙事低估了系统性风险，并被几个未经检验的数字和框架绑架。"

硬要求：
1. 不抄 briefing 描述，每节都要引用 simulation 的具体 agent 发言（带 @name）。
2. 必须使用 R2 cross-fire 里至少 4 条具体反驳；必须用 R3 weakest-claim 里至少 5 个具名 agent 的回应。
3. 标题要尖锐（参考"Tradable Disaster"、"The 38% Lie"风格）；3-4 节，每节小标题。
4. 中段要明确点出：本次 sim 中至少 2 个被反复 parrot 的数字（如 38%、1.6%）和它们如何瓦解。
5. 结尾留一句尖锐的 take-away，不写"综上所述"。
6. 输出纯 Markdown，不带 fence。

== A) Briefing ==
{briefing}

== B) Posts (按 agent 聚合，每 agent 至多 4 帖) ==
{posts_block}

== C) R2 Cross-fire（A 反驳 B / B 反驳 A）==
{r2_block}

== D) R3 Weakest-Claim Challenge ==
weakest_claim: {weakest_claim}
{r3_block}
"""


def aggregate_posts(profiles, posts):
    by_agent = {}
    for p in profiles:
        try:
            uid = int(p.get("user_id"))
        except (TypeError, ValueError):
            continue
        by_agent[uid] = {"name": p.get("name") or f"agent_{uid}", "posts": []}
    for post in posts:
        try:
            uid = int(post.get("user_id"))
        except (TypeError, ValueError):
            continue
        if uid in by_agent:
            content = (post.get("content") or "").strip()
            if content:
                by_agent[uid]["posts"].append(content)
    blocks = []
    for uid in sorted(by_agent.keys()):
        a = by_agent[uid]
        if not a["posts"]:
            continue
        ps = "\n".join(f"  - {p[:280]}" for p in a["posts"][:4])
        blocks.append(f"@{a['name']} (id={uid})\n{ps}")
    return "\n\n".join(blocks)


def fmt_r2(r2_items):
    by_topic = {}
    for it in r2_items:
        tk = it.get("topic_key", "?")
        by_topic.setdefault(tk, []).append(it)
    out = []
    for tk, lst in by_topic.items():
        out.append(f"### topic={tk}")
        for it in lst:
            ans = (it.get("answer") or "").strip().replace("\n", " ")
            if not ans:
                continue
            out.append(f"- @{it['agent_name']} (反驳 @{it.get('facing','?')}): {ans[:600]}")
    return "\n".join(out) or "(no R2 results)"


def fmt_r3(r3_items):
    out = []
    for it in r3_items:
        ans = (it.get("answer") or "").strip().replace("\n", " ")
        if not ans:
            continue
        out.append(f"- @{it['agent_name']}: {ans[:600]}")
    return "\n".join(out) or "(no R3 results)"


def run_pass_b(briefing, raw_dir, interviews_path, out_path, *,
               llm_base_url, llm_api_key, model="glm-4.6",
               max_tokens=12000):
    profiles = _load_body(raw_dir / "art_profiles.json")
    profiles = profiles.get("data", {}).get("profiles") or profiles.get("profiles") or []
    posts_body = _load_body(raw_dir / "art_posts.json")
    posts = posts_body.get("data", {}).get("posts") or posts_body.get("posts") or []
    interviews = json.loads(Path(interviews_path).read_text())

    posts_block = aggregate_posts(profiles, posts)
    r2_block = fmt_r2(interviews.get("R2_cross_fire", []))
    r3_block = fmt_r3(interviews.get("R3_weakest_claim", []))

    prompt = PASS_B_PROMPT.format(
        briefing=briefing[:2400],
        posts_block=posts_block[:9000],
        r2_block=r2_block[:6000],
        weakest_claim=interviews.get("weakest_claim", ""),
        r3_block=r3_block[:6000],
    )
    client = OpenAI(api_key=llm_api_key, base_url=llm_base_url)
    t0 = time.time()
    r = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.75, max_tokens=max_tokens,
    )
    md = (r.choices[0].message.content or "").strip()
    if md.startswith("```"):
        md = md.strip("`")
        if md.startswith("markdown"):
            md = md[8:]
        md = md.strip()
    Path(out_path).write_text(md)
    print(f"[OK] Pass B wrote {out_path} ({len(md)} chars in {time.time()-t0:.1f}s)")
    return md


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="http://127.0.0.1:5002")
    ap.add_argument("--simulation-id", required=True)
    ap.add_argument("--results-dir", required=True)
    ap.add_argument("--briefing", required=True)
    ap.add_argument("--interviews", required=True)
    ap.add_argument("--out-a", required=True)
    ap.add_argument("--out-b", required=True)
    ap.add_argument("--passes", default="A,B")
    ap.add_argument("--llm-base-url",
                    default=os.environ.get("LLM_BASE_URL",
                                           "http://127.0.0.1:8011/v1"))
    ap.add_argument("--llm-api-key",
                    default=os.environ.get("LLM_API_KEY",
                                           os.environ.get("OPENROUTER_API_KEY",
                                           os.environ.get("ZAI_API_KEY", ""))))
    ap.add_argument("--llm-model-b",
                    default=os.environ.get("LLM_MODEL_B", "glm-4.6"))
    args = ap.parse_args()

    raw_dir = Path(args.results_dir) / "raw"
    briefing = Path(args.briefing).read_text()
    passes = [p.strip().upper() for p in args.passes.split(",") if p.strip()]

    if "A" in passes:
        print("\n=== Pass A: neutral synthesis (MiroFish endpoint, fallback to direct LLM) ===")
        run_pass_a(args.backend, args.simulation_id, args.out_a, raw_dir,
                   briefing=briefing,
                   llm_base_url=args.llm_base_url,
                   llm_api_key=args.llm_api_key,
                   fallback_model=os.environ.get("LLM_MODEL_A_FALLBACK",
                                                 "glm-4.5-air"))
    if "B" in passes:
        print("\n=== Pass B: sharp perspective (glm-4.6) ===")
        run_pass_b(briefing, raw_dir, args.interviews, args.out_b,
                   llm_base_url=args.llm_base_url,
                   llm_api_key=args.llm_api_key,
                   model=args.llm_model_b)


if __name__ == "__main__":
    main()
