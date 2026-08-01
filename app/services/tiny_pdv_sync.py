"""Importa as vendas do PDV do TINY pro nosso estoque (27/07/2026).

Pedido do dono: a Cantina vende pelo PDV do Tiny (nao pelo Seru), entao as
vendas dela eram invisiveis — nao baixavam EstoqueLoja, nao entravam em
faturamento nem na previsao de demanda. Este service e o espelho do
`seru_sync` pro Tiny.

O QUE TORNA ISSO SEGURO (conferido em 27/07/2026 antes de escrever):
- O nosso sistema NUNCA cria pedido no Tiny — so NOTA (`tiny_nf.py:504` →
  `incluir_nota_fiscal`); `tiny.incluir_pedido` nao tem um unico chamador.
  Logo TODO pedido que a API devolve nasceu no PDV, e importa-lo nao duplica
  a baixa que o site/B2B ja fazem por conta propria.
- A loja e UMA so (decisao do dono 27/07: "so a Cantina"), configurada em
  AppConfig `tiny_pdv_loja_id`. Sem config o sync NAO RODA — baixar na loja
  errada e pior que nao baixar.

Salvaguardas (mesmas do Seru, e pelos mesmos motivos):
- Idempotencia por `TinyPedidoProcessado` (id do pedido no Tiny).
- Produto sem mapa vira `VendaMapa` PENDENTE (canal 'tiny') e o item e
  PULADO sem alarme — o pedido segue processado e o pendente aparece na tela
  de mapeamento. Espelha o Seru: um produto novo nao trava a venda inteira.
- Detalhe indisponivel (falha de rede) => pedido NAO marcado, retenta.
- Estoque insuficiente => `venda_tiny_sem_estoque` (nunca nega o saldo).
- Venda CANCELADA depois no Tiny => estorno no ciclo seguinte.
"""
import logging

from app.constants import VENDA_TIPOS_LOJA  # noqa: F401  (doc do contrato)
from app.extensions import db
from app.models import AppConfig, Loja, TinyPedidoProcessado, VendaMapa
from app.utils import agora, hoje

logger = logging.getLogger(__name__)

CANAL = 'tiny'
_CFG_LOJA = 'tiny_pdv_loja_id'

# Situacoes do Tiny que representam venda EFETIVADA. 'Faturado' e o que o PDV
# grava (conferido no fim de semana 25-26/07: 54 pedidos, todos 'Faturado').
SITUACOES_VENDA = ('faturado', 'atendido', 'entregue', 'pronto para envio',
                   'enviado')
SITUACOES_CANCELADA = ('cancelado', 'cancelada')


def loja_pdv_tiny():
    """A Loja em que o PDV do Tiny vende. None = nao configurado (sync nao
    roda). Decisao do dono 27/07/2026: e a Cantina, uma so."""
    try:
        lid = int(AppConfig.get(_CFG_LOJA) or 0)
    except (TypeError, ValueError):
        return None
    return db.session.get(Loja, lid) if lid else None


def definir_loja_pdv(loja_id):
    """Configura a loja do PDV do Tiny (gesto de admin)."""
    AppConfig.set(_CFG_LOJA, int(loja_id))
    db.session.commit()


def _situacao(pedido):
    return (pedido.get('situacao') or '').strip().lower()


def _resolver_mapa(nome, tiny_produto_id=None):
    """Acha (ou CRIA como pendente) o VendaMapa do produto do Tiny.

    Chave = `nome_externo` (contrato do VendaMapa, unique por canal+nome).
    O `sku` guarda o id_produto do Tiny — chave estavel pra quando o dono
    renomear o item no Tiny (o nome muda, o id nao)."""
    nome = (nome or '').strip()
    if not nome:
        return None
    mapa = VendaMapa.query.filter_by(canal=CANAL, nome_externo=nome).first()
    if mapa is None:
        mapa = VendaMapa(canal=CANAL, nome_externo=nome,
                         sku=tiny_produto_id, fator_quantidade=1.0)
        db.session.add(mapa)
        db.session.flush()
        logger.info('tiny_pdv: produto NOVO pendente de mapeamento: %r', nome)
    elif tiny_produto_id and not mapa.sku:
        mapa.sku = tiny_produto_id
    return mapa


