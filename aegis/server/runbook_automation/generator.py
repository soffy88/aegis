"""Runbook Generator — LLM-based runbook generation from historical incidents."""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from aegis.server.runbook_automation.templates import RunbookStep, RunbookTemplate, StepType

log = logging.getLogger(__name__)


class RunbookGenerator:
    """基于历史故障 + LLM 生成 runbook 模板"""

    def __init__(self, conn):
        self.conn = conn

    async def generate_from_incident(
        self,
        org_id: UUID,
        incident_id: UUID,
        llm_provider: str = "openai",
        model: str = "gpt-4",
    ) -> RunbookTemplate | None:
        """从 Incident 生成 Runbook"""
        # 1. 获取 incident 详情
        incident = await self.conn.fetchrow("""
            SELECT * FROM incidents WHERE id = $1 AND org_id = $2
        """, incident_id, org_id)
        if not incident:
            return None

        # 2. 获取关联的告警
        alerts = await self.conn.fetch("""
            SELECT * FROM alerts WHERE incident_id = $1 ORDER BY created_at
        """, incident_id)

        # 3. 获取同类历史故障的解决动作
        historical_actions = await self._get_historical_actions(org_id, incident["title"])

        # 4. 构建 Prompt
        prompt = self._build_prompt(incident, alerts, historical_actions)

        # 5. 调用 LLM (需要实际集成)
        llm_response = await self._call_llm(prompt, llm_provider, model)

        # 6. 解析为 RunbookTemplate
        return self._parse_response(llm_response, org_id, incident["title"])

    async def _get_historical_actions(self, org_id: UUID, symptom: str) -> list[dict]:
        """获取同类症状的历史解决动作"""
        rows = await self.conn.fetch("""
            SELECT ro.symptom_key, ro.remediation, ro.success, ro.metadata, ro.created_at
            FROM remediation_outcomes ro
            WHERE ro.org_id = $1
            AND ro.symptom_key ILIKE '%' || $2 || '%'
            ORDER BY ro.created_at DESC LIMIT 10
        """, org_id, symptom)
        return [dict(r) for r in rows]

    def _build_prompt(self, incident: dict, alerts: list[dict], historical: list[dict]) -> str:
        """构建 LLM Prompt"""
        return f"""
You are an SRE expert. Generate a runbook template based on this incident:

INCIDENT:
- Title: {incident.get('title')}
- Severity: {incident.get('severity')}
- Status: {incident.get('status')}
- Description: {incident.get('description')}

ASSOCIATED ALERTS:
{json.dumps([dict(a) for a in alerts], indent=2)}

HISTORICAL REMEDIATIONS:
{json.dumps(historical, indent=2)}

Generate a JSON runbook with:
- name: "Auto-generated: <incident title>"
- description: brief summary
- trigger_conditions: {{severity, alert_names, labels, service}}
- steps: array of steps with id, type (shell|http|docker|approval|notify|wait), name, config
- auto_execute: false
- approval_required: true

Output ONLY valid JSON.
"""

    async def _call_llm(self, prompt: str, provider: str, model: str) -> str:
        """调用 LLM API"""
        # TODO: 实际集成 LLM API (OpenAI, Anthropic, Ollama 等)
        # 这里返回模拟响应用于测试
        return json.dumps({
            "name": f"Auto-generated: {prompt[:50]}",
            "description": "Auto-generated runbook from incident analysis",
            "trigger_conditions": {"severity": ["critical", "high"]},
            "steps": [
                {"id": "step1", "type": "shell", "name": "Check service status",
                 "command": "systemctl status nginx", "timeout_seconds": 30},
                {"id": "step2", "type": "approval", "name": "Confirm restart",
                 "approvers": []},
                {"id": "step3", "type": "shell", "name": "Restart service",
                 "command": "systemctl restart nginx", "timeout_seconds": 60,
                 "on_success": ["step4"], "on_failure": []},
                {"id": "step4", "type": "notify", "name": "Notify team",
                 "message": "Service restarted successfully"}
            ],
            "auto_execute": False,
            "approval_required": True
        })

    def _parse_response(self, response: str, org_id: UUID, incident_title: str) -> RunbookTemplate:
        """解析 LLM 响应为 RunbookTemplate"""
        try:
            data = json.loads(response)
            steps = []
            for s in data.get("steps", []):
                step = RunbookStep(
                    id=s["id"],
                    type=StepType(s["type"]),
                    name=s["name"],
                    command=s.get("command"),
                    timeout_seconds=s.get("timeout_seconds", 300),
                    on_success=s.get("on_success", []),
                    on_failure=s.get("on_failure", []),
                    approvers=[UUID(a) for a in s.get("approvers", [])],
                    message=s.get("message"),
                )
                steps.append(step)

            return RunbookTemplate(
                id=UUID(str(uuid.uuid4())),
                org_id=org_id,
                name=data.get("name", f"Auto-generated: {incident_title}"),
                description=data.get("description", ""),
                trigger_conditions=data.get("trigger_conditions", {}),
                steps=steps,
                auto_execute=data.get("auto_execute", False),
                approval_required=data.get("approval_required", True),
            )
        except Exception as e:
            log.error("runbook_parse_failed: %s", e)
            raise ValueError(f"Failed to parse LLM response: {e}")