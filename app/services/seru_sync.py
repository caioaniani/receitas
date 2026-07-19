"""Sincronizacao Seru → estoque das lojas (auto-baixa).

Fluxo idempotente:
1. Busca pedidos da Seru no intervalo (createdAt no fuso BRT).
2. Pra cada pedido nao registrado em SeruPedidoProcessado:
   a. Resolve a loja via SeruLojaMap (auto-fuzzy se primeira vez).
   b. Pra cada item do pedido:
      - Mapeado (SeruProdutoMap.estado='mapeado'): baixa de EstoqueLoja
        e cria MovEstoqueLoja(tipo='venda_seru', referencia='Seru #<id>').
      - Ignorado: pula sem alarmes.
      - Pendente (nao visto antes): cria SeruProdutoMap em estado pendente
        pra revisao manual; nao baixa.
   c. Marca SeruPedidoProcessado.
3. Pra pedidos ja processados que aparecem com canceledAt: gera estorno
   (cria movs de tipo='venda_seru_estorno') e marca cancelado_em.

Estoque nunca fica negativo: se nao tem o suficiente, baixa o que tem
e registra MovEstoqueLoja(tipo='venda_seru_sem_estoque') com a falta.
"""
import logging
import re
import threading
import unicodedata

from app.extensions import db
from app.models import (
    EstoqueLoja,
    Loja,
    MovEstoqueLoja,
    SeruDebito,
    SeruDebitoMov,
    SeruLojaMap,
    SeruPedidoProcessado,
    SeruProdutoMap,
    VendaMapa,
)
from app.services import seru
from app.utils import agora

logger = logging.getLogger(__name__)


def _ascii(s):
    if not s:
        return ''
    nf = unicodedata.normalize('NFD', s)
    return ''.join(c for c in nf if unicodedata.category(c) != 'Mn').lower().strip()


def _fuzzy_loja(seru_company_name, lojas):
    """Tenta achar nossa Loja pelo nome da company da Seru.
    Retorna Loja ou None. Estrategia: ascii match exato, depois substring."""
    alvo = _ascii(seru_company_name)
    if not alvo:
        return None
    for l in lojas:
        if _ascii(l.nome) == alvo:
            return l
    for l in lojas:
        la = _ascii(l.nome)
        if alvo in la or la in alvo:
            return l
    # Tentativa final: token-overlap. Util pra "Padaria Opao Ribeiro" vs "Loja Ribeiro do Vale"
    tokens_alvo = set(re.split(r'\s+', alvo)) - {'loja', 'padaria', 'do', 'da', 'de', 'opao'}
    melhor = None
    melhor_overlap = 0
    for l in lojas:
        tokens_l = set(re.split(r'\s+', _ascii(l.nome))) - {'loja', 'padaria', 'do', 'da', 'de'}
        overlap = len(tokens_alvo & tokens_l)
        if overlap > melhor_overlap:
            melhor_overlap = overlap
            melhor = l
    return melhor if melhor_overlap >= 1 else None


