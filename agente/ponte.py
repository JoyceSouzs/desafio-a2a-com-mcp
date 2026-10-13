"""O estado que atravessa a fronteira: o que o agente guarda de uma Task pausada.

Cada Task em TASK_STATE_INPUT_REQUIRED tem aqui o tools/call original e a Pausa
devolvida pelo servidor MCP (chave da elicitation, opcoes e requestState). O
requestState e opaco: e guardado e ecoado byte a byte, nunca aberto, e nunca sai
daqui para o cliente A2A.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cliente_mcp import Pausa


@dataclass
class TaskPausada:
    tool: str
    argumentos: dict[str, Any]
    trace_id: str
    politica: str
    pausa: Pausa

    def linha_de_alternativas(self) -> str:
        return "alternativas: " + ", ".join(self.pausa.opcoes)


PAUSADAS: dict[str, TaskPausada] = {}
