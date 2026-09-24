INSERT INTO merchants (name, bank_account)
SELECT 'merchant-' || g, 'DE89' || lpad(g::text, 18, '0')
  FROM generate_series(1, 500) g
ON CONFLICT DO NOTHING;

INSERT INTO settlements (merchant_id, settlement_date, amount_minor, currency, status)
SELECT (g % 500) + 1, DATE '2026-08-01' + (g / 500), 1000 + g, 'EUR', 'executed'
  FROM generate_series(0, 4999) g
ON CONFLICT DO NOTHING;

INSERT INTO payouts (settlement_id, merchant_id, amount_minor, currency, status, bank_ref)
SELECT s.id, s.merchant_id, s.amount_minor, s.currency, 'paid', 'BNK-HIST-' || s.id
  FROM settlements s
 WHERE NOT EXISTS (SELECT 1 FROM payouts p WHERE p.settlement_id = s.id);
