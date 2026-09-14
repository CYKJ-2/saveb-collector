-- 升级前先停止 Collector worker/beat。旧混合任务降为历史优先级，原分片、
-- 归档、进度、操作者与已入库订单完全保留；下一次调度立即补采当天和昨天。
-- 仅处理尚未结束且确实混入单号/滚动历史片的旧 refresh；重复执行无副作用。
WITH moved AS (
    UPDATE collector.jobs j
    SET mode='history',
        params=params || jsonb_build_object('original_mode', 'refresh',
                    'mode', 'history', 'task_kind', 'open_recheck'),
        updated_at=now()
    WHERE j.mode='refresh' AND j.status IN ('queued','running','retrying')
      AND EXISTS (SELECT 1 FROM collector.chunks c WHERE c.job_id=j.id
                  AND (c.scope ? 'order_id' OR c.scope->>'rolling'='true'))
    RETURNING account
)
INSERT INTO collector.schedules(account,next_run_at)
SELECT DISTINCT account,now() FROM moved
ON CONFLICT(account) DO UPDATE SET next_run_at=now();
INSERT INTO collector.schema_versions(version) VALUES (6) ON CONFLICT DO NOTHING;
