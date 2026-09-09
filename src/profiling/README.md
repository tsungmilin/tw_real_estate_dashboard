# 原始資料剖析程式

這個資料夾保存原始實價登錄資料的探索與驗證程式。它們只讀取原始 Stata 檔，不修改原始資料，也不產生正式清理後資料集。程式結果用來制定 [資料清理與欄位契約](../../docs/data-contract.md)。

## 資料剖析 v1 對應的資料

| 項目 | 值 |
|---|---|
| 原始檔 | `data/raw/house_preowned_data_2.0.dta` |
| 檔案大小 | 14,682,511,713 位元組 |
| SHA-256 | `1d81eb74a5c7f3607b93966732b90a7e3b8cdf5debacf905b67e1afab86b25b2` |
| 筆數／欄位數 | 4,380,208／63 |
| 一般分批大小 | 100,000 筆 |

只要原始檔 SHA-256 相同，就可以直接使用現有資料剖析結果，不必重新掃描 14 GB 資料。若檢查碼不同，舊結果只能作比較基準，不能視為新資料的驗證結果。

## 程式如何處理資料

- 程式分批讀取 `.dta`，避免把整份資料同時載入記憶體。
- 每支程式只載入該問題需要的欄位，但仍可能完整掃描所有資料列。
- 完整掃描產生的筆數是精確值；`05` 的百分位數是固定抽樣後的近似值。
- 公開彙總結果寫入 `profiling_output/summary/`；含逐筆資料或暫存資料的輸出寫入 `profiling_output/private/`，不隨公開專案提供。

## 程式清單

| 程式 | 目的與具體做法 | 主要輸出 |
|---|---|---|
| `01_inspect_stata.py` | 開啟 Stata 讀取器，讀取 5 筆，列出 63 個欄位、型別、變數標籤與樣本。用來確認檔案能否讀取及原始綱要。 | 終端顯示，不寫檔 |
| `02_profile_schema_candidates.py` | 分批檢查 `no` 缺值與各批內重複、交易年月日範圍、`year` 與 `trans_y` 是否一致，以及土地總面積和前五筆土地明細的差異。這一步不證明 ID 全域唯一；全域唯一性由 `08` 驗證。 | `key_fields_validation.csv` |
| `03_investigate_anomalies.py` | 統計交易年分布與非法日期，並用 ±0.01 平方公尺容許值比較土地總面積和土地明細合計；另依第 5 筆土地是否存在，判斷差異是否可能來自明細截斷。 | `year_distribution.csv`、`anomaly_summary.csv`、`land_mismatch_by_land5.csv`；逐筆非法日期寫入 `private/` |
| `04_profile_selected_fields.py` | 對交易類型、交易筆數、樓層、建物型態與用途計算缺值、空字串、不重複值數量和前 30 個常見值，確認分類欄位的實際內容。 | `selected_fields_summary.csv`、`selected_fields_top_values.csv` |
| `05_profile_price_area.py` | 對保留交易類型統計價格與面積的缺值、零值、負值、最小值與最大值；每批固定抽樣最多 2,000 筆估算百分位數；並以 ±1 元容許值檢查每平方公尺／每坪單價及基本車位扣除公式。 | `price_area_summary.csv`、`price_area_percentiles.csv`、`price_formula_validation.csv`、`parking_completeness.csv` |
| `06_validate_parking_price_logic.py` | 將含車位交易依「車位價格／面積是正值或零」分成四組，再分別測試不調整、只扣價格、只扣面積、同時扣價格與面積等公式，避免用單一公式解釋所有車位交易。 | `parking_groups.csv`、`parking_formula_validation_detailed.csv` |
| `07_profile_remaining_house_fields.py` | 在房地交易中檢查房／廳／衛數值、隔間、電梯、建物型態與用途；統計完工日期原始位數；並以有限關鍵字統計備註主題。備註分類只供探索，不是正式清理規則。 | `remaining_house_*`、`date_complete_summary.csv`、`date_complete_digit_lengths.csv`、`note_summary.csv`、`note_pattern_summary.csv` |
| `08_profile_final_schema_fields.py` | 使用磁碟型 SQLite 對全部 `no` 做精確全域唯一性檢查，避免把 438 萬個 ID 全留在記憶體；同時驗證完工日期、備註標記、建物面積元件、土地面積和剩餘分類欄位。SQLite 只作暫存，完成後刪除。 | `no_global_uniqueness.csv`、`date_complete_*`、`note_flag_consistency.csv`、`building_area_*`、`land_area_by_transaction_type.csv`、`remaining_category_*` |
| `09_validate_completion_date_edge_cases.py` | 針對五位數民國完工日期與「完工年月晚於交易年月」案例做情境驗證；先解析有效日曆日期，再以月份差分組，觀察建物型態、交易年、交易類型與備註分布。 | `completion_5digit_*`、`completion_after_transaction_*` |

## 何時需要重跑

以下情況才需要重跑相關程式：

- 原始檔檢查碼改變。
- 清理規格要使用尚未驗證的新欄位或新假設。
- 要確認新的資料品質問題是否存在於全資料。

從專案根目錄執行單支程式，例如：

```bash
python src/profiling/03_investigate_anomalies.py
```

需要 Python、`pandas` 與 `numpy`。目前沒有一鍵執行全部程式的入口，避免不小心連續掃描九次大型原始檔。若來源更新，先執行 `01` 確認綱要，再只重跑和變更問題有關的程式。既有 CSV 會被同名輸出覆寫，比較新舊來源前應先保留舊結果。

## 判讀界線

資料剖析回答「原始資料實際長什麼樣」；正式清理則決定「哪些資料列排除、欄位如何轉換，以及哪些情況只加品質標記」。兩者衝突時，以 [資料清理與欄位契約](../../docs/data-contract.md) 為準。
