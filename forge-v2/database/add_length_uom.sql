-- Migration: add length_uom and length_numeric to cut_list_items
ALTER TABLE cut_list_items ADD COLUMN IF NOT EXISTS length_uom VARCHAR(10) DEFAULT 'IN';
ALTER TABLE cut_list_items ADD COLUMN IF NOT EXISTS length_numeric NUMERIC(14,4);
DO $$ BEGIN RAISE NOTICE 'Migration: length_uom and length_numeric added OK'; END $$;
