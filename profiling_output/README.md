# 資料剖析輸出

這裡保存 `src/profiling/` 產生的結果，不是清理流程的輸入，也不是正式清理後資料集。

- `summary/`：欄位統計、分布與公式吻合率等公開彙總結果。
- `private/`：含原始交易資料列、識別資訊或暫存資料，不隨公開專案提供。

目前結果對應的原始檔共有 4,380,208 筆、63 個欄位，SHA-256 為 `1d81eb74a5c7f3607b93966732b90a7e3b8cdf5debacf905b67e1afab86b25b2`。若來源檢查碼不同，不應直接沿用這些統計。

每份結果由哪支程式產生、採用什麼方法，以及何時需要重跑，請見 [資料剖析程式說明](../src/profiling/README.md)。正式清理規則則見 [資料清理與欄位契約](../docs/data-contract.md)。

`summary/six_municipality_transition_validation.csv` 由行政區驗證程式產生，用來證明六都歷史名稱與行政區對應通過；建置方式見 [行政區參照資料](../data/reference/README.md)。
