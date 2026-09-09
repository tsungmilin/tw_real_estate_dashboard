-- Create the persistent building-type dimension and its member constraints.
-- Requires the core schema; preserves existing rows and returns no result set.
CREATE TABLE IF NOT EXISTS core.dim_building_type (
    building_type_id SMALLINT PRIMARY KEY,
    building_type_name TEXT NOT NULL UNIQUE,
    member_type TEXT NOT NULL,

    CONSTRAINT dim_building_type_name_trim_check
        CHECK (
            building_type_name = BTRIM(building_type_name)
            AND building_type_name <> ''
        ),

    CONSTRAINT dim_building_type_member_type_check
        CHECK (
            member_type IN (
                'official',
                'not_applicable',
                'missing'
            )
        )
);