def _tem_alvo(mapa):
    return bool(mapa and (mapa.receita_id or mapa.produto_id
                          or mapa.materia_prima_id))


def _baixar_item(loja_id, mapa, qtd, tiny_pedido_id, user_id=None):
    """Baixa UM item pelo MOTOR UNICO (`baixa_venda.aplicar_venda`): explode
    cesta, aplica `fator_quantidade`, acumula fracao e grava o movimento."""
    from app.services.baixa_venda import aplicar_venda
    return aplicar_venda(
        loja_id,
        receita_id=mapa.receita_id,
        produto_id=mapa.produto_id,
        materia_prima_id=getattr(mapa, 'materia_prima_id', None),
        qtd=qtd, fator=mapa.fator_quantidade or 1.0, canal=CANAL,
        referencia=f'Tiny #{tiny_pedido_id}',
        pedido_ref=f'tiny:{tiny_pedido_id}',
        usuario_id=user_id, nome_venda=mapa.nome_externo)


def _processar_pedido(pedido, loja, user_id=None):
    """Processa UM pedido do Tiny. Devolve o codigo do desfecho:
    'baixado' | 'pendente_detalhe' | 'ignorado' | 'ja_processado'."""
    from app.services import tiny

    pid = str(pedido.get('id') or '').strip()
    if not pid:
        return 'ignorado'
    reg = db.session.get(TinyPedidoProcessado, pid)
    sit = _situacao(pedido)

    # Cancelado depois de processado -> estorna (proximo ciclo ve o status).
    if reg is not None:
        if (sit in SITUACOES_CANCELADA and reg.estornado_em is None
                and (reg.n_itens_baixados or 0) > 0):
            from app.services.baixa_venda import estornar_venda
            # (canal, pedido_ref, referencia) — o pedido_ref e a chave das
            # FRACOES ('tiny:<id>'), a referencia e a dos INTEIROS
            # ('Tiny #<id>'). Trocar os dois deixa fracao fantasma.
            estornar_venda(CANAL, f'tiny:{pid}', f'Tiny #{pid}',
                           loja_id=loja.id, usuario_id=user_id)
            reg.cancelado_em = reg.cancelado_em or agora()
            reg.estornado_em = agora()
            logger.info('tiny_pdv: pedido %s cancelado -> estornado', pid)
            return 'estornado'
        return 'ja_processado'

    if sit in SITUACOES_CANCELADA:
        return 'ignorado'
    if sit and sit not in SITUACOES_VENDA:
        # Situacao desconhecida (orcamento, aberto...): NAO baixa e NAO marca
        # — quando virar venda o proximo ciclo pega.
        return 'ignorado'

    itens = tiny.itens_do_pedido(pid)
    if itens is None:
        # Falha de rede/API: NAO marca como processado (a venda sumiria).
        logger.warning('tiny_pdv: sem detalhe do pedido %s — retenta', pid)
        return 'pendente_detalhe'

    baixados = 0
    for it in itens:
        mapa = _resolver_mapa(it['nome'], it.get('tiny_produto_id'))
        if not mapa or mapa.ignorar or not _tem_alvo(mapa):
            continue                     # pendente/ignorado: some sem alarme
        qtd = int(round(it['quantidade']))
        if qtd <= 0:
            continue
        res = _baixar_item(loja.id, mapa, qtd, pid, user_id)
        baixados += res.get('baixado', 0) + res.get('faltou', 0)

    data_ped = None
    try:
        from datetime import datetime
        data_ped = datetime.strptime(
            (pedido.get('data_pedido') or '').strip(), '%d/%m/%Y').date()
    except ValueError:
        data_ped = None
    db.session.add(TinyPedidoProcessado(
        tiny_pedido_id=pid, numero=str(pedido.get('numero') or '')[:40],
        loja_id=loja.id, data_pedido=data_ped,
        valor=pedido.get('valor'), situacao=(pedido.get('situacao') or '')[:40],
        n_itens_total=len(itens), n_itens_baixados=baixados))
    return 'baixado'


