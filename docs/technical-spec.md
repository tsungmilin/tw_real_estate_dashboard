# 系統架構與技術規格

本文件說明系統元件、資料層責任、跨層契約與批次追蹤。它只維護相對穩定的架構；目前資料筆數、發布月份與工作進度統一放在 [專案進度](project-status.md)，來源與發布頻率見 [資料來源與未來更新](data-sources.md)。

若要查詢欄位與排除規則，見 [資料清理與欄位契約](data-contract.md)；若要執行或復原資料庫流程，見 [PostgreSQL 資料流程與操作](database-operations.md)；若要理解 KPI 與 Tableau 畫面，見 [分析指標與 Tableau Dashboard](analytics-dashboard.md)。

## 1. 系統目標

系統將大型實價登錄 Stata 檔轉換為可追溯的分析資料集，並支援 Tableau 即時查詢與離線作品集展示。設計目標包括：

- 分批處理大型來源，避免一次載入全部資料。
- 以明確契約決定資料列納入、排除、型別與 `NULL` 語意。
- 保留可讀回、可對帳的 Parquet 檢查點與稽核證據。
- 將接收、核心模型、分析聚合與視覺呈現分層。
- 以批次 ID 串接資料更新，支援冪等重跑與失敗復原。
- 讓 Tableau 不需掃描逐筆事實表即可回答 Dashboard 問題。

## 2. 端到端資料流

```text
data/raw/house_preowned_data_2.0.dta
        │
        ▼
src/profiling/               探索來源與驗證假設
        │
        ▼
src/cleaning/                分批清理、契約檢查與稽核
        │
        ├── data/processed/transactions_clean.parquet
        ├── data/processed/transactions_excluded.parquet
        └── data/audit/cleaning_run_<id>.json
        │
        ▼
PostgreSQL
├── staging                  接收、UPSERT、批次對帳
├── core                     一致化維度與交易事實
└── analytics                Dashboard 專用 KPI
        │
        ├── 本機即時開發模式  PostgreSQL 唯讀連線
        └── housing_portfolio.twbx
             └── Hyper       離線可攜版
```

資料剖析用來回答「來源實際長什麼樣」；清理契約決定「哪些資料進入正式資料集」。兩者分離，避免探索程式無意間成為正式業務規則。

## 3. 元件與程式位置

| 元件 | 位置 | 輸入 | 輸出／副作用 |
|---|---|---|---|
| 原始資料剖析 | `src/profiling/` | Stata 原始檔 | 公開彙總與本機逐筆檢查結果 |
| 行政區參照 | `src/reference/`、`data/reference/` | 來源對照資料 | 版本化行政區與別名 CSV |
| 清理流程 | `src/cleaning/` | 原始檔、行政區參照 | 清理後／排除 Parquet、稽核 JSON |
| 暫存層載入 | `src/loading/`、`sql/staging/` | 清理後 Parquet | `staging` 資料與載入紀錄 |
| 核心層同步 | `src/core/`、`sql/core/` | 成功的 staging 批次 | 維度、交易事實與同步紀錄 |
| 分析層更新 | `src/analytics/`、`sql/analytics/` | `core` 與月份變更 | 三張 Tableau KPI 表與更新紀錄 |
| 流程協調 | `src/orchestration/` | 同一個 `load_batch_id` | 依序完成 staging、core、analytics |
| 視覺呈現 | Tableau 工作簿 | `analytics` 與行政區維度 | 即時 `.twb`、可攜 `.twbx` |

## 4. 資料層責任

| 資料層 | 負責 | 不負責 |
|---|---|---|
| 原始資料 | 保存不可變來源 | 修正、覆寫、納入公開專案 |
| 資料剖析 | 驗證來源結構、分布與清理假設 | 決定正式清理規則 |
| 清理 | 納入／排除、欄位標準化、品質標記與稽核 | BI 聚合與視覺呈現 |
| Parquet | 保存目前成功的清理後／排除檢查點 | 保存歷次完整資料快照 |
| `staging` | 接收清理後資料、UPSERT、批次與月份變更追蹤 | 面向使用者的 KPI |
| `core` | 一致化維度、鍵值與逐筆交易事實 | Tableau 呈現邏輯 |
| `analytics` | 聚合、單位換算、移動窗口與年增率 | 解析原始欄位 |
| Tableau | 圖表、控制項、互動與敘事 | 清理來源或重新定義 KPI |

## 5. 跨層契約

