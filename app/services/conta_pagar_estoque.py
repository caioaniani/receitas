"""Processa itens de NF (ContaPagar) -> preco + entrada de estoque por empresa.

Quando um item de NF esta vinculado+confirmado a uma MateriaPrima
(ContaPagarItemMap), capturar uma NF nova:
  (a) atualiza o preco da MP (`custo_por_kg`, global);
  (b) da entrada no estoque da EMPRESA do canal — industria no estoque global
      `MateriaPrima.estoque_atual` (de onde a producao baixa), lojas em
      `EstoqueLoja(loja, materia_prima_id)`;
  (c) registra `HistoricoPrecoMP` (fornecedor auto-vinculado/criado pelo nome);
  (d) se o preco variou, gera `VariacaoPrecoMP` (aviso).

Idempotente por (conta, indice do item) via `ContaPagarItemProcessado` —
reprocessar a mesma NF nao duplica entrada nem historico. Espelha o
seru_sync.processar_pedidos (mesmas salvaguardas).

Dinheiro/estoque tem peso especial (CLAUDE.md): conversao exata, 1 commit no
fim, sem arredondamento silencioso de quantidade.
"""
import json
import logging

from sqlalchemy import func

from app.extensions import db
from app.models import (
    ContaPagarItemMap,
    ContaPagarItemProcessado,
    EstoqueLoja,
    EstoqueProducao,
    Fornecedor,
    HistoricoPrecoMP,
    MovEstoqueLoja,
    MovEstoqueProducao,
    MovimentacaoEstoque,
    SlackCanalLojaMap,
    VariacaoPrecoMP,
)
from app.utils import resolver_loja_por_nome

logger = logging.getLogger(__name__)


def limpar_nome_item(nome):
    """Nome do produto SEM validade/lote/datas — pra exibir e agrupar. Tudo a
    partir do primeiro marcador (VAL/LOTE/...) e descartado, pois muda a cada
    compra e fragmentaria o mesmo produto em varios vinculos.
    Ex: 'FARINHA ... T45 VAL 1/2026 LOTE GXB12603' -> 'FARINHA ... T45'."""
    import re
    nome = (nome or '').strip()
    nome = re.sub(r'\b(?:VAL|VALIDADE|VENC(?:IMENTO)?|LOTE|FAB|FABR)\b.*$', '',
                  nome, flags=re.IGNORECASE | re.DOTALL)
    return re.sub(r'\s+', ' ', nome).strip(' .:;,-/')


def normalizar_item_nome(nome):
    """Chave de reuso do mapeamento item->MP: nome limpo (sem validade/lote),
    sem acento/pontuacao, minusculo. Mesmo produto -> mesma chave."""
    import re

    from app.services.estoque_loja_lote import _ascii
    base = _ascii(limpar_nome_item(nome))
    base = re.sub(r'[^\w\s]', ' ', base)
    return re.sub(r'\s+', ' ', base).strip()


def migrar_nomes_itens():
    """One-shot: re-normaliza os ContaPagarItemMap com a regra nova (ignora
    validade/lote) e MESCLA os que passam a colidir, preservando os vinculos
    confirmados. Idempotente. Retorna stats.

    Grupos com mais de uma MP confirmada distinta sao deixados como estao
    (conflito real — humano revisa); nunca apaga um vinculo confirmado."""
    grupos = {}
    for m in ContaPagarItemMap.query.all():
        novo = normalizar_item_nome(m.item_nome_exemplo or '')
        if not novo:
            continue
        grupos.setdefault(novo, []).append(m)
    stats = {'grupos': 0, 'mesclados': 0, 'atualizados': 0, 'conflitos': 0}
    for novo_norm, grupo in grupos.items():
        mps_conf = {m.materia_prima_id for m in grupo
                    if m.confirmado_em and m.materia_prima_id}
        if len(mps_conf) > 1:
            stats['conflitos'] += 1
            logger.warning('migrar_nomes_itens: conflito em "%s" (MPs %s) — '
                           'mantido sem mesclar', novo_norm, mps_conf)
            continue
        stats['grupos'] += 1
        # vencedor: confirmado primeiro, senao o de menor id
        grupo.sort(key=lambda m: (0 if (m.confirmado_em and m.materia_prima_id)
                                  else 1, m.id))
        vencedor = grupo[0]
        for perdedor in grupo[1:]:
            db.session.delete(perdedor)
            stats['mesclados'] += 1
        db.session.flush()  # aplica deletes antes de reescrever a chave unica
        vencedor.item_nome_norm = novo_norm
        vencedor.item_nome_exemplo = limpar_nome_item(vencedor.item_nome_exemplo or '')
        stats['atualizados'] += 1
    db.session.commit()
    return stats


