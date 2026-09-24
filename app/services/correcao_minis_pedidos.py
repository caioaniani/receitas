"""Correção pontual e reversível dos seis pedidos conferidos em 24/09/2026.

Não altera o corte normal dos pedidos, a regra do catálogo ou o estoque.
A lista fechada evita que uma correção antiga alcance encomendas novas.
Prévia é somente leitura; executar e desfazer exigem owner e o snapshot
que foi mostrado na tela. O histórico completo fica no AppConfig e no
audit_log das alterações ORM.
"""
import hashlib
import hmac
import json
from copy import deepcopy
from datetime import date, datetime

from sqlalchemy import Date, DateTime, inspect, or_, text
from sqlalchemy.orm import selectinload

from app.extensions import db
from app.models import (
    AppConfig,
    FotoRecebimento,
    MovEstoqueLoja,
    MovEstoqueProducao,
    MovimentacaoEstoque,
    PedidoItem,
    PedidoItemFoto,
    PedidoLoja,
    Receita,
    Usuario,
)
from app.services.pedido_lock import travar_pedidos_lojas
from app.utils import agora, hoje

MARCADOR = 'correcao_minis_pedidos_20260924_v1'
# Lock transacional em namespace próprio; não compartilha a família dos crons.
_LOCK_NAMESPACE = 4733
_LOCK_OPERACAO = 20260924
_ALVOS = {
    719: (2, date(2026, 9, 27)),
    712: (4, date(2026, 9, 27)),
    711: (4, date(2026, 9, 26)),
    705: (3, date(2026, 9, 27)),
    704: (3, date(2026, 9, 26)),
    703: (3, date(2026, 9, 25)),
}
_RECEITA_ID = 87
_NOME = 'Mini Danish de Calabresa'
_MOTIVO = ('Correção autorizada pelo owner em 24/09/2026: removidas 2 unidades '
           'de Mini Danish de Calabresa da reposição da loja. Minis somente '
           'na encomenda para o dia da entrega.')


class CorrecaoMinisError(ValueError):
    """A prévia perdeu validade ou o pedido não pode mais ser corrigido."""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'))


def _hash(value):
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def _colunas(obj):
    values = {}
    for col in inspect(type(obj)).columns:
        value = getattr(obj, col.key)
        if isinstance(value, (datetime, date)):
            value = value.isoformat()
        values[col.key] = value
    return values


def _valores_modelo(model, values):
    out = dict(values)
    for col in inspect(model).columns:
        value = out.get(col.key)
        if value is not None and isinstance(col.type, DateTime):
            out[col.key] = datetime.fromisoformat(value)
        elif value is not None and isinstance(col.type, Date):
            out[col.key] = date.fromisoformat(value)
    return out


def _ler_registro():
    row = (AppConfig.query.populate_existing()
           .filter_by(key=MARCADOR).first())
    if row is None:
        return None
    try:
        value = json.loads(row.value)
        if (not isinstance(value, dict) or value.get('versao') != 1
                or value.get('estado') not in ('aplicada', 'restaurada')
                or not all(isinstance(value.get(k), list)
                           for k in ('antes', 'depois', 'pedidos'))
                or {p['pedido']['id'] for p in value['antes']} != set(_ALVOS)
                or {p['pedido']['id'] for p in value['depois']} != set(_ALVOS)
                or value.get('fingerprint_antes') != _hash(value['antes'])
                or value.get('fingerprint_depois') != _hash(value['depois'])):
            raise ValueError('snapshot inválido')
    except (TypeError, ValueError, KeyError) as exc:
        raise CorrecaoMinisError(
            'O registro da correção está inválido. Nenhum pedido foi alterado.') from exc
    return value


def _travar():
    if db.engine.dialect.name == 'postgresql':
        db.session.execute(text('SELECT pg_advisory_xact_lock(:ns, :op)'),
                           {'ns': _LOCK_NAMESPACE, 'op': _LOCK_OPERACAO})
    travar_pedidos_lojas(loja for loja, _ in _ALVOS.values())


def _owner(user_id):
    user = (Usuario.query.populate_existing().filter_by(id=user_id).first())
    if user is None or not user.is_owner:
        raise CorrecaoMinisError('Somente o owner pode executar esta correção.')
    return user


def _tem_movimento(pedido_id):
    # Espaço ou fim após o ID evita confundir, por exemplo, #703 com #7030.
    for model in (MovEstoqueProducao, MovEstoqueLoja, MovimentacaoEstoque):
        ref = model.referencia
        if model.query.filter(or_(
                ref.ilike(f'%pedido #{pedido_id}'),
                ref.ilike(f'%pedido #{pedido_id} %'),
                ref.ilike(f'%pedido #{pedido_id}(%'))).first() is not None:
            return True
    return False


