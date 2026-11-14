-- 0001_init.sql: first version of the Scholarship Hunter database.
--
-- Conventions:
--   * Every primary key is a uuid made by gen_random_uuid() (built into Postgres 13+).
--   * Every time column is timestamptz (stored in UTC, never ambiguous).
--   * Small fixed word lists use either an enum type or a text column with a CHECK.

-- ---------------------------------------------------------------------------
-- Enum types
-- ---------------------------------------------------------------------------

create type source_type as enum ('official', 'lead');
create type funding_type as enum ('full', 'partial', 'tuition_only', 'unknown');
create type sch_status as enum ('open', 'upcoming', 'closed', 'unknown');
create type match_status as enum ('eligible', 'maybe', 'not_eligible');
create type run_status as enum ('running', 'success', 'partial', 'failed');

-- ---------------------------------------------------------------------------
-- profiles: the person (or people) we are hunting scholarships for
-- ---------------------------------------------------------------------------

create table profiles (
    id                      uuid primary key default gen_random_uuid(),
    full_name               text not null,
    email                   text not null unique,
    whatsapp_number         text,
    nationality             text not null default 'PK',
    date_of_birth           date,
    degree                  text,
    cgpa                    numeric(4, 2),
    cgpa_scale              numeric(4, 2) not null default 4.00,
    english_test            text,
    english_score           text,
    moi_letter              boolean not null default false,
    work_experience_months  integer not null default 0 check (work_experience_months >= 0),
    target_fields           text[] not null default '{}',
    target_countries        text[] not null default '{}',
    target_intake           text,
    max_application_fee_eur numeric(10, 2) check (max_application_fee_eur >= 0), -- null = no cap
    prefer_no_fee           boolean not null default true,
    fully_funded_only       boolean not null default true,
    reminder_days           integer[] not null default '{30,14,7,2}',
    emails_paused           boolean not null default false,
    created_at              timestamptz not null default now(),
    check (cgpa is null or (cgpa >= 0 and cgpa <= cgpa_scale))
);

-- ---------------------------------------------------------------------------
-- sources: the web pages we check
-- ---------------------------------------------------------------------------

