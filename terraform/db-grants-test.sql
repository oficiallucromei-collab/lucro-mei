-- Least-privilege grants for the test service's database role (lucromei_test_runtime).
-- Migrations now apply the grant for each new table themselves (lucromei.dbgrants); this file
-- keeps the full list for reference and for rebuilding a database by hand. Schema changes stay with the
-- owner, used only by the lucromei-migrate job.

-- Ledger: no UPDATE, corrections are appended as events. DELETE only for LGPD erasure.
GRANT SELECT, INSERT, DELETE ON transactions, transaction_events, inbound_messages
  TO lucromei_test_runtime;

-- Onboarding updates the business; retention erases receipt text.
GRANT SELECT, INSERT, UPDATE, DELETE ON businesses, command_receipts TO lucromei_test_runtime;

-- Queue, delivery state, clarifications and retention.
GRANT SELECT, INSERT, UPDATE, DELETE ON inbound_events, outbound_messages, jobs, pending_entries
  TO lucromei_test_runtime;

-- Append-only records, deleted only with the account.
GRANT SELECT, INSERT, DELETE ON whatsapp_status_events, terms_acceptances, ai_usage
  TO lucromei_test_runtime;

-- Subscription status follows the payment provider; deleted only with the account.
GRANT SELECT, INSERT, UPDATE, DELETE ON subscriptions TO lucromei_test_runtime;

-- Payment provider webhooks: stored once, deleted only with the account.
GRANT SELECT, INSERT, DELETE ON subscription_events TO lucromei_test_runtime;

-- The anonymous proof of an erasure is never deleted.
GRANT SELECT, INSERT, UPDATE ON deletion_requests TO lucromei_test_runtime;

GRANT USAGE, SELECT ON SEQUENCE inbound_events_id_seq TO lucromei_test_runtime;