def _resolver_loja(seru_company_name, lojas_ativas, seru_company_id=None,
                   seru_company_document=None):
    """Devolve (loja, mapping). Resolucao POR ID primeiro (ancora estavel:
    renome no Seru so atualiza o rotulo — incidente 06-07/07/2026, Ribeiro
    ficou 2 semanas sem baixa), com fallback pro NOME; mapa antigo sem id
    ganha o id na primeira venda (backfill). Se nao existia mapping, cria
    via fuzzy; fuzzy sem acerto cria pendente e devolve (None, mapping)."""
    if not seru_company_name and not seru_company_id:
        return None, None
    mapping = None
    if seru_company_id:
        mapping = (SeruLojaMap.query
                   .filter_by(seru_company_id=str(seru_company_id)).first())
        if (mapping and seru_company_name
                and mapping.seru_company_name != seru_company_name):
            # RENOME no Seru: traz a atualizacao junto. Se o nome novo ja
            # pertence a OUTRO mapa (colisao, como no incidente), mantem o
            # rotulo velho — a resolucao por id continua certa e o sync
            # nunca quebra por unique constraint.
            em_uso = (SeruLojaMap.query
                      .filter(SeruLojaMap.seru_company_name == seru_company_name,
                              SeruLojaMap.id != mapping.id).first())
            if em_uso is None:
                logger.info('seru: company %s renomeada de "%s" pra "%s"',
                            seru_company_id, mapping.seru_company_name,
                            seru_company_name)
                mapping.seru_company_name = seru_company_name
    if mapping is None and seru_company_name:
        mapping = (SeruLojaMap.query
                   .filter_by(seru_company_name=seru_company_name).first())
        if mapping and seru_company_id and not mapping.seru_company_id:
            mapping.seru_company_id = str(seru_company_id)   # backfill
    if mapping and seru_company_document \
            and mapping.seru_company_document != str(seru_company_document):
        mapping.seru_company_document = str(seru_company_document)  # CNPJ
    if mapping:
        if mapping.ignorar:
            return None, mapping
        if mapping.loja_id:
            return mapping.loja, mapping
        return None, mapping  # pendente
    # primeira vez: tenta fuzzy
    loja = _fuzzy_loja(seru_company_name, lojas_ativas)
    mapping = SeruLojaMap(
        seru_company_name=(seru_company_name
                           or f'company:{seru_company_id}'),
        seru_company_id=str(seru_company_id) if seru_company_id else None,
        seru_company_document=(str(seru_company_document)
                               if seru_company_document else None),
        loja_id=loja.id if loja else None,
        auto_match=bool(loja),
    )
    db.session.add(mapping)
    db.session.flush()
    return loja, mapping


def _resolver_produto(seru_nome, seru_sku):
    """Devolve o VendaMapa(canal='seru') do produto (cria pendente na 1a vez).

    Mapa unificado (substitui SeruProdutoMap). Mesma semantica de estado e fator;
    ja vem backfillado do SeruProdutoMap no cutover de startup."""
    mp = VendaMapa.query.filter_by(canal='seru', nome_externo=seru_nome).first()
    if mp:
        if seru_sku and not mp.sku:
            mp.sku = seru_sku
        return mp
    mp = VendaMapa(canal='seru', nome_externo=seru_nome, sku=seru_sku or None)
    db.session.add(mp)
    db.session.flush()
    return mp


def _baixar_item(loja_id, mapping_produto, qtd, seru_pedido_id, user_id):
    """Baixa de venda Seru via o MOTOR UNICO (`app/services/baixa_venda`).

    O motor resolve a composicao (Produto-cesta -> componentes; receita/produto
    simples -> ele mesmo), aplica o `fator_quantidade` do mapa, acumula fracao
    por item fisico (DebitoEstoque) e decrementa a linha canonica do EstoqueLoja.

    Retorna {baixado: int, faltou: int, ...} — o chamador usa baixado/faltou.
    """
    from app.services.baixa_venda import aplicar_venda
    return aplicar_venda(
        loja_id,
        receita_id=mapping_produto.receita_id,
        produto_id=mapping_produto.produto_id,
        materia_prima_id=getattr(mapping_produto, 'materia_prima_id', None),
        qtd=qtd, fator=mapping_produto.fator_quantidade, canal='seru',
        referencia=f'Seru #{seru_pedido_id}',
        pedido_ref=f'seru:{seru_pedido_id}',
        usuario_id=user_id, nome_venda=mapping_produto.alvo_nome)


