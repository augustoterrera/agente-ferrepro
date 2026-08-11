-- Atribución de conversaciones a la pauta de Meta.
--
-- Los anuncios click-to-WhatsApp mandan un objeto `referral` (id del aviso, ctwa_clid) en el
-- PRIMER mensaje del cliente. Ese payload pasa por `relay_webhook` (app/meta.py) camino a
-- Chatwoot y hasta ahora se descartaba: el dato no se puede reconstruir después.
--
-- El referral llega por el webhook de Meta (keyed por teléfono) y la conversación se crea
-- después, por el webhook de Chatwoot (keyed por conversation_id). Por eso se guarda suelto y
-- se vincula en un segundo paso, cuando el worker ya tiene ambos.
--
-- Idempotente: se puede correr varias veces.

create table if not exists public.chat_ad_referrals (
  id bigserial primary key,
  phone text not null,
  wa_message_id text,
  source_id text,
  source_type text,
  source_url text,
  headline text,
  ctwa_clid text,
  conversation_id bigint references public.chat_conversations(id) on delete set null,
  raw jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  linked_at timestamptz
);

-- Meta reintenta webhooks: el wamid es único global, así que dedup por ahí.
create unique index if not exists chat_ad_referrals_wa_message_idx
  on public.chat_ad_referrals (wa_message_id)
  where wa_message_id is not null;

-- Lookup del vínculo: por teléfono, solo los que todavía no se ataron a una conversación.
create index if not exists chat_ad_referrals_pendientes_idx
  on public.chat_ad_referrals (phone, created_at desc)
  where conversation_id is null;

-- Agregación de BI: costo por conversación/lead por aviso.
create index if not exists chat_ad_referrals_source_idx
  on public.chat_ad_referrals (source_id, created_at desc);
create index if not exists chat_ad_referrals_conversation_idx
  on public.chat_ad_referrals (conversation_id);

-- Permisos EXPLÍCITOS, no heredados de las default privileges: acá el SQL editor no corre con
-- el rol que las tiene configuradas, así que una tabla nueva queda legible pero NO escribible
-- por service_role. El síntoma es engañoso — GET 200 y POST 404 con cuerpo vacío, sin código
-- de error de PostgREST. La secuencia va aparte: sin ella el insert falla igual por el bigserial.
grant select, insert, update, delete on public.chat_ad_referrals to service_role;
grant usage, select on sequence public.chat_ad_referrals_id_seq to service_role;


-- ── Vínculo referral → conversación ────────────────────────────────────────
-- "El referral pendiente más reciente de este teléfono" necesita order+limit dentro del UPDATE,
-- que PostgREST no expresa. Va como RPC. `skip locked` evita que dos turnos concurrentes de la
-- misma conversación peleen por la misma fila.
--
-- La ventana existe para no atribuirle una conversación orgánica de hoy a un click de hace un
-- mes: en el flujo real el referral y el primer mensaje llegan con segundos de diferencia.
create or replace function public.chat_link_ad_referral(
  p_conversation_id bigint,
  p_phone text,
  p_max_age_hours integer default 72
) returns public.chat_ad_referrals
language sql
security definer
set search_path = public
as $$
  update public.chat_ad_referrals r
     set conversation_id = p_conversation_id,
         linked_at = now()
   where r.id = (
     select id
       from public.chat_ad_referrals
      where phone = p_phone
        and conversation_id is null
        and created_at >= now() - make_interval(hours => p_max_age_hours)
      order by created_at desc
      limit 1
      for update skip locked
   )
  returning r.*;
$$;

-- Las security definer nacen con EXECUTE para PUBLIC. Este Supabase es COMPARTIDO con otros
-- servicios: se revoca por lista explícita, nunca por patrón (rompería RPC ajenas).
-- Mismo bloque que la 002; ver ahí el porqué de `authenticator` (sin ese grant PostgREST
-- descarta la función de su schema cache y responde PGRST202 aun con la service key).
do $$
declare
  v_signature text;
  v_role text;
begin
  for v_signature in
    select p.oid::regprocedure::text
    from pg_proc p
    join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public'
      and p.prosecdef
      and p.proname = 'chat_link_ad_referral'
  loop
    execute format('revoke all on function %s from public', v_signature);
    foreach v_role in array array['anon', 'authenticated'] loop
      if exists (select 1 from pg_roles where rolname = v_role) then
        execute format('revoke all on function %s from %I', v_signature, v_role);
      end if;
    end loop;
    foreach v_role in array array['service_role', 'authenticator'] loop
      if exists (select 1 from pg_roles where rolname = v_role) then
        execute format('grant execute on function %s to %I', v_signature, v_role);
      end if;
    end loop;
  end loop;
end
$$;
