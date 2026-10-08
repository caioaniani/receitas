"""Zerar TODO o estoque de congelados da indústria (decisão do dono, 08/10/2026).

Dono, com foto da lista "Congelados — em estoque" da TV: "Zerar os congelados
e faz uma lista em Excel para eu imprimir e lançar manualmente, pois tem muito
erro, vamos acompanhar de perto". Avisado de que, até a contagem ser lançada,
o planejamento enxerga estoque zero e as ordens automáticas saem infladas,
escolheu zerar assim mesmo.

- Cada linha de `EstoqueProducao` com saldo vai a 0 com movimento
  `ajuste_conferencia` ASSINADO (mesmo tipo da conferência — a contagem que
  vem depois soma em cima) e referência rastreável. Nada é apagado.
- Massa para folhar passa pelo motor dela (`ajustar_contagem_bolas(…, 0)`),
  que zera também o resíduo em gramas e a fração legada.
- As faltas de saída abertas dos itens zerados são encerradas como
  'contagem': o estoque recomeça do zero e quem lança a produção não deve
  mais ser perguntado sobre saídas anteriores.
- Linhas travadas em ordem de id antes das faltas (mesma ordem da quitação).

NÃO commita — o chamador controla a transação.
"""
from app.extensions import db
from app.models import EstoqueProducao, MovEstoqueProducao

REFERENCIA = 'Zerado pelo dono para recontagem manual (/admin/congelados-zerar)'


def _nome(ep):
    if ep.receita is not None:
        return ep.receita.nome
    if ep.produto is not None:
        return ep.produto.nome
    return ep.nome_pendente or f'linha #{ep.id}'


def zerar(executar=False, usuario_id=None):
    """Lista (e, com `executar`, zera) o estoque de congelados da indústria.

    Devolve {'itens': [{id, item, quantidade, unidade}], 'zerados': n,
    'faltas_encerradas': n, 'executado': bool}."""
    from app.services import faltas_industria
    from app.services.estoque_congelados import travar_linhas_producao
    from app.services.estoque_massa import (
        ajustar_contagem_bolas,
        eh_massa_folhar,
        saldo_bolas,
    )

    candidatas = EstoqueProducao.query.order_by(EstoqueProducao.id).all()
    if executar:
        candidatas = travar_linhas_producao(ids=[ep.id for ep in candidatas])

    itens, contados, massas_feitas = [], [], set()
    zerados = 0
    for ep in candidatas:
        massa = ep.receita is not None and eh_massa_folhar(ep.receita)
        if massa:
            if ep.receita_id in massas_feitas:
                continue
            massas_feitas.add(ep.receita_id)
            atual = float(saldo_bolas(ep.receita))
            if atual == 0:
                continue
            itens.append({'id': ep.id, 'item': _nome(ep), 'quantidade': atual,
                          'unidade': 'bolas'})
            contados.append((ep.receita_id, None))
            if executar:
                ajustar_contagem_bolas(ep.receita, 0, usuario_id, REFERENCIA)
                zerados += 1
            continue
        atual = int(ep.quantidade or 0)
        if atual == 0:
            continue
        itens.append({'id': ep.id, 'item': _nome(ep), 'quantidade': atual,
                      'unidade': None})
        if ep.receita_id or ep.produto_id:
            contados.append((ep.receita_id, ep.produto_id))
        if executar:
            db.session.add(MovEstoqueProducao(
                estoque_producao_id=ep.id, tipo='ajuste_conferencia',
                quantidade=-atual,
                referencia=f'{REFERENCIA}: sistema {atual} → 0',
                usuario_id=usuario_id))
            ep.quantidade = 0
            zerados += 1

    faltas = 0
    if executar and contados:
        faltas = faltas_industria.encerrar_por_contagem(contados, usuario_id)
    return {'itens': itens, 'zerados': zerados, 'faltas_encerradas': faltas,
            'executado': bool(executar)}
