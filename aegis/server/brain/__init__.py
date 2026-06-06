"""Brain service layer — oservice engine assembly.

Design basis: AEGIS_DESIGN v1.1.0 §7 决策 7 + §8.8 (反复服务形态入 oservice).
All agents use oservice engines directly (S1 bypass: new Engine() instead of assemble()).

S1 sprint status:
- S1-Alerter  aegis/server/alert/platform_alerter.py   ✅ ship 64e022c
- S1-RCA      aegis/server/brain/rca.py                🟡 this commit
- S1-Planner  aegis/server/brain/action_planner.py     🟡 this commit
- S1-Triage   aegis/server/brain/triage.py             ⏳ stub (oservice v0.4.2 待 ship)
"""