create table sources (
    id                   uuid primary key default gen_random_uuid(),
    key                  text not null unique,
    url                  text not null unique,
    name                 text not null,
    program_key          text,
    country              text,
    type                 source_type not null default 'official',
    opportunity_type     text not null
                         check (opportunity_type in ('scholarship', 'admission', 'entry_test')),
    location             text not null check (location in ('abroad', 'pakistan')),
    render_js            boolean not null default false,
    is_pdf               boolean not null default false,
    active               boolean not null default true,
    note                 text,
    robots_allowed       boolean,
    robots_checked_at    timestamptz,
    last_hash            text,
    last_fetched_at      timestamptz,
    last_http_status     integer,
    last_error           text,
    consecutive_failures integer not null default 0 check (consecutive_failures >= 0),
    created_at           timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- snapshots: one row per successful fetch of a source
-- ---------------------------------------------------------------------------

create table snapshots (
    id           uuid primary key default gen_random_uuid(),
    source_id    uuid not null references sources (id) on delete cascade,
    fetched_at   timestamptz not null default now(),
    http_status  integer,
    content_type text,
    content_hash text not null,
    text_gz      bytea, -- gzip of the cleaned page text
    char_count   integer check (char_count >= 0)
);

create index snapshots_source_fetched_idx on snapshots (source_id, fetched_at desc);

-- ---------------------------------------------------------------------------
-- scholarships: one opportunity extracted from a source by the LLM
-- ---------------------------------------------------------------------------

create table scholarships (
    id                  uuid primary key default gen_random_uuid(),
    source_id           uuid not null references sources (id) on delete cascade,
    snapshot_id         uuid references snapshots (id) on delete set null,
    name                text not null,
    provider            text,
    opportunity_type    text not null
                        check (opportunity_type in ('scholarship', 'admission', 'entry_test')),
    location            text not null check (location in ('abroad', 'pakistan')),
    campus              text,
    intake              text,
    entry_tests         text[] not null default '{}',
    moi_accepted        text not null default 'unclear'
                        check (moi_accepted in ('yes', 'no', 'unclear')),
    country             text,
    degree_levels       text[] not null default '{}',
    fields_of_study     text[] not null default '{}',
    funding             funding_type not null default 'unknown',
    coverage            jsonb not null default '{}',
    application_fee_eur numeric(10, 2) check (application_fee_eur >= 0),
    open_to_pakistan    text not null default 'unclear'
                        check (open_to_pakistan in ('yes', 'no', 'unclear')),
    application_url     text,
    status              sch_status not null default 'unknown',
    needs_review        boolean not null default false,
    verified_at         timestamptz,
    extraction_version  text,
    extracted_at        timestamptz not null default now(),
    unique (source_id, name)
);

-- ---------------------------------------------------------------------------
-- evidence: the exact quote from the page that backs each fact
-- ---------------------------------------------------------------------------

create table evidence (
    id             uuid primary key default gen_random_uuid(),
    scholarship_id uuid not null references scholarships (id) on delete cascade,
    field_path     text not null, -- which fact this proves, e.g. 'deadlines[0].deadline_at'
    quote          text not null check (length(quote) > 0),
    match_score    integer check (match_score between 0 and 100), -- how well quote matched page
    snapshot_id    uuid references snapshots (id) on delete set null
);

create index evidence_scholarship_idx on evidence (scholarship_id);

-- ---------------------------------------------------------------------------
-- deadlines, requirements, required_documents: details of a scholarship
-- ---------------------------------------------------------------------------

create table deadlines (
    id             uuid primary key default gen_random_uuid(),
    scholarship_id uuid not null references scholarships (id) on delete cascade,
    label          text not null,
    deadline_at    timestamptz not null,
    timezone_note  text,
    is_estimated   boolean not null default false,
    evidence_id    uuid references evidence (id) on delete set null
);

create index deadlines_scholarship_idx on deadlines (scholarship_id);
create index deadlines_deadline_at_idx on deadlines (deadline_at);

create table requirements (
    id             uuid primary key default gen_random_uuid(),
    scholarship_id uuid not null references scholarships (id) on delete cascade,
    kind           text not null, -- e.g. 'cgpa', 'english', 'age', 'work_experience'
    value          text,
    description    text,
    evidence_id    uuid references evidence (id) on delete set null
);

create index requirements_scholarship_idx on requirements (scholarship_id);

create table required_documents (
    id             uuid primary key default gen_random_uuid(),
    scholarship_id uuid not null references scholarships (id) on delete cascade,
    name           text not null,
    evidence_id    uuid references evidence (id) on delete set null
);

create index required_documents_scholarship_idx on required_documents (scholarship_id);

-- ---------------------------------------------------------------------------
-- matches: how well a scholarship fits a profile
-- ---------------------------------------------------------------------------

create table matches (
    profile_id     uuid not null references profiles (id) on delete cascade,
    scholarship_id uuid not null references scholarships (id) on delete cascade,
    status         match_status not null,
    score          integer not null check (score between 0 and 100),
    reasons        jsonb not null default '[]',
    computed_at    timestamptz not null default now(),
    primary key (profile_id, scholarship_id)
);

-- ---------------------------------------------------------------------------
-- changes: what changed in a scholarship since the last check
-- ---------------------------------------------------------------------------

create table changes (
    id             uuid primary key default gen_random_uuid(),
    scholarship_id uuid not null references scholarships (id) on delete cascade,
    field_path     text not null,
    old_value      jsonb,
    new_value      jsonb,
    detected_at    timestamptz not null default now(),
    notified       boolean not null default false
);

create index changes_scholarship_idx on changes (scholarship_id);
create index changes_not_notified_idx on changes (detected_at) where not notified;

-- ---------------------------------------------------------------------------
-- notifications: every email (or other message) we send
-- ---------------------------------------------------------------------------

create table notifications (
    id                  uuid primary key default gen_random_uuid(),
    profile_id          uuid not null references profiles (id) on delete cascade,
    scholarship_id      uuid references scholarships (id) on delete set null,
    deadline_id         uuid references deadlines (id) on delete set null,
    kind                text not null, -- e.g. 'digest', 'reminder_14d', 'change'
    channel             text not null default 'email',
    sent_at             timestamptz not null default now(),
    status              text not null default 'sent', -- e.g. 'sent', 'failed'
    provider_message_id text
);

-- A reminder for the same person, deadline and kind can never be sent twice.
create unique index notifications_one_reminder_idx
    on notifications (profile_id, deadline_id, kind)
    where deadline_id is not null;

-- ---------------------------------------------------------------------------
-- runs: one row per pipeline run, for monitoring
-- ---------------------------------------------------------------------------

create table runs (
    id              uuid primary key default gen_random_uuid(),
    kind            text not null, -- e.g. 'weekly', 'daily_reminders'
    started_at      timestamptz not null default now(),
    finished_at     timestamptz,
    status          run_status not null default 'running',
    sources_total   integer not null default 0,
    sources_changed integer not null default 0,
    sources_failed  integer not null default 0,
    llm_tokens_in   integer not null default 0,
    llm_tokens_out  integer not null default 0,
    error           text
);

-- ---------------------------------------------------------------------------
-- Row level security (RLS)
--
-- Supabase publishes every table in the public schema through its Data API
-- (a REST/GraphQL endpoint anyone with the project's anon key can call).
-- Turning RLS on WITHOUT adding any policies means "nobody is allowed any row"
-- for those API roles (anon, authenticated), so the API sees nothing.
-- Our pipeline connects directly to Postgres as the table owner, and owners
-- bypass RLS, so the pipeline keeps full access.
-- If we ever want API access, add explicit policies in a later migration.
-- ---------------------------------------------------------------------------

alter table profiles           enable row level security;
alter table sources            enable row level security;
alter table snapshots          enable row level security;
alter table scholarships       enable row level security;
alter table evidence           enable row level security;
alter table deadlines          enable row level security;
alter table requirements       enable row level security;
alter table required_documents enable row level security;
alter table matches            enable row level security;
alter table changes            enable row level security;
alter table notifications      enable row level security;
alter table runs               enable row level security;
