#!/usr/bin/env python3
"""Layer 3: 3-round adversarial interview.

R1 self-statement   — per-agent tailored Q via /interview/batch
R2 cross-fire       — for each disagreement pair (A,B), ask A how to rebut B
                      and ask B how to rebut A; both via /interview/batch
R3 weakest-claim    — same Q (built from preflight.weakest_claim) to all
                      agents via /interview/all

Writes interviews_r1_r2_r3.json
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


def _load(p):
    return json.loads(Path(p).read_text())


def gather_agents(raw_dir):
    profiles_blob = _load(raw_dir / "art_profiles.json")
    posts_blob = _load(raw_dir / "art_posts.json")
    body = profiles_blob.get("body") or {}
    profiles = body.get("data", {}).get("profiles") or body.get("profiles") or []
    posts = (posts_blob.get("body") or {}).get("data", {}).get("posts", [])
    agents = {}
    for p in profiles:
        try:
            uid = int(p.get("user_id"))
        except (TypeError, ValueError):
            continue
        agents[uid] = {
            "agent_id": uid,
            "name": p.get("name") or f"agent_{uid}",
            "bio": p.get("description") or "",
            "posts": [],
        }
    for post in posts:
        uid = post.get("user_id")
        try:
            uid = int(uid)
        except (TypeError, ValueError):
            continue
        if uid in agents:
            agents[uid]["posts"].append(post.get("content") or "")
    return agents


def gen_r1_question(client, model, agent, requirement):
    sample_posts = "\n".join(f"- {p[:280]}" for p in agent["posts"][:3]) or "(没发帖)"
    prompt = f"""你是调查记者。为下面这位 swarm-simulation agent 写**一个**犀利的自我陈述提问。

== 模拟主题 ==
{requirement[:400]}

== Agent ==
- 名: {agent['name']}
- bio: {agent['bio'][:480]}
- 代表帖:
{sample_posts}

