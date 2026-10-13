"""Servidor MCP da Central de Salas, em Streamable HTTP stateless.

Tres tools, um resource e o ciclo de MRTR em reservar_sala. O requestState e
selado pelo proprio SDK (RequestStateSecurity, AES-256-GCM) com a chave de
REQUEST_STATE_SECRET, entao sobrevive a um restart do processo.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Annotated, Any, Literal

import uvicorn
from pydantic import BaseModel, Field, create_model

import dominio
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.server.mcpserver import (
    AcceptedElicitation,
    Elicit,
    ElicitationResult,
    MCPServer,
    RequestStateSecurity,
    Resolve,
)
from mcp.server.mcpserver.exceptions import ToolError

PORTA = int(os.environ.get("MCP_PORT", "7301"))
VALIDADE_DO_ESTADO = 600  # segundos: 10 minutos, dentro da faixa de 5 a 30 pedida pela politica do desafio


def log(mensagem: str) -> None:
    print(mensagem, file=sys.stderr, flush=True)


def segredo() -> bytes:
    bruto = os.environ.get("REQUEST_STATE_SECRET", "")
    try:
        chave = bytes.fromhex(bruto)
    except ValueError:
        chave = bruto.encode()
    if len(chave) < 32:
        raise SystemExit(
            "REQUEST_STATE_SECRET ausente ou com menos de 32 bytes. Gere com:\n"
            '  export REQUEST_STATE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")'
        )
    return chave


class RegistroNoStderr:
    """Registra metodo, id e traceparent de cada request recebido."""

    async def __call__(self, ctx: ServerRequestContext[Any, Any], call_next: CallNext) -> HandlerResult:
        meta = (ctx.params or {}).get("_meta") or {}
        meta = meta if isinstance(meta, dict) else {}
        traceparent = meta.get("traceparent")
        caps = json.dumps(meta.get("io.modelcontextprotocol/clientCapabilities"), separators=(",", ":"))
        nome = (ctx.params or {}).get("name") or (ctx.params or {}).get("uri") or ""
        retry = " retry=sim" if (ctx.params or {}).get("requestState") else ""
        log(
            f"[mcp] method={ctx.method} id={ctx.request_id} name={nome} "
            f"traceparent={traceparent or '-'} capabilities={caps}{retry}"
        )
        return await call_next(ctx)


mcp = MCPServer(
    "central-de-salas",
    version="1.0.0",
    request_state_security=RequestStateSecurity(keys=[segredo()], ttl=VALIDADE_DO_ESTADO, bind_principal=None),
    middleware=[RegistroNoStderr()],
)


# --- contratos de saida -----------------------------------------------------


class SalaOut(BaseModel):
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


class SalasOut(BaseModel):
    salas: list[SalaOut]


class ReservaExistente(BaseModel):
    id: str
    sala: str
    inicio: str
    fim: str
    responsavel: str


class DisponibilidadeOut(BaseModel):
    sala: str
    inicio: str
    fim: str
    livre: bool
    conflitos: list[ReservaExistente]


class ReservaOut(BaseModel):
    reserva: str | None = None
    reservado: bool
    sala: str | None = None
    inicio: str | None = None
    fim: str | None = None
    responsavel: str | None = None
    politica: str | None = None
    motivo: str | None = None


class EscolhaDeSala(BaseModel):
    sala: str


# --- tools ------------------------------------------------------------------


def _validar(sala: str, inicio: str, fim: str):
    try:
        return dominio.validar(sala, inicio, fim)
    except dominio.ErroDeRegra as e:
        raise ToolError(str(e)) from e


@mcp.tool()
def listar_salas() -> SalasOut:
    """Lista todas as salas com capacidade e recursos."""
    return SalasOut(salas=[SalaOut(**s) for s in dominio.SALAS])


@mcp.tool()
def consultar_disponibilidade(sala: str, inicio: str, fim: str) -> DisponibilidadeOut:
    """Informa se a sala esta livre no intervalo e, se nao estiver, as reservas em conflito."""
    ini, fi = _validar(sala, inicio, fim)
    conflitos = dominio.conflitos(sala, ini, fi)
    return DisponibilidadeOut(
        sala=sala, inicio=inicio, fim=fim, livre=not conflitos,
        conflitos=[ReservaExistente(**r) for r in conflitos],
    )


def escolha_de_sala(sala: str, inicio: str, fim: str) -> Elicit[EscolhaDeSala] | None:
    """Pergunta a sala alternativa quando a pedida esta ocupada.

    Roda de novo a cada rodada: as alternativas sao recalculadas aqui, e o SDK so
    reaproveita a resposta do requestState se a pergunta sair identica.
    """
    ini, fi = _validar(sala, inicio, fim)
    if not dominio.conflitos(sala, ini, fi):
        return None
    alternativas = dominio.alternativas(sala, ini, fi)
    if not alternativas:
        raise ToolError("Sem alternativas disponiveis no intervalo")
    esquema = create_model(
        "EscolhaDeSala",
        __base__=EscolhaDeSala,
        sala=(Literal[tuple(alternativas)], Field(title="Sala", description="Sala alternativa escolhida")),
    )
    return Elicit("A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa.", esquema)


def _reservar(sala: str, inicio: str, fim: str, responsavel: str) -> ReservaOut:
    ini, fi = _validar(sala, inicio, fim)
    if dominio.conflitos(sala, ini, fi):
        raise ToolError("Sem alternativas disponiveis no intervalo")
    r = dominio.criar_reserva(sala, inicio, fim, responsavel)
    log(f"[mcp] reserva criada {r['id']} sala={sala} {inicio}..{fim} responsavel={responsavel}")
    return ReservaOut(
        reserva=r["id"], reservado=True, sala=sala, inicio=inicio, fim=fim,
        responsavel=responsavel, politica=dominio.POLITICA_VERSAO,
    )


@mcp.tool()
def reservar_sala(
    sala: str,
    inicio: str,
    fim: str,
    responsavel: str,
    # Sem `| None` na anotacao: o SDK so entrega o desfecho completo (accept, decline,
    # cancel) quando o tipo e exatamente ElicitationResult. Sem conflito, o resolver
    # devolve None e o SDK o entrega como AcceptedElicitation(data=None).
    escolha: Annotated[ElicitationResult[EscolhaDeSala], Resolve(escolha_de_sala)],
) -> ReservaOut:
    """Reserva a sala no intervalo. Se estiver ocupada, pergunta qual sala alternativa usar."""
    if escolha is None or (isinstance(escolha, AcceptedElicitation) and escolha.data is None):
        return _reservar(sala, inicio, fim, responsavel)
    if isinstance(escolha, AcceptedElicitation):
        return _reservar(escolha.data.sala, inicio, fim, responsavel)
    return ReservaOut(reservado=False, motivo="recusado")


# --- resource ---------------------------------------------------------------


@mcp.resource("politica://uso", name="politica-de-uso", mime_type="text/markdown")
def politica_de_uso() -> str:
    """Politica de uso das salas. A primeira linha declara a versao."""
    return dominio.POLITICA_TEXTO


def main() -> None:
    app = mcp.streamable_http_app(stateless_http=True, json_response=True)
    log(f"[mcp] central-de-salas em http://127.0.0.1:{PORTA}/mcp (politica {dominio.POLITICA_VERSAO})")
    uvicorn.run(app, host="127.0.0.1", port=PORTA, log_level="warning")


if __name__ == "__main__":
    main()
