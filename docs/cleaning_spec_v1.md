# Taiwan Real Estate Dashboard — Cleaning Specification v1

- 狀態：設計完成，待實作驗證
- 更新日期：2026-08-26
- 適用來源：`data/raw/house_preowned_data_2.0.dta`
- 規模：4,380,208 rows、63 raw columns

## 1. 文件定位

本文件是 Python cleaning pipeline 的實作依據。舊版 project plan、canonical schema 與 profiling 紀錄保留為背景；若內容衝突，以本文件所整理的最新討論與全資料驗證結果為準。

專案目標是資料分析／Dashboard portfolio，因此 cleaning 必須可重現、可對帳，但不在此層提前建立所有分析指標。

```text
Raw .dta
   ↓
Python chunked cleaning + validation
   ↓
current clean Parquet + excluded Parquet + audit
   ↓
PostgreSQL staging / core / analytics
   ↓
Tableau
```

Parquet 是本機可重建的 cleaning checkpoint，不設計成複雜資料湖，也不保存每次完整資料快照。

## 2. 全域原則

1. Raw `.dta` immutable，不覆寫、不納入 Git。
2. 使用 chunked reading；預設每批 100,000 rows，並只 materialize 必要欄位。
3. Canonical 保持新台幣元與平方公尺；坪、萬元、每坪單價與屋齡留到 analytics mart。
4. 有明確語意的無效值轉為 `NULL`；正值極端值不截尾、不 winsorize、不自動刪除。
5. 個別資料問題依規則排除或加 flag；結構與對帳問題才讓整次 run 失敗。
6. 每次 run 產生唯一 `cleaning_run_id`，clean、excluded 與 audit 共用。
7. 不保存 `source_row_number`；`source_transaction_id` 使用 raw `no`。

## 3. Row eligibility 與排除順序

### 3.1 Primary exclusion precedence

同一筆資料可能同時觸犯多項規則。`transactions_excluded` 每筆只指定一個 `primary_exclusion_reason`，依下列順序決定；audit 仍獨立統計所有檢查命中數。

1. `missing_source_transaction_id`
2. `duplicate_exact` / `duplicate_conflict`
3. `invalid_transaction_period`
4. `unmapped_location`
5. `excluded_transaction_type`

此順序用於排除歸因與筆數對帳，不限制程式內部的最佳化執行順序。

### 3.2 Source ID 與重複

- `no` 去除前後空白後作為 `source_transaction_id`。
- 缺失或空白：排除為 `missing_source_transaction_id`，不讓整次 run 失敗。
- 同一 run、相同 ID、其他欄位完全相同：保留一筆，其餘為 `duplicate_exact`。
- 同一 run、相同 ID、內容不同：整組排除為 `duplicate_conflict`，不任選一筆。
- 不同 ID 即使其他欄位相同，也不視為重複交易。
- 未來不同成功 run 出現相同 ID 時，以較新的來源批次更新 PostgreSQL；交易日期不可作為版本時間。
- 目前 profiling：4,380,208 個 `no` 均非空且全域唯一。

### 3.3 Transaction period

- 來源：`trans_y`（民國年）、`trans_m`、`trans_d`。
- Canonical 期間：民國 101 年 8 月至 113 年 12 月，即 2012-08 至 2024-12。
- 年或月缺失、月份不在 1–12、或年月超出期間：排除為 `invalid_transaction_period`。
- 年月有效但日缺失或不是有效日曆日：保留交易，`transaction_date=NULL`、`transaction_date_valid=false`。
- 年月日有效：民國年加 1911，產生 Gregorian `transaction_date`。
- Raw `year` 與 `trans_y` 已驗證一致；`year` 不進 canonical。
- `date_trans` 只可作 optional cross-check，不影響 row eligibility，也不進 canonical。

### 3.4 Transaction type

只保留：

- `房地(土地+建物)`
- `房地(土地+建物)+車位`
- `土地`

排除 `車位`、`建物`、空白與其他未知值，原因為 `excluded_transaction_type`。

