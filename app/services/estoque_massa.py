"""Estoque exato da Massa para folhar sem trocar a unidade histórica.

Uma bola continua sendo uma bola do cadastro anterior (3.580 g em produção).
``EstoqueProducao.quantidade`` guarda bolas completas; ``SaldoResidualMassa``
guarda o restante, em gramas. Novas entradas e consumos têm um movimento em
gramas e, quando muda a parte inteira, um movimento legado vinculado.

Todos os cálculos usam Decimal, na precisão de seis casas das tabelas novas.
Nenhuma função commita. Confirmação, MP e avanço da ordem devem compartilhar
a transação do chamador. Leituras não criam saldo nem convertem o legado.
"""

import unicodedata
from decimal import ROUND_FLOOR, ROUND_HALF_EVEN, Decimal, InvalidOperation

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session

from app.extensions import db
from app.models.catalogo import Receita
from app.models.estoque import ConsumoSubFracao, EstoqueProducao, MovEstoqueProducao
from app.models.estoque_massa import MovEstoqueMassa, SaldoResidualMassa
from app.models.producao import PlanejamentoItem
from app.models.producao_batelada import PlanejamentoItemBatelada

_PRECISAO_G = Decimal('0.000001')
_LIMITE_G = Decimal('100000000000000')
_ZERO = Decimal(0)
_CACHE_IDENTIDADES = 'estoque_massa_receitas_historicas'


@event.listens_for(Session, 'after_flush')
def _invalidar_identidades_apos_escrita(session, flush_context):
    modelos = (SaldoResidualMassa, PlanejamentoItemBatelada, PlanejamentoItem)
    if any(isinstance(obj, modelos)
           for grupo in (session.new, session.dirty, session.deleted) for obj in grupo):
        session.info.pop(_CACHE_IDENTIDADES, None)


@event.listens_for(Session, 'after_transaction_end')
def _invalidar_identidades_apos_transacao(session, transaction):
    if transaction.parent is None:
        session.info.pop(_CACHE_IDENTIDADES, None)


@event.listens_for(Session, 'after_soft_rollback')
def _invalidar_identidades_apos_rollback(session, previous_transaction):
    # Um SAVEPOINT também pode desfazer o primeiro saldo/snapshot, sem
    # encerrar a transação externa que ainda reutilizará a sessão.
    session.info.pop(_CACHE_IDENTIDADES, None)


def _peso_snapshot_valido(valor):
    if valor is None:
        return None
    try:
        return _gramas(valor, 'Peso histórico da bola', positivo=True)
    except ValueError:
        # Snapshots anteriores/incompletos não fixaram uma unidade válida.
        return None


def _historico_massa(session):
    from app.services.viennoiserie import TIPO_MASSA
    if _CACHE_IDENTIDADES not in session.info:
        # Duas consultas por sessão/transação, não duas por receita da lista.
        # A leitura não faz flush nem cria estado de estoque.
        with session.no_autoflush:
            ids = {rid for (rid,) in session.query(SaldoResidualMassa.receita_id).all()}
            snapshots = (session.query(
                PlanejamentoItem.receita_id,
                PlanejamentoItemBatelada.dados['peso_bola_g'].as_string())
                .join(PlanejamentoItemBatelada,
                      PlanejamentoItemBatelada.item_id == PlanejamentoItem.id)
                .filter(PlanejamentoItemBatelada.dados['tipo'].as_string() == TIPO_MASSA)
                .order_by(PlanejamentoItemBatelada.criado_em, PlanejamentoItemBatelada.item_id)
                .all())
        pesos = {}
        for rid, valor in snapshots:
            ids.add(rid)
            peso = _peso_snapshot_valido(valor)
            if peso is not None:
                pesos.setdefault(rid, peso)
        session.info[_CACHE_IDENTIDADES] = (frozenset(ids), pesos)
    ids_salvos, pesos_salvos = session.info[_CACHE_IDENTIDADES]
    ids, pesos = set(ids_salvos), dict(pesos_salvos)
    # Um snapshot instalado nesta mesma transação também já fixa a identidade,
    # mesmo antes do flush que o persistirá.
    with session.no_autoflush:
        for obj in session.new | session.dirty:
            if isinstance(obj, SaldoResidualMassa):
                ids.add(obj.receita_id)
            elif (isinstance(obj, PlanejamentoItemBatelada)
                  and (obj.dados or {}).get('tipo') == TIPO_MASSA):
                item = obj.item
                if item is not None:
                    rid = item.receita_id or (item.receita.id if item.receita else None)
                    ids.add(rid)
                    peso = _peso_snapshot_valido(obj.dados.get('peso_bola_g'))
                    if peso is not None:
                        pesos.setdefault(rid, peso)
    return ids, pesos