def _estornar_pedido(reg, lojas_ativas, user_id):
    """Reverte baixas de um pedido Seru cancelado via o MOTOR UNICO
    (`baixa_venda.estornar_venda`): inteiros pela referencia, fracoes pelo
    DebitoEstoqueMov. So marca `estornado_em` se algo foi revertido.

    Transicao: pedidos baixados ANTES do cutover guardam a fracao em
    SeruDebitoMov (tag '(fator)' no mov). A fase 1 do motor exclui '(fator', e
    `_estornar_fracoes_legado` reverte os SeruDebitoMov ainda nao migrados —
    cobre o pedido cancelado entre o deploy e a migracao de fracoes. Removivel
    quando nao restar SeruDebitoMov pendente.
    """
    from app.services.baixa_venda import estornar_venda
    pid = str(reg.seru_pedido_id)
    res = estornar_venda('seru', f'seru:{pid}', f'Seru #{pid}',
                         usuario_id=user_id, loja_id=reg.loja_id)
    legado = _estornar_fracoes_legado(pid, user_id)
    if res['revertido_inteiros'] or res['revertido_fracoes'] or legado:
        reg.estornado_em = agora()


def _estornar_fracoes_legado(pid, user_id):
    """TRANSICAO: reverte fracoes de pedidos baixados ANTES do cutover que ainda
    nao foram migrados (SeruDebitoMov pendente, sem DebitoEstoqueMov). Mesma
    logica da fase 2 antiga (SeruDebitoMov -> SeruDebito). A migracao de fracoes
    marca `estornado_em` nos convertidos, entao nao ha dupla reversao com o
    motor novo. Retorna quantas fracoes reverteu."""
    from app.services.estoque_helpers import serializar_lojas
    fracoes = SeruDebitoMov.query.filter_by(
        seru_pedido_id=pid, estornado_em=None).all()
    # Serializa as lojas das fracoes antes do 1o UPDATE em EstoqueLoja. Dentro
    # do Seru ja vem coberto pela trava de todas as lojas ativas; reentrante.
    serializar_lojas({fm.loja_id for fm in fracoes})
    revertido = 0
    for fm in fracoes:
        debito = SeruDebito.query.filter_by(
            loja_id=fm.loja_id, seru_produto_map_id=fm.seru_produto_map_id).first()
        if not debito:
            fm.estornado_em = agora()
            continue
        novo = float(debito.fracao_pendente or 0.0) - float(fm.fracao)
        if novo < -1e-9:
            inteiros_devolver = int(-novo + 1.0 - 1e-9)
            mapping = SeruProdutoMap.query.get(fm.seru_produto_map_id)
            if mapping and (mapping.receita_id or mapping.produto_id):
                filtro = {'loja_id': fm.loja_id}
                if mapping.receita_id:
                    filtro['receita_id'] = mapping.receita_id
                else:
                    filtro['produto_id'] = mapping.produto_id
                el = EstoqueLoja.query.filter_by(**filtro).first()
                if el:
                    el.quantidade = (el.quantidade or 0) + inteiros_devolver
                    db.session.add(MovEstoqueLoja(
                        estoque_loja_id=el.id, tipo='venda_seru_estorno',
                        quantidade=inteiros_devolver,
                        referencia=f'Estorno Seru #{pid} (fracao residual)',
                        usuario_id=user_id))
                    novo = novo + inteiros_devolver
        debito.fracao_pendente = max(0.0, round(novo, 6))
        fm.estornado_em = agora()
        revertido += 1
    return revertido