`transaction_count` 定義為 canonical transaction rows 數，不是 `trans_num` 中的建物或車位單位數。

## 4. Location mapping

### 4.1 Reference sources

使用兩份既有 reference source 建立專案內的 district-level lookup：

- `vill_code_2020AndAfter.dta`：368 個鄉鎮市區，欄位為 `hsn_nm / town_nm / hsn_cd / town_cd`。
- `vill_code_內政編碼跟財資編碼對照版(逸芩整理).xlsx`：村里層級的新舊代碼 crosswalk。

實作時產生並版本化：

```text
data/reference/location_lookup.csv
data/reference/location_aliases.csv
```

Pipeline 不得硬編碼 Desktop 或論文資料夾的絕對路徑。

### 4.2 Lookup construction

`location_lookup` 應含：

| field | rule |
|---|---|
| `legacy_county_code` | `hsn_cd`，字串 |
| `legacy_town_code` | `town_cd`，兩位字串 |
| `county_id` | 五位字串，補回前導 0 |
| `town_id` | 八位字串，補回前導 0 |
| `city` | lookup 標準縣市名 |
| `district` | lookup 標準鄉鎮市區名 |

建立規則：

1. Excel 先排除 `hsn_cd` 或 `town_cd` 缺失的列；已知一筆為雲林縣斗六市正心里。
2. 由村里層級去重為 368 個 district rows。
3. 與 town-level `.dta` 的 368 rows 全數對上。
4. `legacy_county_code + district` 必須唯一。
5. `county_id + town_id` 必須唯一。
6. 任一唯一性或 368-row 完整性檢查失敗，整次 cleaning 停止。

### 4.3 Raw mapping

Raw join key 使用 `countycd + normalized town`，不是 `county + town`。Raw `county` 只作交叉驗證，canonical 名稱以 lookup 為準。

已確認 alias：

| raw `countycd` | raw `town` | canonical `district` |
|---|---|---|
| `K` | `頭份鎮` | `頭份市` |
| `N` | `員林鎮` | `員林市` |
| `Q` | `阿里山` | `阿里山鄉` |

另有 raw `桃園縣 → 桃園市` 共 576,235 rows；因 join 使用 `countycd`，不需改寫 join key，輸出 `city` 直接採 lookup 的 `桃園市`。

Alias 後仍無法 mapping 的 row 排除為 `unmapped_location`。目前有效期間內剩 97 rows，皆為 raw `town` 空白。

Clean Parquet 保存 `county_id / town_id / city / district`；PostgreSQL `core.dim_location` 之後才建立 surrogate `location_id`。

## 5. 文字與類別標準化

通用規則：

- 去除前後空白。
- 空字串轉為 `NULL`。
- 統一全形／半形英數字。
- 不將 `NULL` 寫成「未知」或「未提供」；顯示文字留到 analytics mart。

欄位規則：

- `building_type`：保留 12 個官方類別，不合併。
- `usage → primary_use`：`見其它登記事項` 統一為 `見其他登記事項`；其他值保留。
- 不合併 `住家用 / 住宅 / 集合住宅 / 國民住宅` 等語意可能不同的值。
- `manage → has_management`：`有=true`、`無=false`；其他或缺失為 `NULL` 並 audit。
- `usage_type → urban_land_use_type`：保留 `住 / 商 / 工 / 農 / 其他` 等來源值。
- `nonurban_type → nonurban_land_use_zone`：保留來源分區名稱。
- `urban_land_use_type` 與 `nonurban_land_use_zone` 是條件式欄位，任一為 `NULL` 不等於資料錯誤。
- 兩個土地分區欄位保留於 clean Parquet；v1 PostgreSQL analytics mart 與 Tableau 不使用。
- Dashboard 若需較大的用途群組，於 analytics mart 另外 derive，不回寫 clean table。

## 6. 日期規則

### 6.1 Transaction date

