# PostgreSQL 資料流程與操作

本文件集中說明 PostgreSQL `staging`、`core`、`analytics` 的操作契約，以及初始化、日常更新、驗證與復原方式。它不重複保存目前資料筆數；最新快照見 [專案進度](project-status.md)，KPI 定義見 [分析指標與 Tableau Dashboard](analytics-dashboard.md)，欄位語意見 [資料清理與欄位契約](data-contract.md)。

## 1. 前置條件

- PostgreSQL 可連線，且命令列可執行 `psql`。
- 目標資料庫預設為 `real_estate_dashboard`。
- 連線使用標準 `PGHOST`、`PGPORT`、`PGUSER`、`PGPASSWORD`、`PGDATABASE` 環境變數。
- 已產生成功的 `data/processed/transactions_clean.parquet`。
- `data/reference/location_lookup.csv` 通過 368 筆行政區檢核。

所有載入器都會寫入資料庫。正式執行前應確認來源 Parquet、目標資料庫及備份策略。

## 2. 資料層與主要物件

```text
transactions_clean.parquet
        ↓
staging.stg_transactions
        ↓
core.fact_transactions
        ↓
analytics.national_monthly_kpi
analytics.city_monthly_kpi
analytics.district_rolling_3m
```

| 資料層 | 主要物件 | 責任 |
|---|---|---|
| `staging` | `stg_transactions`、`load_runs`、`load_month_changes` | 接收 Parquet、UPSERT、記錄批次與受影響月份 |
| `core` | `dim_location`、`dim_building_type`、`fact_transactions`、`sync_runs` | 一致化維度與交易事實、記錄核心同步 |
| `analytics` | 三張 KPI 表、`refresh_runs` | 完整重建或依批次增量更新，驗證後發布 |

`source_transaction_id` 是 staging 與 core 交易主鍵。三層都採新增或更新，不因本批來源缺少某個 ID 自動刪除既有交易。

## 3. 新環境初始化

從專案根目錄依序執行：

```bash
python -m src.cleaning.run_cleaning
python -m src.loading.run_postgres_load
python -m src.core.run_core_sync initialize
python -m src.analytics.run_analytics_refresh --full
```

初始化會：

1. 產生並驗證清理後 Parquet。
2. 建立 staging 物件並載入交易。
3. 建立完整 368 筆行政區、14 筆建物類型與核心事實表。
4. 完整重建三張 `analytics` 表並執行對帳。

完整重建是明確的管理操作，不由日常流程自動觸發。

## 4. 日常更新

新環境完成初始化後，日常入口只有：

```bash
python -m src.orchestration.run_pipeline
```

流程協調器會在同一把跨層 PostgreSQL advisory lock 下依序：

```text
檢查固定維度與未完成批次
        ↓
恢復唯一可安全重試的失敗批次
        ↓
Parquet → staging
        ↓ 同一個 load_batch_id
staging → core
        ↓ 同一個 load_batch_id
core → analytics 增量更新
        ↓
三組分析層驗證全部通過
```

存在進行中的批次、多個無法判斷順序的失敗批次、固定維度不完整，或最近一次完整重建未成功時，流程會在新 staging 寫入前停止。

## 5. `staging` 載入

獨立執行：

```bash
python -m src.loading.run_postgres_load
```

載入器在連線前驗證 Parquet 欄位、筆數與單一 `cleaning_run_id`，再以資料列群組串流至同一個資料庫交易：

- 新 ID：新增。
- 既有 ID 且業務內容改變：更新。
- 內容相同：不重寫。
- 本次 Parquet 缺少的既有 ID：保留。

`cleaning_run_id` 是追蹤資訊，不作為業務內容比較欄位。每次嘗試使用獨立 UUID 作為 `load_batch_id`，來源內容另以完整 SHA-256 辨識。

同步、月份變更紀錄與驗證在同一個資料庫交易中完成；任一步失敗都會復原。失敗狀態由獨立操作寫入 `staging.load_runs`。