def norms_em_notas():
    """Set de nomes normalizados que aparecem nos itens de ALGUMA conta atual.
    E a referencia de 'esse item existe em NF' — mapeamentos fora desse set
    sao orfaos (sobra de leitura da IA que foi corrigida por re-extracao)."""
    from app.models import ContaPagar
    norms = set()
    contas = ContaPagar.query.filter(ContaPagar.itens_json.isnot(None)).all()
    for c in contas:
        try:
            itens = json.loads(c.itens_json or '[]')
        except (ValueError, TypeError):
            continue
        if not isinstance(itens, list):
            continue
        for it in itens:
            if isinstance(it, dict):
                n = normalizar_item_nome(it.get('nome') or '')
                if n:
                    norms.add(n)
    return norms


def limpar_mapas_orfaos():
    """Apaga ContaPagarItemMap PENDENTES cujo item nao existe em nenhuma conta
    (a re-extracao reescreve itens_json e pode deixar pra tras vinculos de
    leituras erradas — ex: a IA leu o destinatario como item). So pendente
    cru sai: confirmado/ignorado/com MP escolhida e decisao humana e fica.
    Sem perda: se o nome reaparecer numa NF, processar_conta recria pendente.
    Retorna quantos foram removidos."""
    norms = norms_em_notas()
    removidos = 0
    for m in ContaPagarItemMap.query.all():
        if m.confirmado_em or m.ignorar or m.materia_prima_id:
            continue
        if m.item_nome_norm not in norms:
            db.session.delete(m)
            removidos += 1
    db.session.commit()
    return removidos


def sugerir_para_item(nome, unidade_nf=None):
    """Sugestoes de MateriaPrima pra um nome de item (pra UI de mapeamento).

    Reusa o resolver fuzzy do copilot e — quando a unidade da NF e conhecida
    — adiciona candidatos por TOKEN do nome filtrados pela mesma grandeza
    fisica. Sem isso, "Bebida de Aveia Nude 1L" em **un** so devolvia
    "Aveia (g)" (resolver casa por substring "aveia") e o usuario tinha que
    rolar todo o dropdown pra achar "Leite de aveia NUDE (un)".
    """
    from app.services.copilot import _resolver_mp
    nome = (nome or '').strip()
    if not nome:
        return []
    base = _resolver_mp(nome)
    grandeza_nf = _grandeza(unidade_nf)
    if not grandeza_nf:
        return base

    # Candidatos extra: MPs cujo nome compartilha algum token >=4 letras com
    # o item e cuja unidade casa com a grandeza da NF. Limita a 5 — eh
    # complemento, nao substituto.
    import re

    from sqlalchemy import or_

    from app.models import MateriaPrima
    tokens = [t for t in re.findall(r'\w+', nome.lower())
              if len(t) >= 4 and t not in {'litro', 'litros'}]
    extras = []
    if tokens:
        clausulas = [MateriaPrima.nome.ilike(f'%{t}%') for t in tokens]
        candidatos = MateriaPrima.query.filter(or_(*clausulas)).limit(20).all()
        ids_base = {x.get('id') for x in base}
        for mp in candidatos:
            if mp.id in ids_base:
                continue
            if _grandeza(mp.unidade) != grandeza_nf:
                continue
            # ranqueia por # de tokens que aparecem no nome da MP
            score = sum(1 for t in tokens if t in mp.nome.lower())
            extras.append((score, {'id': mp.id, 'nome': mp.nome,
                                   'unidade': mp.unidade, 'match': 'token'}))
        extras.sort(key=lambda x: -x[0])
        base = [e[1] for e in extras[:5]] + base

    # Re-rank por afinidade de unidade: exato > grandeza casa > resto.
    base.sort(key=lambda x: (0 if x.get('match') == 'exato'
                             else (1 if _grandeza(x.get('unidade')) == grandeza_nf
                                   else 2)))
    # dedupe preservando ordem (extras podem reaparecer no base)
    vistos, out = set(), []
    for s in base:
        if s['id'] in vistos:
            continue
        vistos.add(s['id'])
        out.append(s)
    return out