def processar_pedidos(data_inicial, data_final, user=None,
                      expandir_dias_frente=0):
    """Sincroniza Seru → EstoqueLoja no intervalo.

    Retorna dict com resumo: pedidos novos, pedidos cancelados,
    itens baixados, itens ignorados, itens pendentes (novos),
    sem estoque, lojas pendentes.
    """
    user_id = getattr(user, 'id', None) if user else None
    lojas_ativas = Loja.query.filter_by(ativa=True).all()

    # Serializa TODAS as lojas (ordem crescente de id) no inicio desta transacao
    # unica multi-loja: cobre as baixas e estornos internos por reentrancia, e a
    # ordem canonica evita deadlock de advisory lock com caminhos single-loja
    # (checkout, balanco) e com o outro multi-loja (aplicacao de NF). Trava
    # inclusive lojas inativas — barato e a prova de pedido mapeado pra elas.
    # Pego DEPOIS do fetch da API (abaixo): pegar antes reteria o lock durante
    # o I/O de rede (potencialmente lento em backfill), bloqueando as lojas
    # sem necessidade e podendo estourar idle_in_transaction_timeout.
    from app.services.estoque_helpers import serializar_lojas

    pedidos = seru.listar_pedidos_completo(
        data_inicial, data_final, expandir_dias_frente=expandir_dias_frente)

    # Pre-busca dos XMLs de NF dos pedidos SEM itens (99Food etc.) ANTES do
    # serializar_lojas — MESMO motivo do fetch da API acima: I/O de rede
    # segurando o lock de todas as lojas travaria checkout/balanco/
    # desperdicio enquanto o S3 responde (achado de revisao 19/07).
    # So pedido NOVO (nao processado), nao cancelado e dentro da janela.
    nf_cache = {}
    for p in pedidos:
        if not isinstance(p, dict) or seru.pedido_cancelado(p):
            continue
        pid = str(p.get('id') or p.get('orderNumber')
                  or p.get('code') or '').strip()
        if not pid or seru.extrair_itens(p):
            continue
        d = seru.data_local(p.get('createdAt'))
        if not d or not (data_inicial <= d <= data_final):
            continue
        if SeruPedidoProcessado.query.get(pid):
            continue                      # ja processado: nunca re-baixa XML
        nf_cache[pid] = seru.itens_da_nf(p)

    serializar_lojas(r.id for r in Loja.query.with_entities(Loja.id).all())

    stats = {
        'pedidos_novos': 0,
        'pedidos_ja_processados': 0,
        'pedidos_cancelados_estornados': 0,
        'pedidos_sem_loja_mapeada': 0,
        'pedidos_aguardando_loja': 0,
        'itens_baixados': 0,
        'itens_ignorados': 0,
        'itens_pendentes_novos': 0,
        'itens_sem_estoque': 0,
        'pedidos_aguardando_nf': 0,
        'erros': [],
    }

    for p in pedidos:
        if not isinstance(p, dict):
            continue
        pid = str(p.get('id') or p.get('orderNumber') or p.get('code') or '').strip()
        if not pid:
            continue
        # Filtra createdAt dentro da janela BRT solicitada
        d = seru.data_local(p.get('createdAt'))
        if not d or not (data_inicial <= d <= data_final):
            continue

        reg = SeruPedidoProcessado.query.get(pid)
        cancelado_at = p.get('canceledAt')

        # Caso ja processado
        if reg:
            stats['pedidos_ja_processados'] += 1
            # Se foi cancelado depois e ainda nao estornamos, estornar
            if cancelado_at and not reg.estornado_em:
                _estornar_pedido(reg, lojas_ativas, user_id)
                reg.cancelado_em = agora()
                stats['pedidos_cancelados_estornados'] += 1
            continue

        # Pedido novo. Cancelado por canceledAt OU por status=='canceled'
        # (caso real 18/07/2026: cobranca cancelada veio com canceledAt
        # VAZIO — sem este guard, um pedido assim COM NF autorizada
        # baixaria estoque de venda cancelada e o estorno, keyed em
        # canceledAt, nunca dispararia). O gatilho de ESTORNO de pedido ja
        # processado segue keyed em canceledAt (decisao separada,
        # documentada no CLAUDE.md).
        if cancelado_at or seru.pedido_cancelado(p):
            # Ja cancelado — registra mas nao processa items
            db.session.add(SeruPedidoProcessado(
                seru_pedido_id=pid,
                cancelado_em=agora(),
                n_itens_total=len(seru.extrair_itens(p)),
                n_itens_baixados=0,
            ))
            continue

        # Resolve loja
        company = p.get('company') or {}
        cname = ''
        cid = None
        if isinstance(company, dict):
            cname = (company.get('name') or '').strip()
            cid = company.get('id')
            cdoc = company.get('document')
        elif isinstance(company, str):
            cname = company.strip()
            cdoc = None

        loja, loja_map = _resolver_loja(cname, lojas_ativas, cid, cdoc)
        if not loja:
            # Sem loja mapeada — registra pedido como processado mas sem baixar
            stats['pedidos_sem_loja_mapeada'] += 1
            db.session.add(SeruPedidoProcessado(
                seru_pedido_id=pid,
                loja_id=None,
                n_itens_total=len(seru.extrair_itens(p)),
                n_itens_baixados=0,
            ))
            continue

        # SALVAGUARDA: so baixa estoque se voce CONFIRMOU o mapeamento da loja.
        # Auto-fuzzy sozinho nao basta — pode ter chutado errado.
        # Pedido fica aguardando, sera retentado na proxima sync depois que
        # voce abrir /pdv/mapeamentos e clicar OK/Vincular.
        if not loja_map.confirmado_em:
            stats['pedidos_aguardando_loja'] += 1
            continue  # NAO marca como processado — retenta depois

        itens = seru.extrair_itens(p)
        if not itens:
            # Pedido SEM itens (delivery 99Food / cobrança avulsa): a NFC-e
            # emitida traz os produtos REAIS — enriquece pra dar baixa
            # (pedido do dono 18/07/2026; nomes da NF = nomes do
            # SeruProdutoMap, mesmo motor de mapeamento de sempre).
            nf_itens = seru.itens_da_nf(p)
            if nf_itens is None:
                # NF existe mas o download/parse falhou: NÃO marca como
                # processado — retenta no próximo ciclo (padrão do
                # "aguardando loja"). Sem NF nenhuma, nf_itens vem [] e o
                # pedido segue o fluxo normal (processado com 0 itens).
                stats['pedidos_aguardando_nf'] = \
                    stats.get('pedidos_aguardando_nf', 0) + 1
                continue
            itens = nf_itens
        n_total = len(itens)
        n_baixados = 0

        for it in itens:
            if it['cancelado']:
                continue
            mp = _resolver_produto(it['nome'], it['sku'])
            if mp.ignorar:
                stats['itens_ignorados'] += 1
                continue
            if mp.estado == 'pendente':
                # Primeira vez OU ja era pendente — apenas conta pra revisao
                if not mp.id or not mp.primeira_visto_em:
                    pass
                stats['itens_pendentes_novos'] += 1
                continue
            # Mapeado — baixa
            res = _baixar_item(loja.id, mp, it['qtd'], pid, user_id)
            if res['baixado']:
                stats['itens_baixados'] += 1
                n_baixados += 1
            if res['faltou']:
                stats['itens_sem_estoque'] += 1

        db.session.add(SeruPedidoProcessado(
            seru_pedido_id=pid,
            loja_id=loja.id,
            n_itens_total=n_total,
            n_itens_baixados=n_baixados,
        ))
        stats['pedidos_novos'] += 1

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.exception('seru_sync commit falhou')
        stats['erros'].append(f'commit: {type(e).__name__}: {str(e)[:200]}')

    return stats