- `transaction_year` 為 Gregorian year。
- `transaction_month` 為 1–12。
- `transaction_date_valid` 在 clean rows 中固定為 boolean，不為 `NULL`。
- 日無效時仍保留年月，確保 monthly analysis 可用。

### 6.2 Completion date

只解析正值、6 或 7 位的民國 `yyyymmdd`；轉換後必須是有效日曆日期。

| `completion_date_status` | meaning |
|---|---|
| `not_applicable` | 土地交易 |
| `missing` | 房地交易但 raw 缺失 |
| `valid` | 6/7 位且可解析 |
| `ambiguous_5_digit` | 五位數，語意無法可靠判斷 |
| `invalid` | 其他長度、非正值或不存在的日期 |

- 只有 `valid` 才填入 `completion_date`。
- `completion_after_transaction` 只有兩個完整日期都有效時才比較；否則為 `NULL`。
- 完工日晚於交易日只設 flag，不修改、不排除。
- v1 不計算 `building_age`。

## 7. 價格、面積與格局

### 7.1 價格

- `housing_totprice → total_price_ntd`。
- `h_price_m2 → unit_price_ntd_m2`。
- 缺失、0 或負值轉為 `NULL`，對應 valid flag 為 `false`。
- 正值極端值保留，不設上限。
- 官方單價不以總價與面積重算或覆寫，也不自動補值。
- 公式重算差異只記 aggregate audit，不新增 row-level mismatch flag。
- `h_price_pin` 排除；每坪單價於 analytics mart 由 m² 單價換算。

### 7.2 面積

- `trans_landsize → land_transfer_area_m2`。
- `trans_size → building_transfer_area_m2`。
- 缺失、0 或負值轉為 `NULL`，由 valid flag 說明。
- 正值極端值保留，不四捨五入、不扣除車位。
- 面積使用 source Float64；PostgreSQL 對應 `DOUBLE PRECISION`。
- 房地交易同時可有土地持分與建物移轉面積；房屋面積分析主要使用建物移轉面積。
- 土地交易主要使用土地移轉面積；土地與房地不可混合比較平均面積或單價。

### 7.3 格局

- `room / hall / bath` 轉為 nullable nonnegative integer。
- 0 可有實際意義，房地交易不轉為 `NULL`。
- 缺失、負值、非整數或無法解析時轉為 `NULL` 並 audit。
- 不設定上限、不截尾、不因極端值刪除交易。
- v1 不建立 layout outlier flags；格局分析的合理範圍留到 analytics mart。

### 7.4 Parking helper

- `parking_price` 與 `parking_size` 只作 helper，不進 clean table。
- `房地(土地+建物)+車位` 且兩者都大於 0：`parking_data_complete=true`。
- 車位交易但任一缺失、0 或負值：`parking_data_complete=false`。
- 非車位交易：`parking_data_complete=NULL`。
- 此 flag 只表示車位價與面積資料是否完整，不代表官方單價已調整。
- v1 不扣除車位價格或面積，也不建立扣車位後衍生欄位。

### 7.5 Note

- `note` 非空白：`has_note=true`；否則 `false`。
- `note` 原文與 redundant `note_yn` 不進 clean table。
- `has_note` 不代表異常交易，也不是排除條件。

## 8. Transaction-type null semantics

| field group | 土地 | 房地（土地＋建物） | 房地＋車位 |
|---|---|---|---|
| 土地移轉面積 | 正值保留 | 正值保留 | 正值保留 |
| 建物移轉面積 | `NULL` | 正值保留 | 正值保留 |
| 建物型態、用途、完工日、格局、管理 | `NULL` / `not_applicable` | 依來源清理 | 依來源清理 |
| `parking_data_complete` | `NULL` | `NULL` | `true/false` |
| 價格與官方單價 | 依有效性保留 | 依有效性保留 | 依有效性保留 |

土地 row 若意外帶有建物值，不進 clean table，但在 audit 統計。

## 9. `transactions_clean` schema

### 9.1 Types

