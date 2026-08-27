# Canonical schema — `transactions_clean`

- **Spec version:** Cleaning Specification v1
- **Status:** Design complete; awaiting implementation validation
- **Authority:** [Cleaning Specification v1](cleaning_spec_v1.md)
- **Grain:** One eligible real-estate transaction per row

本文件將 clean dataset 的欄位設定拆成 definition、source、type、transformation、nullable、validation、unit 與 applicability。若 cleaning 行為變更，必須同時更新本文件與 `cleaning_spec_v1.md`。

## 1. Run metadata and identity

| Field | Definition | Source | Logical / physical type | Transformation | Nullable | Validation / constraint | Unit / applicability |
|---|---|---|---|---|---:|---|---|
| `cleaning_run_id` | 產生該 row 的 cleaning run 識別碼 | Generated once per run | String / PostgreSQL `TEXT` | 同一次 clean、excluded、audit 共用同一值 | No | 每個 published row 必須能對應成功 audit | All clean rows |
| `source_transaction_id` | 來源交易案件識別碼 | Raw `no` | String / PostgreSQL `TEXT` | 去除前後空白；不改編為流水號 | No | Clean output 必須唯一；missing／blank 排除；same-run duplicates 依 spec 處理 | Natural key；UPSERT candidate |

## 2. Transaction dates

| Field | Definition | Source | Logical / physical type | Transformation | Nullable | Validation / constraint | Unit / applicability |
|---|---|---|---|---|---:|---|---|
| `transaction_year` | 交易發生的西元年份 | `trans_y` | Integer / `SMALLINT` | 民國年 + 1911 | No | Canonical 只接受 2012-08 至 2024-12 | Monthly analysis required |
| `transaction_month` | 交易發生月份 | `trans_m` | Integer / `SMALLINT` | 轉為整數，不由完整日期反推 | No | 只允許 1–12 | Monthly analysis required |
| `transaction_date` | 完整 Gregorian 交易日期 | `trans_y`, `trans_m`, `trans_d` | Date / `DATE` | 有效年月日組成日期 | Yes | Day 缺失或 calendar-invalid 時為 `NULL`；不排除有效年月 row | Daily analysis optional |
| `transaction_date_valid` | 完整交易日是否有效 | Derived | Boolean / `BOOLEAN` | 完整有效日期 → `TRUE`；否則 `FALSE` | No | 與 `transaction_date` null state 必須一致 | Quality flag |

年或月缺失、月份不在 1–12 或年月超出 canonical period 時，row 不進 clean table，而進 `transactions_excluded`，reason 為 `invalid_transaction_period`。

## 3. Completion dates

| Field | Definition | Source | Logical / physical type | Transformation | Nullable | Validation / constraint | Unit / applicability |
|---|---|---|---|---|---:|---|---|
| `completion_date` | 建築完成日期 | `date_complete` | Date / `DATE` | 解析有效 6/7 位 ROC date 並轉 Gregorian | Yes | Missing、0、negative、digit-length invalid、calendar-invalid → `NULL` | Building transactions；land rows `NULL` |
| `completion_date_status` | 完工日期解析狀態 | Derived | String / `TEXT` | 依 raw value 與解析結果指派固定 status code | No | 必須使用 cleaning spec 定義的狀態集合；實作時建立 enum list | Audit / quality category |
| `completion_after_transaction` | 完工日是否晚於交易日 | Derived dates | Boolean / `BOOLEAN` | 兩個日期皆有效時比較 | Yes | 任一日期為 `NULL` 時為 `NULL`；`TRUE` 不自動排除 | Building quality flag |

## 4. Location

