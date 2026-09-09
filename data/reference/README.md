# 行政區參照資料

這裡保存清理流程使用的行政區對照資料：

- `location_lookup.csv`：368 個行政區的舊代碼、官方識別碼與標準名稱。
- `location_aliases.csv`：原始交易資料中已確認的 3 個行政區舊名／簡寫。

兩份 CSV 由 `src/reference/build_location_reference.py` 根據既有鄉鎮市區層級 Stata 與對照 Excel 建立，不手動填寫。Excel 只保留官方識別碼、縣市與鄉鎮名稱，去重後必須恰好 368 筆；舊代碼由鄉鎮市區層級 Stata 提供，兩邊再以縣市＋鄉鎮名稱一對一合併。

六都的行政區完整性及原始交易中的歷史縣市名稱，另由 `src/reference/validate_location_mapping.py` 分批驗證。

## 程式

- `build_location_reference.py`：將 Excel 去重到鄉鎮層級，再與 368 筆 DTA 合併，輸出行政區對照表與別名資料。
- `validate_location_mapping.py`：分批套用行政區對照表，核對 97 筆無法對應的基準資料，並輸出六都摘要。

兩支程式都以命令列參數接收來源路徑；使用方式可執行 `python <script> --help` 查看。

## 資料剖析 v1 來源

- 鄉鎮市區層級 DTA SHA-256：`9103af50a3f231673628ab46ac4e003e7533d9a65a7821b59f924bb6e5ccfd41`
- 官方識別碼 Excel SHA-256：`8cf088d2237f905ee0caef6348d34541a2baf906eb93b79b25680e231c9fe3e3`
