-- 0101: attribution reconciles paid amounts in integer cents, not floats.
-- Materialise the cents value and index it so the nightly attribution job can join on it.
ALTER TABLE claims ADD COLUMN paid_amount_cents bigint;
UPDATE claims SET paid_amount_cents = round(paid_amount * 100);
CREATE INDEX claims_paid_amount_cents_idx ON claims (paid_amount_cents);
