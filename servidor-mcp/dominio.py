"""Regras de sala da Central de Salas: dados do starter, politica e alternativas.

As reservas vivem em memoria: as do arquivo carregadas na subida e as criadas
durante a execucao. Nada aqui sabe de protocolo.
"""

from __future__ import annotations

import json
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

DADOS = Path(__file__).resolve().parent.parent / "dados"
SAO_PAULO = timezone(timedelta(hours=-3))
JANELA_INICIO = time(8, 0)
JANELA_FIM = time(20, 0)
DURACAO_MAXIMA = timedelta(hours=2)
MAX_ALTERNATIVAS = 3


class ErroDeRegra(Exception):
    """Violacao de regra de uso, com a mensagem exata que vai para o cliente."""


SALAS: list[dict] = json.loads((DADOS / "salas.json").read_text(encoding="utf-8"))
SALAS_POR_ID = {s["id"]: s for s in SALAS}
RESERVAS: list[dict] = json.loads((DADOS / "reservas.json").read_text(encoding="utf-8"))
POLITICA_TEXTO = (DADOS / "politica-de-uso.md").read_text(encoding="utf-8")
POLITICA_VERSAO = POLITICA_TEXTO.splitlines()[0].split(":", 1)[1].strip()


def _instante(valor: str) -> datetime:
    try:
        dt = datetime.fromisoformat(valor)
    except ValueError as e:
        raise ErroDeRegra(f"Data invalida: {valor}") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=SAO_PAULO)
    return dt


def validar(sala: str, inicio: str, fim: str) -> tuple[datetime, datetime]:
    """Aplica as regras na ordem: sala, intervalo, janela, duracao."""
    if sala not in SALAS_POR_ID:
        raise ErroDeRegra(f"Sala inexistente: {sala}")
    ini, fi = _instante(inicio), _instante(fim)
    if fi <= ini:
        raise ErroDeRegra("Intervalo invalido: fim deve ser posterior a inicio")
    ini_sp, fim_sp = ini.astimezone(SAO_PAULO), fi.astimezone(SAO_PAULO)
    if (
        ini_sp.date() != fim_sp.date()
        or ini_sp.time() < JANELA_INICIO
        or fim_sp.time() > JANELA_FIM
    ):
        raise ErroDeRegra("Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00")
    if fi - ini > DURACAO_MAXIMA:
        raise ErroDeRegra("Duracao acima do limite: a politica permite no maximo 2 horas")
    return ini, fi


def conflitos(sala: str, ini: datetime, fim: datetime) -> list[dict]:
    return [
        r for r in RESERVAS
        if r["sala"] == sala and _instante(r["inicio"]) < fim and ini < _instante(r["fim"])
    ]


def alternativas(sala: str, ini: datetime, fim: datetime) -> list[str]:
    """Salas livres no intervalo com capacidade >= a pedida, por (capacidade, id), no maximo tres."""
    minima = SALAS_POR_ID[sala]["capacidade"]
    livres = [
        s for s in SALAS
        if s["id"] != sala and s["capacidade"] >= minima and not conflitos(s["id"], ini, fim)
    ]
    livres.sort(key=lambda s: (s["capacidade"], s["id"]))
    return [s["id"] for s in livres[:MAX_ALTERNATIVAS]]


def criar_reserva(sala: str, inicio: str, fim: str, responsavel: str) -> dict:
    numero = max((int(r["id"].split("-")[1]) for r in RESERVAS), default=0) + 1
    reserva = {"id": f"res-{numero:04d}", "sala": sala, "inicio": inicio, "fim": fim, "responsavel": responsavel}
    RESERVAS.append(reserva)
    return reserva
