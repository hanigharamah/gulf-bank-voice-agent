-- ============================================================================
-- Gulf Bank Voice Agent — Supabase schema
--
-- Run this once in the Supabase dashboard: Project → SQL Editor → New query
-- → paste this whole file → Run.
--
-- Replaces, from the original Flask + JSON backend:
--   bank_data.json "accounts"              -> accounts + transactions tables
--   bank_data.json "processed_tool_calls"  -> processed_tool_calls table
--   in-memory verified_sessions dict       -> verified_sessions table
--     (the TTL check that used to be `time.time() - verified_at > TTL` in
--      Python becomes a `verified_at > now() - interval` filter in the query
--      — same logic, same session-bound-verification design, just backed by
--      a table instead of a process-local dict so it survives across
--      stateless serverless invocations.)
-- ============================================================================

-- ---------------------------------------------------------------------------
-- Accounts — the mock core-banking record.
-- ---------------------------------------------------------------------------
create table accounts (
  account_id           text primary key,
  holder_name          text not null,
  balance_halalas      bigint not null,          -- integer money, never float
  card_status          text not null check (card_status in ('active', 'frozen')),
  verification_last_four text not null
);

-- ---------------------------------------------------------------------------
-- Transactions — recent activity per account.
-- ---------------------------------------------------------------------------
create table transactions (
  id               bigint generated always as identity primary key,
  account_id       text not null references accounts(account_id) on delete cascade,
  occurred_on      date not null,
  merchant         text not null,
  amount_halalas   bigint not null,
  country          text not null,
  flagged          boolean not null default false   -- demo cue, not a fraud model
);
create index transactions_account_recent_idx on transactions (account_id, occurred_on desc);

-- ---------------------------------------------------------------------------
-- Verified sessions — THE key security table.
--
-- Written once when verify_customer_identity succeeds, keyed on the
-- conversation_id the ElevenLabs platform injects (never something the LLM
-- invents). Every sensitive endpoint queries this table for the CURRENT
-- conversation_id and CURRENT account_id before acting — the same
-- "don't trust the LLM, bind verification to the session" design as the
-- original in-memory version, just persisted so it works across stateless
-- serverless function invocations.
-- ---------------------------------------------------------------------------
create table verified_sessions (
  conversation_id  text primary key,
  account_id       text not null references accounts(account_id),
  verified_at      timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- Idempotency store — one row per tool_call_id already processed.
-- A retried webhook call with the same key returns this stored result
-- instead of acting again (e.g. freezing a card twice).
-- ---------------------------------------------------------------------------
create table processed_tool_calls (
  idempotency_key  text primary key,
  tool_name        text not null,
  result           jsonb not null,
  processed_at     timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- Row Level Security — deny by default for every table.
--
-- The backend only ever talks to Supabase using the SERVICE ROLE key, which
-- bypasses RLS entirely — that's the trusted server-side path. Enabling RLS
-- with no policies means the public/anon key (the one safe to expose to a
-- browser) can read or write NOTHING in these tables. This mirrors the same
-- "don't trust the client" principle as the session-bound verification
-- design, applied one layer deeper at the database itself.
-- ---------------------------------------------------------------------------
alter table accounts enable row level security;
alter table transactions enable row level security;
alter table verified_sessions enable row level security;
alter table processed_tool_calls enable row level security;
-- No policies added for anon/authenticated roles — default deny.

-- ============================================================================
-- Seed data — identical to the original bank_data.seed.json, so the demo
-- script and runbook narrative (accounts 1001 / 1002 / 1003) are unchanged.
-- ============================================================================

insert into accounts (account_id, holder_name, balance_halalas, card_status, verification_last_four) values
  ('1001', 'Ahmed Al-Rashid',  1254375, 'active', '4821'),
  ('1002', 'Noura Al-Qahtani',  487220, 'frozen', '7359'),
  ('1003', 'Khalid Al-Otaibi', 2093140, 'active', '2648');

insert into transactions (account_id, occurred_on, merchant, amount_halalas, country, flagged) values
  -- Account 1001 — active, healthy (balance-check / transactions demo path)
  ('1001', '2026-07-12', 'Tamimi Markets, Riyadh',              -28450, 'SA', false),
  ('1001', '2026-07-11', 'Careem',                                -3200, 'SA', false),
  ('1001', '2026-07-09', 'Jarir Bookstore, Riyadh',              -41900, 'SA', false),
  ('1001', '2026-07-07', 'STC Pay Top-up',                       -11500, 'SA', false),
  ('1001', '2026-07-05', 'Salary - Al Faisal Trading Co.',       950000, 'SA', false),
  ('1001', '2026-07-03', 'Nahdi Pharmacy, Riyadh',                -8675, 'SA', false),

  -- Account 1002 — already frozen (idempotent "already frozen" demo path)
  ('1002', '2026-07-10', 'Danube, Jeddah',                       -35780, 'SA', false),
  ('1002', '2026-07-08', 'Careem',                                -2750, 'SA', false),
  ('1002', '2026-07-06', 'STC Monthly Bill',                     -19900, 'SA', false),
  ('1002', '2026-07-04', 'Nahdi Pharmacy, Jeddah',                -12340, 'SA', false),
  ('1002', '2026-07-01', 'Tamimi Markets, Jeddah',                -22115, 'SA', false),

  -- Account 1003 — active, with the flagged suspicious charge (narrative peak)
  ('1003', '2026-07-13', 'TECHNO GADGETS LTD, London',           -485000, 'GB', true),
  ('1003', '2026-07-12', 'Jarir Bookstore, Dammam',               -15975, 'SA', false),
  ('1003', '2026-07-10', 'Danube, Dammam',                        -31260, 'SA', false),
  ('1003', '2026-07-08', 'Careem',                                 -4100, 'SA', false),
  ('1003', '2026-07-06', 'STC Monthly Bill',                      -24900, 'SA', false),
  ('1003', '2026-07-02', 'Tamimi Markets, Dammam',                -18830, 'SA', false);
