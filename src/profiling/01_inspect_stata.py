"""Inspect Stata metadata and a small sample without loading the full source file."""

from pathlib import Path

import pandas as pd


# 從程式位置取得專案根目錄，不依賴執行時的工作目錄。
PROJECT_ROOT = Path(__file__).resolve().parents[2]

DTA_PATH = (
    PROJECT_ROOT
    / "data"
    / "raw"
    / "house_preowned_data_2.0.dta"
)


# 只讀取中繼資料，不將 14 GB 的 `.dta` 載入記憶體。
with pd.read_stata(
    DTA_PATH,
    iterator=True,
    convert_categoricals=False,
) as reader:

    print("File:")
    print(DTA_PATH)

    print("\nVariable labels:")
    print(reader.variable_labels())

    sample = reader.read(nrows=5)

    print("\nSample shape:")
    print(sample.shape)

    print("\nColumns:")
    print(sample.columns.tolist())

    print("\nData types:")
    print(sample.dtypes.to_string())

    print("\nFirst 5 rows:")
    print(sample)