# Agrupa unidades pelo TIPO fisico (un, massa, volume) — pra re-ranquear
# sugestoes por afinidade de unidade. Une 'g'/'kg' e 'ml'/'l' como mesma
# grandeza pra nao penalizar variacao de prefixo.
def _grandeza(unidade):
    u = (unidade or '').strip().lower()
    if u in ('un', 'unidade', 'unidades', 'cx', 'fardo', 'fd', 'pct',
             'pacote', 'pote', 'sache', 'sc', 'mc', 'pk'):
        return 'un'
    if u in ('g', 'kg', 'mg', 'gramas'):
        return 'massa'
    if u in ('ml', 'l', 'lt', 'litro', 'litros'):
        return 'volume'
    return None


# 1 unidade -> (grandeza-base, quantos da base). So unidades METRICAS entram —
# cx/un/fardo sao embalagem: o conteudo delas e o fator, nao uma conversao.
_UNIDADES_METRICAS = {
    'kg': ('g', 1000.0), 'g': ('g', 1.0), 'mg': ('g', 0.001),
    'l': ('ml', 1000.0), 'lt': ('ml', 1000.0), 'litro': ('ml', 1000.0),
    'litros': ('ml', 1000.0), 'ml': ('ml', 1.0),
}


def conversao_metrica(unidade_de, unidade_para):
    """Fator fisico de "1 <unidade_de> = X <unidade_para>" quando AMBAS sao
    metricas da mesma grandeza (kg->g: 1000.0; l->ml: 1000.0; g->g: 1.0).
    None quando nao se aplica (cx/un/fardo, ou massa vs volume)."""
    de = _UNIDADES_METRICAS.get((unidade_de or '').strip().lower())
    para = _UNIDADES_METRICAS.get((unidade_para or '').strip().lower())
    if not de or not para or de[0] != para[0]:
        return None
    return de[1] / para[1]


def prefill_sugestao(ia_fator, ia_unidade, unidade_mp, unidade_nf):
    """(fator, unidade_compra) pre-preenchidos num form de vinculo item->MP
    (tela de mapeamentos e detalhe da conta).

    A IA le o conteudo da embalagem no nome do item ('CX 1,8KG' -> 1.8 com
    unidade kg), mas a entrada de estoque multiplica a QTD DA NF pelo fator
    NA UNIDADE DA MP (`processar_conta`, qtd_estoque = qtd_nf * fator) —
    confirmar 1.8 cru numa MP em gramas daria entrada de 1,8 g por caixa.
    Regras, na ordem:
      1. NF quantificada em unidade metrica (farinha comprada por kg):
         fator = conversao fisica pura (kg->g: 1000); o tamanho da
         embalagem que a IA leu no nome e irrelevante.
      2. MP contada em 'un' + sugestao da IA em unidade METRICA (ml/l/g/
         kg/mg): a IA leu o CONTEUDO da embalagem no nome ("Bebida de
         Aveia Nude 1L" -> 1000 ml, 2026-06-10), mas conteudo nao conta
         unidades — o fator da IA e descartado. NF tambem na familia 'un'
         (un/cx/fd/pct...): fator = 1.0 (1 caixinha = 1 un); NF em outra
         grandeza: fator vazio (None), sem chute.
      3. NF em embalagem (cx/un/fd) + sugestao da IA em unidade metrica:
         converte o conteudo pra unidade da MP (1.8 kg -> 1800 g) e usa a
         unidade de compra real da NF. (So MP metrica chega aqui — MP em
         'un' cai na regra 2.)
      4. Resto: sugestao crua da IA, como antes.
    """
    try:
        ia_fator = float(ia_fator) if ia_fator is not None else None
    except (TypeError, ValueError):
        ia_fator = None
    unidade_nf = (unidade_nf or '').strip()
    conv_nf = conversao_metrica(unidade_nf, unidade_mp)
    if conv_nf is not None:
        return conv_nf, unidade_nf
    # Regra 2 — MP contavel ('un') com sugestao metrica da IA (caso
    # "Bebida de Aveia Nude 1L", 2026-06-10: NF em 'un', IA sugeriu
    # 1000/ml lendo o conteudo; prefill mostrava "1 un = 1000 un").
    if (_grandeza(unidade_mp) == 'un'
            and _grandeza(ia_unidade) in ('massa', 'volume')):
        fator = 1.0 if _grandeza(unidade_nf) == 'un' else None
        return fator, (unidade_nf or ia_unidade)
    conv_ia = conversao_metrica(ia_unidade, unidade_mp)
    if ia_fator and conv_ia is not None:
        return ia_fator * conv_ia, (unidade_nf or ia_unidade)
    return ia_fator, ia_unidade


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _resolver_ou_criar_fornecedor(nome):
    """Acha um Fornecedor pelo nome (exato case-insensitive, depois ilike) ou
    cria um novo. Garante que o historico de preco sempre tenha fornecedor."""
    nome = (nome or '').strip()
    if not nome:
        return None
    f = Fornecedor.query.filter(func.lower(Fornecedor.nome) == nome.lower()).first()
    if f:
        return f
    f = Fornecedor.query.filter(Fornecedor.nome.ilike(f'%{nome}%')).first()
    if f:
        return f
    f = Fornecedor(nome=nome, ativo=True)
    db.session.add(f)
    db.session.flush()
    return f


