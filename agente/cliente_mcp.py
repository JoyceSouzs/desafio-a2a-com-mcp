"""O agente como host MCP: um cliente do SDK oficial falando HTTP com o servidor de salas.

O cliente fica vivo entre chamadas (isso e processo, nao sessao de protocolo): cada
request carrega no _meta a versao, as capabilities e o traceparent, e o SDK
espelha MCP-Protocol-Version, Mcp-Method e Mcp-Name nos headers.

O ponto central e `allow_input_required=True`: o SDK devolve o InputRequiredResult
cru em vez de resolver a elicitation sozinho. Quem responde a pergunta e o cliente
A2A, la fora, e nao este processo.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from typing import Any

import mcp_types as types
from mcp.client import Client

PROTOCOLO = "2026-07-28"
MCP_URL = os.environ.get("MCP_URL", "http://127.0.0.1:7301/mcp")
POLITICA_URI = "politica://uso"


def traceparent(trace_id: str) -> str:
    """Mesmo trace-id da Task, span-id novo a cada request MCP."""
    return f"00-{trace_id}-{secrets.token_hex(8)}-01"


async def _nunca_responder(*_: Any) -> Any:
    # Existe so para o SDK declarar a capability de elicitation. Como toda chamada
    # usa allow_input_required=True, a pergunta nunca e respondida aqui dentro.
    raise RuntimeError("a elicitation e devolvida ao cliente A2A, nunca respondida pelo agente")


@dataclass
class Pausa:
    """O pedido de informacao do servidor, como chegou: chave, schema e estado opaco."""

    chave: str
    opcoes: list[str]
    request_state: str


@dataclass
class Conclusao:
    erro: bool
    texto: str
    dados: dict[str, Any] | None


class HostMCP:
    def __init__(self) -> None:
        self._cliente = Client(
            MCP_URL,
            mode=PROTOCOLO,
            client_info=types.Implementation(name="agente-central-de-salas", version="1.0.0"),
            elicitation_callback=_nunca_responder,
            cache=None,
        )
        self._aberto = False

    async def _sessao(self):
        if not self._aberto:
            await self._cliente.__aenter__()
            self._aberto = True
        return self._cliente.session

    async def fechar(self) -> None:
        if self._aberto:
            await self._cliente.__aexit__(None, None, None)
            self._aberto = False

    async def descobrir_tools(self, trace_id: str) -> set[str]:
        sessao = await self._sessao()
        params = types.PaginatedRequestParams(_meta={"traceparent": traceparent(trace_id)})
        resultado = await sessao.list_tools(params=params)
        return {t.name for t in resultado.tools}

    async def versao_da_politica(self, trace_id: str) -> str:
        sessao = await self._sessao()
        resultado = await sessao.read_resource(POLITICA_URI, meta={"traceparent": traceparent(trace_id)})
        texto = next((c.text for c in resultado.contents if getattr(c, "text", None)), "")
        primeira = texto.splitlines()[0] if texto else ""
        return primeira.split(":", 1)[1].strip() if ":" in primeira else ""

    async def chamar(
        self,
        tool: str,
        argumentos: dict[str, Any],
        trace_id: str,
        *,
        input_responses: dict[str, Any] | None = None,
        request_state: str | None = None,
    ) -> Pausa | Conclusao:
        """Um tools/call. No retry, o SDK emite um request novo, com id novo."""
        sessao = await self._sessao()
        resultado = await sessao.call_tool(
            tool,
            argumentos,
            input_responses=input_responses,
            request_state=request_state,
            meta={"traceparent": traceparent(trace_id)},
            allow_input_required=True,
        )
        if isinstance(resultado, types.InputRequiredResult):
            return _pausa(resultado)
        texto = " ".join(getattr(c, "text", "") for c in resultado.content)
        return Conclusao(erro=bool(resultado.is_error), texto=texto, dados=resultado.structured_content)


def _pausa(resultado: types.InputRequiredResult) -> Pausa:
    pedidos = resultado.input_requests or {}
    chave, pedido = next(iter(pedidos.items()))
    esquema = pedido.params.requested_schema
    esquema = esquema if isinstance(esquema, dict) else esquema.model_dump(by_alias=True, exclude_none=True)
    campo = (esquema.get("properties") or {}).get("sala") or {}
    opcoes = list(campo.get("enum") or ([campo["const"]] if "const" in campo else []))
    return Pausa(chave=chave, opcoes=opcoes, request_state=resultado.request_state or "")