def reprocessar_pedido(pid, user=None):
    """Forca reprocessamento de um pedido (apaga registro + reaplica).
    Util quando o admin acabou de mapear um produto e quer aplicar retroativo.
    NAO estorna baixas anteriores — caller deve garantir que faz sentido.
    """
    reg = SeruPedidoProcessado.query.get(pid)
    if reg:
        db.session.delete(reg)
        db.session.commit()
    # Caller pode chamar processar_pedidos com janela cobrindo o pedido.


def reprocessar_retroativo(dias=7, user=None):
    """RECUPERA baixas perdidas da janela [hoje-dias+1, hoje] (03/07/2026).

    Pedidos processados com ZERO baixa (produto pendente na epoca, ou loja
    nao reconhecida — inclusive os registrados com `loja_id=None`) tem o
    registro apagado (junto com os movs `Seru #<id>` remanescentes, todos
    `*_sem_estoque` por definicao de zero baixa) e a janela e reprocessada —
    agora com os mapeamentos/lojas ATUAIS. Antes disso, mapear um produto so
    valia dali pra frente: o passado ficava sem baixa pra sempre.

    Pedidos PARCIAIS (alguma baixa) NAO sao tocados — reprocessar re-baixaria
    os itens que ja sairam. Eles voltam so na contagem `parciais_na_janela`,
    pro caller reportar como nao-recuperaveis automaticamente.

    Retorna {'liberados', 'parciais_na_janela', 'stats' (do sync)}.
    """
    from datetime import datetime, time, timedelta

    from sqlalchemy import or_

    from app.models import MovEstoqueLoja
    from app.utils import hoje

    dias = max(1, min(int(dias or 7), 30))
    fim = hoje()
    inicio = fim - timedelta(days=dias - 1)
    # processado_em e gravado em UTC; 00:00 BRT = 03:00 UTC.
    inicio_utc = datetime.combine(inicio, time.min) + timedelta(hours=3)

    base = SeruPedidoProcessado.query.filter(
        SeruPedidoProcessado.processado_em >= inicio_utc,
        SeruPedidoProcessado.estornado_em.is_(None),
        SeruPedidoProcessado.cancelado_em.is_(None),
    )
    alvo_q = base.filter(SeruPedidoProcessado.n_itens_baixados == 0)
    ids = [p.seru_pedido_id for p in alvo_q.all()]
    if ids:
        # Delimitador de ESPACO: sem ele, 'Seru #1%' casaria 'Seru #10',
        # 'Seru #123'... e apagaria movs de OUTROS pedidos ao reprocessar o #1.
        # Ref e 'Seru #<id>' exato ou 'Seru #<id> — sem estoque...' (com espaco).
        clauses = []
        for i in ids:
            ref = f'Seru #{i}'
            clauses.append(MovEstoqueLoja.referencia == ref)
            clauses.append(MovEstoqueLoja.referencia.like(ref + ' %'))
        MovEstoqueLoja.query.filter(or_(*clauses)).delete(
            synchronize_session=False)
        alvo_q.delete(synchronize_session=False)
        db.session.commit()

    parciais = base.filter(
        SeruPedidoProcessado.n_itens_baixados > 0,
        SeruPedidoProcessado.n_itens_total
        > SeruPedidoProcessado.n_itens_baixados).count()

    stats = processar_pedidos(inicio, fim, user=user)
    return {'liberados': len(ids), 'parciais_na_janela': int(parciais),
            'stats': stats}


