-- 用途：放寬 core 交易月份上限，讓後續 MOI 發布批次可超過 2024 年。
-- 寫入：必要時永久替換既有檢查限制；重跑不會重複修改。
-- 前置條件：core.fact_transactions 已存在。
-- 回傳：無；新限制啟用前會先驗證既有事實資料。
DO $core_transaction_month_contract$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_constraint AS constraint_info
        WHERE constraint_info.conrelid =
              'core.fact_transactions'::REGCLASS
          AND constraint_info.conname = 'fact_transactions_month_check'
          AND pg_get_constraintdef(constraint_info.oid) LIKE '%2024-12-01%'
    ) THEN
        ALTER TABLE core.fact_transactions
            DROP CONSTRAINT fact_transactions_month_check;

        ALTER TABLE core.fact_transactions
            ADD CONSTRAINT fact_transactions_month_check
            CHECK (
                EXTRACT(DAY FROM transaction_month) = 1
                AND transaction_month BETWEEN
                    DATE '2012-08-01' AND DATE '9999-12-01'
            )
            NOT VALID;

        ALTER TABLE core.fact_transactions
            VALIDATE CONSTRAINT fact_transactions_month_check;
    END IF;
END
$core_transaction_month_contract$;