def eh_massa_folhar(rec):
    """Nome exato para iniciar; saldo ou snapshot preservam identidade histórica.

    O estoque não depende de a ficha atual ainda ter farinha válida: uma
    edição do nome/ingredientes não pode esconder massa já produzida ou uma
    ordem enviada. Não inclui Massa de Danish nem produtos derivados.
    """
    nome = unicodedata.normalize('NFKD', str(getattr(rec, 'nome', '') or ''))
    nome = ''.join(c for c in nome if not unicodedata.combining(c))
    if ' '.join(nome.casefold().split()) == 'massa para folhar':
        return True
    estado = inspect(rec, raiseerr=False)
    if (estado is None or estado.session is None
            or getattr(rec, 'id', None) is None):
        return False
    return rec.id in _historico_massa(estado.session)[0]


def _numero(valor, campo, *, positivo=False):
    try:
        numero = Decimal(str(valor if valor is not None else 0))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f'{campo}: quantidade inválida.') from None
    if not numero.is_finite() or numero < 0 or (positivo and numero <= 0):
        raise ValueError(f'{campo}: quantidade inválida.')
    return numero


def _gramas(valor, campo, *, positivo=False):
    numero = _numero(valor, campo, positivo=positivo)
    if numero >= _LIMITE_G:
        raise ValueError(f'{campo}: quantidade excede a precisão do estoque.')
    numero = numero.quantize(_PRECISAO_G, rounding=ROUND_HALF_EVEN)
    if numero >= _LIMITE_G or (positivo and numero <= 0):
        raise ValueError(f'{campo}: quantidade fora da precisão do estoque.')
    return numero


def _validar_receita(rec):
    if not eh_massa_folhar(rec) or getattr(rec, 'id', None) is None:
        raise ValueError('O estoque de massa exige a receita Massa para folhar.')


def _peso(rec, residual, *, peso_bola_g_snapshot=None):
    if residual is not None:
        valor = residual.peso_bola_g
    else:
        estado = inspect(rec, raiseerr=False)
        historico = (_historico_massa(estado.session)[1].get(rec.id)
                     if estado is not None and estado.session is not None else None)
        # Uma ordem já enviada fixa a unidade antes da primeira produção.
        # O primeiro snapshot válido prevalece também sobre ordens posteriores.
        valor = (historico if historico is not None else peso_bola_g_snapshot
                 if peso_bola_g_snapshot is not None else rec.peso_unitario)
    return _gramas(valor, 'Peso da bola', positivo=True)


def peso_bola_g(rec):
    """Peso congelado no saldo ou primeira ordem; leitura não cria estoque."""
    _validar_receita(rec)
    with db.session.no_autoflush:
        return _peso(rec, db.session.get(SaldoResidualMassa, rec.id))


def saldo_bolas(rec):
    """Saldo em bolas equivalentes, incluindo a fração legada ainda não baixada.

    Pode ser negativo se o acumulador legado representa consumo sem saldo.
    A primeira escrita registra essa falta e encerra o acumulador, sem criar
    estoque negativo nem cobrar uma produção futura pelo consumo histórico.
    """
    _validar_receita(rec)
    with db.session.no_autoflush:
        residual = db.session.get(SaldoResidualMassa, rec.id)
        inteiros = sum((ep.quantidade or 0) for ep in
                       EstoqueProducao.query.filter_by(receita_id=rec.id).all())
        frac = ConsumoSubFracao.query.filter_by(receita_id=rec.id).first()
        pendente = _numero(frac.fracao_pendente if frac else 0, 'Fração pendente')
        # O saldo legado já está em bolas, inclusive a fração pendente.
        # Ler uma ficha antiga ainda sem peso não exige conversão em gramas.
        if residual is None:
            return Decimal(inteiros) - pendente
        peso = _peso(rec, residual)
        resto = _numero(residual.g, 'Saldo residual')
        return Decimal(inteiros) + resto / peso - pendente


def _carregar_travado(rec, usuario_id, *, peso_bola_g_snapshot=None):
    """Ordem única de locks, inclusive quando ainda não existe saldo residual."""
    _validar_receita(rec)
    rec = (Receita.query.filter_by(id=rec.id).with_for_update()
           .populate_existing().one())
    _validar_receita(rec)
    from app.services.estoque_congelados import obter_linha_producao
    estoque = obter_linha_producao(receita_id=rec.id, usuario_id=usuario_id)
    residual = (SaldoResidualMassa.query.filter_by(receita_id=rec.id)
                .with_for_update().populate_existing().first())
    # A ordem pode preceder a primeira entrada. Nesse caso sua unidade
    # congelada prevalece sobre uma edição posterior da ficha viva.
    peso = _peso(rec, residual, peso_bola_g_snapshot=peso_bola_g_snapshot)
    if residual is None:
        residual = SaldoResidualMassa(receita_id=rec.id, g=_ZERO, peso_bola_g=peso)
        db.session.add(residual)
    frac = (ConsumoSubFracao.query.filter_by(receita_id=rec.id)
            .with_for_update().populate_existing().first())
    return estoque, residual, frac, peso


