-- Comportamiento de chat_link_ad_referral (migración 003).
-- Correr contra un Postgres con 001 + 003 aplicadas. Hace rollback al final.
begin;

do $$
declare
  v_conversation_id bigint;
  v_otra_conversacion bigint;
  v_ref record;
  v_viejo_id bigint;
begin
  insert into public.chat_conversations (channel, external_conversation_id)
  values ('chatwoot', 'conv-ads-1') returning id into v_conversation_id;
  insert into public.chat_conversations (channel, external_conversation_id)
  values ('chatwoot', 'conv-ads-2') returning id into v_otra_conversacion;

  -- ── Caso normal: el click llegó segundos antes que el mensaje ────────────
  insert into public.chat_ad_referrals (phone, wa_message_id, source_id, ctwa_clid)
  values ('5493816506312', 'wamid.A', 'ad-123', 'clid-A');

  select * into v_ref from public.chat_link_ad_referral(v_conversation_id, '5493816506312');
  assert v_ref.source_id = 'ad-123', 'debe atar el referral pendiente del teléfono';
  assert v_ref.conversation_id = v_conversation_id, 'debe quedar atado a la conversación';
  assert v_ref.linked_at is not null, 'debe sellar linked_at';

  -- ── Idempotencia: el worker llama en CADA turno ─────────────────────────
  select * into v_ref from public.chat_link_ad_referral(v_conversation_id, '5493816506312');
  assert v_ref is null, 'un referral ya atado no se vuelve a tocar';
  assert (select count(*) from public.chat_ad_referrals
           where conversation_id = v_conversation_id) = 1,
         'no puede duplicar el vínculo';

  -- ── Charla orgánica: teléfono sin ningún click ──────────────────────────
  select * into v_ref from public.chat_link_ad_referral(v_otra_conversacion, '5490000000000');
  assert v_ref is null, 'sin referral pendiente devuelve null, no explota';

  -- ── Ventana: un click viejo no se le cuelga a una charla de hoy ─────────
  insert into public.chat_ad_referrals (phone, wa_message_id, source_id, created_at)
  values ('5493811111111', 'wamid.VIEJO', 'ad-viejo', now() - interval '100 hours')
  returning id into v_viejo_id;

  select * into v_ref from public.chat_link_ad_referral(v_otra_conversacion, '5493811111111', 72);
  assert v_ref is null, 'fuera de la ventana no debe atribuir';
  assert (select conversation_id is null from public.chat_ad_referrals where id = v_viejo_id),
         'el referral viejo queda pendiente, no se pierde';

  -- Con ventana más amplia sí entra: la ventana es política, no pérdida de dato.
  select * into v_ref from public.chat_link_ad_referral(v_otra_conversacion, '5493811111111', 200);
  assert v_ref.source_id = 'ad-viejo', 'con ventana amplia debe atar';

  -- ── Varios clicks del mismo teléfono: gana el más reciente ──────────────
  insert into public.chat_ad_referrals (phone, wa_message_id, source_id, created_at)
  values ('5493812222222', 'wamid.B1', 'ad-vieja', now() - interval '2 hours'),
         ('5493812222222', 'wamid.B2', 'ad-nueva', now() - interval '1 minute');

  select * into v_ref from public.chat_link_ad_referral(v_conversation_id, '5493812222222');
  assert v_ref.source_id = 'ad-nueva', 'debe ganar el click más reciente';

  raise notice 'chat_link_ad_referral: OK';
end
$$;

-- Dedup del reintento de Meta: el wamid es único global.
do $$
begin
  insert into public.chat_ad_referrals (phone, wa_message_id, source_id)
  values ('5493819999999', 'wamid.DUP', 'ad-1');
  begin
    insert into public.chat_ad_referrals (phone, wa_message_id, source_id)
    values ('5493819999999', 'wamid.DUP', 'ad-1');
    raise exception 'el wamid duplicado debía rebotar';
  exception when unique_violation then
    raise notice 'dedup por wamid: OK';
  end;

  -- Sin wamid el índice parcial no aplica: dos referrals sin id conviven.
  insert into public.chat_ad_referrals (phone, source_id) values ('5493818888888', 'ad-x');
  insert into public.chat_ad_referrals (phone, source_id) values ('5493818888888', 'ad-x');
  raise notice 'indice parcial (wamid null): OK';
end
$$;

rollback;