def _nome_canal_config(canal_id):
    """Nome amigavel do canal em SLACK_CANAIS_NF_NOMES (config) — pra auto-fuzzy."""
    from flask import current_app
    raw = (current_app.config.get('SLACK_CANAIS_NF_NOMES') or '')
    for par in raw.split(';'):
        if '=' in par:
            cid, nome = par.split('=', 1)
            if cid.strip() == canal_id:
                return nome.strip()
    return None


def resolver_canal_map(canal_id):
    """Acha (ou cria, pendente) o SlackCanalLojaMap de um canal. Na primeira
    aparicao tenta auto-fuzzy pelo nome configurado — mas so processa estoque
    apos o admin confirmar (confirmado_em). Espelha SeruLojaMap."""
    if not canal_id:
        return None
    m = SlackCanalLojaMap.query.filter_by(canal_id=canal_id).first()
    if m:
        return m
    m = SlackCanalLojaMap(canal_id=canal_id)
    nome_cfg = _nome_canal_config(canal_id)
    if nome_cfg:
        loja = resolver_loja_por_nome(nome_cfg)
        if loja:
            m.loja_id = loja.id
            m.auto_match = True
            m.eh_industria = (normalizar_item_nome(loja.nome) == 'industria')
    db.session.add(m)
    db.session.flush()
    return m


def _carregar_itens(conta):
    try:
        itens = json.loads(conta.itens_json or '[]')
    except (ValueError, TypeError):
        logger.warning('conta %s: itens_json invalido', conta.id)
        return []
    return itens if isinstance(itens, list) else []


