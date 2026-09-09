BEGIN;

-- Create the persistent core synchronization ledger used for retries and auditing.
-- Requires staging.load_runs; preserves existing rows and returns no result set.
CREATE TABLE IF NOT EXISTS core.sync_runs (
    load_batch_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,

    source_rows BIGINT,
    rows_inserted BIGINT,
    rows_updated BIGINT,
    rows_unchanged BIGINT,
    rows_applied BIGINT,

    attempt_count INTEGER NOT NULL DEFAULT 1,
    validation_summary JSONB,
    error_message TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    completed_at TIMESTAMPTZ,

    CONSTRAINT sync_runs_load_batch_fk
        FOREIGN KEY (load_batch_id)
        REFERENCES staging.load_runs (load_batch_id)
        ON DELETE RESTRICT,

    CONSTRAINT sync_runs_load_batch_id_check
        CHECK (
            load_batch_id = BTRIM(load_batch_id)
            AND load_batch_id <> ''
        ),

    CONSTRAINT sync_runs_status_check
        CHECK (status IN ('running', 'success', 'failed')),

    CONSTRAINT sync_runs_counts_check
        CHECK (
            (source_rows IS NULL OR source_rows >= 0)
            AND (rows_inserted IS NULL OR rows_inserted >= 0)
            AND (rows_updated IS NULL OR rows_updated >= 0)
            AND (rows_unchanged IS NULL OR rows_unchanged >= 0)
            AND (rows_applied IS NULL OR rows_applied >= 0)
            AND attempt_count > 0
        ),

    CONSTRAINT sync_runs_success_reconciliation_check
        CHECK (
            status <> 'success'
            OR (
                source_rows =
                    rows_inserted + rows_updated + rows_unchanged
                AND rows_applied = rows_inserted + rows_updated
            )
        ),

    CONSTRAINT sync_runs_state_check
        CHECK (
            (
                status = 'running'
                AND source_rows IS NULL
                AND rows_inserted IS NULL
                AND rows_updated IS NULL
                AND rows_unchanged IS NULL
                AND rows_applied IS NULL
                AND validation_summary IS NULL
                AND completed_at IS NULL
                AND error_message IS NULL
            )
            OR (
                status = 'success'
                AND source_rows IS NOT NULL
                AND rows_inserted IS NOT NULL
                AND rows_updated IS NOT NULL
                AND rows_unchanged IS NOT NULL
                AND rows_applied IS NOT NULL
                AND validation_summary IS NOT NULL
                AND error_message IS NULL
                AND completed_at IS NOT NULL
            )
            OR (
                status = 'failed'
                AND validation_summary IS NULL
                AND error_message IS NOT NULL
                AND BTRIM(error_message) <> ''
                AND completed_at IS NOT NULL
            )
        )
);

CREATE INDEX IF NOT EXISTS idx_sync_runs_status_started_at
    ON core.sync_runs (status, started_at DESC);

COMMENT ON TABLE core.sync_runs IS
    '每列記錄一個 staging load batch 在 core 層的同步狀態與對帳結果。';

COMMENT ON COLUMN core.sync_runs.load_batch_id IS
    '對應 staging.load_runs 的批次識別碼；也是 core 同步的冪等鍵。';

COMMENT ON COLUMN core.sync_runs.source_rows IS
    '本批實際進入 core 比對的 staging 新增或更新列數。';

COMMENT ON COLUMN core.sync_runs.rows_inserted IS
    '本批在 core.fact_transactions 新增的列數。';

COMMENT ON COLUMN core.sync_runs.rows_updated IS
    '本批在 core.fact_transactions 更新的列數。';

COMMENT ON COLUMN core.sync_runs.rows_unchanged IS
    'staging 有變更，但 core 欄位內容不變的列數。';

COMMENT ON COLUMN core.sync_runs.rows_applied IS
    '實際寫入 core.fact_transactions 的新增與更新列數合計。';

COMMENT ON COLUMN core.sync_runs.attempt_count IS
    '同一個 staging batch 嘗試進行 core 同步的累計次數。';

COMMENT ON COLUMN core.sync_runs.validation_summary IS
    '成功同步後保存的批次分類、mapping 與最終值對帳摘要。';

COMMENT ON COLUMN core.sync_runs.error_message IS
    '最近一次失敗的錯誤訊息；成功或執行中時必須為 NULL。';

COMMENT ON COLUMN core.sync_runs.started_at IS
    '最近一次 core 同步嘗試的開始時間。';

COMMENT ON COLUMN core.sync_runs.completed_at IS
    '最近一次 core 同步成功或失敗的完成時間。';

COMMIT;
