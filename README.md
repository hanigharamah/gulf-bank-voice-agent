# Gulf Bank Voice Agent — Supabase + Vercel variant

A serverless-hosted variant of the mock Gulf-bank voice-agent backend, built
so the whole demo (backend + frontend) can be deployed to a public URL
instead of running only on a laptop. Same webhook contract, same security
design, same demo accounts as the original Flask + local-JSON version — only
the storage and live-panel mechanism changed, to fit Vercel's stateless
serverless functions.

## What changed vs. the original backend, and why

| Original (Flask + JSON, laptop-only) | This variant (Supabase + Vercel) | Why |
|---|---|---|
| `bank_data.json` accounts/transactions | Postgres `accounts` / `transactions` tables | Serverless functions have no persistent filesystem |
| In-memory `verified_sessions` dict + TTL | `verified_sessions` table, TTL as a `WHERE verified_at >= cutoff` filter | Serverless functions don't share memory between invocations — the **session-bound verification design is unchanged**, just persisted instead of process-local |
| In-memory + JSON idempotency record | `processed_tool_calls` table | Same reason as above |
| Hand-rolled SSE `/events` stream | Supabase Realtime on a `tool_call_events` table | Serverless functions can't hold a long-lived connection open; Realtime is Supabase's Postgres-backed pub/sub |

The endpoint routes, request/response shapes, security gating, and money
handling (integer halalas, never floats) are otherwise identical to the
original — see the original project's `README.md` and `TOOL_DEFINITIONS.md`
for the full contract.

## One-time setup (do this before deploying)

1. **Run the schema** in the Supabase SQL editor, in order:
   - `schema.sql` — accounts, transactions, verified_sessions, processed_tool_calls (RLS deny-by-default)
   - `schema_realtime.sql` — tool_call_events (public read policy + Realtime publication)
2. **Copy `.env.example` to `.env`** and fill in your Supabase project URL,
   anon key, service role key, and a `WEBHOOK_SECRET`. Never commit `.env`.

## Deploy to Vercel

This repo relies on Vercel's zero-config auto-detection — no `vercel.json`
routing needed at all:
- `api/bank.py` — the Flask backend, deployed as a single Vercel Function.
  It's named `bank.py` (not `index.py`) to avoid colliding with
  `public/index.html`; `pyproject.toml`'s `[tool.vercel] entrypoint` tells
  Vercel exactly where to find the Flask `app` object since the filename
  isn't one of Vercel's auto-detected defaults.
- `public/index.html` — the static frontend, served automatically at `/`.
- Any request that doesn't match a static file in `public/**` (e.g.
  `/tools/verify-identity`, `/health`) falls through to the Flask function,
  whose own `@app.route` decorators handle it — no manual rewrites required.

Steps:
1. Push this repo to GitHub (already done if you're reading this from the repo).
2. In the [Vercel dashboard](https://vercel.com), **Add New → Project → Import** this GitHub repo.
3. In the project's **Settings → Environment Variables**, add:
   `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `WEBHOOK_SECRET` (and
   `VERIFICATION_TTL_SECONDS` if you want a non-default TTL). These are the
   same values from your local `.env` — set them directly in Vercel's UI, not
   in any committed file.
4. Deploy. Vercel gives you a stable `https://<project>.vercel.app` URL.
5. **Update the frontend's Supabase config** in `index.html` if you used a
   different Supabase project than the one already filled in
   (`SUPABASE_URL` / `SUPABASE_ANON_KEY` constants near the top of the
   `<script type="module">` block — these are safe to be public, see below).
6. Update the five webhook tools in the ElevenLabs dashboard to point at
   `https://<project>.vercel.app/tools/...` instead of a tunnel URL. Unlike a
   cloudflared quick tunnel, this URL never changes on restart.

## Security notes specific to this variant

- **`SUPABASE_SERVICE_ROLE_KEY`** bypasses Row Level Security entirely and is
  used **only** server-side, inside `api/bank.py`, sourced from Vercel's
  environment variables. It is never sent to the browser.
- **`SUPABASE_ANON_KEY`** (the "publishable" key) is safe to be public and
  lives directly in `index.html` — but it can only read the
  `tool_call_events` table (see `schema_realtime.sql`'s explicit `select`
  policy). It has zero access to `accounts`, `transactions`,
  `verified_sessions`, or `processed_tool_calls` — those four tables have RLS
  enabled with **no** policies at all, so only the service-role key can touch
  them. This is the same "don't trust the client" principle as the
  session-bound verification design, applied one layer deeper at the
  database itself.

## Local testing before deploying

```bash
pip install -r requirements.txt
cp .env.example .env      # fill in real values
python api/bank.py       # NOTE: add app.run() locally, or use `flask run`
```

(Vercel calls the `app` WSGI object directly and never executes `app.run()`
in production — that line is intentionally omitted from `api/bank.py`.)
