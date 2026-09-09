# 貢獻與文件維護規範

本文件說明程式、Markdown 與註解的共同維護原則。目標是讓第一次接觸專案的人能快速找到正確入口，同時避免相同規則散落在多份文件中。

## 1. 文件責任

| 資訊 | 維護位置 |
|---|---|
| 專案入口、成果摘要、閱讀導覽與第一次操作 | `README.md` |
| 目前快照、驗收結果與未來展望 | `docs/project-status.md` |
| 資料來源、更新頻率與未來換源 | `docs/data-sources.md` |
| 系統架構與資料層邊界 | `docs/technical-spec.md` |
| 清理規則與輸出欄位 | `docs/data-contract.md` |
| PostgreSQL 初始化、更新與復原 | `docs/database-operations.md` |
| KPI、分析期間、Tableau 呈現與交付驗收 | `docs/analytics-dashboard.md` |
| 跨資料層的重要決策 | `docs/decision-log.md` |

其他文件可以摘要，但不得重複維護完整規則。摘要後應連結上表的權威文件。

### 資訊層次

| 資訊類型 | 放置位置 | 範例 |
|---|---|---|
| 導覽摘要 | `README.md` | 專案目的、資料流、Dashboard 成果與閱讀順序 |
| 穩定規則 | 對應權威規格 | 欄位語意、KPI 公式、UPSERT 行為、資料層責任 |
| 當前快照 | `docs/project-status.md` | 筆數、發布月份、測試結果與正式交付物 |
| 執行證據 | 產生的 audit／summary | 單次清理檢查碼、排除原因與品質分布 |
| 歷史原因 | `docs/decision-log.md` | 決策演進及被取代項目 |

快照數字若出現在 README，只能作首頁摘要，並連結 `project-status.md`；長期規格不得複製維護同一組當前數字。

## 2. 撰寫原則

- 使用繁體中文；程式識別碼、CLI 參數、SQL schema、資料表與欄位名稱保留原文。
- 專有名詞第一次出現時使用「中文（英文）」；後續固定使用同一名稱。
- 一段只表達一個重點，優先使用短句。
- 先寫讀者需要採取的行動，再補充原因與限制。
- 以表格呈現欄位對照，以流程圖呈現順序；簡單資訊不額外視覺化。
- 不在長期規格中加入會快速過時的目前進度、批次 ID 或備份路徑。

建議用語：

| 英文 | 中文敘述 |
|---|---|
| loader | 載入器 |
| orchestrator | 流程協調器 |
| full refresh | 完整重建 |
| incremental refresh | 增量更新 |
| validation / reconciliation | 驗證／對帳 |
| mismatch | 不一致 |
| anchor month | 基準月 |
| gate | 阻擋條件／發布檢核 |
| reference data | 參照資料 |
| smoke test | 基本流程測試 |

`staging`、`core`、`analytics` 是資料庫 schema 名稱，在指涉實體名稱時保留小寫原文。

## 3. 註解與 docstring

註解應說明程式碼本身無法清楚表達的內容：

- 設計原因與資料語意。
- 不可破壞的限制。
- 副作用、失敗行為與復原方式。
- 特殊效能或記憶體考量。

避免：

- 逐行翻譯程式碼。
- 解釋一般 Python／SQL 語法。
- 使用大量裝飾性分隔線。
- 保留已失效的檔名、開發計畫或執行狀態。
- 在註解中寫死可能變動的資料筆數；若確有需要，必須附上快照日期或來源檢查碼。

主要 Python 模組的 docstring 應簡述角色、輸入輸出及重要副作用。SQL 檔頭應說明用途、是否寫入永久資料、前置條件與回傳內容。

## 4. 連動更新

- 欄位或 `NULL` 語意改變：更新 `docs/data-contract.md`、Parquet 結構、資料庫 DDL 與測試。
- 資料來源或發布頻率改變：更新 `docs/data-sources.md`、README 的來源摘要與專案進度。
- KPI 改變：更新 `docs/analytics-dashboard.md`、`analytics` SQL、驗證與 Tableau 說明。
- 資料流程階段改變：更新 `README.md`、`docs/technical-spec.md` 與相關模組 docstring。
- 只有目前快照或驗收結果改變：原則上只更新 `docs/project-status.md` 與 README 的狀態摘要。
- Tableau 頁面、KPI 卡、控制項或交付方式改變：更新 `docs/analytics-dashboard.md`、`docs/project-status.md` 與 README 的成果摘要；跨層取捨另追加到決策紀錄。
- 已接受的跨層決策改變：在 `docs/decision-log.md` 新增決策，並標記被取代項目。

## 5. 完成前檢查

- 相對連結均有效。
- README 與專案進度使用相同階段。
- README 的閱讀順序能從專案全貌逐步導向架構、契約、操作與 Dashboard。
- 沒有「尚未套用」「日後完成」等過時敘述。
- 初始化、日常更新與完整修復可以清楚區分。
- 精確規則只有一個權威來源。
- 當前筆數、發布月份、測試結果與正式 Tableau 檔名以專案進度為準。
- Tableau 可攜版的 extract 已啟用、內含 Hyper，且完成離線操作驗收。
- 文件或註解修改沒有改變 Python／SQL 行為。
- 產生檔、Parquet、原始資料及私人稽核輸出未被手動修改。
