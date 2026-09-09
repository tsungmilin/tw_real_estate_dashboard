# 資料清理與欄位契約

本文件是 Python 清理行為與 `transactions_clean`／`transactions_excluded` 欄位的權威來源。它維護穩定規則，不重複保存會隨來源更新的筆數與檢查碼；目前快照見 [專案進度](project-status.md)，來源與更新方式見 [資料來源與未來更新](data-sources.md)，系統邊界見 [技術規格](technical-spec.md)。

## 1. 適用資料

| 項目 | 值 |
|---|---|
| 原始檔 | `data/raw/house_preowned_data_2.0.dta` |
| 標準期間 | 2012-08 至 2024-12 |
| 預設分批大小 | 100,000 筆 |

原始 `.dta` 不可變、不覆寫，也不隨公開專案提供。清理流程只讀取必要欄位，分批處理全部資料列；Parquet 是可重建的成功檢查點，不保存每次完整快照。

## 2. 資料列納入與排除

清理後資料只保留行政區可對應，且交易類型為下列三種的標準期間資料：

- `房地(土地+建物)`
- `房地(土地+建物)+車位`
- `土地`

同一筆資料可能符合多個排除條件，但 `transactions_excluded` 只保存一個主要原因，優先順序如下：

1. `missing_source_transaction_id`
2. `duplicate_exact`／`duplicate_conflict`
3. `invalid_transaction_period`
4. `unmapped_location`
5. `excluded_transaction_type`

相同 ID 且內容相同時保留第一次出現的資料，其餘列為 `duplicate_exact`；相同 ID 但內容不同時整組排除為 `duplicate_conflict`。不同 ID 即使內容相同，也不視為重複交易。

## 3. 共通轉換原則

- 文字去除前後空白並統一全形／半形英數字；空字串轉為 `NULL`。
- 新台幣金額保留元，面積保留平方公尺。萬元、坪及每坪單價在分析層衍生。
- 缺失、不可解析、非有限、0 或負值的價格與面積轉為 `NULL`，並由有效性標記保留原始狀態。
- 正值極端值保留，不截尾、不縮尾（winsorize），也不自動排除。
- 土地交易不適用的建物欄位設為 `NULL`／`not_applicable`，不以 0 代替。
- 每次執行產生唯一 `cleaning_run_id`，清理後資料、排除資料與稽核輸出共用該值。

## 4. 日期規則

- `trans_y` 為民國年，轉換時加 1911；`trans_m` 只接受 1–12。
- 年月缺失、無效或超出標準期間時排除為 `invalid_transaction_period`。
- 年月有效但日期缺失或無效時保留資料列，`transaction_date=NULL`、`transaction_date_valid=FALSE`。
- `date_complete` 只解析正值且為 6／7 位的民國 `yyyymmdd`。
- 完工日晚於交易日只設定 `completion_after_transaction`，不修改或排除資料。
- v1 不計算屋齡。

`completion_date_status` 的合法值：

| 值 | 意義 |
|---|---|
| `not_applicable` | 土地交易 |
| `missing` | 建物交易缺少原始值 |
| `valid` | 6／7 位且可解析 |
| `ambiguous_5_digit` | 五位數，無法可靠判斷 |
| `invalid` | 其他長度、非正值或無效日期 |

## 5. 行政區規則

`data/reference/location_lookup.csv` 是行政區權威來源，必須包含 368 筆行政區、22 個縣市，且官方 ID 與名稱關係唯一。原始資料以 `countycd + 標準化 town` 對應，`county` 只作交叉驗證。

已確認三個舊名／簡寫：`頭份鎮 → 頭份市`、`員林鎮 → 員林市`、`阿里山 → 阿里山鄉`。歷史 `桃園縣` 透過代碼對應為 `桃園市`。無法對應的資料列排除為 `unmapped_location`。

清理後 Parquet 保存 `county_id`、`town_id`、`city`、`district`；資料倉儲代理鍵 `location_id` 只存在於 PostgreSQL `core`。

## 6. 交易屬性規則

- `building_type` 保留來源官方分類，不自行合併。
- `usage` 轉為 `primary_use`；`見其它登記事項` 統一為 `見其他登記事項`。
- `manage` 的 `有／無` 轉為 `TRUE／FALSE`，其他值為 `NULL` 並記入稽核。
- `room`、`hall`、`bath` 解析為可為 `NULL` 的非負整數；0 保留，負值、非整數或解析失敗轉為 `NULL`。
- `parking_data_complete` 只表示含車位交易是否同時具備正值車位價格與面積，不表示單價已重新計算。
- `has_note` 只表示官方備註是否非空白，不是排除條件。

## 7. `transactions_clean` 欄位

一列代表一筆符合條件的交易，共 32 個欄位。

### 識別、日期與行政區

