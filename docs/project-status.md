# Project status

- **Updated:** 2026-08-27
- **Current phase:** Block 2 — Python Cleaning / Ingestion
- **Current milestone:** Implement Cleaning Specification v1 and reproduce the accepted baseline

## 1. Completed

### Raw profiling

- 檢查 63 個 Stata columns、types、labels 與樣本。
- 以 100,000-row chunks 完成全資料 profiling。
- 驗證 `no` 在 4,380,208 rows 中無 missing、無 duplicate。
- 完成 transaction dates、location、price、area、parking、building attributes、completion date 與 note targeted checks。
- Aggregate profiling outputs 已保存於 `profiling_output/summary/`。
- Profiling v1 對應的 raw SHA-256 為 `1d81eb74a5c7f3607b93966732b90a7e3b8cdf5debacf905b67e1afab86b25b2`；scripts、方法與重跑條件見 [`src/profiling/README.md`](../src/profiling/README.md)。

### Design

- Canonical period 定為 2012-08 至 2024-12。
- 保留房地、房地+車位、土地三種 transaction types。
- 完成 368-row location lookup reconciliation design。
- 完成 row exclusion precedence、null semantics、quality flags 與 audit rules。
- [Cleaning Specification v1](cleaning_spec_v1.md) 已完成設計，待實作驗證。
- [Canonical schema](canonical-schema.md) 已整理為詳細 data contract。

## 2. Current-source baseline

| Stage / reason | Rows |
|---|---:|
| Raw input | 4,380,208 |
| `missing_source_transaction_id` | 0 |
| Same-run duplicates | 0 |
| `invalid_transaction_period` | 34,519 |
| `unmapped_location` | 97 |
| `excluded_transaction_type` | 84,126 |
| Total excluded | 118,742 |
| Expected clean | 4,261,466 |

```text
4,380,208 = 4,261,466 + 118,742
```

這些精確筆數適用於目前 raw source checksum 與已驗證 lookup。未來來源變動時，schema、uniqueness 與 reconciliation 仍是 hard gates，精確 counts 則作 comparison baseline。

## 3. Next milestone

實作單一正式 cleaning entry point，產出：

```text
data/processed/transactions_clean.parquet
data/processed/transactions_excluded.parquet
data/audit/cleaning_run_<cleaning_run_id>.json
```

完成條件：

- Full run 通過 cleaning spec 所有 hard-fail publication gates。
- 重現目前 clean／excluded baseline。
- Clean ID non-null and unique。
- Output schema 與 canonical data contract 一致。
- 三份 output 可讀回並通過 row-count reconciliation。
- 相同 source checksum 的重跑結果可重現。

## 4. Immediate work

1. 建立 `data/reference/location_lookup.csv` 與 `location_aliases.csv`。
2. 實作 chunked raw reader 與 required-schema check。
3. 實作 exclusion precedence 與 duplicate handling。
4. 實作 canonical transformations and flags。
5. 實作 clean／excluded writers 與 audit collector。
6. 建立 automated tests and full-run publication gates。
7. 補上 Python dependency／lock file 與執行指令。

## 5. Repository state after Profiling v1 closeout

- Profiling scripts 已依 01–09 集中於 `src/profiling/`，用途、方法、輸出與重跑條件都有獨立說明。
- Aggregate results 保存在 `profiling_output/summary/`；row-level extracts 與暫存 SQLite 保存在 ignored local paths。
- Raw `.dta`、processed Parquet、cleaning audit、private profiling outputs 與本機環境檔不提交 Git。
- Python dependency／lock file 與正式執行指令仍屬 Block 2 cleaning pipeline 工作。

## 6. Later phases

| Block | Scope | Status |
|---|---|---|
| 1 | Data profiling / schema design | Completed |
| 2 | Python cleaning / ingestion | In progress |
| 3 | PostgreSQL staging / loading | Planned |
| 4 | Core dimensional model | Planned |
| 5 | SQL analytics mart | Planned |
| 6 | Tableau / portfolio presentation | Planned |

## 7. Deferred decisions

- PostgreSQL physical model、PK/FK/indexes、UPSERT strategy
- Core partition necessity
- Analytics-layer outlier thresholds
- Monthly mart final metrics
- Dashboard information architecture
- Tableau presentation and portfolio narrative
