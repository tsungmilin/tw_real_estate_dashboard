# 決策紀錄

- **狀態：** 使用中
- **更新日期：** 2026-09-08

本文件記錄已接受且會影響多個資料層的決策。精確實作仍以右欄連結的權威規格為準；新決策只追加，或明確標記為「已取代」，不會直接改寫歷史。

## 1. 資料與清理

| ID | 已接受的決策 | 影響 | 權威規格 |
|---|---|---|---|
| D-001 | 原始 `.dta` 不可變 | 資料流程必須可由原始輸入重建 | [資料契約](data-contract.md) |
| D-002 | 使用清理 → Parquet → PostgreSQL → Tableau | 將儲存、資料供應與呈現分層 | [技術規格](technical-spec.md) |
| D-003 | 資料契約是清理行為與欄位的權威來源 | 其他文件不重複維護完整規則 | [資料契約](data-contract.md) |
| D-004 | 標準資料期間為 2012-08 至 2024-12 | 超出期間的資料列進入排除資料集 | [資料契約](data-contract.md) |
| D-005 | 清理後資料集只保留房地、房地＋車位與土地 | 其他交易類型排除 | [資料契約](data-contract.md) |
| D-006 | 正值極端值不截尾、不縮尾（winsorize）、不自動刪除 | 以稽核與下游揭露處理 | [資料契約](data-contract.md) |
| D-007 | 清理後資料、排除資料與稽核輸出共用 `cleaning_run_id` | 支援對帳與追溯 | [資料契約](data-contract.md) |
| D-008 | 標準欄位保存 NTD 與 m²；核心層保留範圍已由 D-023 精簡 | 顯示單位在分析層衍生 | [資料契約](data-contract.md) |
| D-009 | 保留政府行政區代碼；核心層另建代理鍵 | 同時支援官方行政區與星狀綱要 | [資料契約](data-contract.md) |
| D-010 | Tableau 主要讀取分析資料集 | 呈現層不負責原始資料清理 | [技術規格](technical-spec.md) |

## 2. PostgreSQL 暫存層

| ID | 已接受的決策 | 影響 | 權威規格 |
|---|---|---|---|
| D-011 | 已由 D-025 取代：暫存層保存完整快照並刪除來源不存在的 ID | 歷史決策，不再作為目前載入語意 | [資料庫操作](database-operations.md) |
| D-012 | `source_transaction_id` 是暫存層主鍵 | 相同 Parquet 可冪等重跑 | [資料庫操作](database-operations.md) |
| D-013 | 載入緩衝區、同步與驗證共用一個資料庫交易 | 載入失敗時不發布部分資料 | [資料庫操作](database-operations.md) |
| D-025 | staging 採 UPSERT-only；本批來源未出現的既有交易保留 | 一般載入只新增或更新，月份影響另存 `load_month_changes`；取代 D-011 | [資料庫操作](database-operations.md) |

## 3. KPI

| ID | 已接受的決策 | 影響 | 權威規格 |
|---|---|---|---|
| D-014 | Tableau v1 房屋 KPI 納入房地與房地＋車位，排除土地 | 土地不與房屋價格混算 | [分析指標與 Tableau Dashboard](analytics-dashboard.md) |
| D-015 | 已由 D-022 取代：中位單價與案件數使用不同納入條件 | 歷史決策，不再用於分析層 | [分析指標與 Tableau Dashboard](analytics-dashboard.md) |
| D-016 | Tableau v1 不提供建物類型與主要用途篩選；核心欄位保留範圍已由 D-023 取代 | 介面決策繼續有效；核心層採精簡欄位 | [分析指標與 Tableau Dashboard](analytics-dashboard.md) |
| D-017 | KPI 對每筆符合條件的交易給予相同權重 | 備註、完工日晚於交易日與正值極端價格均納入 | [分析指標與 Tableau Dashboard](analytics-dashboard.md) |
| D-018 | 已由 D-030 取代：Tableau v1 的分析截止月份與預設月份皆固定為 2024-07 | 歷史決策；現有快照依新規則仍得到 2024-07 | [分析指標與 Tableau Dashboard](analytics-dashboard.md) |
| D-019 | 已由 D-034 取代：中位單價與案件數顯示年增率（YoY），總價不顯示 | 歷史決策；目前三張 KPI 卡都顯示 YoY | [分析指標與 Tableau Dashboard](analytics-dashboard.md) |
| D-030 | 以各月價格完整房屋案件數中位數的 75% 為尾端門檻 | 門檻只決定尾端；增量只延伸、不自動縮短 | [分析指標與 Tableau Dashboard](analytics-dashboard.md) |
| D-031 | 行政區近三個月 KPI 由逐筆交易重算，行政區骨架動態取自 core | 不組合月度中位數；參照成員改變時完整重建 | [分析指標與 Tableau Dashboard](analytics-dashboard.md) |
| D-032 | `analytics` 支援完整重建與明確批次的增量更新；日常流程只執行增量 | 執行狀態永久保存，同批可冪等回傳或失敗重試 | [資料庫操作](database-operations.md) |