# ── Reprocesso retroativo em BACKGROUND (pos-mapeamento) ────────────────
#
# O reprocesso de 7 dias disparado ao vincular produto/loja rodava SINCRONO
# dentro do request (7 dias de API Seru ≈ 4.000+ pedidos) — a tela de
# mapeamentos ficava minutos carregando a cada clique (08/07/2026). Agora o
# vincular so marca a PENDENCIA (AppConfig) e uma thread drena com
# coalescing: N vinculos seguidos = 1 reprocesso ao final, nao N. A logica
# de negocio do reprocesso (reprocessar_retroativo) nao muda em nada.

FLAG_REPROCESSO = 'pdv_reprocesso_pendente'
ULTIMO_REPROCESSO = 'pdv_reprocesso_ultimo'
LOCK_KEY_REPROCESSO = 7749  # advisory lock — familia do seru_cron (7723-7748)
_REPROCESSO_DIAS = 7

# Fallback pra SQLite/dev (sem advisory lock): exclusao dentro do processo.
# No nivel do modulo (nao lazy) — init preguicoso tinha corrida de dupla
# criacao entre threads.
_LOCK_LOCAL = threading.Lock()


def reprocesso_pendente():
    """True se ha pendencia agendada (flag = nonce; '0'/vazio = quitada)."""
    from app.models import AppConfig
    v = AppConfig.get(FLAG_REPROCESSO)
    return bool(v) and v != '0'