def processar_periodo(data_ini=None, data_fim=None, user_id=None):
    """Le as vendas do PDV do Tiny no periodo e baixa o estoque da loja
    configurada. Idempotente — pode rodar quantas vezes quiser.

    Retorna dict de stats. NUNCA levanta: o cron nao pode morrer por causa
    de uma janela ruim da API.
    """
    from app.services import tiny

    stats = {'pedidos': 0, 'baixados': 0, 'ja_processados': 0,
             'estornados': 0, 'ignorados': 0, 'pendentes_detalhe': 0,
             'mapas_pendentes': 0, 'erro': None}
    loja = loja_pdv_tiny()
    if loja is None:
        stats['erro'] = f'AppConfig {_CFG_LOJA} nao configurado'
        logger.warning('tiny_pdv: %s — sync nao roda', stats['erro'])
        return stats
    if not tiny.disponivel():
        stats['erro'] = 'TINY_API_TOKEN ausente'
        return stats

    di = data_ini or hoje()
    df = data_fim or hoje()
    try:
        pedidos = tiny.listar_pedidos_periodo(di, df)
    except Exception as exc:                              # noqa: BLE001
        stats['erro'] = f'{type(exc).__name__}: {exc}'
        logger.warning('tiny_pdv: falha ao listar pedidos: %s', exc)
        return stats

    stats['pedidos'] = len(pedidos)
    for pedido in pedidos:
        try:
            desfecho = _processar_pedido(pedido, loja, user_id)
        except Exception as exc:                          # noqa: BLE001
            # Um pedido torto nao pode matar a varredura (licao do
            # estorno_pendente_vigia, 26/07/2026).
            logger.warning('tiny_pdv: pedido %s falhou: %s',
                           pedido.get('id'), exc)
            db.session.rollback()
            continue
        chave = {'baixado': 'baixados', 'ja_processado': 'ja_processados',
                 'estornado': 'estornados', 'ignorado': 'ignorados',
                 'pendente_detalhe': 'pendentes_detalhe'}[desfecho]
        stats[chave] += 1
    db.session.commit()
    stats['mapas_pendentes'] = VendaMapa.query.filter(
        VendaMapa.canal == CANAL,
        VendaMapa.ignorar.is_(False),
        VendaMapa.receita_id.is_(None),
        VendaMapa.produto_id.is_(None),
        VendaMapa.materia_prima_id.is_(None)).count()
    logger.info('tiny_pdv: %s', stats)
    return stats


def pendentes_de_mapeamento():
    """Produtos do PDV do Tiny ainda sem receita/produto vinculado — o que a
    tela de mapeamento mostra pro dono resolver."""
    return (VendaMapa.query
            .filter(VendaMapa.canal == CANAL,
                    VendaMapa.ignorar.is_(False),
                    VendaMapa.receita_id.is_(None),
                    VendaMapa.produto_id.is_(None),
                    VendaMapa.materia_prima_id.is_(None))
            .order_by(VendaMapa.nome_externo).all())


# ── Sugestao automatica de mapeamento (27/07/2026) ──────────────────
#
# Sao ~77 produtos no Tiny e mapear na mao e trabalho de horas. O nome do
# Tiny descreve o PREPARO ("SOURDOUGH TRADICIONAL NA CHAPA COM MANTEIGA
# CANTINA") e o catalogo ja tem o item equivalente (receita ou cesta), entao
# um matcher por tokens acerta a maioria.
#
# A sugestao NUNCA se aplica sozinha: ela so PRE-SELECIONA na tela, e quem
# confirma e o dono. Mapeamento errado = baixa de estoque errada em silencio
# — e a licao do "cafe com fator 0.2" (CLAUDE.md) e que so o dono sabe a
# regra de negocio local.

# Piso pra SUGERIR (aparece na tela como dica).
PISO_SUGESTAO = 0.5
# Piso pra PRE-PREENCHER o campo. Mais alto de proposito: validado contra os
# nomes reais em 27/07/2026, a faixa 0.50-0.74 produz erro CONVINCENTE —
# "CROISSANT DE AMENDOAS" casava "Creme de Amendoas" e "CROISSANT FRANCES"
# casava "Croissant Almond" com 0.50. Pre-preencher isso e convidar o dono a
# clicar Salvar num vinculo errado = baixa de estoque errada em silencio.
# Abaixo do piso a sugestao aparece como DICA e o campo fica VAZIO.
PISO_PREENCHE = 0.75

