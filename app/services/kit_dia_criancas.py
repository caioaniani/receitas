"""Cadastro do Kit Dia das Crianças 2026 (pedido do dono, 07/10/2026).

"Cadastra esse Kit Dia das Crianças para vender R$120,00 ... até dia 12 às
18:00". Composição informada pelo dono: Brioche do Mickey (receita NOVA, sem
ficha — o dono completa), bisnaga de chocolate e bisnaga de confete (MPs
compradas/montadas, custo pendente), Croissant Tradicional e Salada de
Frutas 100g. Uma unidade de cada.

Roda pelo seed one-shot `migrations_legacy._seed_kit_dia_criancas_2026`.
Idempotente: kit já existente (mesmo nome) não é alterado — edição do dono
manda. Nunca escolhe item por aproximação: nome normalizado exato, e item
existente ambíguo ou arquivado recusa com ValueError (nada é gravado).
Sem commit — quem chama decide.
"""
import logging
from datetime import datetime
from pathlib import Path

from app.extensions import db
from app.models import MateriaPrima, Produto, ProdutoItem, Receita
from app.utils import normalizar_busca

logger = logging.getLogger(__name__)

NOME_KIT = 'Kit Dia das Crianças'
PRECO_SITE = 120.0
SITE_ATE = datetime(2026, 10, 12, 18, 0)
CATEGORIA = 'Cestas'
DESCRICAO = ('Croissant tradicional, brioche do Mickey, salada de frutas e '
             'bisnagas de chocolate e de confete.')
FOTO = Path(__file__).resolve().parent.parent / 'seeds_data' / 'kit_dia_criancas_2026.jpg'

NOME_BRIOCHE = 'Brioche do Mickey'
MPS = ('Bisnaga de chocolate', 'Bisnaga de confete')


def _unico(query_itens, nome, rotulo):
    alvo = normalizar_busca(nome).strip()
    achados = [x for x in query_itens if normalizar_busca(x.nome).strip() == alvo]
    if len(achados) > 1:
        raise ValueError(f'{rotulo} "{nome}" aparece {len(achados)} vezes no cadastro.')
    return achados[0] if achados else None


def _receita_existente(nome):
    r = _unico(Receita.ativas().all(), nome, 'Receita')
    if not r:
        raise ValueError(f'Receita "{nome}" não encontrada (ativa).')
    return r


def _brioche():
    r = _unico(Receita.ativas().all(), NOME_BRIOCHE, 'Receita')
    if r:
        return r, False
    if _unico(Receita.query.filter(Receita.arquivada_em.isnot(None)).all(),
              NOME_BRIOCHE, 'Receita'):
        raise ValueError(f'"{NOME_BRIOCHE}" existe ARQUIVADA — desarquive ou renomeie.')
    base = _unico(Receita.ativas().all(), 'Brioche', 'Receita')
    r = Receita(nome=NOME_BRIOCHE, categoria=(base.categoria if base else None) or '',
                rendimento_qtd=1, rendimento_unidade='unidades', peso_base=1000,
                site_ativo=False)
    db.session.add(r)
    db.session.flush()
    return r, True


def _mp(nome):
    todas = MateriaPrima.query.all()
    mp = _unico(todas, nome, 'Matéria-prima')
    if mp:
        if mp.arquivada_em is not None:
            raise ValueError(f'Matéria-prima "{nome}" está ARQUIVADA — desarquive.')
        return mp, False
    mp = MateriaPrima(nome=nome, unidade='un', custo_por_kg=None,
                      sugerir_pedido_loja=False)
    db.session.add(mp)
    db.session.flush()
    return mp, True


def _subir_foto(kit):
    """Best-effort: sem Dropbox ou com falha, o kit fica sem foto (o dono
    sobe no cadastro). Mesmo caminho/compressão do upload da tela."""
    from app.services import dropbox_storage
    from app.utils import comprimir_imagem
    if not FOTO.exists() or not dropbox_storage.disponivel():
        return False
    try:
        final = comprimir_imagem(FOTO.read_bytes(), max_size=1600, quality=88)
        info = dropbox_storage.upload_publico(
            final, f'/cardapio/produto/{kit.id}.jpg', mode='overwrite', autorename=False)
        kit.imagem_dropbox_url = info['url']
        kit.imagem_storage_path = info['storage_path']
        kit.imagem_mimetype = 'image/jpeg'
        return True
    except Exception:  # foto é acessória: falha nunca impede o cadastro
        logger.exception('kit dia das criancas: falha ao subir a foto')
        return False