- Parquet string → PostgreSQL `TEXT`。
- `transaction_year / transaction_month` → `SMALLINT`。
- `room_count / hall_count / bathroom_count` → nullable `INTEGER`。
- 價格 → nullable 64-bit integer / PostgreSQL `BIGINT`。
- 面積 → nullable Float64 / PostgreSQL `DOUBLE PRECISION`。
- 日期 → `DATE`。
- Flag → `BOOLEAN`；`NULL` 代表不適用。

### 9.2 Fields

| canonical field | source | nullable | definition |
|---|---|---:|---|
| `cleaning_run_id` | generated | no | 本次 cleaning run |
| `source_transaction_id` | `no` | no | 來源交易識別碼 |
| `transaction_year` | `trans_y + 1911` | no | Gregorian year |
| `transaction_month` | `trans_m` | no | 1–12 |
| `transaction_date` | `trans_y/m/d` | yes | 完整有效日期才填入 |
| `transaction_date_valid` | derived | no | 完整交易日是否有效 |
| `completion_date` | `date_complete` | yes | 有效 6/7 位民國日期 |
| `completion_date_status` | derived | no | 完工日期狀態 |
| `completion_after_transaction` | derived | yes | 完工日是否晚於交易日 |
| `county_id` | location lookup | no | 五位字串 |
| `town_id` | location lookup | no | 八位字串 |
| `city` | location lookup | no | 標準縣市名 |
| `district` | location lookup | no | 標準鄉鎮市區名 |
| `transaction_type` | `type` | no | 三種保留交易類型 |
| `urban_land_use_type` | `usage_type` | yes | 都市土地使用分區；Parquet only |
| `nonurban_land_use_zone` | `nonurban_type` | yes | 非都市土地使用分區；Parquet only |
| `building_type` | `building_type` | yes | 土地交易為 `NULL` |
| `primary_use` | `usage` | yes | 建物主要用途 |
| `has_management` | `manage` | yes | 土地交易為 `NULL` |
| `land_transfer_area_m2` | `trans_landsize` | yes | 土地移轉面積 |
| `building_transfer_area_m2` | `trans_size` | yes | 建物移轉總面積；土地交易為 `NULL` |
| `room_count` | `room` | yes | 非負整數 |
| `hall_count` | `hall` | yes | 非負整數 |
| `bathroom_count` | `bath` | yes | 非負整數 |
| `total_price_ntd` | `housing_totprice` | yes | 新台幣元 |
| `unit_price_ntd_m2` | `h_price_m2` | yes | 新台幣元／m² |
| `total_price_valid` | derived | no | raw 總價是否大於 0 |
| `unit_price_valid` | derived | no | raw 單價是否大於 0 |
| `land_transfer_area_valid` | derived | no | raw 土地面積是否大於 0 |
| `building_transfer_area_valid` | derived | yes | 土地交易 `NULL`；房地交易為 true/false |
| `parking_data_complete` | parking helpers | yes | 只適用房地＋車位 |
| `has_note` | `note` | no | note 是否非空白 |

不進 clean table：`location_id`、`transaction_count`、坪數、每坪單價、屋齡、扣車位後價格／面積。

## 10. Raw helper 與排除欄位

### 10.1 Helper only

| raw fields | use |
|---|---|
| `county / countycd / town` | location mapping 與驗證 |
| `date_trans` | optional transaction-date cross-check |
| `parking_price / parking_size` | derive `parking_data_complete` |
| `note` | derive `has_note` |

Helper 原值不進 clean table。

### 10.2 Excluded

```text
year
address
trans_num
trans_story
story
material
partition
h_price_pin
parking_type
longitude
latitude
note_yn
mainbuilding_area
affbuilding_area
balcony_area
elevator
nonurban_categ
landtotarea
landno1 ~ landno5
landloc1 ~ landloc5
landtransarea1 ~ landtransarea5
landareashare1 ~ landareashare5
```

理由包括 redundant、v1 Dashboard 無需求、全缺失、來源額外加工或 repeated wide structure。`trans_landsize` 與 `trans_size` 是正式交易層級面積，不以其他面積元件重建或覆寫。

