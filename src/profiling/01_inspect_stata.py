from pathlib import Path

import pandas as pd


# 找到 project root：
# dashboard/src/profiling/01_inspect_stata.py
# 往上兩層就是 dashboard/
PROJECT_ROOT = Path(__file__).resolve().parents[2]

DTA_PATH = (
    PROJECT_ROOT
    / "data"
    / "raw"
    / "house_preowned_data_2.0.dta"
)


# iterator=True：
# 不直接把整個 14 GB .dta 讀進記憶體
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