| Field | Definition | Source | Logical / physical type | Transformation | Nullable | Validation / constraint | Unit / applicability |
|---|---|---|---|---|---:|---|---|
| `county_id` | 政府縣市代碼 | Location lookup | Five-character string / `TEXT` | 由 legacy raw location mapping，保留前導 0 | No | 必須存在於通過 368-row reconciliation 的 lookup | Government code |
| `town_id` | 政府鄉鎮市區代碼 | Location lookup | Eight-character string / `TEXT` | 由 lookup 產生，保留前導 0 | No | 必須唯一對應 `county_id`、`city`、`district` | Government code |
| `city` | 標準縣市名稱 | Location lookup | String / `TEXT` | 不直接保存 raw `county`；使用 lookup 標準名稱 | No | 必須與 `county_id` 一致 | Display attribute |
| `district` | 標準鄉鎮市區名稱 | Location lookup | String / `TEXT` | 不直接保存 raw `town`；使用 lookup 標準名稱 | No | 在同一縣市內必須與 `town_id` 一致 | Display attribute |

Mapping 失敗的 row 排除為 `unmapped_location`。`location_id` 不進 clean Parquet；它由 PostgreSQL `core.dim_location` 建立。

## 5. Transaction and land attributes

| Field | Definition | Source | Logical / physical type | Transformation | Nullable | Validation / constraint | Unit / applicability |
|---|---|---|---|---|---:|---|---|
| `transaction_type` | 實價登錄交易標的分類 | `type` | Controlled string / `TEXT` | Trim 後套用 inclusion list | No | 只允許房地、房地+車位、土地三類 | All clean rows |
| `urban_land_use_type` | 都市土地使用分區 | `usage_type` | String / Parquet string | Trim；blank／missing → `NULL` | Yes | 不自行推測或重新分類 unknown values | Urban land when applicable；Parquet only |
| `nonurban_land_use_zone` | 非都市土地使用分區 | `nonurban_type` | String / Parquet string | Trim；blank／missing → `NULL` | Yes | 與 urban field 條件式互補，不要求兩者皆非空 | Non-urban land when applicable；Parquet only |

## 6. Building attributes

| Field | Definition | Source | Logical / physical type | Transformation | Nullable | Validation / constraint | Unit / applicability |
|---|---|---|---|---|---:|---|---|
| `building_type` | 官方建物型態分類 | `building_type` | String / `TEXT` | Trim；blank／missing → `NULL` | Yes | 不自行合併來源分類 | Building rows；land rows forced `NULL` |
| `primary_use` | 建物主要用途 | `usage` | String / `TEXT` | Trim；blank／missing → `NULL` | Yes | 缺值不得推定為住家用 | Building rows；land rows forced `NULL` |
| `has_management` | 是否有管理組織 | `manage` | Boolean / `BOOLEAN` | `有` → `TRUE`；`無` → `FALSE`；其他 → `NULL` | Yes | 未知來源值進 audit；不等同有無保全或管理員 | Building rows；land rows forced `NULL` |
| `room_count` | 建物現況格局房數 | `room` | Integer / nullable `INTEGER` | 解析 nonnegative integer | Yes | Negative、fractional、parse failure → `NULL` 並 audit；正值極端值保留 | Building rows；land rows forced `NULL` |
| `hall_count` | 建物現況格局廳數 | `hall` | Integer / nullable `INTEGER` | 解析 nonnegative integer | Yes | Negative、fractional、parse failure → `NULL` 並 audit；正值極端值保留 | Building rows；land rows forced `NULL` |
| `bathroom_count` | 建物現況格局衛浴數 | `bath` | Integer / nullable `INTEGER` | 解析 nonnegative integer | Yes | Negative、fractional、parse failure → `NULL` 並 audit；正值極端值保留 | Building rows；land rows forced `NULL` |

## 7. Price and area measures

