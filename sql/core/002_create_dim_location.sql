-- Create the persistent conformed location dimension and its integrity constraints.
-- Requires the core schema; preserves existing rows and returns no result set.
CREATE TABLE IF NOT EXISTS core.dim_location (
    location_id SMALLINT
        GENERATED ALWAYS AS IDENTITY
        PRIMARY KEY,

    county_id TEXT NOT NULL,
    town_id TEXT NOT NULL,
    city TEXT NOT NULL,
    district TEXT NOT NULL,

    CONSTRAINT dim_location_town_id_unique
        UNIQUE (town_id),

    CONSTRAINT dim_location_county_id_length_check
        CHECK (LENGTH(county_id) = 5),

    CONSTRAINT dim_location_town_id_length_check
        CHECK (LENGTH(town_id) = 8),

    CONSTRAINT dim_location_city_trim_check
        CHECK (city = BTRIM(city) AND city <> ''),

    CONSTRAINT dim_location_district_trim_check
        CHECK (district = BTRIM(district) AND district <> '')
);