## 6. `core` 同步

日常同步一個明確且成功的 staging 批次：

```bash
python -m src.core.run_core_sync sync --load-batch-id <load_batch_id>
```

`core` 不讀取 Parquet，也不自行猜測最新批次。它只處理該批實際新增或更新的 `staging` 資料：

- 事實資料不存在：`inserted`
- 核心欄位改變：`updated`
- staging 業務內容改變但核心欄位相同：`unchanged`

已成功的批次直接回傳原結果；失敗批次可增加 `attempt_count` 後重試；進行中的批次不允許另一個程序接手。

初始化或完整修復使用：

```bash
python -m src.core.run_core_sync initialize
```

此命令會建立維度與事實表、從完整 `staging` 回填事實資料，並執行全量稽核，但不刪除既有交易。

## 7. `analytics` 更新

依成功 core 批次增量更新：

```bash
python -m src.analytics.run_analytics_refresh \
  --load-batch-id <load_batch_id>
```

明確完整重建：

```bash
python -m src.analytics.run_analytics_refresh --full
```

更新順序固定為全國 → 縣市 → 鄉鎮市區，接著執行三組唯讀驗證。只有全部成功，`analytics.refresh_runs.status` 才會成為 `success`。

同一批增量更新成功後重跑會直接回傳原結果；失敗後以相同 `load_batch_id` 重試，沿用原 `analytics_run_id` 並增加嘗試次數。行政區參照成員改變時必須人工檢查後完整重建。

## 8. 批次識別與順序

- `cleaning_run_id`：一次 Python 清理執行。
- `load_batch_id`：一次 `staging` 載入，也作為 `core` 與 `analytics` 增量輸入。
- `analytics_run_id`：一次 `analytics` 完整或增量更新嘗試序列。

`staging.stg_transactions.load_batch_id` 表示最後實際改變該列的批次，不是不可變歷史。流程必須先完成同一批的 `core` 與 `analytics`，才發布下一個 `staging` 批次。

## 9. 失敗與復原

| 狀況 | 處理方式 |
|---|---|
| Parquet 契約錯誤 | 修正或重新清理；資料庫不會留下新批次 |
| `staging` 失敗 | 修正原因後重新執行載入 |
| 唯一一個 `core`／`analytics` 增量批次失敗 | 日常流程會先重試該批 |
| 存在進行中或多個失敗批次 | 停止並人工確認順序 |
| 完整重建失敗 | 修正後重新執行 `--full` |
| 行政區參照成員改變 | 更新參照資料、初始化維度並完整重建 `analytics` |
| 批次來源無法對帳 | 保留證據，使用明確的完整修復，不猜測或跳過差異 |

## 10. 驗證方式

不啟動一次性 PostgreSQL 的一般測試：

```bash
python -m pytest -q
```

完整資料庫整合測試會在暫存目錄建立獨立 PostgreSQL cluster，依序驗證 staging、core 與三張 analytics 表，完成後移除：

```bash
RUN_POSTGRES_INTEGRATION=1 \
python -m pytest -q tests/test_postgres_pipeline_integration.py
```

正式資料庫更新時，除了 Python 載入器的發布檢核，也會執行各層 SQL 驗證：

- `sql/staging/004_validate_load.sql` 與 `006_validate_upsert_load.sql`
- `sql/core/004_validate_dim_location.sql`、`007_validate_dim_building_type.sql`、`010_validate_fact_transactions.sql`
- `sql/analytics/004_validate_national_monthly_kpi.sql`、`008_validate_city_monthly_kpi.sql`、`012_validate_district_rolling_3m.sql`

目前已驗證的資料筆數、發布期間與測試結果統一記錄在 [專案進度](project-status.md)。這些數字是特定來源快照的證據，不是永久限制；正式規則由資料庫約束、載入器發布檢核及驗證 SQL 定義。