| 欄位 | 型別 | 可為 `NULL` | 語意／限制 |
|---|---|---:|---|
| `cleaning_run_id` | 字串 | 否 | 產生資料列的清理執行 ID |
| `source_transaction_id` | 字串 | 否 | 來源 `no`；清理後輸出唯一 |
| `transaction_year` | `SMALLINT` | 否 | 公曆交易年 |
| `transaction_month` | `SMALLINT` | 否 | 交易月份 1–12 |
| `transaction_date` | `DATE` | 是 | 有效的完整交易日 |
| `transaction_date_valid` | `BOOLEAN` | 否 | 必須與 `transaction_date` 的空值狀態一致 |
| `completion_date` | `DATE` | 是 | 有效的建築完成日 |
| `completion_date_status` | `TEXT` | 否 | 完工日期解析狀態 |
| `completion_after_transaction` | `BOOLEAN` | 是 | 兩個日期皆有效時才比較 |
| `county_id` | 五位 `TEXT` | 否 | 政府縣市代碼 |
| `town_id` | 八位 `TEXT` | 否 | 政府鄉鎮市區代碼 |
| `city` | `TEXT` | 否 | 標準縣市名稱 |
| `district` | `TEXT` | 否 | 標準鄉鎮市區名稱 |

### 交易、土地與建物

| 欄位 | 型別 | 可為 `NULL` | 語意／限制 |
|---|---|---:|---|
| `transaction_type` | `TEXT` | 否 | 只允許三種納入類型 |
| `urban_land_use_type` | 字串 | 是 | 都市土地使用分區；只存於 Parquet |
| `nonurban_land_use_zone` | 字串 | 是 | 非都市土地使用分區；只存於 Parquet |
| `building_type` | `TEXT` | 是 | 建物型態；土地交易為 `NULL` |
| `primary_use` | `TEXT` | 是 | 建物主要用途；土地交易為 `NULL` |
| `has_management` | `BOOLEAN` | 是 | 是否有管理組織；土地交易為 `NULL` |
| `room_count` | `INTEGER` | 是 | 非負房數；土地交易為 `NULL` |
| `hall_count` | `INTEGER` | 是 | 非負廳數；土地交易為 `NULL` |
| `bathroom_count` | `INTEGER` | 是 | 非負衛浴數；土地交易為 `NULL` |

### 價格、面積與品質

| 欄位 | 型別 | 可為 `NULL` | 單位／語意 |
|---|---|---:|---|
| `land_transfer_area_m2` | `DOUBLE PRECISION` | 是 | 土地移轉面積，m² |
| `building_transfer_area_m2` | `DOUBLE PRECISION` | 是 | 建物移轉面積，m²；土地交易為 `NULL` |
| `total_price_ntd` | `BIGINT` | 是 | 官方登錄總價，NTD |
| `unit_price_ntd_m2` | `DOUBLE PRECISION` | 是 | 官方每平方公尺單價，NTD/m² |
| `total_price_valid` | `BOOLEAN` | 否 | 原始總價是否為可解析正值 |
| `unit_price_valid` | `BOOLEAN` | 否 | 原始單價是否為可解析正值 |
| `land_transfer_area_valid` | `BOOLEAN` | 否 | 原始土地面積是否為可解析正值 |
| `building_transfer_area_valid` | `BOOLEAN` | 是 | 建物面積是否有效；土地交易為 `NULL` |
| `parking_data_complete` | `BOOLEAN` | 是 | 只適用房地＋車位交易 |
| `has_note` | `BOOLEAN` | 否 | 官方備註是否非空白 |

## 8. `transactions_excluded` 欄位

| 欄位 | 可為 `NULL` | 語意 |
|---|---:|---|
| `exclusion_record_id` | 否 | 系統產生的排除紀錄 ID，不是交易 ID |
| `cleaning_run_id` | 否 | 對應同一次清理稽核 |
| `source_transaction_id` | 是 | 原始 `no`，允許缺失或重複 |
| `primary_exclusion_reason` | 否 | 依優先順序指派的固定原因代碼 |

## 9. 稽核與發布

資料發布前必須確認：

- 必要原始欄位與行政區參照資料有效。
- 清理後 `source_transaction_id` 唯一且不可為 `NULL`。
- `input_rows = clean_rows + excluded_rows`。
- 輸出可完整寫入、讀回，且欄位結構與筆數一致。
- 相同來源檢查碼可重現既有基準。

目前來源的已驗證筆數、檢查碼與排除基準統一記錄在 [專案進度](project-status.md) 及 `data/audit/` 的最新成功報告。來源改變時，舊數字只作比較；綱要、唯一性、輸出讀回與筆數對帳仍是強制檢核。

發布流程先寫入暫存檔；全部檢核通過後才一起更新目前的清理後 Parquet、排除 Parquet 與稽核 JSON。失敗時保留失敗稽核，但不覆蓋上一版成功輸出。

## 10. 不在本版範圍

- 屋齡、坪數、萬元與每坪單價。
- 用途大類、分析層極端值門檻。
- 扣除車位後的面積、價格或單價。
- 地址、經緯度與地號子表。
- PostgreSQL 代理鍵、KPI 與 Tableau 呈現。