def agendar_reprocesso_retroativo(dias=_REPROCESSO_DIAS, user_id=None):
    """Marca a pendencia e garante que UM drenador esteja rodando.

    A flag guarda um NONCE (timestamp), nao '1': o drain so a quita com
    compare-and-clear apos SUCESSO — thread morta no meio (deploy) deixa a
    pendencia de pe, e o cron de 15min retoma (`retomar_reprocesso_pendente`).
    Coalescing: vinculos durante a rodada gravam nonce novo e o
    compare-and-clear falha → nova passada. Nunca ha 2 reprocessos
    simultaneos (lock LOCK_KEY_REPROCESSO, compartilhado com o botao manual).
    Em teste (PYTEST_RUNNING) drena INLINE (deterministico)."""
    import os

    from flask import current_app

    from app.models import AppConfig
    AppConfig.set(FLAG_REPROCESSO, agora().isoformat())
    db.session.commit()
    app_obj = current_app._get_current_object()
    if os.environ.get('PYTEST_RUNNING'):
        _drain_reprocesso(app_obj, dias, user_id)
        return
    threading.Thread(target=_drain_reprocesso, args=(app_obj, dias, user_id),
                     daemon=True).start()


def retomar_reprocesso_pendente(app_obj):
    """Chamado pelo cron do Seru (15min): retoma pendencia orfa — drenador
    morto em deploy, erro de API na tentativa anterior, ou flag setada na
    janela unlock/recheck de outro worker. No-op sem pendencia."""
    with app_obj.app_context():
        if not reprocesso_pendente():
            return
    _drain_reprocesso(app_obj, _REPROCESSO_DIAS, None)


def _drain_reprocesso(app_obj, dias, user_id):
    """Drena a pendencia sob lock global. Depois de soltar o lock, re-checa
    a flag (vinculo na janela entre o ultimo check e o unlock teria o
    drenador dele desistindo ao ver o lock ocupado). Nunca propaga excecao
    — e o target de thread."""
    try:
        with app_obj.app_context():
            while True:
                rodou = _com_lock_reprocesso(lambda: _drain_flag(dias, user_id))
                if rodou in ('ocupado', 'erro'):
                    return
                if not reprocesso_pendente():
                    return
    except Exception:  # noqa: BLE001 — target de thread: nunca propagar
        logger.exception('drenador de reprocesso retroativo morreu')


def _com_lock_reprocesso(fn):
    """Roda fn() sob o lock global de reprocesso (advisory 7749; fallback
    threading.Lock fora do Postgres). Devolve 'ocupado' se outro
    worker/thread segura o lock; senao, o retorno de fn()."""
    from sqlalchemy import text
    if db.engine.dialect.name != 'postgresql':
        if not _LOCK_LOCAL.acquire(blocking=False):
            return 'ocupado'
        try:
            return fn()
        finally:
            _LOCK_LOCAL.release()
    # Advisory lock de sessao: lock e unlock na MESMA conexao (licao do
    # _com_lock do seru_cron — unlock em conexao do pool deixa o lock preso).
    conn = db.engine.connect()
    try:
        got = bool(conn.execute(text('SELECT pg_try_advisory_lock(:k)'),
                                {'k': LOCK_KEY_REPROCESSO}).scalar())
        if not got:
            return 'ocupado'
        try:
            return fn()
        finally:
            try:
                conn.execute(text('SELECT pg_advisory_unlock(:k)'),
                             {'k': LOCK_KEY_REPROCESSO})
            except Exception:  # noqa: BLE001 — conexao morta; lock some com ela
                pass
    finally:
        conn.close()


def _quitar_flag(v0):
    """Compare-and-clear: quita a pendencia SO se o nonce nao mudou. Vinculo
    durante a rodada grava nonce novo → o clear falha → nova passada."""
    from app.models import AppConfig
    AppConfig.query.filter_by(key=FLAG_REPROCESSO, value=v0).update(
        {'value': '0'}, synchronize_session=False)