| Field | Definition | Source | Logical / physical type | Transformation | Nullable | Validation / constraint | Unit / applicability |
|---|---|---|---|---|---:|---|---|
| `land_transfer_area_m2` | 案件土地移轉總面積 | `trans_landsize` | Numeric / nullable `Float64`, PostgreSQL `DOUBLE PRECISION` | 保留官方 transaction-level value；不以 parcel fields 重建 | Yes | > 0 為 valid；0／invalid 轉 `NULL`，原始有效性由 flag 保存 | m²；all transaction types |
| `building_transfer_area_m2` | 案件建物移轉總面積 | `trans_size` | Numeric / nullable `Float64`, PostgreSQL `DOUBLE PRECISION` | 保留官方 value | Yes | Building rows > 0 為 valid；0／invalid → `NULL`；land rows forced `NULL` | m²；building transactions |
| `total_price_ntd` | 案件官方登錄總價 | `housing_totprice` | Integer / nullable 64-bit, PostgreSQL `BIGINT` | 保留 NTD，不換算萬元 | Yes | > 0 為 valid；0／invalid → `NULL`；正值極端值保留 | NTD；all transaction types |
| `unit_price_ntd_m2` | 官方每平方公尺單價 | `h_price_m2` | Integer / nullable 64-bit, PostgreSQL `BIGINT` | 保留官方值；不由 `h_price_pin` 反推 | Yes | > 0 為 valid；0／invalid → `NULL`；公式差異只進 audit | NTD/m²；land and building markets must be analyzed separately |

坪、萬元、每坪單價、屋齡，以及扣車位後重算價格／面積不進 clean table。

## 8. Validity and quality flags

| Field | Definition | Source | Logical / physical type | Transformation | Nullable | Validation / constraint | Applicability / interpretation |
|---|---|---|---|---|---:|---|---|
| `total_price_valid` | Raw 總價是否大於 0 且可解析 | `housing_totprice` | Boolean / `BOOLEAN` | Valid positive → `TRUE`；其他 → `FALSE` | No | 必須與 `total_price_ntd` null state 一致 | All clean rows |
| `unit_price_valid` | Raw 官方單價是否大於 0 且可解析 | `h_price_m2` | Boolean / `BOOLEAN` | Valid positive → `TRUE`；其他 → `FALSE` | No | 必須與 `unit_price_ntd_m2` null state 一致 | All clean rows |
| `land_transfer_area_valid` | Raw 土地面積是否大於 0 且可解析 | `trans_landsize` | Boolean / `BOOLEAN` | Valid positive → `TRUE`；其他 → `FALSE` | No | 必須與 `land_transfer_area_m2` null state 一致 | All clean rows |
| `building_transfer_area_valid` | Raw 建物面積是否大於 0 且可解析 | `trans_size` | Boolean / `BOOLEAN` | Building row valid positive → `TRUE`；invalid → `FALSE`; land → `NULL` | Yes | `NULL` 只允許在不適用的 land rows | Building validity flag |
| `parking_data_complete` | 含車位交易是否同時具備正值車位價格與面積 | `parking_price`, `parking_size` | Boolean / `BOOLEAN` | 兩者 > 0 → `TRUE`；否則 `FALSE` | Yes | `TRUE/FALSE` 只用於房地+車位；其他 types → `NULL` | Completeness flag，不是調整後單價 |
| `has_note` | 官方備註是否非空白 | `note` | Boolean / `BOOLEAN` | Trim 後非空 → `TRUE`；空白／missing → `FALSE` | No | 必須為 boolean；free text 不進 clean table | Filter／quality flag；不自動排除 |

## 9. Excluded dataset contract

`transactions_excluded` 每個 raw row 最多一列：

| Field | Type | Nullable | Constraint |
|---|---|---:|---|
| `exclusion_record_id` | Generated unique ID | No | 唯一識別 excluded record，不是交易 ID |
| `cleaning_run_id` | String | No | 必須對應同一次 audit |
| `source_transaction_id` | String | Yes | Raw `no`；允許缺失或重複 |
| `primary_exclusion_reason` | Controlled string | No | 依 cleaning spec precedence 指派固定 reason code |

Primary reasons：`missing_source_transaction_id`、`duplicate_exact`、`duplicate_conflict`、`invalid_transaction_period`、`unmapped_location`、`excluded_transaction_type`。

## 10. Publication constraints

Clean schema 發布前至少滿足：

- Required raw schema and reference sources valid。
- Clean `source_transaction_id` unique and non-null。
- `input_rows = clean_rows + excluded_rows`。
- Expected current-source baseline 可重現。
- Output 可完整寫入、讀回並驗證 schema／row count。
- Failed run 不得覆蓋上一版 successful outputs。
