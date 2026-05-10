#!/usr/bin/env python3
"""Layer 3: 3-round adversarial interview.

R1 self-statement   — per-agent tailored Q
R2 cross-fire       — for each disagreement pair (A,B), ask A how to rebut B
                      and ask B how to rebut A
R3 weakest-claim    — same Q (built from preflight.weakest_claim) to all agents

Modes:
  online   call MiroFish/OASIS /interview/{batch,all} (requires live env)
  offline  LLM roleplay each agent locally (no backend dependency)
  auto     probe env-status; downgrade to offline when env is dead.
           This is the default — OASIS round-counter freezes have stranded
           interviews before, and the interview only needs profiles+posts to
           proceed.

Writes interviews_r1_r2_r3.json. Both modes emit the same schema:
  R1_self_statement: [{agent_id, agent_name, question, answer}, ...]
  R2_cross_fire    : flat list of {agent_id, agent_name, topic_key,
                                   facing, side, question, answer}
                     (each pair contributes 2 entries — A→B, B→A)
  R3_weakest_claim : [{agent_id, agent_name, question, answer}, ...]
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
    # Prompt asks for ≤55 字; enforce hard cap so "≤55" is a real contract.
    return q.strip('"').strip("「」").strip("“”").strip()[:55]


def build_r2_questions(pair):
    """Build (q_for_supportive, q_for_opposed) from a disagreement pair.

    disagreement_pairs.json uses keys `id` and `quote` for sub-agents
    (not `agent_id` / `key_quote`)."""
    topic = pair["topic_label"]
    a = pair["supportive_agent"]
    b = pair["opposed_agent"]
    q_ab = (f"在「{topic}」这个话题上，{b['name']} 主张 "
            f"『{(b.get('quote') or '反对你的立场')[:120]}』。"
            f"你（{a['name']}）正面回应 ta 的论证，并指出最大漏洞。")
    q_ba = (f"在「{topic}」这个话题上，{a['name']} 主张 "
            f"『{(a.get('quote') or '反对你的立场')[:120]}』。"
            f"你（{b['name']}）正面回应 ta 的论证，并指出最大漏洞。")
    return q_ab[:600], q_ba[:600]


def build_r3_prompt(weakest_claim):
    return (f"本次 briefing 里有这条声明：『{weakest_claim}』。"
            f"从你的视角看，这个声明哪一句话最经不起追问？"
            f"给出最强的反方证据或反例（哪怕你之前的发帖是支持立场，也要诚实陈述）。")


# -------- online (live OASIS env) --------------------------------------------


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


def env_alive(backend, sim_id, *, timeout=30):
    try:
        es = requests.post(f"{backend}/api/simulation/env-status",
                           json={"simulation_id": sim_id},
                           timeout=timeout).json()
    except Exception as e:
        return False, f"env-status error: {e}"
    data = (es.get("data") or {})
    if data.get("env_alive"):
        return True, data
    return False, data


class EnvDiedMidFlight(Exception):
    """Raised when env-status drops to env_alive=False between rounds."""


def _require_env_alive(backend, sim_id, *, where):
    alive, info = env_alive(backend, sim_id)
    if not alive:
        raise EnvDiedMidFlight(f"env not alive before {where}: {info}")


def run_online(args, agents, pairs, weakest_claim, requirement, client):
    """Round 1/2/3 against live OASIS env. Re-probes env-status between rounds
    so a mid-flight env death surfaces as a clean exception (caller in auto
    mode will downgrade to offline)."""
    print("\n=== R1 self-statement (online) ===")
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
    print(f"[OK]   R1 done in {elapsed_r1:.1f}s "
          f"({sum(1 for it in r1_items if it['answer'])}/{len(r1_items)} answered)")

    # -------- R2: cross-fire
    _require_env_alive(args.backend, args.simulation_id, where="R2")
    print("\n=== R2 cross-fire (online) ===")
    r2_items = []
    pair_log = []
    for p in pairs:
        a = p["supportive_agent"]
        b = p["opposed_agent"]
        q_ab, q_ba = build_r2_questions(p)
        r2_items.append({"agent_id": a["id"], "agent_name": a["name"],
                         "question": q_ab, "topic_key": p["topic_key"],
                         "facing": b["name"], "side": "supportive"})
        r2_items.append({"agent_id": b["id"], "agent_name": b["name"],
                         "question": q_ba, "topic_key": p["topic_key"],
                         "facing": a["name"], "side": "opposed"})
        pair_log.append({"topic": p["topic_label"],
                         "supportive": a["name"], "opposed": b["name"]})
        print(f"  - [{p['topic_key']}] {a['name'][:25]} ⇄ {b['name'][:25]}")

    if r2_items:
        elapsed_r2 = 0
        for i in range(0, len(r2_items), 2):
            chunk = r2_items[i:i + 2]
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
        print(f"[OK]   R2 done in {elapsed_r2:.1f}s "
              f"({answered}/{len(r2_items)} answered)")
    else:
        elapsed_r2 = 0
        print("[skip] no pairs → R2 empty")

    # -------- R3: weakest-claim challenge
    _require_env_alive(args.backend, args.simulation_id, where="R3")
    print("\n=== R3 weakest-claim challenge (online) ===")
    r3_prompt = build_r3_prompt(weakest_claim)
    print(f"  weakest: {weakest_claim[:100]}")
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
    print(f"[OK]   R3 done in {elapsed_r3:.1f}s "
          f"({answered_r3}/{len(r3_items)} answered)")
    return {
        "R1_self_statement": r1_items,
        "R2_cross_fire": r2_items,
        "R3_weakest_claim": r3_items,
        "pairs_log": pair_log,
        "timings": {"R1_s": round(elapsed_r1, 1),
                    "R2_s": round(elapsed_r2, 1),
                    "R3_s": round(elapsed_r3, 1)},
    }


# -------- offline (LLM roleplay) ---------------------------------------------


def llm_call(client, model, prompt, *, max_tokens=600, temperature=0.7):
    r = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return (r.choices[0].message.content or "").strip()


def roleplay_answer(client, model, agent, question, *, ctx=""):
    """Returns (answer, error). On any LLM failure, answer is "" so downstream
    consumers (twopass_report.fmt_r2/fmt_r3, gap_audit) silently drop the row
    via their `if ans:` gate, instead of leaking '[error: ...]' as if it were a
    real interview response."""
    sample = "\n".join(f"- {p[:200]}" for p in agent["posts"][:3]) or "(没发帖)"
    prompt = f"""你现在扮演下面这位 agent，用第一人称回答记者提问。