def _drain_flag(dias, user_id=None):
    """Loop: enquanto ha pendencia, roda o reprocesso e quita a flag SO no
    sucesso (compare-and-clear do nonce). Erro/parcial: a flag FICA de pe —
    o cron de 15min (ou o proximo vinculo) retenta — e para aqui, sem loop
    infinito. `user_id` preserva a autoria nos MovEstoqueLoja."""
    from app.models import AppConfig, Usuario
    usuario = Usuario.query.get(user_id) if user_id else None
    while True:
        v0 = AppConfig.get(FLAG_REPROCESSO)
        if not v0 or v0 == '0':
            return 'ok'
        try:
            res = reprocessar_retroativo(dias=dias, user=usuario)
        except Exception as e:  # noqa: BLE001 — thread: nunca propagar
            logger.exception('reprocesso retroativo em background falhou')
            try:
                db.session.rollback()
                AppConfig.set(ULTIMO_REPROCESSO,
                              'erro em %s: %s'
                              % (agora().strftime('%d/%m %H:%M'),
                                 str(e)[:180]))
                db.session.commit()
            except Exception:  # noqa: BLE001 — DB fora; o log acima ja registrou
                pass
            return 'erro'
        st = res.get('stats') or {}
        if st.get('erros'):
            # Commit falhou dentro do processar_pedidos (baixas revertidas).
            # NAO quita a pendencia nem grava 'ok' — o cron retenta.
            # `erros` e LISTA de mensagens (seru_sync ~l.270), nao contador.
            AppConfig.set(ULTIMO_REPROCESSO,
                          'parcial em %s: %d erro(s) no reprocesso — '
                          'nova tentativa no proximo ciclo'
                          % (agora().strftime('%d/%m %H:%M'),
                             len(st.get('erros') or [])))
            db.session.commit()
            return 'erro'
        _quitar_flag(v0)
        AppConfig.set(ULTIMO_REPROCESSO,
                      'ok em %s: %d pedido(s) liberado(s), %d item(ns) '
                      'baixado(s), %d parciais fora'
                      % (agora().strftime('%d/%m %H:%M'),
                         res.get('liberados', 0),
                         st.get('itens_baixados', 0),
                         res.get('parciais_na_janela', 0)))
        db.session.commit()


def reprocessar_retroativo_manual(dias=30, user=None):
    """Botao da Saude do PDV: roda sob o MESMO lock do drain — reprocesso
    manual e background NUNCA simultaneos (o DELETE de movs de um apagaria
    baixas recem-criadas pelo outro). Sucesso tambem quita a pendencia
    agendada, se houver. Devolve ('ok', res) ou ('ocupado', None)."""
    from app.models import AppConfig

    def _run():
        v0 = AppConfig.get(FLAG_REPROCESSO)
        res = reprocessar_retroativo(dias=dias, user=user)
        st = res.get('stats') or {}
        if st.get('erros'):
            # Commit falhou (baixas revertidas): NAO quita pendencia nem
            # grava 'ok' — o caller reporta e o operador re-tenta.
            AppConfig.set(ULTIMO_REPROCESSO,
                          'parcial manual em %s: %d erro(s) — baixas da '
                          'rodada revertidas'
                          % (agora().strftime('%d/%m %H:%M'),
                             len(st.get('erros') or [])))
        else:
            if v0 and v0 != '0':
                _quitar_flag(v0)
            AppConfig.set(ULTIMO_REPROCESSO,
                          'ok manual em %s: %d pedido(s) liberado(s), '
                          '%d item(ns) baixado(s)'
                          % (agora().strftime('%d/%m %H:%M'),
                             res.get('liberados', 0),
                             st.get('itens_baixados', 0)))
        db.session.commit()
        return res

    r = _com_lock_reprocesso(_run)
    if r == 'ocupado':
        return 'ocupado', None
    return 'ok', r