def _saldo_g(estoque, residual, peso):
    return _gramas(Decimal(estoque.quantidade or 0) * peso + residual.g,
                    'Saldo da massa')


def _registrar(estoque, residual, peso, novo, quantidade, tipo,
               usuario_id, referencia, *, conferencia=False, perda_id=None,
               estorno_de_id=None):
    """Conserva o total em gramas e audita separadamente sua parte inteira."""
    anterior = _saldo_g(estoque, residual, peso)
    novo = _gramas(novo, 'Saldo da massa')
    quantidade = _gramas(quantidade, 'Movimento da massa')
    if not quantidade:
        return
    inteiro = int((novo / peso).to_integral_value(rounding=ROUND_FLOOR))
    delta = inteiro - int(estoque.quantidade or 0)
    movimento_inteiro = None
    if delta:
        movimento_inteiro = MovEstoqueProducao(
            estoque_producao_id=estoque.id, tipo=tipo,
            quantidade=delta if conferencia else abs(delta),
            referencia=(referencia or '')[:200], usuario_id=usuario_id)
        db.session.add(movimento_inteiro)
        db.session.flush()
    estoque.quantidade = inteiro
    residual.g = novo - Decimal(inteiro) * peso
    movimento = MovEstoqueMassa(
        receita_id=estoque.receita_id, estoque_producao_id=estoque.id,
        movimento_inteiro_id=movimento_inteiro.id if movimento_inteiro else None,
        perda_producao_id=perda_id, estorno_de_id=estorno_de_id,
        tipo=tipo, quantidade_g=quantidade,
        saldo_anterior_g=anterior, saldo_posterior_g=novo, peso_bola_g=peso,
        referencia=(referencia or '')[:200], usuario_id=usuario_id)
    db.session.add(movimento)
    return movimento


def _baixar(estoque, residual, peso, quantidade, usuario_id, referencia,
            tipo='consumo_subreceita', *, perda_id=None):
    saldo = _saldo_g(estoque, residual, peso)
    baixado = min(saldo, quantidade)
    falta = quantidade - baixado
    _registrar(estoque, residual, peso, saldo - baixado, baixado, tipo,
               usuario_id, referencia, perda_id=perda_id)
    if falta:
        # A falta pode ser menor que uma bola. O movimento exato registra
        # essa quantidade; arredondá-la no ledger antigo inventaria débito.
        _registrar(estoque, residual, peso, saldo - baixado, falta,
                   tipo + '_sem_estoque', usuario_id, referencia, perda_id=perda_id)
    return baixado, falta


def _consolidar_fracao(estoque, residual, frac, peso, usuario_id):
    if frac is None:
        return
    pendente = _numero(frac.fracao_pendente, 'Fração pendente')
    if pendente:
        quantidade = _gramas(pendente * peso, 'Consumo fracionário legado')
        _baixar(estoque, residual, peso, quantidade, usuario_id,
                'Conversão da fração de consumo anterior', 'consumo_subreceita')
    frac.fracao_pendente = 0.0


def _entrada(estoque, residual, frac, peso, quantidade, user_id, referencia, tipo):
    _consolidar_fracao(estoque, residual, frac, peso, user_id)
    anterior = _saldo_g(estoque, residual, peso)
    _registrar(estoque, residual, peso, anterior + quantidade, quantidade,
               tipo, user_id, referencia)
    return {'massa_g': quantidade, 'bolas': quantidade / peso,
            'saldo_bolas': _saldo_g(estoque, residual, peso) / peso}


def registrar_entrada_massa(rec, massa_g, user_id, referencia, *,
                            peso_bola_g_snapshot=None, tipo='producao'):
    """Credita gramas efetivos; 44.900 g = 12 bolas de 3.580 g + 1.940 g."""
    quantidade = _gramas(massa_g, 'Entrada de massa', positivo=True)
    estoque, residual, frac, peso = _carregar_travado(
        rec, user_id, peso_bola_g_snapshot=peso_bola_g_snapshot)
    return _entrada(estoque, residual, frac, peso, quantidade, user_id, referencia, tipo)


