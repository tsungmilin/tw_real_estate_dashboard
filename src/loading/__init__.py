"""PostgreSQL staging 載入器對其他模組公開的介面。"""

# 只從套件根目錄匯出外部真正需要使用的設定、結果與入口函式，
# 其餘底線開頭的輔助函式仍留在 postgres_loader 模組內部。
from .postgres_loader import (
    LoadConfig,
    LoadResult,
    ParquetSnapshot,
    inspect_clean_parquet,
    load_postgres_staging,
)

# 明確列出 public API，避免使用萬用匯入時暴露內部實作細節。
__all__ = [
    "LoadConfig",
    "LoadResult",
    "ParquetSnapshot",
    "inspect_clean_parquet",
    "load_postgres_staging",
]
