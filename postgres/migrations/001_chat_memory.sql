
-- RESET (opcional): si querés recrearlas desde cero, descomentá este bloque.
drop table if exists public.chat_outbox_messages cascade;
drop table if exists public.chat_webhook_jobs cascade;
drop table if exists public.chat_processed_events cascade;
drop table if exists public.chat_messages cascade;
drop table if exists public.chat_conversations cascade;

create table if not exists public.chat_conversations (
  id bigserial primary key,
  channel text not null,
  external_conversation_id text not null,
  external_contact_id text,
  account_id text,
  state jsonb not null default '{}'::jsonb,
  locked_until timestamptz,
  last_seen_at timestamptz not null default now(),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (channel, external_conversation_id)
);

create index if not exists chat_conversations_last_seen_idx
  on public.chat_conversations (last_seen_at desc);

create table if not exists public.chat_messages (
  id bigserial primary key,
  conversation_id bigint not null references public.chat_conversations(id) on delete cascade,
  external_message_id text,
  role text not null check (role in ('user', 'assistant', 'system')),
  content text not null,
  raw_payload jsonb not null default '{}'::jsonb,
  tool_calls jsonb not null default '[]'::jsonb,
  processing_status text not null default 'processed',
  processed_at timestamptz,
  processing_error text,
  created_at timestamptz not null default now(),
  unique (conversation_id, external_message_id, role)
);

create index if not exists chat_messages_conversation_created_idx
  on public.chat_messages (conversation_id, created_at desc);
create index if not exists chat_messages_processing_idx
  on public.chat_messages (conversation_id, processing_status, created_at);