## 4. 儀表板

| ID | 已接受的決策 | 影響 | 權威規格 |
|---|---|---|---|
| D-020 | 已由 D-033 取代：第 1 頁排名固定顯示前 5 名 | 歷史決策，不再用於目前概覽頁 | [分析指標與 Tableau Dashboard](analytics-dashboard.md) |
| D-021 | 第 2 頁使用 24 個月趨勢與近三個月行政區散點圖 | 趨勢與近期橫斷面分工；不使用四象限或參考線 | [分析指標與 Tableau Dashboard](analytics-dashboard.md) |
| D-033 | 已由 D-034 取代：第 1 頁保留單價與案件數 KPI，搭配縣市 YoY 地圖及近 12 個月全國 YoY 趨勢 | 歷史決策；地圖與趨勢仍保留，KPI 已加入總價 | [分析指標與 Tableau Dashboard](analytics-dashboard.md) |
| D-034 | 兩頁都呈現單價中位數、總價中位數與案件數 KPI，三者皆顯示 YoY；概覽地圖與趨勢仍只切換單價／案件數 | 同時呈現市場價格水準、總價水準與交易規模；取代 D-019、D-033 | [分析指標與 Tableau Dashboard](analytics-dashboard.md) |
| D-035 | Tableau 維護 PostgreSQL 即時版與內嵌 Hyper 的可攜版，兩者共用相同畫面與計算邏輯 | 開發更新與作品集交付分開，避免公開檔依賴本機資料庫 | [分析指標與 Tableau Dashboard](analytics-dashboard.md) |
| D-036 | 可攜版 Hyper 只包含三張 analytics 表與 `dim_location` | 支援離線互動，同時避免交付原始、逐筆或中介資料 | [技術規格](technical-spec.md) |

## 5. 核心層與修訂決策

| ID | 已接受的決策 | 影響 | 權威規格 |
|---|---|---|---|
| D-022 | 房屋單價中位數、總價中位數與案件數使用同一批價格完整交易 | 三個 KPI 均要求總價與單價非 `NULL`；取代 D-015 | [分析指標與 Tableau Dashboard](analytics-dashboard.md) |
| D-023 | 核心層使用 `dim_location`、`dim_building_type` 與 `fact_transactions`，事實表只保留月份、兩個維度鍵、交易類型與兩個價格欄位 | 不建立 `dim_date`；其餘明細與品質欄位保留在 staging 與 Parquet | [技術規格](technical-spec.md) |
| D-024 | 已由 D-026 取代：核心事實表與 staging 完整快照同步並刪除缺失交易 | 歷史決策，不再作為目前核心同步語意 | [資料庫操作](database-operations.md) |
| D-026 | `core` 事實表的完整回填與日常批次同步皆採 UPSERT-only | 不自動刪除交易；完整回填與增量同步分開；取代 D-024 | [資料庫操作](database-operations.md) |
| D-027 | `dim_location` 由完整 368 筆行政區參照資料建立 | 日常交易批次不維護行政區；無法對應時失敗 | [資料庫操作](database-operations.md) |
| D-028 | `staging`、`core`、`analytics` 載入器與流程協調器分層 | 各元件只寫自己的資料層，並傳遞明確 `load_batch_id` | [資料庫操作](database-operations.md) |
| D-029 | 每次 staging 載入嘗試使用獨立 UUID 作為 `load_batch_id` | 批次身分不依賴時間或來源檢查碼 | [資料庫操作](database-operations.md) |
