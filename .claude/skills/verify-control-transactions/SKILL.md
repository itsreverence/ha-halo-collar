---
name: verify-control-transactions
description: Exhaustively check the guarded write transactions (fence mode, Find collar, and the Recall whistle with its one-time registration) against every interleaving of refreshes, option changes, telemetry changes, storage outcomes, provider outcomes, and cancellation. Use when a change touches custom_components/halo_collar/controls.py, recall.py, the write path in api.py or transport.py, how switch.py/button.py call into controls or recall, or the helpers those gates use.
---

The canonical, agent-neutral version of this skill lives at
`.agents/skills/verify-control-transactions/SKILL.md`. Read that file in full and
follow it. Edit only that copy; this stub exists so Claude Code registers the skill.