def registrar_entrada_bolas(rec, bolas, user_id, referencia, *, tipo='producao'):
    """Entrada avulsa em bolas históricas; conversão protegida pela mesma trava."""
    quantidade_bolas = _numero(bolas, 'Entrada de bolas', positivo=True)
    estoque, residual, frac, peso = _carregar_travado(rec, user_id)
    quantidade = _gramas(quantidade_bolas * peso, 'Entrada de massa', positivo=True)
    return _entrada(estoque, residual, frac, peso, quantidade, user_id, referencia, tipo)


def consumir_massa(rec, bolas, user_id, referencia, *, tipo='consumo_subreceita',
                   perda_id=None):
    """Debita bolas equivalentes; retorna baixado/falta nessa mesma unidade.

    O consumo é convertido e registrado em gramas com seis casas. Chamadores
    que dividem uma proporção periódica devem passar deltas do consumo
    acumulado para conservar essa precisão entre confirmações parciais.
    """
    _validar_receita(rec)
    quantidade_bolas = _numero(bolas, 'Consumo de massa')
    if not quantidade_bolas:
        return {'baixado': _ZERO, 'falta': _ZERO,
                'baixado_g': _ZERO, 'falta_g': _ZERO}
    if not isinstance(tipo, str) or not tipo or len(tipo) > 38:
        raise ValueError('Tipo de movimento de massa inválido.')
    estoque, residual, frac, peso = _carregar_travado(rec, user_id)
    quantidade = _gramas(quantidade_bolas * peso, 'Consumo de massa', positivo=True)
    _consolidar_fracao(estoque, residual, frac, peso, user_id)
    baixado, falta = _baixar(estoque, residual, peso, quantidade, user_id,
                            referencia, tipo, perda_id=perda_id)
    return {'baixado': baixado / peso, 'falta': falta / peso,
            'baixado_g': baixado, 'falta_g': falta}


def estornar_movimentos_massa(rec, movimentos_ids, user_id, referencia):
    """Devolve somente a massa realmente debitada nos movimentos indicados.

    O vínculo único ``estorno_de_id`` torna a operação idempotente, sem
    reconstruir quantidades por referência textual. Faltas não movimentaram
    saldo e não geram crédito. Não commita e preserva o movimento original.
    """
    ids = sorted(set(movimentos_ids))
    if not ids:
        return {'estornado': _ZERO, 'estornado_g': _ZERO}
    estoque, residual, frac, peso = _carregar_travado(rec, user_id)
    movimentos = (MovEstoqueMassa.query.filter(MovEstoqueMassa.id.in_(ids))
                  .order_by(MovEstoqueMassa.id).with_for_update()
                  .populate_existing().all())
    if (len(movimentos) != len(ids)
            or any(m.receita_id != rec.id or m.estorno_de_id is not None
                   or m.saldo_posterior_g > m.saldo_anterior_g for m in movimentos)):
        raise ValueError('Estorno exige movimentos de saída desta mesma massa.')
    _consolidar_fracao(estoque, residual, frac, peso, user_id)
    ja_estornados = {m.estorno_de_id for m in MovEstoqueMassa.query.filter(
        MovEstoqueMassa.estorno_de_id.in_(ids)).all()}
    estornado = _ZERO
    for mov in movimentos:
        if mov.id in ja_estornados:
            continue
        quantidade = mov.saldo_anterior_g - mov.saldo_posterior_g
        if not quantidade:
            continue
        anterior = _saldo_g(estoque, residual, peso)
        _registrar(estoque, residual, peso, anterior + quantidade, quantidade,
                   mov.tipo + '_estorno', user_id, referencia, estorno_de_id=mov.id)
        estornado += quantidade
    return {'estornado': estornado / peso, 'estornado_g': estornado}


def ajustar_contagem_bolas(rec, bolas_inteiras, user_id, referencia):
    """Contagem física absoluta de bolas completas, descartando resto anterior.

    Use somente quando a contagem informa o estoque TOTAL. Uma entrada ou
    saída avulsa de bolas preserva o residual, e usa as outras funções.
    Mesmo inteiro com residual diferente é um ajuste real e ganha histórico.
    """
    bolas = _numero(bolas_inteiras, 'Contagem física')
    if bolas != bolas.to_integral_value():
        raise ValueError('A contagem física deve informar bolas inteiras.')
    estoque, residual, frac, peso = _carregar_travado(rec, user_id)
    _consolidar_fracao(estoque, residual, frac, peso, user_id)
    novo = _gramas(bolas * peso, 'Contagem física')
    anterior = _saldo_g(estoque, residual, peso)
    _registrar(estoque, residual, peso, novo, abs(novo - anterior),
               'ajuste_conferencia', user_id, referencia, conferencia=True)
    return {'anterior_g': anterior, 'novo_g': novo, 'delta_g': novo - anterior}