def processar_conta(conta, user_id=None, aovivo=True):
    """Processa os itens da conta. `aovivo=False` (importacao de historico) so
    cria/garante os mapeamentos pendentes — NAO da entrada de estoque nem
    altera custo (decisao do usuario: historico nao mexe no estoque).

    Retorna dict de stats. Faz UM commit no fim (transacao unica).
    """
    stats = {
        'processados': 0, 'pendentes_novos': 0, 'pendentes': 0, 'ignorados': 0,
        'ja_processados': 0, 'dados_invalidos': 0, 'canal_nao_confirmado': 0,
        'fracao_loja_pendente': 0, 'variacoes': 0, 'historico_sem_estoque': 0,
    }
    itens = _carregar_itens(conta)
    if not itens:
        return stats

    canal_map = resolver_canal_map(conta.origem_canal)

    for i, item in enumerate(itens):
        if not isinstance(item, dict):
            continue
        nome = (item.get('nome') or '').strip()
        if not nome:
            continue
        norm = normalizar_item_nome(nome)
        if not norm:
            continue

        mapa = ContaPagarItemMap.query.filter_by(item_nome_norm=norm).first()
        if not mapa:
            mapa = ContaPagarItemMap(
                item_nome_norm=norm,
                item_nome_exemplo=nome,
                ia_unidade_sugerida=(item.get('unidade_base_sugerida')
                                     or item.get('unidade')),
                ia_fator_sugerido=item.get('fator_embalagem'),
            )
            db.session.add(mapa)
            stats['pendentes_novos'] += 1
            continue

        # Importacao de historico: garante o mapa, mas nunca mexe no estoque.
        if not aovivo:
            stats['historico_sem_estoque'] += 1
            continue

        if mapa.ignorar:
            stats['ignorados'] += 1
            continue
        if not mapa.processavel:
            stats['pendentes'] += 1
            continue

        # Empresa do canal precisa estar confirmada (senao estoque iria pro
        # lugar errado). Retenta quando o admin confirmar.
        if not (canal_map and canal_map.confirmado_em and not canal_map.ignorar
                and (canal_map.eh_industria or canal_map.loja_id)):
            stats['canal_nao_confirmado'] += 1
            continue

        # Idempotencia: ja processado? (antes de QUALQUER escrita)
        if db.session.get(ContaPagarItemProcessado, (conta.id, i)):
            stats['ja_processados'] += 1
            continue

        qtd_nf = _to_float(item.get('quantidade'))
        vtot = _to_float(item.get('valor_total'))
        if vtot <= 0:
            vtot = _to_float(item.get('valor_unitario')) * qtd_nf
        if qtd_nf <= 0 or vtot <= 0:
            stats['dados_invalidos'] += 1
            continue

        fator = mapa.fator_conversao or 1.0
        if fator <= 0:
            fator = 1.0
        preco_por_compra = vtot / qtd_nf
        qtd_estoque = qtd_nf * fator

        forn = _resolver_ou_criar_fornecedor(conta.fornecedor_nome)
        if forn and not conta.fornecedor_id:
            conta.fornecedor_id = forn.id

        ref = f'NF {conta.nf_numero or ("conta " + str(conta.id))}'

        # ── Alvo PRODUTO de revenda (agua/chiclete/iogurte pronto):
        # estoque em unidades + Produto.custo_direto atualizado por NF.
        # Sem HistoricoPrecoMP/VariacaoPrecoMP (FKs sao de MP) — limitacao
        # consciente; o custo em si fica sempre atual. ──
        if mapa.produto_id:
            produto = mapa.produto
            custo_base = preco_por_compra / fator   # produto e contado em un
            if abs(qtd_estoque - round(qtd_estoque)) > 1e-9:
                stats['fracao_loja_pendente'] += 1
                logger.warning(
                    'conta %s item %d: qtd fracionaria %.4f (produto %s) — '
                    'nao processado (estoque de produto e inteiro)',
                    conta.id, i, qtd_estoque, produto.nome)
                continue
            qtd_int = int(round(qtd_estoque))
            mov_loja = None
            loja_id = None
            if canal_map.eh_industria:
                ep = (EstoqueProducao.query
                      .filter_by(produto_id=produto.id, estado=None).first())
                if not ep:
                    ep = EstoqueProducao(produto_id=produto.id, quantidade=0,
                                         estado=None)
                    db.session.add(ep)
                    db.session.flush()
                anterior = ep.quantidade or 0
                ep.quantidade = anterior + qtd_int
                db.session.add(MovEstoqueProducao(
                    estoque_producao_id=ep.id, tipo='entrada_nf',
                    quantidade=qtd_int,
                    referencia=f'{ref} (era {anterior}, ficou {ep.quantidade})',
                    usuario_id=user_id))
            else:
                loja_id = canal_map.loja_id
                el = (EstoqueLoja.query
                      .filter_by(loja_id=loja_id, produto_id=produto.id,
                                 estado=None).first())
                if not el:
                    el = EstoqueLoja(loja_id=loja_id, produto_id=produto.id,
                                     quantidade=0, estado=None)
                    db.session.add(el)
                    db.session.flush()
                anterior = el.quantidade or 0
                el.quantidade = anterior + qtd_int
                mov_loja = MovEstoqueLoja(
                    estoque_loja_id=el.id, tipo='entrada_nf',
                    quantidade=qtd_int,
                    referencia=f'{ref} (era {anterior}, ficou {el.quantidade})',
                    usuario_id=user_id)
                db.session.add(mov_loja)
            produto.custo_direto = custo_base
            db.session.flush()
            db.session.add(ContaPagarItemProcessado(
                conta_pagar_id=conta.id, item_indice=i, materia_prima_id=None,
                loja_id=loja_id, custo_aplicado=custo_base,
                qtd_estoque=qtd_estoque,
                mov_estoque_loja_id=(mov_loja.id if mov_loja else None)))
            stats['processados'] += 1
            continue

        mp = mapa.materia_prima
        custo_anterior = mp.custo_por_kg
        # custo_por_kg guarda custo por KG (unidade g/kg/ml) ou por UNIDADE ('un').
        # O fator esta na unidade de ESTOQUE da MP; pra g/ml ela e 1000x menor
        # que o kg, entao converte o fator pra kg SO no custo (o estoque continua
        # na unidade da MP — gramas/ml). Ex: acai 10000 g/cx -> custo por kg usa 10.
        unid = (mp.unidade or '').lower()
        fator_custo = fator / 1000.0 if unid in ('g', 'ml') else fator
        custo_base = preco_por_compra / fator_custo if fator_custo else preco_por_compra

        mov_global = None
        mov_loja = None
        loja_id = None

        if canal_map.eh_industria:
            # Estoque global (float) — de onde a producao baixa.
            mp.estoque_atual = (mp.estoque_atual or 0) + qtd_estoque
            mov_global = MovimentacaoEstoque(
                materia_prima_id=mp.id, tipo='entrada', quantidade=qtd_estoque,
                preco_unitario=custo_base, referencia=ref, usuario_id=user_id,
                fornecedor_id=conta.fornecedor_id)
            db.session.add(mov_global)
        else:
            loja_id = canal_map.loja_id
            # EstoqueLoja.quantidade e Integer. Nao arredondar MP fracionaria
            # silenciosamente (estoque tem peso especial) — deixa pendente.
            if abs(qtd_estoque - round(qtd_estoque)) > 1e-9:
                stats['fracao_loja_pendente'] += 1
                logger.warning(
                    'conta %s item %d: qtd fracionaria %.4f em loja %s (MP %s) '
                    '— nao processado (EstoqueLoja e inteiro)',
                    conta.id, i, qtd_estoque, loja_id, mp.nome)
                continue
            qtd_int = int(round(qtd_estoque))
            el = (EstoqueLoja.query
                  .filter_by(loja_id=loja_id, materia_prima_id=mp.id, estado=None)
                  .first())
            if not el:
                el = EstoqueLoja(loja_id=loja_id, materia_prima_id=mp.id,
                                 quantidade=0, estado=None)
                db.session.add(el)
                db.session.flush()
            anterior = el.quantidade or 0
            el.quantidade = anterior + qtd_int
            mov_loja = MovEstoqueLoja(
                estoque_loja_id=el.id, tipo='entrada_nf', quantidade=qtd_int,
                referencia=f'{ref} (era {anterior}, ficou {el.quantidade})',
                usuario_id=user_id)
            db.session.add(mov_loja)

        # Preco (global) + historico de preco.
        mp.custo_por_kg = custo_base
        hist = None
        if conta.fornecedor_id:
            hist = HistoricoPrecoMP(
                materia_prima_id=mp.id, fornecedor_id=conta.fornecedor_id,
                preco_unitario=custo_base, quantidade=qtd_estoque,
                referencia=ref, usuario_id=user_id)
            db.session.add(hist)

        # Aviso de variacao (todas as diferencas, pra mais caro ou mais barato).
        if custo_anterior and abs(custo_base - custo_anterior) > 1e-9:
            pct = (custo_base - custo_anterior) / custo_anterior * 100.0
            db.session.add(VariacaoPrecoMP(
                materia_prima_id=mp.id, conta_pagar_id=conta.id, item_indice=i,
                custo_anterior=custo_anterior, custo_novo=custo_base,
                variacao_pct=pct, fornecedor_id=conta.fornecedor_id, status='novo'))
            stats['variacoes'] += 1

        db.session.flush()  # pega ids das movimentacoes/historico
        db.session.add(ContaPagarItemProcessado(
            conta_pagar_id=conta.id, item_indice=i, materia_prima_id=mp.id,
            loja_id=loja_id, custo_aplicado=custo_base, qtd_estoque=qtd_estoque,
            movimentacao_id=(mov_global.id if mov_global else None),
            mov_estoque_loja_id=(mov_loja.id if mov_loja else None),
            historico_id=(hist.id if hist else None)))
        stats['processados'] += 1

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception('processar_conta %s: falha no commit', conta.id)
        raise
    return stats