def _carregar(*, bloquear=False):
    query = (PedidoLoja.query.populate_existing()
             .filter(PedidoLoja.id.in_(_ALVOS))
             .options(selectinload(PedidoLoja.itens),
                      selectinload(PedidoLoja.qrcodes),
                      selectinload(PedidoLoja.loja))
             .order_by(PedidoLoja.id))
    if bloquear:
        query = query.with_for_update(of=PedidoLoja)
    rows = query.all()
    if {p.id for p in rows} != set(_ALVOS):
        raise CorrecaoMinisError('A lista de pedidos mudou. Refaça a conferência.')
    if bloquear:
        # Também serializa caminhos antigos que alteram itens sem tomar o
        # advisory lock da loja. Pais e itens seguem sempre ordem crescente.
        (PedidoItem.query.populate_existing()
         .filter(PedidoItem.pedido_id.in_(_ALVOS))
         .order_by(PedidoItem.id).with_for_update(of=PedidoItem).all())
        for pedido in rows:
            db.session.expire(pedido, ['itens'])
    rec = (Receita.query.populate_existing().filter_by(id=_RECEITA_ID).first())
    if (rec is None or rec.nome != _NOME
            or rec.categoria != 'Mini Pães'):
        raise CorrecaoMinisError('O cadastro do mini mudou. Refaça a conferência.')
    for pedido in rows:
        loja, dia = _ALVOS[pedido.id]
        if (pedido.loja_id != loja or pedido.data_entrega != dia
                or pedido.status != 'confirmado'):
            raise CorrecaoMinisError(
                f'O pedido #{pedido.id} mudou de loja, data ou situação.')
        if (pedido.driver_id is not None or pedido.tiny_nota_fiscal_id
                or pedido.nf_status or pedido.nf_emitida_em or pedido.nf_numero
                or any(q.usado_em is not None for q in pedido.qrcodes)
                or any(i.quantidade_recebida is not None for i in pedido.itens)):
            raise CorrecaoMinisError(
                f'O pedido #{pedido.id} já avançou na entrega ou emissão de nota.')
        item_ids = [i.id for i in pedido.itens]
        if (FotoRecebimento.query.filter_by(pedido_id=pedido.id).first()
                or PedidoItemFoto.query.filter(
                    PedidoItemFoto.pedido_item_id.in_(item_ids)).first()
                or _tem_movimento(pedido.id)):
            raise CorrecaoMinisError(
                f'O pedido #{pedido.id} tem conferência ou movimento de estoque.')
    return rows


def _snapshot(rows):
    return [{
        'pedido': _colunas(p),
        'itens': [_colunas(i) for i in sorted(p.itens, key=lambda i: i.id)],
        # Tokens não são necessários na auditoria; somente identidade e estado.
        'qrcodes': [{'id': q.id, 'tipo': q.tipo,
                     'usado_em': q.usado_em.isoformat() if q.usado_em else None}
                    for q in sorted(p.qrcodes, key=lambda q: q.id)],
    } for p in rows]


def _itens_alvo(rows):
    itens = []
    for p in rows:
        candidatos = [i for i in p.itens if i.receita_id == _RECEITA_ID]
        if (len(candidatos) != 1 or candidatos[0].quantidade != 2
                or candidatos[0].produto_id is not None
                or candidatos[0].materia_prima_id is not None
                or len(p.itens) <= 1):
            raise CorrecaoMinisError(
                f'A composição do pedido #{p.id} mudou. Refaça a conferência.')
        itens.append(candidatos[0])
    return itens


def _resumo(rows):
    return [{'id': p.id, 'loja': p.loja.nome,
             'data_entrega': p.data_entrega.isoformat(), 'item': _NOME,
             'quantidade': 2} for p in rows]


def _resultado(estado, snapshot, pedidos):
    return {'estado': estado, 'fingerprint': _hash(snapshot),
            'pedidos': pedidos, 'total_unidades': 2 * len(pedidos)}


def _validar_fingerprint(fingerprint, *permitidos):
    if (not isinstance(fingerprint, str) or len(fingerprint) != 64
            or any(c not in '0123456789abcdef' for c in fingerprint)
            or not any(
                hmac.compare_digest(fingerprint, p) for p in permitidos)):
        raise CorrecaoMinisError(
            'Os pedidos mudaram depois da prévia. Atualize a página e confira novamente.')


def prever_correcao_minis():
    """Lê a prévia ou o resultado registrado, sem gravar configurações."""
    registro = _ler_registro()
    rows = _carregar()
    snapshot = _snapshot(rows)
    if registro:
        esperado = registro['depois' if registro['estado'] == 'aplicada' else 'antes']
        if snapshot != esperado:
            raise CorrecaoMinisError(
                'Os pedidos foram alterados após esta operação. Refaça a conferência.')
        return _resultado(registro['estado'], snapshot, registro['pedidos'])
    if hoje() > date(2026, 9, 25):
        raise CorrecaoMinisError('O prazo desta correção pontual terminou.')
    _itens_alvo(rows)
    return _resultado('disponivel', snapshot, _resumo(rows))