create table if not exists public.chat_processed_events (
  event_key text primary key,
  channel text not null,
  external_conversation_id text,
  external_message_id text,
  raw_payload jsonb not null default '{}'::jsonb,
  status text not null default 'received',
  error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists chat_processed_events_created_idx
  on public.chat_processed_events (created_at desc);

create table if not exists public.chat_webhook_jobs (
  id bigserial primary key,
  event_key text not null references public.chat_processed_events(event_key) on delete cascade,
  channel text not null,
  external_conversation_id text not null,
  external_message_id text,
  status text not null default 'queued',
  attempts integer not null default 0,
  max_attempts integer not null default 5,
  error text,
  raw_payload jsonb not null default '{}'::jsonb,
  run_at timestamptz not null default now(),
  locked_at timestamptz,
  worker_id text,
  created_at timestamptz not null default now(),
  started_at timestamptz,
  finished_at timestamptz,
  completed_at timestamptz
);

create unique index if not exists chat_webhook_jobs_event_key_idx
  on public.chat_webhook_jobs (event_key);
create index if not exists chat_webhook_jobs_status_created_idx
  on public.chat_webhook_jobs (status, created_at);
create index if not exists chat_webhook_jobs_status_run_at_idx
  on public.chat_webhook_jobs (status, run_at);
create index if not exists chat_webhook_jobs_locked_at_idx
  on public.chat_webhook_jobs (status, locked_at);

create table if not exists public.chat_outbox_messages (
  id bigserial primary key,
  conversation_id bigint not null references public.chat_conversations(id) on delete cascade,
  external_conversation_id text not null,
  channel text not null,
  content text not null,
  status text not null default 'pending',
  idempotency_key text not null,
  attempts integer not null default 0,
  max_attempts integer not null default 5,
  error text,
  raw_payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  sent_at timestamptz,
  unique (idempotency_key)
);

create index if not exists chat_outbox_messages_status_created_idx
  on public.chat_outbox_messages (status, created_at);

-- ── Triggers ──────────────────────────────────────────────────────────────
create or replace function public.touch_chat_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at := now();
  return new;
end;
$$;

drop trigger if exists touch_chat_conversations_updated_at on public.chat_conversations;
create trigger touch_chat_conversations_updated_at
before update on public.chat_conversations
for each row execute function public.touch_chat_updated_at();

drop trigger if exists touch_chat_processed_events_updated_at on public.chat_processed_events;
create trigger touch_chat_processed_events_updated_at
before update on public.chat_processed_events
for each row execute function public.touch_chat_updated_at();

-- ── Conversaciones + locks ─────────────────────────────────────────────────
create or replace function public.get_or_create_chat_conversation(
  p_channel text,
  p_external_conversation_id text,
  p_external_contact_id text default null,
  p_account_id text default null
)
returns public.chat_conversations
language plpgsql
security definer
set search_path = public
as $$
declare
  v_row public.chat_conversations;
begin
  insert into public.chat_conversations (
    channel, external_conversation_id, external_contact_id, account_id, last_seen_at
  )
  values (
    p_channel, p_external_conversation_id, p_external_contact_id, p_account_id, now()
  )
  on conflict (channel, external_conversation_id) do update set
    external_contact_id = coalesce(excluded.external_contact_id, public.chat_conversations.external_contact_id),
    account_id = coalesce(excluded.account_id, public.chat_conversations.account_id),
    last_seen_at = now()
  returning * into v_row;

  return v_row;
end;
$$;

create or replace function public.acquire_chat_conversation_lock(
  p_channel text,
  p_external_conversation_id text,
  p_lock_seconds integer default 60
)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
declare
  v_id bigint;
begin
  select id into v_id
  from public.chat_conversations
  where channel = p_channel
    and external_conversation_id = p_external_conversation_id
  for update;

  if v_id is null then
    return false;
  end if;

  update public.chat_conversations
  set locked_until = now() + make_interval(secs => greatest(p_lock_seconds, 1))
  where id = v_id
    and (locked_until is null or locked_until < now());

  return found;
end;
$$;

create or replace function public.release_chat_conversation_lock(
  p_channel text,
  p_external_conversation_id text
)
returns void
language sql
security definer
set search_path = public
as $$
  update public.chat_conversations
  set locked_until = null
  where channel = p_channel
    and external_conversation_id = p_external_conversation_id;
$$;

create or replace function public.cleanup_expired_chat_conversation_locks()
returns integer
language plpgsql
security definer
set search_path = public
as $$
declare
  v_count integer;
begin
  update public.chat_conversations
  set locked_until = null
  where locked_until is not null
    and locked_until < now();

  get diagnostics v_count = row_count;
  return v_count;
end;
$$;

-- ── Dedup de eventos ───────────────────────────────────────────────────────
create or replace function public.mark_chat_event_received(
  p_event_key text,
  p_channel text,
  p_external_conversation_id text,
  p_external_message_id text,
  p_raw_payload jsonb
)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
begin
  insert into public.chat_processed_events (
    event_key, channel, external_conversation_id, external_message_id, raw_payload, status
  )
  values (
    p_event_key, p_channel, p_external_conversation_id, p_external_message_id,
    coalesce(p_raw_payload, '{}'::jsonb), 'received'
  );

  return true;
exception when unique_violation then
  return false;
end;
$$;

create or replace function public.update_chat_event_status(
  p_event_key text,
  p_status text,
  p_error text default null
)
returns void
language sql
security definer
set search_path = public
as $$
  update public.chat_processed_events
  set status = p_status,
      error = p_error
  where event_key = p_event_key;
$$;

-- ── Cola de jobs de webhook ────────────────────────────────────────────────
create or replace function public.enqueue_chat_webhook_job(
  p_event_key text,
  p_channel text,
  p_external_conversation_id text,
  p_external_message_id text,
  p_raw_payload jsonb
)
returns bigint
language plpgsql
security definer
set search_path = public
as $$
declare
  v_id bigint;
begin
  insert into public.chat_webhook_jobs (
    event_key, channel, external_conversation_id, external_message_id, raw_payload, status
  )
  values (
    p_event_key, p_channel, p_external_conversation_id, p_external_message_id,
    coalesce(p_raw_payload, '{}'::jsonb), 'queued'
  )
  returning id into v_id;

  return v_id;
end;
$$;

create or replace function public.update_chat_webhook_job_status(
  p_job_id bigint,
  p_status text,
  p_error text default null
)
returns void
language sql
security definer
set search_path = public
as $$
  update public.chat_webhook_jobs
  set status = p_status,
      error = p_error,
      attempts = case when p_status = 'processing' then attempts + 1 else attempts end,
      started_at = case when p_status = 'processing' then now() else started_at end,
      finished_at = case when p_status in ('completed', 'failed', 'skipped') then now() else finished_at end
  where id = p_job_id;
$$;

create or replace function public.increment_chat_webhook_job_attempts_for_conversation(
  p_channel text,
  p_external_conversation_id text
)
returns void
language sql
security definer
set search_path = public
as $$
  update public.chat_webhook_jobs
  set attempts = attempts + 1
  where channel = p_channel
    and external_conversation_id = p_external_conversation_id
    and status = 'processing';
$$;

create or replace function public.requeue_stale_chat_webhook_jobs(
  p_stale_minutes integer default 15,
  p_limit integer default 100
)
returns bigint[]
language plpgsql
security definer
set search_path = public
as $$
declare
  v_conversation_ids bigint[];
begin
  with stale_jobs as (
    select j.id, c.id as conversation_id
    from public.chat_webhook_jobs j
    join public.chat_conversations c
      on c.channel = j.channel
     and c.external_conversation_id = j.external_conversation_id
    where j.status = 'processing'
      and coalesce(j.locked_at, j.started_at, j.created_at) < now() - make_interval(mins => greatest(p_stale_minutes, 1))
      and j.attempts < j.max_attempts
    order by coalesce(j.locked_at, j.started_at, j.created_at)
    limit greatest(p_limit, 1)
  ),
  updated as (
    update public.chat_webhook_jobs j
    set status = 'retry',
        run_at = now(),
        locked_at = null,
        worker_id = null,
        error = null
    from stale_jobs s
    where j.id = s.id
    returning s.conversation_id
  )
  select coalesce(array_agg(distinct conversation_id), '{}'::bigint[])
  into v_conversation_ids
  from updated;

  return v_conversation_ids;
end;
$$;

create or replace function public.due_chat_webhook_job_conversations(p_limit integer default 100)
returns bigint[]
language sql
security definer
set search_path = public
as $$
  select coalesce(array_agg(distinct conversation_id), '{}'::bigint[])
  from (
    select c.id as conversation_id
    from public.chat_webhook_jobs j
    join public.chat_conversations c
      on c.channel = j.channel
     and c.external_conversation_id = j.external_conversation_id
    where j.status in ('queued', 'retry')
      and j.run_at <= now()
      and j.attempts < j.max_attempts
    order by j.run_at, j.created_at
    limit greatest(p_limit, 1)
  ) due;
$$;

-- ── Outbox ─────────────────────────────────────────────────────────────────
create or replace function public.increment_chat_outbox_attempts(p_outbox_id bigint)
returns void
language sql
security definer
set search_path = public
as $$
  update public.chat_outbox_messages
  set attempts = attempts + 1
  where id = p_outbox_id;
$$;
