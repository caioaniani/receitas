"""Converte vendas em unidades físicas dos dois itens que serão fermentados.

Somente leitura: não usa saldo, não altera mapas e não gera baixa de estoque.
As composições são as atuais do catálogo; o rastro registra os fatores usados.
"""
import re
import unicodedata
from collections import defaultdict
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import load_only, selectinload

from app.models import Produto, ProdutoItem, Receita, ReceitaIngrediente, VendaMapa
from app.utils import SUB_RECEITA_TIPOS

ZERO = Decimal(0)
UM = Decimal(1)
ALVOS = {'croissant tradicional': 'croissant', 'pain au chocolat': 'pain'}
# Regra de preparo do owner (24/09/2026), sem alterar as fichas/baixas:
# Nutella gera tradicional para fermentar mesmo com retorno na composição;
# Almond não entra. Correspondência exata evita incluir minis e bicolor.
NUTELLA = {'croissant de nutella', 'croissant nutella',
           'croissant nutella com morango', 'croissant de nutella com morango'}
ALMOND = {'croissant almond', 'croissant de amendoas', 'mini croissant almond'}


def normalizar(nome):
    return ' '.join(''.join(
        c for c in unicodedata.normalize('NFKD', nome or '')
        if not unicodedata.combining(c)).lower().split())


def _relevante(nome):
    return bool(re.search(r'croiss|\bpain\b|\bcesta\b|\bkit\b|\bbox\b|\bcombo\b',
                          normalizar(nome)))


def _numero(valor, contexto, *, positivo=False):
    try:
        numero = Decimal(str(valor))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f'{contexto}: quantidade não informada ou inválida.') from None
    if not numero.is_finite() or numero < 0 or (positivo and numero == 0):
        raise ValueError(f'{contexto}: quantidade inválida.')
    return numero