要求：
- 一句中文，≤55 字
- 必须直接挑战 ta 的核心立场，并带具体数字/对手/事实
- 不要"你怎么看"这种泛问
- 只输出问题本身
"""
    r = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.6, max_tokens=140,
    )
    q = (r.choices[0].message.content or "").strip()
    for prefix in ("问题：", "问题:", "Q:", "Q：", "提问："):
        if q.startswith(prefix):
            q = q[len(prefix):].strip()
    return q.strip('"').strip("「」").strip("“”").strip()[:200]


def call_batch(base, sim_id, items, *, platform="twitter", timeout=900):
    r = requests.post(
        f"{base}/api/simulation/interview/batch",
        json={
            "simulation_id": sim_id,
            "interviews": [
                {"agent_id": it["agent_id"], "prompt": it["question"],
                 "platform": platform}
                for it in items
            ],
            "platform": platform,
            "timeout": timeout,
        },
        timeout=timeout + 60,
    )
    r.raise_for_status()
    return r.json()


def call_all(base, sim_id, prompt, *, platform="twitter", timeout=900):
    r = requests.post(
        f"{base}/api/simulation/interview/all",
        json={"simulation_id": sim_id, "prompt": prompt,
              "platform": platform, "timeout": timeout},
        timeout=timeout + 60,
    )
    r.raise_for_status()
    return r.json()


def map_answers(batch_body):
    """Both /interview/batch and /interview/all return data.result.results."""
    raw = (batch_body.get("data") or {}).get("result", {}).get("results", {})
    out = {}
    for k, v in raw.items():
        if isinstance(v, dict):
            uid = v.get("agent_id")
            if uid is None:
                continue
            try:
                uid = int(uid)
            except (TypeError, ValueError):
                continue
            out[uid] = (v.get("response") or "").strip()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="http://127.0.0.1:5002")
    ap.add_argument("--simulation-id", required=True)
    ap.add_argument("--results-dir", required=True)
    ap.add_argument("--preflight", required=True)
    ap.add_argument("--pairs", required=True, help="disagreement_pairs.json")
    ap.add_argument("--platform", default="twitter")
    ap.add_argument("--out", required=True)
    ap.add_argument("--llm-base-url",
                    default=os.environ.get("LLM_BASE_URL",
                                           "http://127.0.0.1:8011/v1"))
    ap.add_argument("--llm-api-key",
                    default=os.environ.get("LLM_API_KEY",
                                           os.environ.get("OPENROUTER_API_KEY",
                                           os.environ.get("ZAI_API_KEY", ""))))
    ap.add_argument("--llm-model",
                    default=os.environ.get("LLM_MODEL_NAME", "glm-4.5-air"))
    args = ap.parse_args()

    raw_dir = Path(args.results_dir) / "raw"
    pre = json.loads(Path(args.preflight).read_text())
    pairs_blob = json.loads(Path(args.pairs).read_text())
    pairs = pairs_blob.get("pairs", [])

    # ---- env-status check
    es = requests.post(f"{args.backend}/api/simulation/env-status",
                       json={"simulation_id": args.simulation_id},
                       timeout=30).json()
    if not (es.get("data") or {}).get("env_alive"):
        print("[FAIL] env not alive — start sim first with force=False to bring "
              "OASIS env back up.")
        sys.exit(3)
    print(f"[OK] env alive: {es.get('data')}")

    agents = gather_agents(raw_dir)
    print(f"[OK] {len(agents)} agents loaded")

    client = OpenAI(api_key=args.llm_api_key, base_url=args.llm_base_url)
    config_blob = _load(raw_dir / "art_config.json")
    requirement = ((config_blob.get("body") or {}).get("data", {})
                   .get("simulation_requirement", "")
                   or (config_blob.get("body") or {}).get("simulation_requirement", ""))

    # -------- R1: self-statement
    print("\n=== R1 self-statement ===")
    r1_items = []
    for uid in sorted(agents.keys()):
        a = agents[uid]
        try:
            q = gen_r1_question(client, args.llm_model, a, requirement)
        except Exception as e:
            print(f"  [warn] gen R1 failed for {uid}: {e}")
            q = "你这次发言里最不确定的判断是哪一个？为什么？"
        r1_items.append({"agent_id": uid, "agent_name": a["name"], "question": q})
        print(f"  Q[{uid:>2}] {a['name'][:30]:<30} → {q[:80]}")

    print(f"\n[POST] /interview/batch  R1×{len(r1_items)} ...")
    t0 = time.time()
    body_r1 = call_batch(args.backend, args.simulation_id, r1_items,
                        platform=args.platform)
    elapsed_r1 = time.time() - t0
    ans_r1 = map_answers(body_r1)
    for it in r1_items:
        it["answer"] = ans_r1.get(it["agent_id"], "")
    print(f"[OK]   R1 done in {elapsed_r1:.1f}s ({sum(1 for it in r1_items if it['answer'])}/{len(r1_items)} answered)")

    # -------- R2: cross-fire
    print("\n=== R2 cross-fire ===")
    r2_items = []
    pair_log = []
    for p in pairs:
        topic = p["topic_label"]
        a = p["supportive_agent"]
        b = p["opposed_agent"]
        # ask a about b
        q_ab = (f"在「{topic}」这个话题上，{b['name']} 主张 "
                f"『{(b.get('quote') or '反对你的立场')[:120]}』。"
                f"你（{a['name']}）正面回应 ta 的论证，并指出最大漏洞。")
        # ask b about a
        q_ba = (f"在「{topic}」这个话题上，{a['name']} 主张 "
                f"『{(a.get('quote') or '反对你的立场')[:120]}』。"
                f"你（{b['name']}）正面回应 ta 的论证，并指出最大漏洞。")
        r2_items.append({"agent_id": a["id"], "agent_name": a["name"],
                         "question": q_ab[:600], "topic_key": p["topic_key"],
                         "facing": b["name"], "side": "supportive"})
        r2_items.append({"agent_id": b["id"], "agent_name": b["name"],
                         "question": q_ba[:600], "topic_key": p["topic_key"],
                         "facing": a["name"], "side": "opposed"})
        pair_log.append({"topic": topic,
                         "supportive": a["name"], "opposed": b["name"]})
        print(f"  - [{p['topic_key']}] {a['name'][:25]} ⇄ {b['name'][:25]}")

    # R2 batch in pair-chunks (each pair has unique agent_ids → no collision)
    if r2_items:
        # Group pairs of 2 items (A→B, B→A) so each batch has unique agent_ids
        elapsed_r2 = 0
        for i in range(0, len(r2_items), 2):
            chunk = r2_items[i:i+2]
            print(f"  [R2 batch {i//2 + 1}/{(len(r2_items)+1)//2}] "
                  f"@{chunk[0]['agent_name'][:20]} "
                  f"+ @{chunk[-1]['agent_name'][:20]}")
            t0 = time.time()
            try:
                body = call_batch(args.backend, args.simulation_id, chunk,
                                  platform=args.platform, timeout=900)
                elapsed_r2 += time.time() - t0
                ans = map_answers(body)
                for it in chunk:
                    it["answer"] = ans.get(it["agent_id"], "")
            except Exception as e:
                print(f"  [warn] R2 batch failed: {e}")
                for it in chunk:
                    it["answer"] = ""
        answered = sum(1 for it in r2_items if it.get("answer"))
        print(f"[OK]   R2 done in {elapsed_r2:.1f}s ({answered}/{len(r2_items)} answered)")
    else:
        elapsed_r2 = 0
        print("[skip] no pairs → R2 empty")

    # -------- R3: weakest-claim challenge
    print("\n=== R3 weakest-claim challenge ===")
    weak = pre.get("weakest_claim", "")
    r3_prompt = (f"本次 briefing 里有这条声明：『{weak}』。"
                 f"从你的视角看，这个声明哪一句话最经不起追问？"
                 f"给出最强的反方证据或反例（哪怕你之前的发帖是支持立场，也要诚实陈述）。")
    print(f"  weakest: {weak[:100]}")
    print(f"\n[POST] /interview/all  R3 to all agents ...")
    t0 = time.time()
    body_r3 = call_all(args.backend, args.simulation_id, r3_prompt,
                       platform=args.platform, timeout=1200)
    elapsed_r3 = time.time() - t0
    ans_r3 = map_answers(body_r3)
    r3_items = []
    for uid in sorted(agents.keys()):
        a = agents[uid]
        r3_items.append({"agent_id": uid, "agent_name": a["name"],
                         "question": r3_prompt,
                         "answer": ans_r3.get(uid, "")})
    answered_r3 = sum(1 for it in r3_items if it["answer"])
    print(f"[OK]   R3 done in {elapsed_r3:.1f}s ({answered_r3}/{len(r3_items)} answered)")

    # ---- write
    out = Path(args.out)
    out.write_text(json.dumps({
        "simulation_id": args.simulation_id,
        "platform": args.platform,
        "model": args.llm_model,
        "timings": {"R1_s": round(elapsed_r1, 1),
                    "R2_s": round(elapsed_r2, 1),
                    "R3_s": round(elapsed_r3, 1)},
        "weakest_claim": weak,
        "pairs": pair_log,
        "R1_self_statement": r1_items,
        "R2_cross_fire": r2_items,
        "R3_weakest_claim": r3_items,
    }, ensure_ascii=False, indent=2))
    print(f"\n[OK] wrote {out}")


if __name__ == "__main__":
    main()
