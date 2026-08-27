# Technical specification

本文件描述專案層級的技術架構與資料邊界。Python cleaning 的精確規則以 [Cleaning Specification v1](cleaning_spec_v1.md) 為準；clean output 欄位設定見 [Canonical schema](canonical-schema.md)。

## 1. Project goal

建立一個可重現、可對帳並適合作品集展示的台灣實價登錄 analytics pipeline，涵蓋：

- 大型 `.dta` chunked ingestion
- Cleaning、validation 與 audit
- Parquet checkpoint
- PostgreSQL staging／core／analytics model
- SQL analytics marts
- Tableau dashboard

## 2. Data source

| Property | Value |
|---|---|
| Source file | `data/raw/house_preowned_data_2.0.dta` |
| Coverage | 2012–2024 |
| Canonical period | 2012-08 through 2024-12 |
| File size | 14,682,511,713 bytes |
| Rows | 4,380,208 |
| Raw columns | 63 |
| Default chunk size | 100,000 rows |

該 `.dta` 可能包含資料製作者額外建立的 derived fields。Canonical 優先採用可對應官方交易欄位、Dashboard 實際需要的欄位，以及必要的 quality／audit metadata。

Raw data immutable、不覆寫、不納入 Git。Pipeline 不可依賴 Desktop、論文資料夾或其他機器限定的絕對路徑。

## 3. Architecture

```text
Raw .dta
   │
   ▼
Chunked Python cleaning
   ├── schema and reference validation
   ├── row eligibility
   ├── canonical transformations
   └── quality flags and audit
   │
   ├── data/processed/transactions_clean.parquet
   ├── data/processed/transactions_excluded.parquet
   └── data/audit/cleaning_run_<id>.json
   │
   ▼
PostgreSQL
   ├── staging
   ├── core
   └── analytics
   │
   ▼
Tableau
```

### Layer responsibilities

| Layer | Owns | Does not own |
|---|---|---|
| Raw | Immutable source | 修正、覆寫、Git 儲存 |
| Cleaning | Row eligibility、normalization、flags、audit | BI aggregation、坪價、屋齡 |
| Parquet | Current successful clean/excluded checkpoint | 歷次完整 data snapshots |
| Staging | Load landing、row reconciliation、load metadata | Business-facing metrics |
| Core | Conformed fact/dimensions、keys、relationships | Tableau presentation logic |
| Analytics | Aggregation、units、ranking、growth | Raw parsing |
| Tableau | Visuals、filters、interaction、storytelling | Raw cleaning |

## 4. Cleaning contract

Cleaning 的主要輸出為：

```text
transactions_clean
transactions_excluded
cleaning audit
```

同一筆 raw row 只能進 clean 或 excluded 其中之一，並滿足：

```text
input_rows = clean_rows + excluded_rows
```

目前 source baseline：

| Result | Rows |
|---|---:|
| Raw input | 4,380,208 |
| Invalid transaction period | 34,519 |
| Unmapped location | 97 |
| Excluded transaction type | 84,126 |
| Total excluded | 118,742 |
| Expected clean | 4,261,466 |

精確 exclusion precedence、location construction、日期行為、transaction-type null semantics、audit 與 hard-fail gates 均定義於 `cleaning_spec_v1.md`，不在本文件維護第二份規則。

## 5. PostgreSQL direction

規劃分為三個 schemas：

```text
staging
core
analytics
```

### Staging

接收 current successful clean Parquet，執行 schema validation、row reconciliation、duplicate／UPSERT strategy 與 load logging。

### Core

初步模型：

```text
                  dim_date
                     │
dim_location ─ fact_transactions ─ dim_building_type
```

- `fact_transactions` grain：一列一筆 clean 實價登錄交易。
- 所有縣市共用一張 fact，不依行政區拆表。
- 政府 `county_id`／`town_id` 保留為 geography attributes。
- `location_id` 在 core 建立為 warehouse surrogate key，不進 clean Parquet。
- 如實測需要 partition，優先依時間，不依縣市。

### Analytics

第一個 mart 暫定：

```text
analytics.mart_monthly_market
grain = month × city × district × building_type
```

候選 metrics：transaction count、平均／中位總價、平均／中位單價、平均坪數，後續再依 Dashboard 需求加入 MoM、rolling average 與 ranking。

## 6. Metric boundaries

- `transaction_count` = clean canonical rows count。
- 它不等於官方建物所有權買賣移轉棟數或住宅成交戶數。
- 土地與房地單價不可視為同一市場直接比較。
- Canonical/core 保留 NTD 與 m²。
- 坪、萬元、NTD／坪、屋齡與聚合指標只在 analytics layer 衍生。

## 7. Dashboard scope

Pipeline 與 database 穩定後，第一版 Tableau 規劃包含：

- Market Overview：median unit price、transaction count、趨勢、區域比較、top districts。
- Regional Explorer：city、district、building type、year/month filters，以及價格趨勢、交易量與分布。

所得、人口、affordability 與 price-to-income 屬未來擴充，不納入第一版 cleaning scope。

## 8. Accepted decisions

| ID | Decision | Consequence |
|---|---|---|
| D-001 | Raw `.dta` immutable | Pipeline 可由原始輸入重建 |
| D-002 | Cleaning → Parquet → PostgreSQL → Tableau | Storage、serving、presentation 分層 |
| D-003 | Cleaning spec v1 是目前 cleaning authority | 不由 project overview 重複定義規則 |
| D-004 | Canonical period 為 2012-08 至 2024-12 | 超出期間的 rows 進 excluded dataset |
| D-005 | 只保留房地、房地+車位、土地 | 其他 transaction types 排除 |
| D-006 | 正值極端值不截尾、不 winsorize、不自動刪除 | 以 audit 或 analytics flags 處理 |
| D-007 | Clean、excluded、audit 共用 `cleaning_run_id` | 支援完整對帳與追溯 |
| D-008 | Canonical/core 使用 NTD／m² | 顯示單位在 analytics 衍生 |
| D-009 | Government location codes + core surrogate key | 保留官方 geography 並支援 star schema |
| D-010 | Tableau 主要讀 analytics marts | BI 不負責 raw cleaning |

## 9. Deferred decisions

- PostgreSQL physical types、PK/FK、indexes 與 UPSERT details
- Core 是否需要 physical partition
- Analytics outlier thresholds
- Final metric formulas and Dashboard information architecture
- Reproducible Python dependency／lock-file strategy
