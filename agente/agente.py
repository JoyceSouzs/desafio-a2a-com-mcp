"""Agente A2A da Central de Salas: servidor A2A por fora, host MCP por dentro.

Sem LLM. O pedido chega em formato fixo e e decidido por regra; regra de sala
(conflito, politica, alternativas) e decisao do servidor MCP, nao daqui.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import sys

import uvicorn
from a2a.helpers.proto_helpers import new_task_from_user_message
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.context import ServerCallContext
from a2a.server.routes import DefaultServerCallContextBuilder, create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentProvider,
    AgentSkill,
    Part,
    TaskState,
)
from a2a.utils.constants import VERSION_HEADER
from starlette.applications import Starlette
from starlette.requests import Request

from cliente_mcp import Conclusao, HostMCP, Pausa
from ponte import PAUSADAS, TaskPausada

PORTA = int(os.environ.get("AGENTE_PORT", "7300"))
URL_PUBLICA = os.environ.get("AGENTE_URL", f"http://127.0.0.1:{PORTA}")
TOOL_DE_RESERVA = "reservar_sala"
CAMPOS_DO_ARTIFACT = ("reserva", "sala", "inicio", "fim", "responsavel", "politica")

PEDIDO = re.compile(
    r"^\s*reservar\s+sala=(?P<sala>\S+)\s+inicio=(?P<inicio>\S+)\s+fim=(?P<fim>\S+)\s+responsavel=(?P<responsavel>.+?)\s*$"
)
ESCOLHA = re.compile(r"^\s*escolha=(?P<valor>\S+)\s*$")
TRACEPARENT = re.compile(r"^[0-9a-f]{2}-(?P<trace>[0-9a-f]{32})-[0-9a-f]{16}-[0-9a-f]{2}$")


def log(mensagem: str) -> None:
    print(mensagem, file=sys.stderr, flush=True)


def trace_id_do_header(context: RequestContext) -> str | None:
    cabecalhos = (context.call_context.state or {}).get("headers") or {}
    valor = (cabecalhos.get("traceparent") or "").strip().lower()
    achado = TRACEPARENT.match(valor)
    return achado.group("trace") if achado else None


class AgenteDeSalas(AgentExecutor):
    def __init__(self, host: HostMCP) -> None:
        self.host = host

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        texto = context.get_user_input().strip()
        tarefa = context.current_task
        if tarefa is None:
            tarefa = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(tarefa)
        updater = TaskUpdater(event_queue, tarefa.id, tarefa.context_id)

        pausada = PAUSADAS.get(tarefa.id)
        if pausada is None:
            await self._novo_pedido(texto, context, updater)
        else:
            await self._continuacao(texto, pausada, updater)

    async def _novo_pedido(self, texto: str, context: RequestContext, updater: TaskUpdater) -> None:
        await updater.start_work()
        pedido = PEDIDO.match(texto)
        if not pedido:
            await self._encerrar(updater, "failed", "Pedido invalido: use reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>")
            return
        trace_id = trace_id_do_header(context) or secrets.token_hex(16)

        # Descoberta em runtime: nada de lista fixa de tools no codigo.
        tools = await self.host.descobrir_tools(trace_id)
        if TOOL_DE_RESERVA not in tools:
            await self._encerrar(updater, "failed", f"O servidor MCP nao oferece a tool {TOOL_DE_RESERVA}")
            return
        politica = await self.host.versao_da_politica(trace_id)

        argumentos = pedido.groupdict()
        resultado = await self.host.chamar(TOOL_DE_RESERVA, argumentos, trace_id)
        await self._tratar(resultado, updater, TOOL_DE_RESERVA, argumentos, trace_id, politica)

    async def _continuacao(self, texto: str, pausada: TaskPausada, updater: TaskUpdater) -> None:
        escolha = ESCOLHA.match(texto)
        valor = escolha.group("valor") if escolha else None
        if valor == "recusar":
            resposta = {"action": "decline"}
        elif valor in pausada.pausa.opcoes:
            resposta = {"action": "accept", "content": {"sala": valor}}
        else:
            # Fora do enum: a Task continua pausada e a pergunta se repete, sem ir ao MCP.
            await updater.requires_input(self._mensagem(updater, pausada.linha_de_alternativas()))
            return

        await updater.start_work()
        # O retry: mesmo tools/call, id novo, inputResponses com a chave que veio e
        # o requestState ecoado sem modificacao.
        resultado = await self.host.chamar(
            pausada.tool,
            pausada.argumentos,
            pausada.trace_id,
            input_responses={pausada.pausa.chave: resposta},
            request_state=pausada.pausa.request_state,
        )
        if valor == "recusar" and isinstance(resultado, Conclusao) and not resultado.erro:
            PAUSADAS.pop(updater.task_id, None)
            await self._encerrar(updater, "cancel", "Reserva recusada: nenhuma sala foi reservada.")
            return
        await self._tratar(resultado, updater, pausada.tool, pausada.argumentos, pausada.trace_id, pausada.politica)

    async def _tratar(self, resultado: Pausa | Conclusao, updater: TaskUpdater, tool: str,
                      argumentos: dict, trace_id: str, politica: str) -> None:
        """A ponte: input_required do MCP vira TASK_STATE_INPUT_REQUIRED no A2A."""
        if isinstance(resultado, Pausa):
            pausada = TaskPausada(tool, argumentos, trace_id, politica, resultado)
            PAUSADAS[updater.task_id] = pausada
            await updater.requires_input(self._mensagem(updater, pausada.linha_de_alternativas()))
            return

        PAUSADAS.pop(updater.task_id, None)
        if resultado.erro:
            await self._encerrar(updater, "failed", resultado.texto)
            return
        dados = dict(resultado.dados or {})
        if not dados.get("reservado"):
            await self._encerrar(updater, "cancel", "Reserva nao realizada: " + str(dados.get("motivo") or "recusada"))
            return
        dados["politica"] = politica
        reserva = {campo: dados.get(campo) for campo in CAMPOS_DO_ARTIFACT}
        await updater.add_artifact([Part(text=json.dumps(reserva, ensure_ascii=False))], name="reserva")
        await self._encerrar(updater, "complete", f"Reserva {reserva['reserva']} confirmada na {reserva['sala']}.")

    @staticmethod
    def _mensagem(updater: TaskUpdater, texto: str):
        return updater.new_agent_message([Part(text=texto)])

    async def _encerrar(self, updater: TaskUpdater, como: str, texto: str) -> None:
        mensagem = self._mensagem(updater, texto)
        # O a2a-sdk so move a mensagem de status para o history no evento seguinte, e
        # depois de um estado terminal nao ha evento seguinte. Publicar a mesma mensagem
        # antes, ainda em WORKING, deixa a mensagem final da tool visivel no history.
        await updater.update_status(TaskState.TASK_STATE_WORKING, message=mensagem)
        await {"complete": updater.complete, "failed": updater.failed, "cancel": updater.cancel}[como](mensagem)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        if context.task_id:
            PAUSADAS.pop(context.task_id, None)
            tarefa = context.current_task
            await TaskUpdater(event_queue, context.task_id, tarefa.context_id if tarefa else "").cancel()


def agent_card() -> AgentCard:
    return AgentCard(
        name="Central de Salas",
        description="Reserva salas de reuniao da Hill Valley Tech.",
        provider=AgentProvider(organization="Hill Valley Tech", url="https://hillvalley.example"),
        version="1.0.0",
        supported_interfaces=[
            AgentInterface(url=f"{URL_PUBLICA}/a2a", protocol_binding="JSONRPC", protocol_version="1.0"),
        ],
        capabilities=AgentCapabilities(streaming=False, push_notifications=False, extended_agent_card=False),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        skills=[
            AgentSkill(
                id="reservar-sala",
                name="Reservar sala",
                description="Reserva uma sala em um intervalo. Se houver conflito, pergunta qual alternativa usar.",
                tags=["salas", "agenda"],
                input_modes=["text/plain"],
                output_modes=["text/plain"],
                examples=[
                    "reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 "
                    "fim=2026-11-03T15:00:00-03:00 responsavel=Marty"
                ],
            )
        ],
    )


class ContextoV1(DefaultServerCallContextBuilder):
    """Assume A2A-Version 1.0 quando o cliente nao manda o header.

    O a2a-sdk interpreta header ausente como 0.3 e recusa o SendMessage da v1.0.
    Este endpoint so fala 1.0 (e o que o card anuncia), e o validador nao envia o header.
    """

    def build(self, request: Request) -> ServerCallContext:
        contexto = super().build(request)
        contexto.state["headers"].setdefault(VERSION_HEADER.lower(), "1.0")
        return contexto


def criar_app() -> Starlette:
    host = HostMCP()
    card = agent_card()
    handler = DefaultRequestHandler(agent_executor=AgenteDeSalas(host), task_store=InMemoryTaskStore(), agent_card=card)
    rotas = create_agent_card_routes(card) + create_jsonrpc_routes(handler, rpc_url="/a2a", context_builder=ContextoV1())

    @contextlib.asynccontextmanager
    async def ciclo_de_vida(_app: Starlette):
        yield
        await host.fechar()

    return Starlette(routes=rotas, lifespan=ciclo_de_vida)


def main() -> None:
    log(f"[agente] Central de Salas em {URL_PUBLICA}/a2a (card em /.well-known/agent-card.json)")
    uvicorn.run(criar_app(), host="127.0.0.1", port=PORTA, log_level="warning")


if __name__ == "__main__":
    main()
