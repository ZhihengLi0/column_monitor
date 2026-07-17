-- Pulse-tube compressor analog values, logged each cron cycle by
-- record_pulsetube() in monitor.py. device_states keeps only the latest
-- snapshot, so this table builds the history needed for plotting.
-- Run once on the cs2 database:  psql -h localhost -U postgres -d cs2 -f schema_pulsetube.sql

CREATE TABLE IF NOT EXISTS pulsetube_readings (
    id            BIGSERIAL PRIMARY KEY,
    time          TIMESTAMPTZ NOT NULL DEFAULT now(),
    coolant_in    DOUBLE PRECISION,   -- K
    coolant_out   DOUBLE PRECISION,   -- K
    oil           DOUBLE PRECISION,   -- K
    helium        DOUBLE PRECISION,   -- K
    high_pressure DOUBLE PRECISION,   -- Pa
    low_pressure  DOUBLE PRECISION,   -- Pa
    motor_current DOUBLE PRECISION,   -- A
    running       BOOLEAN
);
CREATE INDEX IF NOT EXISTS idx_pt_time ON pulsetube_readings (time DESC);
