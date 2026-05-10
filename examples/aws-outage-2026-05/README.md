# Example run: AWS US-East-1 outage (2026-05-10)

Sample artifacts from a real `default`-mode run against the briefing in
[`../../inputs/aws-outage-briefing.md`](../../inputs/aws-outage-briefing.md).

| File | What it is |
|---|---|
| `preflight.json` | Layer 1 audit: 10 entities, 5 numerical claims, 4 contested points, 6 missing angles, locked weakest_claim |
| `synthetic_agents.json` | 3 default-mode synthetic agents (Skeptic / SRE Architect / SMB Owner) |
| `disagreement_pairs.json` | 5 stance pairs derived from posts |
| `interviews_r1_r2_r3.json` | R1 self-statement (16) + R2 cross-fire (10) + R3 weakest-claim (16) |
| `report_pass_A.md` | Neutral synthesis (~5.8 KB) |
| `report_pass_B.md` | Sharp critique "可靠性的虚假账面" (~9.9 KB) |
| `report_pass_C_gap.md` | Gap audit table + 5 next-briefing improvements (~7.1 KB) |
| `manifest.json` | Simulation IDs / mode / model (paths sanitized) |

Knowledge graph + replay GIF + bundled `index.html` are not included in the repo
(too large / regenerable). Re-run the pipeline to produce them locally.

> **Note:** this checked-in example preserves provenance from an earlier run made before the
> `glm-4.7` default bump. Embedded `model` fields in these JSON files show `glm-4.6` /
> `glm-4.5-air`, which are historical run-time values — **not** the current default in
> `crucible/*.py`.
