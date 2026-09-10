"""Importa referências documentais; não modifica fichas, ordens nem estoque."""

import json
import re
from pathlib import Path

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.extensions import db
from app.models import ProducaoDiarioReferencia, Usuario
from app.utils import agora

MANIFESTO_PATH = Path(__file__).resolve().parents[1] / 'seeds_data' / 'producao_sourdough_registros.json'
CLASSIFICACOES = {'transcrito', 'calculado', 'extrapolado', 'informado', 'a_confirmar'}


class ReferenciaError(ValueError):
    """Manifesto inválido ou importador não identificado."""


def _texto(valor, limite, nome):
    if not isinstance(valor, str) or not valor.strip() or len(valor) > limite:
        raise ReferenciaError(f'{nome} inválido no manifesto de referências.')
    # Não normalizar: produto e fonte são transcrições literais.
    return valor


def _constante_invalida(valor):
    raise ReferenciaError(f'Número não finito no manifesto: {valor}.')


def _linhas_manifesto(usuario_id):
    try:
        manifesto = json.loads(MANIFESTO_PATH.read_text(encoding='utf-8'),
                               parse_constant=_constante_invalida)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReferenciaError('Não foi possível ler o manifesto de referências.') from exc
    if not isinstance(manifesto, dict):
        raise ReferenciaError('Estrutura inválida no manifesto de referências.')
    arquivo = _texto(manifesto.get('arquivo'), 255, 'Arquivo de origem')
    aba = _texto(manifesto.get('aba'), 100, 'Aba de origem')
    sha = manifesto.get('sha256')
    if not isinstance(sha, str) or re.fullmatch(r'[0-9a-fA-F]{64}', sha) is None:
        raise ReferenciaError('SHA-256 inválido no manifesto de referências.')
    sha = sha.lower()
    registros = manifesto.get('registros')
    if not isinstance(registros, list):
        raise ReferenciaError('Registros inválidos no manifesto de referências.')
    linhas, vistos = [], set()
    importado_em = agora()
    for registro in registros:
        if not isinstance(registro, dict):
            raise ReferenciaError('Registro inválido no manifesto de referências.')
        linha = registro.get('linha')
        if not isinstance(linha, int) or isinstance(linha, bool) or not 0 < linha <= 2147483647:
            raise ReferenciaError('Linha de origem inválida no manifesto de referências.')
        if linha in vistos:
            raise ReferenciaError('O manifesto repete uma linha da planilha de origem.')
        vistos.add(linha)
        produto = _texto(registro.get('produto'), 200, 'Produto')
        valores = registro.get('valores')
        classificacao = registro.get('classificacao')
        if not isinstance(valores, dict) or not isinstance(classificacao, dict):
            raise ReferenciaError('Valores ou classificação inválidos no manifesto de referências.')
        if any(not isinstance(valor, str) or valor not in CLASSIFICACOES
               for valor in classificacao.values()):
            raise ReferenciaError('Classificação desconhecida no manifesto de referências.')
        if any(chave not in classificacao for chave, valor in valores.items() if valor is not None):
            raise ReferenciaError('Uma medição do manifesto não informa sua classificação.')
        linhas.append(dict(
            chave=f'{sha}:{aba}:{linha}', produto=produto, fonte_arquivo=arquivo,
            fonte_sha256=sha, fonte_aba=aba, fonte_linha=linha, dados=registro,
            importado_por_id=usuario_id, importado_em=importado_em,
        ))
    return linhas


def importar_referencias(usuario_id):
    """Importa cada linha uma vez; devolve somente a quantidade de novas linhas.

    A restrição UNIQUE e ON CONFLICT valem também entre workers distintos.
    Nenhuma referência já importada é sobrescrita ou reatribuída ao operador.
    """
    try:
        if (not isinstance(usuario_id, int) or isinstance(usuario_id, bool)
                or usuario_id <= 0 or db.session.get(Usuario, usuario_id) is None):
            raise ReferenciaError('Informe um usuário existente para identificar a importação.')
        linhas = _linhas_manifesto(usuario_id)
        if not linhas:
            return 0
        dialect = db.session.get_bind().dialect.name
        if dialect == 'postgresql':
            insert = pg_insert
        elif dialect == 'sqlite':
            insert = sqlite_insert
        else:
            raise ReferenciaError('Banco não suportado pela importação de referências.')
        comando = (insert(ProducaoDiarioReferencia).values(linhas)
                   .on_conflict_do_nothing(index_elements=['chave'])
                   .returning(ProducaoDiarioReferencia.id))
        quantidade = len(db.session.execute(comando).scalars().all())
        db.session.commit()
        return quantidade
    except Exception:
        db.session.rollback()
        raise


def listar_referencias():
    return (ProducaoDiarioReferencia.query
            .order_by(ProducaoDiarioReferencia.importado_em, ProducaoDiarioReferencia.id).all())


def contar_pendentes():
    """Conta linhas do manifesto atual que ainda não possuem referência salva."""
    chaves = {linha['chave'] for linha in _linhas_manifesto(usuario_id=None)}
    if not chaves:
        return 0
    existentes = (ProducaoDiarioReferencia.query
                  .filter(ProducaoDiarioReferencia.chave.in_(chaves)).count())
    return len(chaves) - existentes
