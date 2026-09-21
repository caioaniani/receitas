"""Produção real informada na TV, com ordem e ficha congelada para pães."""

import hashlib
import json
import re

from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.utils import hoje

REFERENCIA_EXTRA = 'Produção extra (TV padeiro)'


class UsarOrdemDoDia(ValueError):
    pass


def registrar_lote(validados, user_id, chave=None):
    """Tudo ou nada; o recibo único impede repetir um envio após timeout.

    O chamador valida catálogo/quantidades e faz rollback em qualquer erro.
    Pães avulsos criam uma ordem manual concluída na quantidade REAL. O
    arredondamento da ficha nunca credita unidades nem deixa sobra prevista.
    """
    from app.models import AppConfig, PlanejamentoItem, PlanejamentoProducao
    from app.services.bateladas_paes import farinha_padrao_g, normalizar_item
    from app.services.estoque_congelados import entrada_producao
    from app.services.producao import (
        consumir_subreceitas_prontas,
        produzir_item_plano,
        sincronizar_pre_baixa_mp,
    )

    paes = [(obj, qtd) for tipo, obj, qtd in validados
            if tipo == 'receita' and farinha_padrao_g(obj)]
    if paes and not chave:
        raise ValueError('Atualize a tela para registrar este pão com uma ordem de produção.')
    recibo = None
    if chave:
        if not isinstance(chave, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,64}', chave):
            raise ValueError('Identificação do envio inválida. Atualize a tela.')
        conteudo = json.dumps([(t, o.id, q) for t, o, q in validados])
        assinatura = hashlib.sha256(conteudo.encode()).hexdigest()
        key = f'producao_tv:{user_id}:{chave}'

        def repetir(anterior):
            dados = json.loads(anterior.value)
            if dados['assinatura'] != assinatura:
                raise ValueError('Este envio já foi usado com outras quantidades. Atualize a tela.')
            return dados['resumo']

        anterior = db.session.get(AppConfig, key)
        if anterior is not None:
            return repetir(anterior)
        recibo = AppConfig(key=key, value='{}')
        db.session.add(recibo)
        try:
            db.session.flush()
        except IntegrityError:
            # O insert concorrente espera o primeiro commit. Rollback limpa
            # somente esta tentativa; a resposta já confirmada é reutilizada.
            db.session.rollback()
            anterior = db.session.get(AppConfig, key)
            if anterior is None:
                raise
            return repetir(anterior)

    plano = None
    itens_paes = {}
    if paes:
        ids = [rec.id for rec, _ in paes]
        existente = (PlanejamentoItem.query.join(PlanejamentoProducao)
                     .filter(PlanejamentoProducao.data == hoje(),
                             PlanejamentoProducao.origem == 'cronograma',
                             PlanejamentoProducao.enviado_ao_padeiro.isnot(False),
                             PlanejamentoItem.receita_id.in_(ids),
                             PlanejamentoItem.dispensada_em.is_(None),
                             PlanejamentoItem.falta_encerrada_em.is_(None),
                             PlanejamentoItem.qtd_alvo > PlanejamentoItem.produzido_qtd)
                     .first())
        if existente is not None:
            raise UsarOrdemDoDia(
                f'{existente.receita.nome}: já está na ordem de hoje. '
                'Use Registrar produção nessa ordem para atualizar o progresso.')
        plano = PlanejamentoProducao(
            data=hoje(), nome=f'Produção extra {hoje():%d/%m}',
            origem='avulsa_padeiro', criado_por=user_id,
            enviado_ao_padeiro=True, status='aprovado')
        db.session.add(plano)
        for rec, qtd in paes:
            item = PlanejamentoItem(planejamento=plano, receita=rec,
                                     qtd_alvo=qtd, produzido_qtd=0, multiplicador=1)
            db.session.add(item)
            db.session.flush()
            normalizar_item(item)
            itens_paes[rec.id] = item
        sincronizar_pre_baixa_mp(plano, user_id)

    resumo = []
    for tipo, obj, qtd in validados:
        item = itens_paes.get(obj.id) if tipo == 'receita' else None
        if item is not None:
            resultado = produzir_item_plano(
                item.id, qtd, user_id, commit=False, produzido_esperado=0,
                referencia_estoque=f'{REFERENCIA_EXTRA} · ordem {plano.id}')
            if not resultado['ok']:
                raise ValueError(resultado['erro'])
            # Esta é uma declaração de produção já concluída, não um pedido
            # futuro: preserve a ficha, mas encerre o alvo no realizado.
            item.qtd_alvo = item.produzido_qtd
            normalizar_item(item)
        else:
            entrada_producao(
                receita_id=obj.id if tipo == 'receita' else None,
                produto_id=obj.id if tipo == 'produto' else None,
                estado=None, quantidade=qtd, usuario_id=user_id,
                referencia='Produção (TV padeiro)')
            if tipo == 'receita':
                consumir_subreceitas_prontas(obj, qtd, user_id)
        resumo.append({'nome': obj.nome, 'qtd': qtd})
    if plano is not None:
        sincronizar_pre_baixa_mp(plano, user_id)
        plano.status = 'executado'
    if recibo is not None:
        recibo.value = json.dumps({'assinatura': assinatura, 'resumo': resumo},
                                  ensure_ascii=False)
    db.session.commit()
    return resumo