def criar(subir_foto=True):
    """Cria o kit (e o que faltar da composição). Devolve resumo dict."""
    existente = _unico(Produto.query.all(), NOME_KIT, 'Produto')
    if existente:
        return {'kit_id': existente.id, 'criado': False}
    croissant = _receita_existente('Croissant Tradicional')
    salada = _unico(Produto.query.filter(Produto.ativo.is_(True)).all(),
                    'Salada de Frutas 100g', 'Produto')
    if not salada:
        raise ValueError('Produto "Salada de Frutas 100g" não encontrado (ativo).')
    brioche, brioche_novo = _brioche()
    mps = [_mp(n) for n in MPS]

    kit = Produto(nome=NOME_KIT, categoria=CATEGORIA, descricao=DESCRICAO,
                  preco_site=PRECO_SITE, site_ativo=True, ativo=True,
                  site_ate=SITE_ATE)
    db.session.add(kit)
    db.session.flush()
    itens = [
        ProdutoItem(produto_id=kit.id, tipo='receita', receita_id=croissant.id,
                    item_nome=croissant.nome, quantidade=1),
        ProdutoItem(produto_id=kit.id, tipo='receita', receita_id=brioche.id,
                    item_nome=brioche.nome, quantidade=1),
        ProdutoItem(produto_id=kit.id, tipo='produto', produto_componente_id=salada.id,
                    item_nome=salada.nome, quantidade=1),
    ] + [ProdutoItem(produto_id=kit.id, tipo='mp', materia_prima_id=mp.id,
                     item_nome=mp.nome, quantidade=1) for mp, _ in mps]
    db.session.add_all(itens)
    foto = _subir_foto(kit) if subir_foto else False
    db.session.flush()
    return {'kit_id': kit.id, 'criado': True, 'brioche_id': brioche.id,
            'brioche_novo': brioche_novo,
            'mps': {mp.nome: (mp.id, novo) for mp, novo in mps},
            'componentes': len(itens), 'foto': foto}


NOME_ABRACO = 'Abraço em Forma de Pães'
NOME_CAIXA_MINI = 'Caixa de Mini'


def ajustar_d2_e_fiscal():
    """Dono 07/10/2026: "No kit do dia das crianças adotar o mesmo ncm do
    abraço em forma de pães e colocar para o cliente conseguir comprar com
    D+2, mesma regra e processo da caixa de mini".

    - D+2 = `Produto.sob_encomenda` (a regra da Caixa de Mini): data mínima
      D+2, sem express, não abate a prateleira, entra na produção do padeiro
      (card, pré-preparo, demanda firme) e baixa a indústria na saída.
    - NCM: o fiscal mora no cadastro do produto no Tiny e a nota manda só o
      SKU; o kit passa a usar o SKU (canal site) do Abraço — a descrição da
      nota continua sendo o nome do kit. Só copia SKU CONFIRMADO (mesma régua
      de `tiny_nf_transf._sku_confirmado`) e nunca sobrescreve SKU do kit que
      um humano tenha definido.
    Sem commit. Devolve resumo (o seed grava no marcador)."""
    from app.models import TinyProdutoMap
    from app.utils import agora
    kit = _unico(Produto.query.all(), NOME_KIT, 'Produto')
    if not kit:
        raise ValueError(f'Produto "{NOME_KIT}" não encontrado.')
    abraco = _unico(Produto.query.all(), NOME_ABRACO, 'Produto')
    caixa = _unico(Produto.query.all(), NOME_CAIXA_MINI, 'Produto')
    kit.sob_encomenda = True
    resumo = {'kit_id': kit.id, 'sob_encomenda': True,
              'caixa_mini_sob_encomenda': bool(caixa and caixa.sob_encomenda),
              'sku': None, 'sku_motivo': ''}
    if not abraco:
        resumo['sku_motivo'] = 'abraco_nao_encontrado'
        return resumo
    m_ab = TinyProdutoMap.query.filter_by(canal='site', kind='produto',
                                          item_id=abraco.id).first()
    sku = (m_ab.tiny_sku or '').strip() if m_ab else ''
    if not sku or (m_ab.auto_match and not m_ab.confirmado_em):
        resumo['sku_motivo'] = 'abraco_sem_sku_confirmado'
        return resumo
    m_kit = TinyProdutoMap.query.filter_by(canal='site', kind='produto',
                                           item_id=kit.id).first()
    if m_kit and m_kit.confirmado_por and (m_kit.tiny_sku or '').strip() not in ('', sku):
        resumo['sku_motivo'] = 'kit_ja_tem_sku_definido_por_humano'
        resumo['sku'] = m_kit.tiny_sku
        return resumo
    if not m_kit:
        m_kit = TinyProdutoMap(canal='site', kind='produto', item_id=kit.id)
        db.session.add(m_kit)
    m_kit.tiny_sku = sku
    m_kit.tiny_nome = m_ab.tiny_nome
    m_kit.auto_match = False
    m_kit.confirmado_em = agora()
    resumo.update(sku=sku, sku_motivo='copiado_do_abraco', abraco_id=abraco.id)
    return resumo
