# Taiwan Real Estate Dashboard

使用 2012–2024 年台灣實價登錄資料，建立一套從大型 Stata 原始資料清理、PostgreSQL 建模到 Tableau 視覺化的 end-to-end analytics project。

目前專案已完成全資料 profiling 與 Cleaning Specification v1，正在進入正式 cleaning pipeline 實作。

## Current status

- **Current phase:** Block 2 — Python Cleaning / Ingestion
- **Completed:** Raw profiling、targeted validation、location reconciliation、Cleaning Specification v1
- **Raw input:** `data/raw/house_preowned_data_2.0.dta`
- **Raw size:** 14,682,511,713 bytes，4,380,208 rows、63 columns
- **Canonical period:** 2012-08 through 2024-12
- **Expected clean baseline:** 4,261,466 rows
- **Expected excluded baseline:** 118,742 rows

## Architecture

```text
Raw Stata `.dta`
        │
        ▼
Python chunked cleaning + validation
        │
        ├── transactions_clean.parquet
        ├── transactions_excluded.parquet
        └── cleaning audit JSON
        │
        ▼
PostgreSQL
├── staging
├── core
└── analytics
        │
        ▼
Tableau dashboards
```

Tableau 只讀取 analytics-ready data，不直接負責 raw cleaning 或完整 transaction table 上的大型 aggregation。

## Documentation

- [Technical specification](docs/technical-spec.md) — 整體架構、資料來源、data model 與設計邊界
- [Cleaning specification v1](docs/cleaning_spec_v1.md) — Python cleaning pipeline 的最新實作依據
- [Canonical schema](docs/canonical-schema.md) — `transactions_clean` 的詳細 data contract
- [Project status](docs/project-status.md) — 進度、完成條件、下一步與未定案事項
- [Profiling scripts](src/profiling/README.md) — 每支 profiling script 的目的、方法、輸出與重跑條件

若文件內容衝突，cleaning 行為以 `cleaning_spec_v1.md` 為準；`canonical-schema.md` 必須與它同步更新。

## Repository structure

```text
data/
├── raw/          # immutable local input; not committed
├── reference/    # versioned location lookup and aliases
├── processed/    # current successful clean/excluded Parquet
└── audit/        # cleaning run audit JSON

src/
├── profiling/      # numbered profiling scripts and runbook
└── cleaning/

profiling_output/
├── README.md       # output boundaries and source fingerprint
├── summary/      # aggregate profiling results
└── private/      # row-level anomalies; ignored by Git

docs/
├── technical-spec.md
├── cleaning_spec_v1.md
├── canonical-schema.md
└── project-status.md

sql/
├── staging/
├── core/
└── analytics/
```

## Data scope

Canonical dataset 只保留：

- `房地(土地+建物)`
- `房地(土地+建物)+車位`
- `土地`

`transaction_count` 指 clean transaction rows 數，不代表官方建物所有權買賣移轉棟數。Canonical 保持新台幣元與平方公尺；坪、萬元、每坪單價與屋齡留到 analytics layer。

## Running the project

Profiling 的用途、執行方式與重跑條件記錄於 [`src/profiling/README.md`](src/profiling/README.md)。相同 source checksum 不必重跑 profiling。正式 cleaning entry point、Python dependencies 與可重現環境仍在 Block 2 實作中，完整 cleaning 執行方式會在第一版 full run 通過 publication gates 後補入。
