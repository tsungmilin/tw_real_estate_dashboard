# Location reference

這裡保存 cleaning pipeline 使用的行政區對照資料：

- `location_lookup.csv`：368 個行政區的 legacy codes、官方 IDs 與標準名稱。
- `location_aliases.csv`：原始交易資料中已確認的 3 個行政區舊名／簡寫。

兩份 CSV 由 `src/reference/build_location_reference.py` 根據既有 town-level Stata 與 crosswalk Excel 建立，不手動填寫。Excel 只保留官方 IDs、縣市與鄉鎮名稱，去重後必須恰好 368 rows；legacy codes 由 town-level Stata 提供，兩邊再以縣市＋鄉鎮名稱一對一合併。

六都的行政區完整性及原始交易中的歷史縣市名稱，另由 `src/reference/validate_location_mapping.py` 分批驗證。

## Scripts

- `build_location_reference.py`：將 Excel 去重到鄉鎮層級，再與 368-row DTA 合併，輸出 lookup 與 aliases。
- `validate_location_mapping.py`：分批套用 lookup，核對 97 筆 unmapped baseline 並輸出六都摘要。

兩支程式都以 command-line 參數接收來源路徑；使用方式可執行 `python <script> --help` 查看。

## Profiling v1 source

- Town-level DTA SHA-256：`9103af50a3f231673628ab46ac4e003e7533d9a65a7821b59f924bb6e5ccfd41`
- Official ID Excel SHA-256：`8cf088d2237f905ee0caef6348d34541a2baf906eb93b79b25680e231c9fe3e3`
