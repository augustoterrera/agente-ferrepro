begin;

do $$
declare
  v_intake jsonb;
  v_duplicate jsonb;
  v_commit jsonb;
  v_conversation_id bigint;
  v_second_conversation_id bigint;
  v_outbox_id bigint;
  v_generic_outbox_id bigint;
  v_last_assistant_at timestamptz;
  v_transaction_started_at timestamptz := now();
begin
  perform pg_sleep(0.01);
  v_intake := public.chat_persist_incoming_event(
    'event-1', 'chatwoot', 'conversation-1', 'contact-1', 'account-1',
    'message-1', 'Busco un taladro', '{}'::jsonb, 5
  );
  assert (v_intake ->> 'is_new')::boolean, 'el primer evento debe persistirse';
  v_conversation_id := (v_intake -> 'conversation' ->> 'id')::bigint;
  assert (
    select created_at > v_transaction_started_at
    from public.chat_messages
    where conversation_id = v_conversation_id and role = 'user'
  ), 'el timestamp del intake quedó congelado antes de adquirir el lock';

  v_duplicate := public.chat_persist_incoming_event(
    'event-1', 'chatwoot', 'conversation-1', 'contact-1', 'account-1',
    'message-1', 'Busco un taladro', '{}'::jsonb, 5
  );
  assert not (v_duplicate ->> 'is_new')::boolean, 'el evento duplicado debe ignorarse';
  assert (
    select count(*) = 1
    from public.chat_messages
    where conversation_id = v_conversation_id and role = 'user'
  ), 'el intake duplicado creó dos mensajes';

  update public.chat_messages
  set created_at = clock_timestamp() - interval '23 hours'
  where conversation_id = v_conversation_id and role = 'user';

  insert into public.chat_messages (conversation_id, role, content, created_at)
  values (
    v_conversation_id, 'assistant', 'Tengo el Bosch disponible. ¿Te interesa?',
    clock_timestamp() - interval '3 hours'
  )
  returning created_at into v_last_assistant_at;

  insert into public.chat_outbox_messages (
    conversation_id, external_conversation_id, channel, content, status,
    idempotency_key, created_at, sent_at
  )
  values (
    v_conversation_id, 'conversation-1', 'chatwoot',
    'Tengo el Bosch disponible. ¿Te interesa?', 'sent', 'normal:conversation-1',
    v_last_assistant_at, v_last_assistant_at
  );

  assert exists (
    select 1
    from public.chat_retargeting_candidates('chatwoot', 24, 2, 'retargeting', 25)
    where conversation_id = v_conversation_id
  ), 'la conversación válida no apareció como candidata';

  v_commit := public.chat_retargeting_commit(
    v_conversation_id, 'conversation-1', 'chatwoot',
    '¿Seguís interesado en el taladro Bosch?', 'retargeting:conversation-1',
    'retargeting', 'quedó eligiendo', v_last_assistant_at
  );
  assert v_commit ->> 'status' = 'creado', 'el follow-up no se encoló';
  v_outbox_id := (v_commit ->> 'outbox_id')::bigint;
  assert not exists (
    select 1
    from public.chat_messages
    where external_message_id = 'retargeting:conversation-1'
  ), 'el follow-up entró al historial antes de entregarse';

  assert public.chat_claim_outbox(v_outbox_id, 15), 'no se pudo reclamar el outbox';
  insert into public.chat_messages (
    conversation_id, external_message_id, role, content, created_at
  )
  values (
    v_conversation_id, 'message-2', 'user', 'Sí, decime',
    clock_timestamp()
  );
  assert public.chat_cancel_retargeting_if_resumed(v_outbox_id), 'no se canceló tras la respuesta';
  assert (
    select status = 'cancelled'
    from public.chat_outbox_messages
    where id = v_outbox_id
  ), 'el outbox cancelado conservó un estado enviable';

  insert into public.chat_conversations (channel, external_conversation_id)
  values ('chatwoot', 'conversation-2')
  returning id into v_second_conversation_id;
  insert into public.chat_messages (conversation_id, role, content, created_at)
  values
    (v_second_conversation_id, 'user', 'Quiero una amoladora', clock_timestamp() - interval '23 hours'),
    (v_second_conversation_id, 'assistant', 'Tengo una Bosch. ¿La querés?', clock_timestamp() - interval '3 hours');
  select created_at into v_last_assistant_at
  from public.chat_messages
  where conversation_id = v_second_conversation_id and role = 'assistant';
  insert into public.chat_outbox_messages (
    conversation_id, external_conversation_id, channel, content, status,
    idempotency_key, created_at, sent_at
  )
  values (
    v_second_conversation_id, 'conversation-2', 'chatwoot',
    'Tengo una Bosch. ¿La querés?', 'sent', 'normal:conversation-2',
    v_last_assistant_at, v_last_assistant_at
  );

  v_commit := public.chat_retargeting_commit(
    v_second_conversation_id, 'conversation-2', 'chatwoot',
    '¿Seguís interesado en la amoladora Bosch?', 'retargeting:conversation-2',
    'retargeting', 'quedó eligiendo', v_last_assistant_at
  );
  v_outbox_id := (v_commit ->> 'outbox_id')::bigint;
  assert public.chat_claim_outbox(v_outbox_id, 15);
  perform public.chat_mark_outbox_sent(v_outbox_id, '{"id": 123}'::jsonb);
  assert (
    select state -> 'retargeting' ->> 'decision' = 'enviado'
    from public.chat_conversations
    where id = v_second_conversation_id
  ), 'la entrega no confirmó el estado';
  assert (
    select count(*) = 1
    from public.chat_messages
    where external_message_id = 'retargeting:conversation-2'
  ), 'la entrega no insertó exactamente un mensaje';
  perform public.chat_mark_outbox_sent(v_outbox_id, '{"id": 123}'::jsonb);
  assert (
    select count(*) = 1
    from public.chat_messages
    where external_message_id = 'retargeting:conversation-2'
  ), 'la confirmación repetida duplicó el historial';

  insert into public.chat_outbox_messages (
    conversation_id, external_conversation_id, channel, content,
    idempotency_key, max_attempts
  )
  values (
    v_second_conversation_id, 'conversation-2', 'chatwoot',
    'mensaje genérico', 'generic:retry-test', 2
  )
  returning id into v_generic_outbox_id;
  assert public.chat_claim_outbox(v_generic_outbox_id, 15);
  assert not public.chat_claim_outbox(v_generic_outbox_id, 15), 'dos workers reclamaron el mismo lease';
  update public.chat_outbox_messages
  set processing_at = clock_timestamp() - interval '16 minutes'
  where id = v_generic_outbox_id;
  assert public.chat_claim_outbox(v_generic_outbox_id, 15), 'un processing vencido no se recuperó';
  assert public.chat_fail_outbox_attempt(v_generic_outbox_id, 'falló') = 'retry';
  assert public.chat_claim_outbox(v_generic_outbox_id, 15);
  assert public.chat_fail_outbox_attempt(v_generic_outbox_id, 'falló otra vez') = 'failed';
  assert public.chat_fail_outbox_attempt(v_generic_outbox_id, 'retry tardío') = 'failed';
  assert (
    select attempts = 2
    from public.chat_outbox_messages
    where id = v_generic_outbox_id
  ), 'un retry tardío revivió o incrementó una fila terminal';

  if exists (select 1 from pg_roles where rolname = 'anon') then
    assert not has_function_privilege(
      'anon', 'public.chat_retargeting_stats(text)', 'EXECUTE'
    ), 'anon conservó acceso a una RPC security definer';
  end if;
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    assert has_function_privilege(
      'service_role', 'public.chat_retargeting_stats(text)', 'EXECUTE'
    ), 'service_role perdió acceso a las RPC del agente';
  end if;
end
$$;

rollback;