class ConsumoFermentacao:
    """Um catálogo por cálculo, reutilizado para todas as lojas e datas."""

    def __init__(self):
        receitas = Receita.query.options(
            load_only(Receita.id, Receita.nome, Receita.rendimento_qtd,
                      Receita.peso_base, Receita.peso_unitario,
                      Receita.sub_na_amassadeira, Receita.retorno_receita_id,
                      Receita.arquivada_em),
            selectinload(Receita.ingredientes).load_only(
                ReceitaIngrediente.id, ReceitaIngrediente.tipo,
                ReceitaIngrediente.ingrediente_nome,
                ReceitaIngrediente.porcentagem, ReceitaIngrediente.sub_receita_id),
        ).all()
        produtos = Produto.query.options(
            load_only(Produto.id, Produto.nome, Produto.menu_configuravel),
            selectinload(Produto.itens).load_only(
                ProdutoItem.id, ProdutoItem.tipo, ProdutoItem.item_nome,
                ProdutoItem.quantidade, ProdutoItem.receita_id,
                ProdutoItem.produto_componente_id, ProdutoItem.materia_prima_id),
        ).all()
        self.receitas = {r.id: r for r in receitas}
        self.produtos = {p.id: p for p in produtos}
        self.retornos = {r.retorno_receita_id for r in receitas if r.retorno_receita_id}
        self.alvos = {}
        for nome, grupo in ALVOS.items():
            candidatos = [r for r in receitas if r.arquivada_em is None
                          and normalizar(r.nome) == nome]
            if len(candidatos) != 1:
                raise ValueError(f'{nome}: receita ativa não identificada de forma única.')
            self.alvos[candidatos[0].id] = grupo
        self.retornos_croissant = {r.retorno_receita_id for r in receitas
                                  if self.alvos.get(r.id) == 'croissant'
                                  and r.retorno_receita_id}
        self.mapas = {m.nome_externo: m for m in VendaMapa.query.filter_by(canal='seru').all()}
        self.mapas_normalizados = defaultdict(list)
        for mapa in self.mapas.values():
            self.mapas_normalizados[normalizar(mapa.nome_externo)].append(mapa)
        self._cache = {}

    def _mapa(self, nome):
        if nome in self.mapas:
            return self.mapas[nome]
        candidatos = self.mapas_normalizados.get(normalizar(nome), [])
        if len(candidatos) == 1:
            return candidatos[0]
        if len(candidatos) > 1:
            pode_afetar = _relevante(nome) or any(
                (m.receita_id and self._pode_conter_alvo('receita', m.receita_id))
                or (m.produto_id and self._pode_conter_alvo('produto', m.produto_id))
                for m in candidatos)
            if pode_afetar:
                raise ValueError(f'{nome}: mais de um vínculo do PDV para conferir.')
        return None

    def _unidades_sub(self, ing, receita):
        # Mesmo contrato de utils.unidades_subreceita, em Decimal: o helper
        # público usa /100.0, inadequado para preservar esta conta sem floats.
        qtd = _numero(ing.porcentagem, f'{receita.nome} / {ing.ingrediente_nome}')
        if ing.tipo == 'sub_pct':
            qtd = qtd / Decimal(100) * _numero(receita.peso_base, receita.nome)
        return qtd

    def _rendimento(self, receita):
        # Mesma regra de massa_base.rendimento_massa_crua, sem arredondamentos:
        # montagem usa rendimento cadastrado; massa branca usa massa/peso.
        montagem = False
        massa = ZERO
        for ing in receita.ingredientes:
            tipo = ing.tipo or 'mp'
            valor = _numero(ing.porcentagem, f'{receita.nome} / {ing.ingrediente_nome}')
            if tipo in SUB_RECEITA_TIPOS:
                sub = self.receitas.get(ing.sub_receita_id)
                if sub is None:
                    raise ValueError(f'{receita.nome}: sub-receita {ing.ingrediente_nome} sem vínculo.')
                if sub.sub_na_amassadeira:
                    massa += self._unidades_sub(ing, receita) * _numero(
                        sub.peso_unitario, sub.nome, positivo=True)
                else:
                    montagem = True
            elif tipo == 'mp_direto':
                massa += valor
            elif tipo != 'mp_un':
                massa += valor / Decimal(100) * _numero(receita.peso_base, receita.nome)
        peso = _numero(receita.peso_unitario or 0, receita.nome)
        if not montagem and massa > 0 and peso > 0:
            return massa / peso
        return _numero(receita.rendimento_qtd, f'{receita.nome}: rendimento', positivo=True)

    def _expandir(self, tipo, item_id, pilha=(), incluir_retorno=False):
        chave = (tipo, item_id)
        if chave in pilha:
            raise ValueError('Ciclo na composição: ' + ' → '.join(
                f'{t}:{i}' for t, i in (*pilha, chave)))
        if tipo == 'receita':
            item = self.receitas.get(item_id)
        else:
            item = self.produtos.get(item_id)
        if item is None:
            raise ValueError(f'{tipo} {item_id}: componente sem cadastro.')
        nome = normalizar(item.nome)
        if nome in ALMOND:
            return [(None, UM, [item.nome], 'Almond excluído da lista de fermentação')]
        incluir_retorno = incluir_retorno or nome in NUTELLA
        cache_chave = (*chave, incluir_retorno)
        if cache_chave in self._cache:
            return self._cache[cache_chave]
        grupo = self.alvos.get(item_id) if tipo == 'receita' else None
        if grupo:
            return [(grupo, UM, [item.nome], 'fermentar')]
        if tipo == 'receita' and item_id in self.retornos:
            if incluir_retorno and item_id in self.retornos_croissant:
                return [('croissant', UM, [item.nome],
                         'Nutella: incluir tradicional novo na fermentação conforme regra do owner')]
            return [(None, UM, [item.nome], 'retorno já assado; não fermentar novamente')]
        filhos = []
        if tipo == 'produto':
            if item.menu_configuravel:
                raise ValueError(f'{item.nome}: menu configurável exige composição efetivamente vendida.')
            if not item.itens and _relevante(item.nome):
                raise ValueError(f'{item.nome}: produto sem composição para conferir.')
            for filho in item.itens:
                qtd = _numero(filho.quantidade, f'{item.nome} / {filho.item_nome}')
                if qtd == 0:
                    continue
                if filho.tipo == 'mp':
                    continue
                if filho.tipo == 'receita':
                    alvo = filho.receita_id
                elif filho.tipo == 'produto':
                    alvo = filho.produto_componente_id
                else:
                    raise ValueError(f'{item.nome}: tipo de componente inválido em {filho.item_nome}.')
                if not alvo:
                    raise ValueError(f'{item.nome}: componente {filho.item_nome} sem vínculo.')
                filhos.append((filho.tipo, alvo, qtd))
        else:
            ingredientes = [i for i in item.ingredientes if i.tipo in SUB_RECEITA_TIPOS]
            if (not item.ingredientes and _relevante(item.nome)
                    and not re.search(r'\bmini\b|\bbicolor\b', normalizar(item.nome))):
                raise ValueError(f'{item.nome}: receita sem composição para conferir.')
            if ingredientes:
                rendimento = self._rendimento(item)
                for ing in ingredientes:
                    if not ing.sub_receita_id:
                        raise ValueError(f'{item.nome}: sub-receita {ing.ingrediente_nome} sem vínculo.')
                    qtd = self._unidades_sub(ing, item) / rendimento
                    if qtd:
                        filhos.append(('receita', ing.sub_receita_id, qtd))
        folhas = []
        for filho_tipo, filho_id, fator in filhos:
            for alvo, qtd, caminho, motivo in self._expandir(
                    filho_tipo, filho_id, (*pilha, chave), incluir_retorno):
                folhas.append((alvo, fator * qtd, [item.nome, *caminho], motivo))
        self._cache[cache_chave] = folhas
        return folhas

    def _pode_conter_alvo(self, tipo, item_id, vistos=None):
        """Identifica se uma composição defeituosa é relevante para esta lista."""
        vistos = set(vistos or ())
        chave = (tipo, item_id)
        if chave in vistos:
            return False
        vistos.add(chave)
        if tipo == 'receita':
            item = self.receitas.get(item_id)
            if item_id in self.alvos:
                return True
            if item_id in self.retornos:
                return False
            filhos = [('receita', i.sub_receita_id, i.ingrediente_nome)
                      for i in item.ingredientes if i.tipo in SUB_RECEITA_TIPOS] if item else []
        else:
            item = self.produtos.get(item_id)
            filhos = [(i.tipo, i.receita_id if i.tipo == 'receita' else i.produto_componente_id,
                       i.item_nome) for i in item.itens if i.tipo != 'mp'] if item else []
        return bool(item and _relevante(item.nome)) or any(
            _relevante(nome) or (alvo and self._pode_conter_alvo(t, alvo, vistos))
            for t, alvo, nome in filhos)

    def resolver(self, linha):
        """Devolve Decimal por alvo e fonte serializável; pendência usa ValueError."""
        nome = linha.seru_nome or ''
        fonte = {'nome': nome, 'sku': linha.sku, 'qtd': str(linha.qtd), 'componentes': [],
                 'capturado_em': linha.atualizado_em.isoformat() if linha.atualizado_em else None}
        saida = {'croissant': ZERO, 'pain': ZERO, 'fonte': fonte}
        if normalizar(nome) in ALMOND:
            fonte['componentes'] = [{'grupo': None, 'unidades': str(linha.qtd),
                                     'por_unidade': '1', 'caminho': [nome],
                                     'motivo': 'Almond excluído da lista de fermentação'}]
            return saida
        mapa = self._mapa(nome)
        mapa_relevante = bool(mapa and (
            (mapa.receita_id and self._pode_conter_alvo('receita', mapa.receita_id))
            or (mapa.produto_id and self._pode_conter_alvo('produto', mapa.produto_id))))
        if mapa is None or mapa.ignorar or not mapa.alvo_tipo:
            if _relevante(nome) or mapa_relevante:
                raise ValueError(f'{nome}: vínculo do PDV ausente, pendente ou ignorado.')
            fonte['motivo'] = 'sem vínculo com os itens de fermentação'
            return saida
        tipo = mapa.alvo_tipo
        item_id = mapa.receita_id if tipo == 'receita' else mapa.produto_id
        if tipo == 'mp':
            if _relevante(nome):
                raise ValueError(f'{nome}: vínculo com matéria-prima incompatível com fermentação.')
            fonte['motivo'] = 'matéria-prima'
            return saida
        relevante = _relevante(nome) or self._pode_conter_alvo(tipo, item_id)
        try:
            folhas = self._expandir(tipo, item_id)
        except ValueError:
            if relevante:
                raise
            fonte['motivo'] = 'cadastro de outro produto fora dos itens de fermentação'
            return saida
        nome_norm = normalizar(nome)
        if (tipo == 'receita' and item_id in self.alvos
                and re.search(r'\bmini\b|\bbicolor\b', nome_norm)):
            raise ValueError(f'{nome}: mini/bicolor vinculado ao folhado tradicional; conferir o PDV.')
        if ('frances' in nome_norm.split() and 'croissant' not in nome_norm
                and any(g == 'croissant' for g, _, _, _ in folhas)):
            raise ValueError(f'{nome}: pão francês vinculado a croissant; corrigir o cadastro do PDV.')
        # VendaMapa é identificado por (canal, nome_externo), não por SKU.
        # O código legado do mapa pode diferir do Colibri no snapshot fechado
        # (ex.: Croissant Francês 1 no mapa e 272 nas vendas). Preserve ambos
        # para auditoria, sem invalidar um vínculo nominal já cadastrado.
        qtd = _numero(linha.qtd, nome)
        if any(grupo for grupo, _, _, _ in folhas) and qtd != qtd.to_integral_value():
            raise ValueError(f'{nome}: quantidade de vendas em unidades deve ser inteira.')
        fator = _numero(mapa.fator_quantidade, f'{nome}: fator do PDV', positivo=True)
        fonte.update({'mapa_id': mapa.id, 'sku_vinculo': mapa.sku, 'tipo': tipo,
                      'alvo_id': item_id, 'fator': str(fator)})
        for grupo, consumo, caminho, motivo in folhas:
            total = qtd * fator * consumo
            fonte['componentes'].append({'grupo': grupo, 'unidades': str(total),
                                        'por_unidade': str(consumo),
                                        'caminho': caminho, 'motivo': motivo})
            if grupo:
                saida[grupo] += total
        if not folhas:
            fonte['motivo'] = 'composição não consome os itens de fermentação'
        return saida
