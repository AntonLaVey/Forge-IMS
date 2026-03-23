-- Add contact_name to vendors table (was missing from original schema)
ALTER TABLE vendors ADD COLUMN IF NOT EXISTS contact_name VARCHAR(200);
DO $$ BEGIN RAISE NOTICE 'vendors.contact_name column added OK'; END $$;