| 邊界 | 必須成立的條件 | 權威文件 |
|---|---|---|
| 原始資料 → 清理 | 必要欄位存在；每列只能進入清理後或排除資料其中之一 | [資料清理與欄位契約](data-contract.md) |
| 清理 → Parquet | 欄位、型別、`NULL` 與品質標記符合契約；輸出可完整讀回 | [資料清理與欄位契約](data-contract.md) |
| Parquet → `staging` | 單一清理執行、來源 ID 唯一、筆數與綱要通過載入前檢核 | [PostgreSQL 資料流程與操作](database-operations.md) |
| `staging` → `core` | 只接受明確且成功的 `load_batch_id`；固定維度完整 | [PostgreSQL 資料流程與操作](database-operations.md) |
| `core` → `analytics` | KPI 母體與影響月份可重算；三組驗證全部通過才發布 | [分析指標與 Tableau Dashboard](analytics-dashboard.md) |
| `analytics` → Tableau | Tableau 使用既定聚合，不平均中位數、不混合土地與房屋 | [分析指標與 Tableau Dashboard](analytics-dashboard.md) |

`staging` 與 `core` 都採 UPSERT-only：新資料新增、既有內容改變時更新，本批沒有出現的既有 ID 不自動刪除。這項語意避免把不完整批次誤當成完整快照。

## 6. PostgreSQL 邏輯模型

### `staging`

- `stg_transactions`：符合清理契約的目前已知交易狀態。
- `load_runs`：每次載入嘗試、來源資訊、結果與錯誤。
- `load_month_changes`：永久保存新增、同月變更與跨月移動影響的月份。

載入緩衝、同步、月份摘要與對帳在同一個資料庫交易中完成；失敗時不發布部分結果。

### `core`

```text
dim_location ─ fact_transactions ─ dim_building_type
```

- `fact_transactions` 一列代表一筆清理後交易，以 `source_transaction_id` 為主鍵。
- `dim_location` 由完整行政區參照建立，不從已出現的交易推導成員。
- `dim_building_type` 統一建物型態名稱與成員。
- 事實表只保留月份、兩個維度鍵、交易類型、總價與每平方公尺單價。
- 完整日期、格局、用途、面積與品質標記留在 `staging` 與 Parquet。
- 交易月份直接保存為每月第一天的 `DATE`；第一版不建立 `dim_date`。
- `sync_runs` 記錄每個 staging 批次的核心同步狀態與對帳。

日常同步只處理指定批次實際新增或更新的資料；初始化或完整修復才掃描全部 `staging`。若日後需要分割事實表，優先依時間而非縣市。

### `analytics`

| 資料表 | 粒度 | Tableau 用途 |
|---|---|---|
| `national_monthly_kpi` | 月份 | 全台 KPI、發布期間與近 12 個月趨勢 |
| `city_monthly_kpi` | 月份 × 縣市 | 縣市地圖、KPI 與 24 個月比較 |
| `district_rolling_3m` | 基準月 × 行政區 | 行政區近三個月散布圖 |

分析層保留完整月份與地理骨架。沒有符合條件的交易時，案件數為 0、價格為 `NULL`，讓 Tableau 能區分「零筆」與「沒有成員」。

## 7. 批次追蹤與發布

| 識別碼 | 範圍 | 用途 |
|---|---|---|
| `cleaning_run_id` | 清理輸出 | 串接清理後資料、排除資料與稽核 |
| `load_batch_id` | `staging`、`core`、`analytics` | 串接同一批資料庫更新與增量影響 |
| `analytics_run_id` | 分析層更新 | 記錄完整或增量更新的嘗試序列 |

日常流程在同一把 PostgreSQL advisory lock 下依序發布各層。只有前一層成功且對帳一致，下一層才可更新；存在無法判斷順序的失敗批次時，新批次會被阻擋。

## 8. Tableau 交付模式

| 模式／交付物 | 資料存取 | 使用情境 |
|---|---|---|
| 本機即時開發模式 | 以 `tableau_reader` 唯讀連線 PostgreSQL | 本機開發與更新後驗證；工作簿不公開 |
| `housing_portfolio.twbx` | 啟用並內嵌 Hyper extract | 離線檢視、作品集分享 |

Hyper 只包含 `national_monthly_kpi`、`city_monthly_kpi`、`district_rolling_3m` 與 `dim_location`。它不包含 `staging`、逐筆 `fact_transactions`、原始資料或 Parquet。即時版與可攜版的畫面與計算邏輯相同；可攜版是特定發布快照，不會自動跟隨 PostgreSQL 更新。

## 9. 系統邊界

- 原始 `.dta` 不可變、不覆寫，也不隨公開專案提供。
- 標準資料保存新台幣元與平方公尺；萬元與萬元／坪在分析層衍生。
- 土地交易不與房屋 KPI 混算。
- 正值極端值不在清理層自動刪除；其限制由稽核與 Dashboard 揭露。
- Tableau 不直接查詢原始檔、Parquet 或完整交易事實表。
- 逐筆剖析結果、Parquet、稽核輸出、密碼與本機資料庫不隨公開專案提供。
- 所得、人口、負擔能力、房價所得比與品質調整房價指數不屬於 Tableau v1。

已接受及被取代的跨層選擇見 [決策紀錄](decision-log.md)。