def aplicar_correcao_minis(user_id, fingerprint):
    """Remove somente as seis linhas verificadas; tudo ou nada e auditado."""
    anterior = db.session.info.get('audit_user_id')
    try:
        _travar()
        user = _owner(user_id)
        registro = _ler_registro()
        rows = _carregar(bloquear=True)
        antes = _snapshot(rows)
        if registro:
            if registro['estado'] == 'restaurada':
                raise CorrecaoMinisError('Esta correção já foi desfeita e está encerrada.')
            if antes != registro['depois']:
                raise CorrecaoMinisError(
                    'Os pedidos foram alterados após a correção. Refaça a conferência.')
            _validar_fingerprint(fingerprint, registro['fingerprint_antes'], _hash(antes))
            resultado = _resultado('aplicada', antes, registro['pedidos'])
            db.session.rollback()
            return resultado
        if hoje() > date(2026, 9, 25):
            raise CorrecaoMinisError('O prazo desta correção pontual terminou.')
        _validar_fingerprint(fingerprint, _hash(antes))
        itens = _itens_alvo(rows)
        resumo = _resumo(rows)
        db.session.info['audit_user_id'] = user.id
        instante = agora()
        esperado = deepcopy(antes)
        for pedido, item, esp in zip(rows, itens, esperado, strict=True):
            db.session.delete(item)
            pedido.observacao = ((pedido.observacao + '\n')
                                 if pedido.observacao else '') + _MOTIVO
            pedido.modificado_em = instante
            pedido.modificado_por_id = user.id
            esp['itens'] = [i for i in esp['itens'] if i['id'] != item.id]
            esp['pedido'].update(observacao=pedido.observacao,
                                 modificado_em=instante.isoformat(),
                                 modificado_por_id=user.id)
        db.session.flush()
        for pedido in rows:
            db.session.expire(pedido, ['itens'])
        depois = _snapshot(_carregar())
        if depois != esperado:
            raise CorrecaoMinisError(
                'A composição mudou durante a correção. Nenhum pedido foi alterado.')
        registro = {'versao': 1, 'estado': 'aplicada', 'antes': antes,
                    'depois': depois, 'pedidos': resumo,
                    'fingerprint_antes': _hash(antes),
                    'fingerprint_depois': _hash(depois),
                    'aplicada_em': instante.isoformat(), 'aplicada_por': user.id}
        AppConfig.set(MARCADOR, _json(registro))
        db.session.commit()
        return _resultado('aplicada', depois, resumo)
    except Exception:
        db.session.rollback()
        raise
    finally:
        if anterior is None:
            db.session.info.pop('audit_user_id', None)
        else:
            db.session.info['audit_user_id'] = anterior


def restaurar_correcao_minis(user_id, fingerprint):
    """Restaura IDs e valores originais somente se ninguém alterou os pedidos."""
    anterior = db.session.info.get('audit_user_id')
    try:
        _travar()
        user = _owner(user_id)
        registro = _ler_registro()
        if registro is None:
            raise CorrecaoMinisError('Não há correção para desfazer.')
        rows = _carregar(bloquear=True)
        atual = _snapshot(rows)
        if registro['estado'] == 'restaurada':
            if atual != registro['antes']:
                raise CorrecaoMinisError(
                    'Os pedidos foram alterados após desfazer. Refaça a conferência.')
            _validar_fingerprint(fingerprint, registro['fingerprint_depois'], _hash(atual))
            resultado = _resultado('restaurada', atual, registro['pedidos'])
            db.session.rollback()
            return resultado
        _validar_fingerprint(fingerprint, _hash(atual))
        if atual != registro['depois']:
            raise CorrecaoMinisError(
                'Os pedidos foram alterados após a correção. Não é seguro desfazer automaticamente.')
        db.session.info['audit_user_id'] = user.id
        por_id = {p.id: p for p in rows}
        for original in registro['antes']:
            pedido = por_id[original['pedido']['id']]
            removidos = [i for i in original['itens'] if i['receita_id'] == _RECEITA_ID]
            if len(removidos) != 1 or db.session.get(PedidoItem, removidos[0]['id']):
                raise CorrecaoMinisError('O item original não pode ser restaurado com segurança.')
            db.session.add(PedidoItem(**_valores_modelo(PedidoItem, removidos[0])))
            # Só estes campos foram alterados ao aplicar. As demais colunas
            # precisam continuar iguais pelo snapshot integral conferido acima.
            campos = _valores_modelo(PedidoLoja, original['pedido'])
            for nome in ('observacao', 'modificado_em', 'modificado_por_id'):
                setattr(pedido, nome, campos[nome])
        db.session.flush()
        for pedido in rows:
            db.session.expire(pedido, ['itens'])
        restaurado = _snapshot(_carregar())
        if restaurado != registro['antes']:
            raise CorrecaoMinisError('A restauração divergiu do original. Nada foi alterado.')
        registro.update(estado='restaurada', restaurada_em=agora().isoformat(),
                        restaurada_por=user.id)
        AppConfig.set(MARCADOR, _json(registro))
        db.session.commit()
        return _resultado('restaurada', restaurado, registro['pedidos'])
    except Exception:
        db.session.rollback()
        raise
    finally:
        if anterior is None:
            db.session.info.pop('audit_user_id', None)
        else:
            db.session.info['audit_user_id'] = anterior