_RUIDO = (
    'cantina', 'un', 'ml', 'g', 'kg', 'de', 'do', 'da', 'com', 'no', 'na',
    'e', 'a', 'o', 'ao', 'em', 'so', 'sem', 'para', 'pra',
)


def _tokens(nome):
    """Tokens normalizados (sem acento, sem pontuacao, sem ruido)."""
    import re
    import unicodedata
    s = unicodedata.normalize('NFKD', (nome or '')).encode(
        'ascii', 'ignore').decode().lower()
    s = re.sub(r'[^a-z0-9]+', ' ', s)
    return [t for t in s.split() if t and t not in _RUIDO and not t.isdigit()]


def fator_do_nome(nome):
    """Le o multiplicador embutido no nome: 'CONE DE PÃO DE QUEIJO COM 5 UN'
    -> 5.0. Sem padrao reconhecido -> 1.0. So conta 'COM N UN(IDADES)' — um
    '300 ml' ou '500 g' e gramagem, nao quantidade."""
    import re
    m = re.search(r'\bcom\s+(\d{1,3})\s*un', (nome or ''), re.IGNORECASE)
    if m:
        try:
            n = float(m.group(1))
            return n if 0 < n <= 100 else 1.0
        except ValueError:
            return 1.0
    return 1.0


def _score(tokens_tiny, tokens_alvo):
    """0..1. Premia o alvo cujos tokens estao TODOS no nome do Tiny (o nome
    do Tiny e mais longo, por descrever o preparo), e penaliza alvo curto
    demais que casaria com qualquer coisa."""
    if not tokens_tiny or not tokens_alvo:
        return 0.0
    st, sa = set(tokens_tiny), set(tokens_alvo)
    comuns = st & sa
    if not comuns:
        return 0.0
    # cobertura do ALVO (quanto do nome do catalogo aparece no Tiny)
    cob_alvo = len(comuns) / len(sa)
    # cobertura do TINY (evita 'croissant' casar tudo que tem croissant)
    cob_tiny = len(comuns) / len(st)
    return round(0.75 * cob_alvo + 0.25 * cob_tiny, 4)


def sugerir_alvo(nome_tiny, catalogo=None):
    """(kind, id, nome_alvo, score) do melhor candidato pro nome do Tiny, ou
    None. `catalogo` = [(kind, id, nome)] — passe pronto pra nao requerer o
    banco em loop."""
    from app.models import Produto, Receita
    if catalogo is None:
        catalogo = ([('receita', r.id, r.nome)
                     for r in Receita.ativas().all()]
                    + [('produto', p.id, p.nome)
                       for p in Produto.query.filter_by(ativo=True).all()])
    tt = _tokens(nome_tiny)
    melhor = None
    for kind, iid, nome in catalogo:
        s = _score(tt, _tokens(nome))
        if s > 0 and (melhor is None or s > melhor[3]):
            melhor = (kind, iid, nome, s)
    # Piso: abaixo disso a sugestao atrapalha mais do que ajuda.
    if melhor and melhor[3] >= PISO_SUGESTAO:
        return melhor
    return None


def sugestoes_pendentes():
    """{venda_mapa_id: {'kind','id','nome','score','fator'}} pros produtos do
    Tiny ainda sem vinculo. So sugere — nao grava nada."""
    from app.models import Produto, Receita
    catalogo = ([('receita', r.id, r.nome) for r in Receita.ativas().all()]
                + [('produto', p.id, p.nome)
                   for p in Produto.query.filter_by(ativo=True).all()])
    out = {}
    for m in pendentes_de_mapeamento():
        sug = sugerir_alvo(m.nome_externo, catalogo)
        if sug:
            kind, iid, nome, score = sug
            out[m.id] = {'kind': kind, 'id': iid, 'nome': nome,
                         'score': score,
                         'preenche': score >= PISO_PREENCHE,
                         'fator': fator_do_nome(m.nome_externo)}
    return out