== 你是 ==
- 名: {agent['name']}
- bio: {agent['bio'][:480]}
- 你之前发过的帖:
{sample}

{ctx}

== 记者问 ==
{question}

== 回答要求 ==
- 第一人称中文，120-220 字
- 必须正面回应问题，可以反驳/补充/承认困难
- 引用具体数字、对手名、事实，不要空话
- 不要重复你之前的帖子原文
- 直接输出回答，不要任何前缀如"我认为"、"作为..."
"""
    try:
        return llm_call(client, model, prompt, max_tokens=500, temperature=0.7), None
    except Exception as e:
        return "", f"{type(e).__name__}: {e}"


def run_offline(args, agents, pairs, weakest_claim, requirement, client):
    """LLM roleplay R1/R2/R3 entirely client-side. Same output schema as online."""
    # -------- R1
    print("\n=== R1 self-statement (offline) ===")
    t0 = time.time()
    r1_items = []
    r1_errors = 0
    for uid in sorted(agents.keys()):
        a = agents[uid]
        try:
            q = gen_r1_question(client, args.llm_model, a, requirement)
        except Exception as e:
            print(f"  [warn] gen R1 failed for {uid}: {e}")
            q = "你这次发言里最不确定的判断是哪一个？为什么？"
        ans, err = roleplay_answer(client, args.llm_model, a, q)
        rec = {"agent_id": uid, "agent_name": a["name"],
               "question": q, "answer": ans}
        if err:
            rec["error"] = err
            r1_errors += 1
        r1_items.append(rec)
        marker = "✗" if err else "→"
        print(f"  [{uid:>2}] {a['name'][:24]:<24} {marker} Q={q[:48]}…")
    elapsed_r1 = time.time() - t0
    answered_r1 = sum(1 for it in r1_items if it["answer"])
    print(f"[OK]   R1 done in {elapsed_r1:.1f}s "
          f"({answered_r1}/{len(r1_items)} answered, {r1_errors} errors)")

    # -------- R2 (flat list, matching online schema)
    print(f"\n=== R2 cross-fire (offline, {len(pairs)} pairs × 2) ===")
    t0 = time.time()
    r2_items = []
    pair_log = []
    for p in pairs:
        topic_label = p.get("topic_label") or p.get("topic_key", "")
        topic_key = p.get("topic_key", topic_label)
        a = p["supportive_agent"]
        b = p["opposed_agent"]
        a_id = a.get("id")
        b_id = b.get("id")
        if a_id not in agents or b_id not in agents:
            print(f"  [skip] pair refs missing agent_id (sup={a_id} opp={b_id})")
            continue
        q_ab, q_ba = build_r2_questions(p)
        ans_a, err_a = roleplay_answer(client, args.llm_model, agents[a_id], q_ab)
        ans_b, err_b = roleplay_answer(client, args.llm_model, agents[b_id], q_ba)
        rec_a = {"agent_id": a_id, "agent_name": a["name"],
                 "topic_key": topic_key, "facing": b["name"],
                 "side": "supportive", "question": q_ab, "answer": ans_a}
        if err_a:
            rec_a["error"] = err_a
        rec_b = {"agent_id": b_id, "agent_name": b["name"],
                 "topic_key": topic_key, "facing": a["name"],
                 "side": "opposed", "question": q_ba, "answer": ans_b}
        if err_b:
            rec_b["error"] = err_b
        r2_items.append(rec_a)
        r2_items.append(rec_b)
        pair_log.append({"topic": topic_label,
                         "supportive": a["name"], "opposed": b["name"]})
        print(f"  - [{topic_key}] {a['name'][:25]} ⇄ {b['name'][:25]}")
    elapsed_r2 = time.time() - t0
    answered_r2 = sum(1 for it in r2_items if it["answer"])
    r2_errors = sum(1 for it in r2_items if it.get("error"))
    print(f"[OK]   R2 done in {elapsed_r2:.1f}s "
          f"({answered_r2}/{len(r2_items)} answered, {r2_errors} errors)")

    # -------- R3
    print("\n=== R3 weakest-claim (offline) ===")
    t0 = time.time()
    r3_prompt = build_r3_prompt(weakest_claim)
    print(f"  weakest: {weakest_claim[:100]}")
    r3_items = []
    r3_errors = 0
    for uid in sorted(agents.keys()):
        a = agents[uid]
        ans, err = roleplay_answer(
            client, args.llm_model, a, r3_prompt,
            ctx=f"== 上下文 ==\n这条声明在初步审计中被标为最弱：「{weakest_claim}」")
        rec = {"agent_id": uid, "agent_name": a["name"],
               "question": r3_prompt, "answer": ans}
        if err:
            rec["error"] = err
            r3_errors += 1
        r3_items.append(rec)
        marker = "✗ err" if err else f"answered ({len(ans)}c)"
        print(f"  [{uid:>2}] {a['name'][:24]:<24} {marker}")
    elapsed_r3 = time.time() - t0
    answered_r3 = sum(1 for it in r3_items if it["answer"])
    print(f"[OK]   R3 done in {elapsed_r3:.1f}s "
          f"({answered_r3}/{len(r3_items)} answered, {r3_errors} errors)")

    return {
        "R1_self_statement": r1_items,
        "R2_cross_fire": r2_items,
        "R3_weakest_claim": r3_items,
        "pairs_log": pair_log,
        "timings": {"R1_s": round(elapsed_r1, 1),
                    "R2_s": round(elapsed_r2, 1),
                    "R3_s": round(elapsed_r3, 1)},
    }


# -------- main ----------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="http://127.0.0.1:5002")
    ap.add_argument("--simulation-id", required=True)
    ap.add_argument("--results-dir", required=True)
    ap.add_argument("--preflight", required=True)
    ap.add_argument("--pairs", required=True, help="disagreement_pairs.json")
    ap.add_argument("--platform", default="twitter")
    ap.add_argument("--out", required=True)
    ap.add_argument("--mode", choices=["online", "offline", "auto"], default="auto",
                    help="online=require live OASIS env (legacy behaviour); "
                         "offline=LLM roleplay only; "
                         "auto=probe env-status, downgrade to offline when dead.")
    ap.add_argument("--llm-base-url",
                    default=os.environ.get("LLM_BASE_URL",
                                           "http://127.0.0.1:8011/v1"))
    ap.add_argument("--llm-api-key",
                    default=os.environ.get("LLM_API_KEY",
                                           os.environ.get("OPENROUTER_API_KEY",
                                           os.environ.get("ZAI_API_KEY", ""))))
    ap.add_argument("--llm-model",
                    default=os.environ.get("LLM_MODEL_NAME", "glm-4.7"))
    args = ap.parse_args()

    raw_dir = Path(args.results_dir) / "raw"
    pre = json.loads(Path(args.preflight).read_text())
    pairs_blob = json.loads(Path(args.pairs).read_text())
    pairs = pairs_blob.get("pairs", [])
    weakest = pre.get("weakest_claim", "")

    config_blob = _load(raw_dir / "art_config.json")
    requirement = ((config_blob.get("body") or {}).get("data", {})
                   .get("simulation_requirement", "")
                   or (config_blob.get("body") or {}).get("simulation_requirement", ""))

    # ---- mode resolution
    chosen = args.mode
    env_info = None
    if chosen in ("online", "auto"):
        alive, env_info = env_alive(args.backend, args.simulation_id)
        if alive:
            print(f"[OK] env alive: {env_info}")
            chosen = "online"
        else:
            if args.mode == "online":
                print(f"[FAIL] mode=online but env not alive: {env_info}")
                sys.exit(3)
            print(f"[INFO] env not alive ({env_info}); auto-downgrade to offline.")
            chosen = "offline"
    else:
        print("[INFO] mode=offline (skipping env-status probe)")

    agents = gather_agents(raw_dir)
    print(f"[OK] {len(agents)} agents loaded; {len(pairs)} pairs; "
          f"weakest_claim={weakest[:60]}")
    client = OpenAI(api_key=args.llm_api_key, base_url=args.llm_base_url)

    if chosen == "online":
        try:
            rounds = run_online(args, agents, pairs, weakest, requirement, client)
        except (requests.exceptions.RequestException, requests.HTTPError,
                EnvDiedMidFlight) as e:
            if args.mode == "auto":
                print(f"\n[WARN] online interview path raised {type(e).__name__}: "
                      f"{e}; env likely died after env-status probe. "
                      "Re-running all rounds in offline mode.")
                chosen = "offline"
                rounds = run_offline(args, agents, pairs, weakest, requirement, client)
            else:
                # mode=online — caller asked for hard-fail behavior.
                raise
    else:
        rounds = run_offline(args, agents, pairs, weakest, requirement, client)

    out = Path(args.out)
    out.write_text(json.dumps({
        "simulation_id": args.simulation_id,
        "platform": args.platform,
        "model": args.llm_model,
        "mode": chosen,
        "env_status": env_info,
        "weakest_claim": weakest,
        "pairs": rounds["pairs_log"],
        "timings": rounds["timings"],
        "R1_self_statement": rounds["R1_self_statement"],
        "R2_cross_fire": rounds["R2_cross_fire"],
        "R3_weakest_claim": rounds["R3_weakest_claim"],
    }, ensure_ascii=False, indent=2))
    print(f"\n[OK] wrote {out}  (mode={chosen})")


if __name__ == "__main__":
    main()