## 11. `transactions_excluded`

每個 raw row 最多對應一個 excluded row：

| field | nullable | definition |
|---|---:|---|
| `exclusion_record_id` | no | generated unique ID |
| `cleaning_run_id` | no | 本次 cleaning run |
| `source_transaction_id` | yes | raw `no`；可缺失或重複 |
| `primary_exclusion_reason` | no | 固定 reason code |

不複製 raw 欄位內容。`exclusion_record_id` 只識別排除紀錄，不是交易 ID。

## 12. Audit

每次 run 保存一份小型 audit，至少包含：

- `cleaning_run_id`、spec version、開始／結束時間、成功／失敗。
- raw source filename 與 checksum。
- location lookup 與 alias reference checksum。
- input、clean、excluded row counts 與 reconciliation。
- 各 primary exclusion reason 筆數。
- 各檢查的獨立命中數與比例。
- output filename、row count、schema 與 checksum。

必要 aggregate checks：

- missing／duplicate ID。
- invalid period、invalid transaction day。
- unmatched location 原始組合。
- excluded transaction type。
- completion date status 與 completion-after-transaction。
- invalid／zero price and area。
- layout parse failures與 min/max/percentiles。
- building values on land rows。
- parking data completeness。
- note presence。
- unit-price formula差異的 aggregate validation。
- category missing／unexpected values。

Main table 只保存 `cleaning_run_id`，不為每項 audit check 增加 ID。

## 13. Hard-fail publication gates

以下情況整次 run 失敗，且不得覆蓋上一版成功輸出：

1. 必要 raw 欄位缺少或整欄型別無法解析。
2. Location reference 缺少、未通過 368-row reconciliation、join key 或 official ID 不唯一。
3. Clean `source_transaction_id` 仍不唯一。
4. `input_rows != clean_rows + excluded_rows`。
5. Clean dataset 為空。
6. Output 無法完整寫入、讀回、驗證 schema 或 row count。
7. 對相同 source checksum 執行時，基準 exclusion／clean counts 無法重現。

Run 失敗仍應保存 failed audit。發布流程先寫 temporary output，全部 gate 通過後才更新 current clean／excluded Parquet。

## 14. Current-source reconciliation baseline

依目前 raw 與已驗證 lookup，primary precedence 的預期結果為：

| stage / reason | rows |
|---|---:|
| Raw input | 4,380,208 |
| `missing_source_transaction_id` | 0 |
| same-run duplicate rows | 0 |
| `invalid_transaction_period` | 34,519 |
| `unmapped_location` | 97 |
| `excluded_transaction_type` | 84,126 |
| Total excluded | 118,742 |
| Expected clean | 4,261,466 |

```text
4,380,208 = 4,261,466 + 118,742
```

此表是目前 source checksum 的 acceptance baseline。未來來源版本改變時，精確筆數只作 comparison；唯一性、schema 與 row reconciliation 仍是 hard gates。

## 15. Output policy

```text
data/processed/transactions_clean.parquet
data/processed/transactions_excluded.parquet
data/audit/cleaning_run_<cleaning_run_id>.json
```

- Clean／excluded Parquet 只保留最新成功版本。
- Audit 很小，保留歷次 run。
- 不建立年度 partition、每次完整 snapshot 或 CSV 大型副本。
- PostgreSQL 載入與 analytics marts 屬下一階段，不在本 cleaning spec 實作。

## 16. Deferred / out of scope

- `building_age`。
- 坪數、萬元、每坪單價。
- 用途大類 `usage_group`。
- 格局、價格、面積的分析層 outlier threshold。
- 土地與房地的統一 transaction-area 欄位。
- 扣除車位後的面積、價格或重算單價。
- 備註文字分類。
- 地址、經緯度、parcel child table。
- PostgreSQL `location_id`、core model、analytics mart 與 Tableau metrics。

完成條件：依本 spec 實作的 full run 通過所有 hard gates、重現 current-source baseline，並產生可讀回的 clean／excluded Parquet 與 audit。
