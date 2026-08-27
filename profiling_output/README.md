# Profiling outputs

這裡保存 `src/profiling/` 產生的結果，不是 cleaning pipeline 的輸入，也不是正式 cleaned dataset。

- `summary/`：欄位統計、分布與公式吻合率等 aggregate 結果，可提交 Git。
- `private/`：含原始交易 rows、識別資訊或暫存資料，只保留在本機並由 `.gitignore` 排除。

目前結果對應的原始檔共有 4,380,208 rows、63 columns，SHA-256 為 `1d81eb74a5c7f3607b93966732b90a7e3b8cdf5debacf905b67e1afab86b25b2`。若來源 checksum 不同，不應直接沿用這些統計。

每份結果由哪支程式產生、採用什麼方法，以及何時需要重跑，請見 [profiling scripts 說明](../src/profiling/README.md)。
