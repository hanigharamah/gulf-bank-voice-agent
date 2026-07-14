-- ============================================================================
-- Realtime event log for the live tool-call panel.
--
-- Run this in the Supabase SQL Editor AFTER schema.sql (Project → SQL Editor
-- → New query → paste → Run). Adds one new table used only for the demo's
-- live activity panel — it holds no banking data, so unlike accounts /
-- transactions / verified_sessions / processed_tool_calls it is given a
-- public read policy on purpose: this is the "make the invisible plumbing
-- visible" feature, so it's meant to be readable by anyone watching the demo
-- page. Only the backend (service_role key, which bypasses RLS) can write to
-- it — the public/anon key can read, never write.
-- ============================================================================

create table tool_call_events (
  call_id     text primary key,
  tool_name   text not null,
  status      text not null check (status in ('received', 'completed', 'error')),
  parameters  jsonb not null,
  result      jsonb,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

alter table tool_call_events enable row level security;

-- Public/anon key may SELECT (needed for the browser to receive Realtime
-- postgres_changes payloads) but has no insert/update/delete policy at all —
-- only the service_role-authenticated backend can write.
create policy "anyone can read tool call events"
  on tool_call_events for select
  to anon
  using (true);

-- Register the table with Supabase Realtime so postgres_changes events
-- (INSERT on "received", UPDATE on "completed"/"error") are broadcast to
-- subscribed clients.
alter publication supabase_realtime add table tool_call_events;
