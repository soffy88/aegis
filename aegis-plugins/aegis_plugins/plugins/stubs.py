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
        # Marks this as a not-yet-implemented placeholder so the registry can keep
        # it out of the advertised catalog (see registry.list_plugins).
        is_stub = True

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
EvacuatePodPlugin = _stub(
    "EvacuatePodPlugin",
    "evacuate-pod",
    "pod_eviction_needed",
    "Cordon + drain a K8s node to evacuate pods.",
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
