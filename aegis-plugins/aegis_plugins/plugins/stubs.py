"""Stub plugins — 20 placeholders wired in entry_points, real impl deferred to S5+."""

from __future__ import annotations

from aegis_autoheal_sdk import ActionResult, AutoHealContext, AutoHealPlugin


def _stub(cls_name: str, plugin_name: str, alert: str, desc: str) -> type:
    """Factory for stub plugin classes. pre_check always returns False (no-op)."""

    class _Stub(AutoHealPlugin):
        name = plugin_name
        version = "0.0.1"
        matches_alert = alert
        description = desc

        async def pre_check(self, ctx: AutoHealContext) -> bool:
            return False

        async def execute(self, ctx: AutoHealContext) -> ActionResult:
            return ActionResult.skipped(f"{plugin_name}: stub, not yet implemented")

        async def post_verify(self, ctx: AutoHealContext) -> bool:
            return True

        async def rollback(self, ctx: AutoHealContext) -> ActionResult:
            return ActionResult.skipped("stub")

    _Stub.__name__ = cls_name
    _Stub.__qualname__ = cls_name
    return _Stub


KillProcessPlugin = _stub(
    "KillProcessPlugin",
    "kill-process",
    "process_hung",
    "Kill a hung process by name or PID.",
)
CompactDbPlugin = _stub(
    "CompactDbPlugin",
    "compact-db",
    "db_fragmentation_high",
    "Trigger database compaction / VACUUM.",
)
RotateLogsPlugin = _stub(
    "RotateLogsPlugin",
    "rotate-logs",
    "log_volume_high",
    "Force log rotation via logrotate.",
)
RebalanceClusterPlugin = _stub(
    "RebalanceClusterPlugin",
    "rebalance-cluster",
    "cluster_imbalanced",
    "Rebalance workloads across cluster nodes.",
)
FailoverPrimaryPlugin = _stub(
    "FailoverPrimaryPlugin",
    "failover-primary",
    "primary_db_down",
    "Promote a replica to primary when primary is down.",
)
ClearDnsCachePlugin = _stub(
    "ClearDnsCachePlugin",
    "clear-dns-cache",
    "dns_resolution_slow",
    "Flush the local DNS cache.",
)
ReloadConfigPlugin = _stub(
    "ReloadConfigPlugin",
    "reload-config",
    "config_outdated",
    "Send SIGHUP / reload endpoint to pick up new config.",
)
EvacuatePodPlugin = _stub(
    "EvacuatePodPlugin",
    "evacuate-pod",
    "pod_eviction_needed",
    "Cordon + drain a K8s node to evacuate pods.",
)
ResetCircuitBreakerPlugin = _stub(
    "ResetCircuitBreakerPlugin",
    "reset-circuit-breaker",
    "circuit_breaker_stuck",
    "Reset an open circuit breaker via management API.",
)
ThrottleTrafficPlugin = _stub(
    "ThrottleTrafficPlugin",
    "throttle-traffic",
    "traffic_spike",
    "Apply rate limiting rules to shed excess traffic.",
)
UpgradeContainerPlugin = _stub(
    "UpgradeContainerPlugin",
    "upgrade-container",
    "container_outdated",
    "Pull a newer image and recreate the container.",
)
BackupSnapshotPlugin = _stub(
    "BackupSnapshotPlugin",
    "backup-snapshot",
    "pre_maintenance_backup",
    "Trigger an on-demand backup snapshot before maintenance.",
)
RestoreSnapshotPlugin = _stub(
    "RestoreSnapshotPlugin",
    "restore-snapshot",
    "data_corruption",
    "Restore from the most recent backup snapshot.",
)
RotateSslCertPlugin = _stub(
    "RotateSslCertPlugin",
    "rotate-ssl-cert",
    "ssl_cert_expiry_warning",
    "Renew and reload a TLS certificate via ACME.",
)
FlushSessionsPlugin = _stub(
    "FlushSessionsPlugin",
    "flush-sessions",
    "session_overflow",
    "Flush all active sessions from the session store.",
)
ReindexSearchPlugin = _stub(
    "ReindexSearchPlugin",
    "reindex-search",
    "search_index_stale",
    "Trigger a full reindex of the search index.",
)
CompactQueuePlugin = _stub(
    "CompactQueuePlugin",
    "compact-queue",
    "queue_fragmented",
    "Compact and defragment the message queue storage.",
)
ResetRateLimitPlugin = _stub(
    "ResetRateLimitPlugin",
    "reset-rate-limit",
    "rate_limit_misconfigured",
    "Reset rate limit counters to clear misconfigured throttles.",
)
SyncReplicasPlugin = _stub(
    "SyncReplicasPlugin",
    "sync-replicas",
    "replica_lag_high",
    "Force a replica sync / catch-up when lag is too high.",
)
HealthcheckExternalPlugin = _stub(
    "HealthcheckExternalPlugin",
    "healthcheck-external",
    "external_service_degraded",
    "Probe an external dependency and report status.",
)
