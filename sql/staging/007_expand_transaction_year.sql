-- 用途：放寬 staging 交易年份上限，讓後續 MOI 發布批次可超過 2024 年。
-- 寫入：必要時永久替換既有檢查限制；重跑不會重複修改。
-- 前置條件：staging.stg_transactions 已存在。
-- 回傳：無；新限制啟用前會先驗證既有資料列。
DO $staging_transaction_year_contract$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_constraint AS constraint_info
        WHERE constraint_info.conrelid =
              'staging.stg_transactions'::REGCLASS
          AND constraint_info.conname = 'stg_transactions_year_check'
          AND pg_get_constraintdef(constraint_info.oid) LIKE '%2024%'
    ) THEN
        ALTER TABLE staging.stg_transactions
            DROP CONSTRAINT stg_transactions_year_check;

        ALTER TABLE staging.stg_transactions
            ADD CONSTRAINT stg_transactions_year_check
            CHECK (transaction_year BETWEEN 2012 AND 9999)
            NOT VALID;

        ALTER TABLE staging.stg_transactions
            VALIDATE CONSTRAINT stg_transactions_year_check;
    END IF;
END
$staging_transaction_year_contract$;
