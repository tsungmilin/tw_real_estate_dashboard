-- UPSERT the fixed building-type members used by the transaction fact table.
-- Requires core.dim_building_type; writes persistent rows and returns no result set.
INSERT INTO core.dim_building_type AS current_type (
    building_type_id,
    building_type_name,
    member_type
)
VALUES
    (0, '不適用', 'not_applicable'),
    (1, '未提供', 'missing'),
    (2, '住宅大樓(11層含以上有電梯)', 'official'),
    (3, '透天厝', 'official'),
    (4, '華廈(10層含以下有電梯)', 'official'),
    (5, '公寓(5樓含以下無電梯)', 'official'),
    (6, '套房(1房1廳1衛)', 'official'),
    (7, '店面(店鋪)', 'official'),
    (8, '辦公商業大樓', 'official'),
    (9, '農舍', 'official'),
    (10, '廠辦', 'official'),
    (11, '工廠', 'official'),
    (12, '倉庫', 'official'),
    (13, '其他', 'official')
ON CONFLICT (building_type_id)
DO UPDATE SET
    building_type_name = EXCLUDED.building_type_name,
    member_type = EXCLUDED.member_type
WHERE
    current_type.building_type_name
        IS DISTINCT FROM EXCLUDED.building_type_name
    OR current_type.member_type
        IS DISTINCT FROM EXCLUDED.member_type;
