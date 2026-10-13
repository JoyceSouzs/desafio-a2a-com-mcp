# A Ponte: um agente A2A com MCP por dentro

Dois processos Python, cada um com as dependências travadas no próprio `pyproject.toml` + `uv.lock`:

| Processo | Pasta | Porta | Endpoints |
|---|---|---|---|
| Servidor MCP `central-de-salas` | `servidor-mcp/` | 7301 | `POST /mcp` (Streamable HTTP, stateless, respostas JSON) |
| Agente `Central de Salas` | `agente/` | 7300 | `GET /.well-known/agent-card.json`, `POST /a2a` (JSON-RPC 2.0) |

Stack: SDK oficial `mcp==2.3.0` (v2, revisão 2026-07-28 da spec) nos dois lados, e `a2a-sdk==1.2.2` (A2A v1.0) no agente. Sem LLM: o agente interpreta o pedido em formato fixo por expressão regular e decide por regra.

## Como rodar

Pré-requisitos: Python 3.10+ e [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh` ou `brew install uv`).

**1. Gere a chave de integridade do `requestState`.** Ela tem 32 bytes aleatórios e nunca vai para o repositório. Use a mesma chave em todas as subidas do servidor MCP: é ela que permite que um `requestState` emitido antes de um restart continue válido depois.

```bash
export REQUEST_STATE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
```

**2. Terminal 1, servidor MCP** (o stderr mostra método, id, traceparent e capabilities de cada request):

```bash
cd servidor-mcp
uv sync
REQUEST_STATE_SECRET=$REQUEST_STATE_SECRET uv run python servidor.py
```

**3. Terminal 2, agente:**

```bash
cd agente
uv sync
uv run python agente.py
```

**4. Terminal 3, validador** (com os dois processos recém-iniciados):

```bash
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

Variáveis opcionais: `MCP_PORT` (padrão 7301), `AGENTE_PORT` (padrão 7300), `MCP_URL` (padrão `http://127.0.0.1:7301/mcp`, para onde o agente aponta) e `AGENTE_URL` (padrão `http://127.0.0.1:7300`, a base anunciada no card).

Sem uv, com pip e as mesmas versões travadas:

```bash
cd servidor-mcp && python3 -m venv .venv && .venv/bin/pip install mcp==2.3.0 uvicorn==0.34.0 && .venv/bin/python servidor.py
cd agente && python3 -m venv .venv && .venv/bin/pip install "a2a-sdk[http-server]==1.2.2" mcp==2.3.0 uvicorn==0.34.0 && .venv/bin/python agente.py
```

## Onde a ponte acontece

A costura está em `agente/agente.py`. O host MCP (`agente/cliente_mcp.py:89`, `HostMCP.chamar`) chama o `tools/call` com `allow_input_required=True`. Com isso, o SDK devolve o `InputRequiredResult` cru em vez de resolver a elicitation por callback. O callback registrado (`_nunca_responder`) existe só para o SDK declarar a capability e nunca é chamado. Em `AgenteDeSalas._tratar` (`agente/agente.py:128`), quando o resultado é uma `Pausa` (o `input_required` do MCP), o agente guarda em `PAUSADAS[task_id]` (`agente/ponte.py`) o `tools/call` original, a chave do `inputRequests`, as opções do enum e o `requestState` opaco. Em seguida, coloca a Task em `TASK_STATE_INPUT_REQUIRED` com a mensagem exata `alternativas: …` (`agente/agente.py:131-134`). O `requestState` volta para o servidor em `AgenteDeSalas._continuacao` (`agente/agente.py:100`). Quando chega um `SendMessage` com `taskId` e `escolha=<id>`, o agente traduz para `{"action":"accept","content":{"sala":…}}`, ou para `{"action":"decline"}` se a escolha for `recusar`. Depois repete o mesmo `tools/call` levando `inputResponses` com a mesma chave e o `requestState` ecoado sem modificação (`agente/agente.py:115-121`). O SDK emite esse retry como um request novo, com um id de JSON-RPC novo. Escolha fora do enum não vai ao MCP: a Task continua pausada e a linha de alternativas se repete.

No servidor, o outro lado da ponte é o resolver `escolha_de_sala` (`servidor-mcp/servidor.py:149`). Ele devolve `Elicit(...)` com um schema plano `sala: enum[alternativas]`, e o SDK transforma isso em `resultType: "input_required"`. O SDK também devolve `-32021` quando o cliente não declarou `elicitation.form`.

## Decisões técnicas

- **Proteção do `requestState`.** Usei o utilitário do próprio SDK, `RequestStateSecurity(keys=[REQUEST_STATE_SECRET], ttl=600, bind_principal=None)` (`servidor-mcp/servidor.py:72`). Ele faz AES-256-GCM (AEAD, com chave derivada via HKDF): o token é cifrado e autenticado. Qualquer alteração de um caractere falha na tag e volta `-32602`. O envelope também é amarrado à tool e a um digest dos argumentos, então um retry que reenvia argumentos diferentes dos selados é rejeitado com `-32602` e a divergência nunca toma efeito. A chave vem só de `REQUEST_STATE_SECRET` (hex de 32 bytes; o processo se recusa a subir com menos), e não existe segredo no código. Não usei o padrão do `MCPServer` (`RequestStateSecurity.ephemeral()`), porque ele gera a chave em memória e invalida o estado a cada restart.
- **Validade.** 600 s (10 minutos), dentro da faixa de 5 a 30 minutos.
- **Servidor sem memória entre rodadas.** Tudo o que o retry precisa viaja no `requestState`. O resolver roda de novo no retry, recalcula as alternativas a partir das reservas e aplica a resposta selada. Testei: um `requestState` emitido antes de reiniciar o servidor MCP conclui a reserva depois do restart.
- **Estado das Tasks.** Fica em memória no agente: as Tasks no `InMemoryTaskStore` do a2a-sdk, e o vínculo Task → `requestState` em um `dict` por `task_id` (`agente/ponte.py`). O vínculo é removido quando a Task chega a um estado terminal. O `requestState` nunca entra em mensagem, artifact, metadata nem no card.
- **Descoberta e contexto.** Toda Task nova faz `tools/list` (o agente confirma que `reservar_sala` existe, sem lista fixa) e `resources/read politica://uso`, de onde sai a versão da primeira linha que vai no campo `politica` do artifact.
- **traceparent.** O trace-id vem do header `traceparent` do `SendMessage` que abre a Task e fica guardado na pausa. Todo request MCP daquela Task, inclusive o retry, leva `00-<mesmo trace-id>-<span-id novo>-01` no `_meta`. Sem header, o agente gera um trace-id para a Task.
- **Histórico da Task.** O a2a-sdk só move a mensagem de status para o `history` no evento seguinte. Para que a mensagem final (por exemplo, `Sala inexistente: …` em `TASK_STATE_FAILED`) apareça no histórico, o agente a publica em `WORKING` e depois no estado terminal (`agente/agente.py:154`).
- **Header `A2A-Version`.** O a2a-sdk 1.2.2 trata a ausência do header como versão 0.3 e recusa os métodos da v1.0. Como este endpoint só fala 1.0 (é o que o card anuncia) e o validador não envia o header, um `ServerCallContextBuilder` (`ContextoV1`, `agente/agente.py:198`) assume `1.0` quando o header falta.

### Limitação do SDK documentada

O enunciado pede que o agente declare `{"elicitation": {"form": {}}}`. O cliente do `mcp==2.3.0` monta essa capability de forma fixa, com form **e** url, sempre que existe um `elicitation_callback`. Trecho de `mcp/client/session.py:634-638`:

```python
elicitation = (
    types.ElicitationCapability(form=types.FormElicitationCapability(), url=types.UrlElicitationCapability())
    if self._elicitation_callback is not _default_elicitation_callback
    else None
)
```

Por isso os requests do agente levam `"io.modelcontextprotocol/clientCapabilities": {"elicitation": {"form": {}, "url": {}}}`, o que aparece no stderr do servidor MCP. A capability de form mode está declarada, que é o que o servidor exige. Não sobrescrevi o método privado do SDK para remover o `url`.

## Saída do validador

Última execução, com os dois processos recém-iniciados:

```
trace-id desta execucao: f71a0683ae2a875f9ac6489bba0ec1b1
procure esse valor no stderr do servidor MCP para conferir a propagacao do traceparent.

PASS 01 tools/list traz as tres tools
PASS 02 toda tool tem inputSchema de objeto
PASS 03 listar_salas devolve structuredContent e o mesmo JSON em texto
PASS 04 _meta sem protocolVersion devolve -32602 e HTTP 400
PASS 05 _meta sem clientCapabilities devolve -32602 e HTTP 400
PASS 06 tool inexistente e recusada, por -32602 ou por isError
PASS 07 resources/read de politica://uso devolve a politica
PASS 08 resources/read de URI inexistente devolve -32602
PASS 09 sala inexistente devolve isError com a mensagem exata
PASS 10 fora da janela devolve isError com a mensagem exata
PASS 11 duracao acima de 2h devolve isError com a mensagem exata
PASS 12 intervalo invertido devolve isError com a mensagem exata
PASS 13 conflito devolve input_required com inputRequests e requestState
PASS 14 a elicitation e form mode e oferece as alternativas na ordem certa
PASS 15 conflito sem a capability elicitation devolve -32021 e HTTP 400
PASS 16 retry com inputResponses e requestState conclui a reserva
PASS 17 requestState adulterado e rejeitado com -32602
PASS 18 argumentos adulterados no retry nao tomam efeito
PASS 19 recusa conclui sem reservar e sem isError
PASS 20 conflito sem alternativa possivel devolve isError com a mensagem exata

PASS 21 agent card responde 200 no well-known com JSON
PASS 22 o card declara a interface JSON-RPC com url e versao 1.0
PASS 23 o card declara a skill reservar-sala
PASS 24 SendMessage com sala livre conclui a Task
PASS 25 o artifact chama reserva e traz a versao da politica
PASS 26 GetTask devolve id, contextId e estado corrente
PASS 27 SendMessage com sala ocupada pausa a Task
PASS 28 a Task pausada lista as alternativas na ordem certa
PASS 29 escolha fora do enum mantem a Task pausada
PASS 30 a continuacao conclui a Task na sala escolhida
PASS 31 SendMessage em Task terminal e recusado
PASS 32 a recusa termina a Task em CANCELED
PASS 33 duas Tasks pausadas ao mesmo tempo concluem cada uma com a sua reserva
PASS 34 nenhuma resposta A2A carrega o requestState
PASS 35 sala inexistente termina a Task em FAILED com a mensagem da tool
PASS 36 o agente e deterministico: o mesmo pedido produz a mesma pausa

resumo: 36 passaram, 0 falharam, de 36 verificacoes
```
