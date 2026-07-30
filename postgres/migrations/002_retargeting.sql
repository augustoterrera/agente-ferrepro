-- Retargeting one-shot: seguimiento a leads que se colgaron a mitad de charla.
--
-- No hay Postgres directo (Supabase self-hosted, solo PostgREST) → la selección de candidatos
-- vive acá como función security definer y se invoca como RPC. El resto de la lógica (horario
-- comercial, reglas de exclusión, redacción del mensaje) está en app/retargeting.py.
--
-- Idempotente: se puede correr varias veces.

-- El lateral de "último mensaje del cliente" filtra por role → índice con role adentro.
create index if not exists chat_messages_conversation_role_created_idx
  on public.chat_messages (conversation_id, role, created_at desc);
create index if not exists chat_outbox_messages_conversation_created_idx
  on public.chat_outbox_messages (conversation_id, created_at desc, id desc);

-- Lease del outbox: permite recuperar un `processing` si el worker murió después de reclamarlo.
alter table public.chat_outbox_messages
  add column if not exists processing_at timestamptz;


-- ── Intake atómico ─────────────────────────────────────────────────────────
-- El retargeting serializa sobre la fila de chat_conversations. El webhook tiene que hacer lo
-- mismo: si conversación/evento/mensaje/job fueran cuatro requests REST separados, podría quedar
-- un mensaje entrante "en vuelo" justo mientras se confirma un follow-up.
create or replace function public.chat_persist_incoming_event(
  p_event_key text,
  p_channel text,
  p_external_conversation_id text,
  p_external_contact_id text,
  p_account_id text,
  p_external_message_id text,
  p_content text,
  p_raw_payload jsonb,
  p_max_attempts integer default 5
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_conversation public.chat_conversations;
  v_is_new boolean;
  v_message_id bigint;
  v_job_id bigint;
begin
  -- El upsert toma el row lock primero y lo conserva hasta que mensaje + job estén persistidos.
  insert into public.chat_conversations (
    channel, external_conversation_id, external_contact_id, account_id, last_seen_at
  )
  values (
    p_channel, p_external_conversation_id, p_external_contact_id, p_account_id, clock_timestamp()
  )
  on conflict (channel, external_conversation_id) do update set
    external_contact_id = coalesce(excluded.external_contact_id, public.chat_conversations.external_contact_id),
    account_id = coalesce(excluded.account_id, public.chat_conversations.account_id),
    last_seen_at = clock_timestamp()
  returning * into v_conversation;

  insert into public.chat_processed_events (
    event_key, channel, external_conversation_id, external_message_id, raw_payload, status
  )
  values (
    p_event_key, p_channel, p_external_conversation_id, p_external_message_id,
    coalesce(p_raw_payload, '{}'::jsonb), 'received'
  )
  on conflict (event_key) do nothing
  returning true into v_is_new;

  if not coalesce(v_is_new, false) then
    return jsonb_build_object(
      'is_new', false,
      'conversation', to_jsonb(v_conversation),
      'job_id', null
    );
  end if;

  insert into public.chat_messages (
    conversation_id, external_message_id, role, content, raw_payload, processing_status, created_at
  )
  values (
    v_conversation.id, p_external_message_id, 'user', coalesce(p_content, ''),
    coalesce(p_raw_payload, '{}'::jsonb), 'pending', clock_timestamp()
  )
  on conflict (conversation_id, external_message_id, role) do nothing
  returning id into v_message_id;

  -- Dos deliveries distintos del mismo message_id no crean dos turnos ni dos jobs.
  if v_message_id is null then
    update public.chat_processed_events
    set status = 'completed'
    where event_key = p_event_key;
    return jsonb_build_object(
      'is_new', false,
      'conversation', to_jsonb(v_conversation),
      'job_id', null
    );
  end if;

  insert into public.chat_webhook_jobs (
    event_key, channel, external_conversation_id, external_message_id, status,
    attempts, max_attempts, raw_payload, run_at
  )
  values (
    p_event_key, p_channel, p_external_conversation_id, p_external_message_id, 'queued',
    0, greatest(p_max_attempts, 1), coalesce(p_raw_payload, '{}'::jsonb), now()
  )
  returning id into v_job_id;

  return jsonb_build_object(
    'is_new', true,
    'conversation', to_jsonb(v_conversation),
    'job_id', v_job_id
  );
end
$$;


-- ── Estado de la conversación (merge shallow) ───────────────────────────────
create or replace function public.chat_merge_conversation_state(
  p_conversation_id bigint,
  p_patch jsonb
)
returns void
language sql
security definer
set search_path = public
as $$
  update public.chat_conversations
  set state = coalesce(state, '{}'::jsonb) || coalesce(p_patch, '{}'::jsonb)
  where id = p_conversation_id;
$$;


-- ── Candidatos ─────────────────────────────────────────────────────────────
-- Firma vieja (tenía p_silence_hours / p_window_closing_hours): el disparador pasó a ser el
-- cierre de la ventana, no las horas de silencio. Drop para que create or replace no overloadee.
drop function if exists public.chat_retargeting_candidates(
  text, double precision, double precision, double precision, double precision, text, integer
);

create or replace function public.chat_retargeting_candidates(
  p_channel text,
  p_window_hours double precision default 24,
  p_min_silence_hours double precision default 2,
  p_state_key text default 'retargeting',
  p_limit integer default 25
)
returns table (
  conversation_id bigint,
  channel text,
  external_conversation_id text,
  external_contact_id text,
  account_id text,
  state jsonb,
  last_user_at timestamptz,
  last_assistant_at timestamptz,
  window_closes_at timestamptz
)
language sql
security definer
set search_path = public
as $$
  select
    c.id,
    c.channel,
    c.external_conversation_id,
    c.external_contact_id,
    c.account_id,
    c.state,
    last_user.at,
    last_msg.created_at,
    last_user.at + make_interval(secs => p_window_hours * 3600)
  from public.chat_conversations c
  join lateral (
    -- Último mensaje de la charla: si es del cliente, el bot le debe respuesta (eso es un
    -- job pendiente o un bug, no un candidato a retargeting).
    select m.role, m.content, m.created_at
    from public.chat_messages m
    where m.conversation_id = c.id
      and m.role in ('user', 'assistant')
    order by m.created_at desc, m.id desc
    limit 1
  ) last_msg on true
  join lateral (
    select max(m.created_at) as at
    from public.chat_messages m
    where m.conversation_id = c.id
      and m.role = 'user'
  ) last_user on true
  join lateral (
    -- El flujo normal guarda la respuesta antes del POST a Chatwoot. No perseguimos a alguien
    -- por una respuesta que quedó en retry/failed y que, por lo tanto, nunca vio.
    select o.status, o.content
    from public.chat_outbox_messages o
    where o.conversation_id = c.id
    order by o.created_at desc, o.id desc
    limit 1
  ) last_outbox on true
  where c.channel = p_channel
    and last_msg.role = 'assistant'
    and last_outbox.status = 'sent'
    and last_outbox.content = last_msg.content
    and last_user.at is not null
    -- Ventana de texto libre de WhatsApp Cloud API (24h desde el último mensaje del cliente):
    -- fuera de eso Meta rechaza el mensaje y solo pasaría una plantilla aprobada.
    and last_user.at > now() - make_interval(secs => p_window_hours * 3600)
    -- Piso: nunca escribimos a alguien recién atendido.
    and last_msg.created_at <= now() - make_interval(secs => p_min_silence_hours * 3600)
    -- One-shot: una conversación evaluada (enviada o descartada) no se vuelve a tocar.
    and (c.state -> p_state_key) is null
  -- Por ventana más próxima a cerrar: el recontacto se manda lo más tarde posible (lo decide
  -- momento_de_enviar() en app/retargeting.py), así que los urgentes van primero y el batch
  -- limit nunca los deja afuera.
  order by last_user.at asc
  limit greatest(p_limit, 1);
$$;


-- ── Commit del follow-up (atómico) ─────────────────────────────────────────
-- Entre que el sweep lee el historial y encola el mensaje pasan segundos (Chatwoot + LLM). Si el
-- cliente escribe en el medio, el follow-up pisaría su mensaje. Revalidar desde Python dejaba un
-- hueco entre el chequeo y el insert, así que validación + outbox + estado pasan acá.
--
-- Devuelve {status: creado|ya_existia|retomada, outbox_id}.
create or replace function public.chat_retargeting_commit(
  p_conversation_id bigint,
  p_external_conversation_id text,
  p_channel text,
  p_content text,
  p_idempotency_key text,
  p_state_key text,
  p_motivo text,
  p_last_assistant_at timestamptz
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_outbox_id bigint;
  v_creado boolean := false;
  v_intacta boolean;
  -- Lo que se va a ENTREGAR. En un reintento el outbox ya existe con el texto original, y el
  -- redactor pudo generar otro distinto: el estado siempre conserva el original.
  v_content text := p_content;
begin
  -- Serializa contra otro sweep y contra chat_persist_incoming_event.
  perform 1 from public.chat_conversations where id = p_conversation_id for update;

  -- Un retry tardío no vuelve a evaluar una entrega ya creada: el outbox es la fuente de
  -- idempotencia y puede haberse enviado después de que el sweep leyó el candidato.
  select id, content into v_outbox_id, v_content
  from public.chat_outbox_messages
  where idempotency_key = p_idempotency_key;

  if v_outbox_id is not null then
    return jsonb_build_object('status', 'ya_existia', 'outbox_id', v_outbox_id);
  end if;

  select exists (
    select 1
    from (
      select m.role, m.created_at
      from public.chat_messages m
      where m.conversation_id = p_conversation_id
        and m.role in ('user', 'assistant')
      order by m.created_at desc, m.id desc
      limit 1
    ) ultimo
    where ultimo.role = 'assistant'
      and ultimo.created_at = p_last_assistant_at
  ) into v_intacta;

  if not v_intacta then
    return jsonb_build_object('status', 'retomada');
  end if;

  insert into public.chat_outbox_messages (
    conversation_id, external_conversation_id, channel, content, status, idempotency_key, created_at
  )
  values (
    p_conversation_id, p_external_conversation_id, p_channel, p_content, 'pending',
    p_idempotency_key, clock_timestamp()
  )
  on conflict (idempotency_key) do nothing
  returning id into v_outbox_id;

  if v_outbox_id is not null then
    v_creado := true;
  else
    -- No insertó por una de dos razones: ya existía (reintento) o la conversación se retomó.
    select id, content into v_outbox_id, v_content
    from public.chat_outbox_messages
    where idempotency_key = p_idempotency_key;
    if v_outbox_id is null then
      return jsonb_build_object('status', 'retomada');
    end if;
  end if;

  update public.chat_conversations
  set state = coalesce(state, '{}'::jsonb) || jsonb_build_object(
        p_state_key,
        jsonb_build_object(
          'decision', 'pendiente',
          'motivo', left(coalesce(p_motivo, ''), 300),
          'mensaje', v_content,
          'outbox_id', v_outbox_id,
          'queued_at', clock_timestamp()
        )
      )
  where id = p_conversation_id
    and (state -> p_state_key) is null;

  return jsonb_build_object(
    'status', case when v_creado then 'creado' else 'ya_existia' end,
    'outbox_id', v_outbox_id
  );
end
$$;


-- ── Outbox durable ─────────────────────────────────────────────────────────
create or replace function public.chat_claim_outbox(
  p_outbox_id bigint,
  p_stale_minutes integer default 15
)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
begin
  update public.chat_outbox_messages
  set status = 'processing',
      processing_at = clock_timestamp()
  where id = p_outbox_id
    and (
      status in ('pending', 'retry')
      or (
        status = 'processing'
        and coalesce(processing_at, created_at) < now() - make_interval(mins => greatest(p_stale_minutes, 1))
      )
    );
  return found;
end
$$;


create or replace function public.chat_due_outbox_messages(
  p_channel text,
  p_stale_minutes integer default 15,
  p_limit integer default 100
)
returns setof public.chat_outbox_messages
language sql
security definer
set search_path = public
as $$
  select o.*
  from public.chat_outbox_messages o
  where o.channel = p_channel
    and (
      o.status in ('pending', 'retry')
      or (
        o.status = 'processing'
        and coalesce(o.processing_at, o.created_at) < now() - make_interval(mins => greatest(p_stale_minutes, 1))
      )
    )
  order by o.created_at
  limit greatest(p_limit, 1);
$$;


create or replace function public.chat_cancel_retargeting_if_resumed(p_outbox_id bigint)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
declare
  v_outbox public.chat_outbox_messages;
begin
  -- Mismo orden de locks que chat_retargeting_commit: conversación → outbox.
  select * into v_outbox
  from public.chat_outbox_messages
  where id = p_outbox_id;

  if v_outbox.id is null
     or v_outbox.idempotency_key not like 'retargeting:%'
     or v_outbox.status <> 'processing' then
    return false;
  end if;

  perform 1
  from public.chat_conversations
  where id = v_outbox.conversation_id
  for update;

  select * into v_outbox
  from public.chat_outbox_messages
  where id = p_outbox_id
  for update;

  if v_outbox.status <> 'processing' then
    return false;
  end if;

  if not exists (
    select 1
    from public.chat_messages m
    where m.conversation_id = v_outbox.conversation_id
      and m.role = 'user'
      and m.created_at > v_outbox.created_at
  ) then
    return false;
  end if;

  update public.chat_outbox_messages
  set status = 'cancelled',
      processing_at = null,
      error = 'El cliente retomó la conversación antes del envío'
  where id = p_outbox_id;

  update public.chat_conversations
  set state = coalesce(state, '{}'::jsonb) || jsonb_build_object(
        'retargeting',
        coalesce(state -> 'retargeting', '{}'::jsonb) || jsonb_build_object(
          'decision', 'cancelado',
          'cancelled_at', now()
        )
      )
  where id = v_outbox.conversation_id;

  return true;
end
$$;


create or replace function public.chat_mark_outbox_sent(
  p_outbox_id bigint,
  p_raw_payload jsonb default '{}'::jsonb
)
returns void
language plpgsql
security definer
set search_path = public
as $$
declare
  v_outbox public.chat_outbox_messages;
  v_sent_at timestamptz;
begin
  select * into v_outbox
  from public.chat_outbox_messages
  where id = p_outbox_id;

  if v_outbox.id is null then
    return;
  end if;

  -- Las rutas de retargeting toman siempre conversación → outbox para no invertir el orden
  -- contra chat_retargeting_commit.
  if v_outbox.idempotency_key like 'retargeting:%' then
    perform 1
    from public.chat_conversations
    where id = v_outbox.conversation_id
    for update;
  end if;

  v_sent_at := clock_timestamp();
  update public.chat_outbox_messages
  set status = 'sent',
      sent_at = v_sent_at,
      processing_at = null,
      raw_payload = coalesce(p_raw_payload, '{}'::jsonb),
      error = null
  where id = p_outbox_id
    and status = 'processing'
  returning * into v_outbox;

  if v_outbox.id is null or v_outbox.idempotency_key not like 'retargeting:%' then
    return;
  end if;

  -- El historial refleja entregas, no intenciones de envío.
  insert into public.chat_messages (
    conversation_id, role, content, external_message_id, processing_status, created_at
  )
  values (
    v_outbox.conversation_id, 'assistant', v_outbox.content,
    v_outbox.idempotency_key, 'processed', v_sent_at
  )
  on conflict (conversation_id, external_message_id, role) do nothing;

  update public.chat_conversations
  set state = coalesce(state, '{}'::jsonb) || jsonb_build_object(
        'retargeting',
        coalesce(state -> 'retargeting', '{}'::jsonb) || jsonb_build_object(
          'decision', 'enviado',
          'mensaje', v_outbox.content,
          'outbox_id', v_outbox.id,
          'at', v_sent_at,
          'sent_at', v_sent_at
        )
      )
  where id = v_outbox.conversation_id;
end
$$;


create or replace function public.chat_fail_outbox_attempt(
  p_outbox_id bigint,
  p_error text
)
returns text
language plpgsql
security definer
set search_path = public
as $$
declare
  v_outbox public.chat_outbox_messages;
begin
  select * into v_outbox
  from public.chat_outbox_messages
  where id = p_outbox_id;

  if v_outbox.id is null then
    return 'failed';
  end if;

  -- Un timeout del cliente HTTP puede ocurrir después de que esta RPC ya confirmó el envío.
  -- Ese retry no debe resucitar una fila terminal.
  if v_outbox.status in ('sent', 'failed', 'cancelled') then
    return v_outbox.status;
  end if;

  if v_outbox.idempotency_key like 'retargeting:%' then
    perform 1
    from public.chat_conversations
    where id = v_outbox.conversation_id
    for update;
  end if;

  update public.chat_outbox_messages
  set attempts = attempts + 1,
      status = case when attempts + 1 >= max_attempts then 'failed' else 'retry' end,
      processing_at = null,
      error = left(coalesce(p_error, ''), 500)
  where id = p_outbox_id
    and status = 'processing'
  returning * into v_outbox;

  if v_outbox.id is null then
    return 'failed';
  end if;

  if v_outbox.status = 'failed' and v_outbox.idempotency_key like 'retargeting:%' then
    update public.chat_conversations
    set state = coalesce(state, '{}'::jsonb) || jsonb_build_object(
          'retargeting',
          coalesce(state -> 'retargeting', '{}'::jsonb) || jsonb_build_object(
            'decision', 'fallido',
            'failed_at', now()
          )
        )
    where id = v_outbox.conversation_id;
  end if;

  return v_outbox.status;
end
$$;


-- ── Cap diario ─────────────────────────────────────────────────────────────
create or replace function public.chat_retargeting_sent_count(
  p_channel text,
  p_hours double precision default 24
)
returns integer
language sql
security definer
set search_path = public
as $$
  select count(*)::integer
  from public.chat_conversations c
  where c.channel = p_channel
    and c.state -> 'retargeting' ->> 'decision' in ('pendiente', 'enviado')
    and coalesce(
          (c.state -> 'retargeting' ->> 'at')::timestamptz,
          (c.state -> 'retargeting' ->> 'queued_at')::timestamptz
        ) > now() - make_interval(secs => p_hours * 3600);
$$;


-- ── Funnel ─────────────────────────────────────────────────────────────────
create or replace function public.chat_retargeting_stats(p_channel text)
returns jsonb
language sql
security definer
set search_path = public
as $$
  -- "respondio" se calcula del historial, no de un flag: no depende de que el clasificador
  -- haya corrido después del follow-up.
  select jsonb_build_object(
    'enviados', count(*) filter (where r.decision = 'enviado'),
    'pendientes', count(*) filter (where r.decision = 'pendiente'),
    'fallidos', count(*) filter (where r.decision = 'fallido'),
    'cancelados', count(*) filter (where r.decision = 'cancelado'),
    'descartados', count(*) filter (where r.decision = 'descartado'),
    'respondieron', count(*) filter (where r.decision = 'enviado' and r.replied),
    'en_compra', count(*) filter (where r.decision = 'enviado' and r.replied and r.stage = 'compra'),
    'simulados', count(*) filter (where r.dry_run_decision = 'enviado')
  )
  from (
    select
      c.state -> 'retargeting' ->> 'decision' as decision,
      c.state -> 'retargeting_dryrun' ->> 'decision' as dry_run_decision,
      c.state ->> 'stage' as stage,
      exists (
        select 1
        from public.chat_messages m
        where m.conversation_id = c.id
          and m.role = 'user'
          and m.created_at > (c.state -> 'retargeting' ->> 'at')::timestamptz
      ) as replied
    from public.chat_conversations c
    where c.channel = p_channel
      and (c.state ? 'retargeting' or c.state ? 'retargeting_dryrun')
  ) r;
$$;


-- ── Permisos ───────────────────────────────────────────────────────────────
-- Postgres le da EXECUTE a PUBLIC por default y estas funciones son SECURITY DEFINER: tal como
-- quedaban, cualquiera con la anon key podía llamarlas por PostgREST y leer contactos o pisar el
-- state de cualquier conversación. El agente entra SIEMPRE con la service_role key, así que al
-- resto le sacamos el execute.
--
-- Alcanza también a las funciones de la 001 (locks, eventos, jobs): tenían el mismo agujero y son
-- más peligrosas. Lista EXPLÍCITA a propósito: este Supabase es compartido con otros proyectos y
-- un patrón tipo '%chat%' podría revocarle permisos a RPC ajenas.
--
-- Va al final para alcanzar todo lo definido arriba. Idempotente y tolerante a que los roles de
-- Supabase no existan (ej. un Postgres pelado de test).
do $$
declare
  v_fn record;
  v_role text;
  v_propias constant text[] := array[
    -- 001_chat_memory.sql
    'get_or_create_chat_conversation',
    'acquire_chat_conversation_lock',
    'release_chat_conversation_lock',
    'cleanup_expired_chat_conversation_locks',
    'mark_chat_event_received',
    'update_chat_event_status',
    'enqueue_chat_webhook_job',
    'update_chat_webhook_job_status',
    'increment_chat_webhook_job_attempts_for_conversation',
    'requeue_stale_chat_webhook_jobs',
    'due_chat_webhook_job_conversations',
    'increment_chat_outbox_attempts',
    -- 002_retargeting.sql
    'chat_persist_incoming_event',
    'chat_merge_conversation_state',
    'chat_retargeting_candidates',
    'chat_retargeting_commit',
    'chat_claim_outbox',
    'chat_due_outbox_messages',
    'chat_cancel_retargeting_if_resumed',
    'chat_mark_outbox_sent',
    'chat_fail_outbox_attempt',
    'chat_retargeting_sent_count',
    'chat_retargeting_stats'
  ];
begin
  for v_fn in
    select p.oid::regprocedure as signature
    from pg_proc p
    join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public'
      and p.prosecdef                      -- solo las security definer
      and p.proname = any(v_propias)
  loop
    execute format('revoke all on function %s from public', v_fn.signature);
    foreach v_role in array array['anon', 'authenticated'] loop
      if exists (select 1 from pg_roles where rolname = v_role) then
        execute format('revoke all on function %s from %I', v_fn.signature, v_role);
      end if;
    end loop;
    -- service_role es quien ejecuta (el agente entra con esa key).
    --
    -- authenticator NO ejecuta nada, pero es imprescindible igual: PostgREST se conecta con ese
    -- rol y arma su schema cache introspeccionando CON ESE ROL, filtrando por
    -- has_function_privilege(). En Supabase authenticator es NOINHERIT, así que no hereda los
    -- permisos de service_role: sin este grant, PostgREST descarta las funciones de su cache y
    -- responde PGRST202 "no matches were found in the schema cache" aun con la service key.
    -- No abre nada: la llamada real corre después de un SET ROLE, así que anon sigue rebotando
    -- con "permission denied" (verificado contra los roles reales de Supabase).
    foreach v_role in array array['service_role', 'authenticator'] loop
      if exists (select 1 from pg_roles where rolname = v_role) then
        execute format('grant execute on function %s to %I', v_fn.signature, v_role);
      end if;
    end loop;
  end loop;
end
$$;
