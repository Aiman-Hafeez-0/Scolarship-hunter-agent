-- 0007_login_failures.sql: the dashboard's login rate limit, kept in the database.
--
-- On Vercel every request may run on a different server instance, and instances share no
-- memory, so a counter kept in memory would not stop anyone. Instead each failed login is
-- one row here. At most 5 failed logins per 15 minutes per client address are allowed.
-- A successful login deletes that address's rows, and rows older than 15 minutes are
-- deleted whenever a new failure is recorded, so the table stays tiny.
create table login_failures (
    id         bigint generated always as identity primary key,
    client_key text not null,  -- the client's IP address
    failed_at  timestamptz not null default now()
);

create index login_failures_client_time_idx on login_failures (client_key, failed_at);

-- RLS on with no policies (like the other tables): hidden from Supabase's public Data API.
alter table login_failures enable row level security;
