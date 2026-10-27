"""Agenda: reservas de Google Calendar → confirmación por WhatsApp.

Lo que se fija acá es lo que no se puede reparar después: un turno que nadie confirma, un
cliente que recibe dos veces lo mismo, o un mensaje que WhatsApp rechaza por estar fuera de la
ventana de 24 h. Y que el único recordatorio es para AGENDAR, nunca para asistir.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from pydantic_ai import ModelRetry

from app import agenda, chatwoot_service, retargeting
from app.chat_memory import Conversation
from app.models import AgentMessage
from app.tasks import chatwoot_tasks as tasks

AHORA = datetime(2026, 10, 27, 15, 0, tzinfo=timezone.utc)  # 12:00 en Tucumán
TURNO = AHORA + timedelta(days=2)  # jueves 29/10 12:00 en Tucumán
TEL = "5493815551234"
LINK = "https://calendar.app.google/8dhSCmb5b81yTk9g7"


def _reserva(**kwargs) -> agenda.Reserva:
    datos = dict(event_id="ev1", cancelada=False, inicio=TURNO, nombre="Juan Pérez", email="juan@x.com", telefonos=("3815551234",))
    return agenda.Reserva(**(datos | kwargs))


def _conv(state: dict | None = None, cid: int = 5) -> Conversation:
    base = {"agenda_link": {"at": (AHORA - timedelta(hours=1)).isoformat(), "telefono": TEL}}
    return Conversation(cid, "chatwoot", str(800 + cid), "6", state=base | (state or {}))


class _Chatwoot:
    def __init__(self, labels: list[str] | None = None) -> None:
        self.labels = list(labels or [])
        self.notas: list[str] = []

    def get_conversation_labels(self, account_id, conversation_id):
        return list(self.labels)

    def set_conversation_labels(self, account_id, conversation_id, labels):
        self.labels = list(labels)
        return list(labels)

    def create_outgoing_message(self, account_id, conversation_id, content, *, private=False):
        assert private, "la agenda solo escribe notas privadas por Chatwoot; al cliente va por el outbox"
        self.notas.append(content)
        return {"id": 1}


class Telefonos(unittest.TestCase):
    def test_como_lo_escribe_la_gente(self) -> None:
        for escrito in (
            "381 555-1234",
            "0381 15 555-1234",
            "(0381) 155551234",
            "+54 9 381 555 1234",
            "5493815551234",
            "15-555-1234",
            "555-1234",
        ):
            self.assertTrue(agenda.mismo_telefono(escrito, TEL), escrito)

    def test_no_confunde_numeros_distintos(self) -> None:
        for escrito in ("381 555-9999", "11 5555-1234", "1234", ""):
            self.assertFalse(agenda.mismo_telefono(escrito, TEL), escrito)

    def test_buenos_aires_con_15(self) -> None:
        self.assertTrue(agenda.mismo_telefono("011 15 5555-1234", "5491155551234"))

    def test_saca_telefonos_del_formulario(self) -> None:
        texto = "Reservado por Juan Pérez\njuan@x.com\nTeléfono: 0381 15 555-1234\n17:00 a 17:30"
        self.assertEqual(agenda.extraer_telefonos(texto), ["0381155551234"])


class EventosDelCalendario(unittest.TestCase):
    def _evento(self, **kwargs) -> dict:
        evento = {
            "id": "ev1",
            "status": "confirmed",
            "summary": "Asesoría FerrePro (Juan Pérez)",
            "description": "Teléfono: 381 555-1234",
            "start": {"dateTime": "2026-10-29T12:00:00-03:00"},
            "attendees": [
                {"email": "agenda@ferrepro.com", "organizer": True, "self": True},
                {"email": "juan@x.com", "displayName": "Juan Pérez"},
            ],
        }
        return evento | kwargs

    def test_reserva_con_telefono(self) -> None:
        reserva = agenda.parse_evento(self._evento())
        self.assertEqual(reserva.email, "juan@x.com")
        self.assertEqual(reserva.telefonos, ("3815551234",))
        self.assertEqual(agenda.describir_turno(reserva.inicio), "jueves 29/10 a las 12 hs")
        # En UTC: el state se filtra comparando el ISO como texto contra "ahora" en UTC.
        self.assertEqual(reserva.inicio.isoformat(), "2026-10-29T15:00:00+00:00")

    def test_cancelada_viene_sin_datos(self) -> None:
        reserva = agenda.parse_evento({"id": "ev1", "status": "cancelled"})
        self.assertTrue(reserva.cancelada)

    def test_bloqueo_propio_no_es_reserva(self) -> None:
        self.assertIsNone(agenda.parse_evento(self._evento(attendees=[{"email": "a@b", "self": True}])))
        self.assertIsNone(agenda.parse_evento(self._evento(start={"date": "2026-10-29"})))

    def test_filtro_por_titulo(self) -> None:
        with patch.object(agenda.settings, "agenda_titulo", "Asesoría FerrePro"):
            self.assertIsNotNone(agenda.parse_evento(self._evento()))
            self.assertIsNone(agenda.parse_evento(self._evento(summary="Dentista")))

    def test_plantilla_sin_variables_no_manda_componentes(self) -> None:
        """Meta rechaza un componente body con la lista de parámetros vacía."""
        self.assertNotIn("components", agenda.plantilla_payload(TEL, "recordatorio_agenda")["template"])
        con = agenda.plantilla_payload(TEL, "confirmacion_reunion", ["jueves 29/10 a las 12 hs"])
        self.assertEqual(con["template"]["components"][0]["parameters"][0]["text"], "jueves 29/10 a las 12 hs")


class _Sweep(unittest.TestCase):
    """Corre _run_agenda_sweep con todo lo externo simulado."""

    def correr(self, reservas, conversaciones, chatwoot=None, *, ultimo_mensaje=AHORA - timedelta(hours=1), historial=(), referral=None, **settings):
        self.chatwoot = chatwoot or _Chatwoot()
        self.estados: dict[int, dict] = {}

        def merge(cid, patch_):
            self.estados.setdefault(cid, {}).update(patch_)

        valores = {"whatsapp_template_confirmacion": None, "whatsapp_template_recordatorio_agenda": None} | settings
        with (
            patch.multiple(tasks.settings, **valores),
            patch.object(tasks.agenda, "proximas_reservas", return_value=reservas),
            patch.object(tasks.chat_memory, "conversaciones_de_agenda", return_value=conversaciones),
            patch.object(tasks.chat_memory, "merge_state", side_effect=merge),
            patch.object(tasks.chat_memory, "last_user_message_at", return_value=ultimo_mensaje),
            patch.object(tasks.chat_memory, "recent_history", return_value=list(historial)),
            patch.object(tasks.chat_memory, "primer_referral", return_value=referral),
            patch.object(tasks.chat_memory, "create_outbox", return_value={"id": 99, "status": "pending"}) as self.outbox,
            patch.object(tasks.chat_memory, "add_message") as self.historial,
            patch.object(tasks, "send_chatwoot_outbound_message") as self.envio,
            patch.object(tasks, "send_whatsapp", return_value={"messages": [{"id": "wamid"}]}) as self.meta,
            patch.object(tasks, "sync_crm_labels"),
            patch.object(tasks, "notifier") as self.notifier,
            patch.object(tasks, "_redis") as redis_,
        ):
            redis_.return_value.set.return_value = True
            return tasks._run_agenda_sweep(self.chatwoot, AHORA)


class ConfirmacionDelTurno(_Sweep):
    def test_reserva_nueva_dentro_de_24h_va_por_texto_libre(self) -> None:
        r = self.correr([_reserva()], [_conv()])

        self.assertEqual(r["confirmadas"], 1)
        texto = self.outbox.call_args.args[3]
        self.assertIn("me llegó tu turno: jueves 29/10 a las 12 hs", texto)
        self.assertEqual(self.outbox.call_args.args[4], f"agenda:confirmacion:ev1:{TURNO.isoformat()}")
        self.envio.apply_async.assert_called_once()
        self.meta.assert_not_called()
        self.assertIn("agendado", self.chatwoot.labels)
        self.assertIn("Reservó la reunión para el jueves 29/10 a las 12 hs", self.chatwoot.notas[0])
        estado = self.estados[5]["agenda"]
        self.assertEqual((estado["event_id"], estado["estado"], estado["confirmacion"]), ("ev1", "agendado", "texto_libre"))

    def test_pasadas_24h_va_por_plantilla(self) -> None:
        """Texto libre fuera de la ventana: Meta lo rechaza (131047) y el cliente no se entera."""
        self.correr(
            [_reserva()], [_conv()],
            ultimo_mensaje=AHORA - timedelta(hours=30),
            whatsapp_template_confirmacion="confirmacion_reunion",
        )
        self.outbox.assert_not_called()
        payload = self.meta.call_args.args[0]
        self.assertEqual(payload["to"], TEL)
        self.assertEqual(payload["template"]["name"], "confirmacion_reunion")
        self.assertEqual(self.estados[5]["agenda"]["confirmacion"], "plantilla")

    def test_pasadas_24h_sin_plantilla_avisa_a_una_persona(self) -> None:
        self.correr([_reserva()], [_conv()], ultimo_mensaje=AHORA - timedelta(hours=30))
        self.outbox.assert_not_called()
        self.meta.assert_not_called()
        self.assertIn("Confirmalo a mano", self.chatwoot.notas[-1])
        self.assertEqual(self.estados[5]["agenda"]["confirmacion"], "fuera_de_ventana")

    def test_si_lo_atiende_una_persona_no_le_escribe_el_bot(self) -> None:
        self.correr([_reserva()], [_conv()], _Chatwoot(["bot_apagado"]))
        self.outbox.assert_not_called()
        self.assertEqual(len(self.chatwoot.notas), 1)  # la persona se entera por la nota
        self.assertEqual(self.estados[5]["agenda"]["confirmacion"], "humano")

    def test_no_repite_si_el_bot_ya_confirmo_en_el_chat(self) -> None:
        historial = [AgentMessage(role="assistant", content=agenda.texto_confirmacion(TURNO))]
        self.correr([_reserva()], [_conv()], historial=historial)
        self.outbox.assert_not_called()
        self.assertEqual(self.estados[5]["agenda"]["confirmacion"], "ya_confirmado_en_chat")

    def test_una_reserva_ya_procesada_no_hace_nada(self) -> None:
        procesada = {"agenda": {"event_id": "ev1", "inicio": TURNO.isoformat(), "estado": "agendado", "confirmacion": "texto_libre"}}
        chatwoot = MagicMock()
        r = self.correr([_reserva()], [_conv(procesada)], chatwoot)
        self.assertEqual(r["sin_cambios"], 1)
        chatwoot.assert_not_called()
        chatwoot.get_conversation_labels.assert_not_called()
        self.outbox.assert_not_called()

    def test_si_el_aviso_fallo_se_reintenta_sin_repetir_la_nota(self) -> None:
        a_medias = {"agenda": {"event_id": "ev1", "inicio": TURNO.isoformat(), "estado": "agendado"}}
        self.correr([_reserva()], [_conv(a_medias)])
        self.outbox.assert_called_once()
        self.assertEqual(self.chatwoot.notas, [])

    def test_cambio_de_horario_reconfirma(self) -> None:
        antes = {"agenda": {"event_id": "ev1", "inicio": (TURNO - timedelta(hours=3)).isoformat(), "estado": "agendado", "confirmacion": "texto_libre"}}
        historial = [AgentMessage(role="assistant", content=agenda.texto_confirmacion(TURNO - timedelta(hours=3)))]
        self.correr([_reserva()], [_conv(antes)], historial=historial)
        self.assertIn("Cambió el turno", self.chatwoot.notas[0])
        self.outbox.assert_called_once()

    def test_cancelacion_saca_la_etiqueta(self) -> None:
        reservado = {"agenda": {"event_id": "ev1", "inicio": TURNO.isoformat(), "estado": "agendado", "confirmacion": "texto_libre"}}
        r = self.correr([_reserva(cancelada=True, inicio=None)], [_conv(reservado)], _Chatwoot(["interesado", "agendado"]))
        self.assertEqual(r["canceladas"], 1)
        self.assertNotIn("agendado", self.chatwoot.labels)
        self.assertEqual(self.estados[5]["agenda"]["estado"], "cancelado")
        self.outbox.assert_not_called()

    def test_dos_reservas_del_mismo_chat_no_se_pisan(self) -> None:
        r = self.correr([_reserva(), _reserva(event_id="ev2", inicio=TURNO + timedelta(days=1))], [_conv()])
        self.assertEqual((r["confirmadas"], r["duplicadas"]), (1, 1))
        self.assertEqual(self.estados[5]["agenda"]["event_id"], "ev1")

    def test_reserva_sin_chat_avisa_una_vez(self) -> None:
        r = self.correr([_reserva(telefonos=("3819999999",))], [_conv()])
        self.assertEqual(r["sin_chat"], 1)
        self.notifier.notify_warning.assert_called_once()
        self.outbox.assert_not_called()


class RecordatorioParaAgendar(_Sweep):
    """Solo para AGENDAR, por plantilla, a chats de anuncios, una vez, antes de que cierre la
    ventana gratis del anuncio. No existe recordatorio para asistir a la reunión."""

    CLICK = AHORA - timedelta(hours=71, minutes=30)  # la ventana de 72 h cierra en 30 min

    def correr_recordatorio(self, conv, **kwargs):
        valores = {
            "referral": {"created_at": self.CLICK.isoformat()},
            "ultimo_mensaje": AHORA - timedelta(hours=40),
            "whatsapp_template_recordatorio_agenda": "recordatorio_agenda",
        } | kwargs
        return self.correr([], [conv], **valores)

    def test_manda_la_plantilla_en_la_ultima_chance(self) -> None:
        r = self.correr_recordatorio(_conv())
        self.assertEqual(r["recordatorio_enviado"], 1)
        self.assertEqual(self.meta.call_args.args[0]["template"]["name"], "recordatorio_agenda")
        self.assertIn(LINK, self.historial.call_args.args[2])  # el bot ve lo que se le mandó
        self.assertEqual(self.estados[5]["recordatorio_agenda"]["decision"], "enviado")

    def test_es_una_sola_vez(self) -> None:
        enviado = {"recordatorio_agenda": {"decision": "enviado"}}
        self.correr_recordatorio(_conv(enviado))
        self.meta.assert_not_called()

    def test_si_ya_agendo_no_se_le_recuerda(self) -> None:
        self.correr_recordatorio(_conv({"agenda": {"estado": "agendado"}}))
        self.meta.assert_not_called()

    def test_chat_organico_no_tiene_recordatorio_por_plantilla(self) -> None:
        r = self.correr_recordatorio(_conv(), referral=None)
        self.assertEqual(r["recordatorio_no_aplica"], 1)
        self.meta.assert_not_called()

    def test_espera_a_la_ultima_chance(self) -> None:
        r = self.correr_recordatorio(_conv(), referral={"created_at": (AHORA - timedelta(hours=30)).isoformat()})
        self.meta.assert_not_called()
        self.assertEqual(self.estados[5]["recordatorio_agenda"]["decision"], "pendiente")
        self.assertNotIn("recordatorio_enviado", r)

    def test_ventana_del_anuncio_cerrada(self) -> None:
        self.correr_recordatorio(_conv(), referral={"created_at": (AHORA - timedelta(hours=80)).isoformat()})
        self.meta.assert_not_called()
        self.assertEqual(self.estados[5]["recordatorio_agenda"]["decision"], "vencido")

    def test_con_la_etiqueta_agendado_se_descarta(self) -> None:
        self.correr_recordatorio(_conv(), chatwoot=_Chatwoot(["agendado"]))
        self.meta.assert_not_called()
        self.assertEqual(self.estados[5]["recordatorio_agenda"]["decision"], "descartado")

    def test_si_lo_derivaron_no_se_le_recuerda(self) -> None:
        derivado = [AgentMessage(role="assistant", content="Te derivo con un asesor de FerrePro para que lo vea con vos.")]
        self.correr_recordatorio(_conv(), historial=derivado)
        self.meta.assert_not_called()

    def test_sin_plantilla_no_consulta_nada(self) -> None:
        self.correr_recordatorio(_conv(), whatsapp_template_recordatorio_agenda=None)
        self.meta.assert_not_called()
        self.assertNotIn(5, self.estados)

    def test_no_hay_recordatorio_para_asistir(self) -> None:
        """Un turno a 1 hora no dispara nada: el negocio no quiere recordatorio de asistencia."""
        ya_confirmado = {"agenda": {"event_id": "ev1", "inicio": (AHORA + timedelta(hours=1)).isoformat(), "estado": "agendado", "confirmacion": "texto_libre"}}
        r = self.correr([_reserva(inicio=AHORA + timedelta(hours=1))], [_conv(ya_confirmado)])
        self.assertEqual(r["sin_cambios"], 1)
        self.outbox.assert_not_called()
        self.meta.assert_not_called()


class LinkDeAgendaEnElTurno(unittest.TestCase):
    def test_anota_telefono_y_momento_al_mandar_el_link(self) -> None:
        with (
            patch.object(chatwoot_service.settings, "calendar_booking_url", LINK),
            patch.object(chatwoot_service.chat_memory, "merge_state") as merge,
        ):
            chatwoot_service.registrar_link_de_agenda(5, f"Elegí acá 👇\n{LINK}", {"sender": {"phone_number": "+54 9 381 555-1234"}})
        self.assertEqual(merge.call_args.args[1]["agenda_link"]["telefono"], TEL)

    def test_sin_link_no_anota(self) -> None:
        with patch.object(chatwoot_service.chat_memory, "merge_state") as merge:
            chatwoot_service.registrar_link_de_agenda(5, "¿Es para tu casa?", {"sender": {"phone_number": TEL}})
        merge.assert_not_called()

    def test_el_agente_sabe_que_ya_reservo(self) -> None:
        state = {"agenda": {"estado": "agendado", "inicio": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()}}
        self.assertIsNotNone(agenda.turno_vigente(state))
        self.assertIsNone(agenda.turno_vigente({"agenda": {"estado": "cancelado", "inicio": state["agenda"]["inicio"]}}))
        self.assertIsNone(agenda.turno_vigente({}))

        class _Corrida:
            output = "Perfecto."

        agente = MagicMock()
        agente.run_sync.return_value = _Corrida()
        with patch("app.agent.build_agent", return_value=agente):
            from app.agent import run_agent_reply

            run_agent_reply("a qué hora era?", [], turno="jueves 29/10 a las 12 hs")
        self.assertIn("YA reservó la reunión para el jueves 29/10 a las 12 hs", agente.run_sync.call_args.args[0])


class RetargetingConAgenda(unittest.TestCase):
    def test_la_etiqueta_agendado_veta(self) -> None:
        self.assertTrue(retargeting.veto_por_etiquetas(["interesado", "agendado"]))

    def test_con_el_link_mandado_vale_retomar(self) -> None:
        historia = [AgentMessage(role="assistant", content=f"Elegí el día y la hora acá 👇\n{LINK}")]
        with patch.object(retargeting.settings, "calendar_booking_url", LINK):
            self.assertIsNone(retargeting.veto_por_conversacion(historia, None))

    def test_el_redactor_no_puede_inventar_links(self) -> None:
        with patch.object(retargeting.settings, "calendar_booking_url", LINK):
            bien = retargeting.Followup(vale_la_pena=True, motivo="x", mensaje=f"¿Pudiste elegir horario?\n{LINK}")
            self.assertIs(retargeting.revisar_followup(bien), bien)
            mal = retargeting.Followup(vale_la_pena=True, motivo="x", mensaje="Mirá https://ferrepro.example/precios")
            with self.assertRaises(ModelRetry):
                retargeting.revisar_followup(mal)

    def test_pasar_el_link_no_es_una_promesa(self) -> None:
        with patch.object(retargeting.settings, "calendar_booking_url", LINK):
            ok = retargeting.Followup(vale_la_pena=True, motivo="x", mensaje=f"Te paso el link de nuevo:\n{LINK}")
            self.assertIs(retargeting.revisar_followup(ok), ok)
            with self.assertRaises(ModelRetry):
                retargeting.revisar_followup(
                    retargeting.Followup(vale_la_pena=True, motivo="x", mensaje="¿Querés que te paso el precio?")
                )


class ClasificadorRespetaAgendado(unittest.TestCase):
    def test_no_borra_la_etiqueta_al_reescribir(self) -> None:
        conv = Conversation(5, "chatwoot", "805", "6", state={"agenda": {"estado": "agendado"}})
        chatwoot = _Chatwoot(["interesado"])
        with (
            patch.object(tasks.chat_memory, "get_conversation", return_value=conv),
            patch.object(tasks, "_chatwoot_client_for", return_value=(chatwoot, "6")),
            patch.object(tasks.chat_memory, "recent_history", return_value=[]),
            patch.object(tasks, "classify", return_value=MagicMock(stage="compra", flags=[])),
            patch.object(tasks, "sync_crm_labels"),
            patch.object(tasks.chat_memory, "save_classification"),
        ):
            tasks.classify_and_label_conversation("5")
        self.assertIn("agendado", chatwoot.labels)


if __name__ == "__main__":
    unittest.main()
