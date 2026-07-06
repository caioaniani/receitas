"""Emissão de NF-e via Tiny pra vendas B2B (06/07/2026).

Espelha o fluxo da loja online (`tiny_nf`): monta a nota com cabeçalho
fiscal explícito (tipo/série/natureza), cliente com endereço estruturado e
itens por SKU (o Tiny aplica NCM/CFOP/CST do cadastro do produto), e chama
o motor comum `tiny_nf.emitir_nf_generico` (incluir → emitir → obter).

O mapeamento de SKU é o MESMO da loja (`TinyProdutoMap`, tela
/admin/loja-online/tiny-skus) — Receita/Produto são as mesmas entidades
nos dois canais.
"""
import logging
from decimal import Decimal

from app.services import tiny, tiny_nf
from app.utils import agora

logger = logging.getLogger(__name__)


def _so_digitos(s):
    return ''.join(c for c in (s or '') if c.isdigit())


def _payload_cliente(venda):
    """Cliente da NF a partir do ClienteB2B, COM endereço estruturado.

    Devolve (dict, None) ou (None, erro). B2B costuma ser PJ — o tipo de
    pessoa sai do tamanho do documento (14 dígitos = CNPJ = 'J')."""
    cli = venda.cliente
    if not cli:
        return None, ('Venda avulsa (sem cliente B2B cadastrado) — cadastre '
                      'o cliente com CNPJ/CPF e endereço pra emitir NF.')
    doc = _so_digitos(cli.cnpj_cpf)
    if len(doc) not in (11, 14):
        return None, (f'CNPJ/CPF do cliente "{cli.nome}" inválido ou ausente '
                      '— corrija no cadastro (Clientes B2B) pra emitir NF.')
    faltam = [rotulo for rotulo, valor in (
        ('logradouro', cli.endereco_logradouro),
        ('número', cli.endereco_numero),
        ('bairro', cli.endereco_bairro),
        ('CEP', cli.endereco_cep),
        ('cidade', cli.endereco_cidade),
        ('UF', cli.endereco_uf),
    ) if not (valor or '').strip()]
    if faltam:
        return None, ('Endereço do cliente incompleto pra NF (SEFAZ exige '
                      'campos separados): falta ' + ', '.join(faltam)
                      + '. Complete no cadastro (Clientes B2B).')
    return {
        'nome': cli.nome,
        'tipo_pessoa': 'J' if len(doc) == 14 else 'F',
        'cpf_cnpj': doc,
        'email': (cli.email or '').strip(),
        'fone': (cli.telefone or '').strip(),
        'endereco': cli.endereco_logradouro.strip(),
        'numero': cli.endereco_numero.strip(),
        'complemento': (cli.endereco_complemento or '').strip(),
        'bairro': cli.endereco_bairro.strip(),
        'cep': cli.endereco_cep.strip(),
        'cidade': cli.endereco_cidade.strip(),
        'uf': cli.endereco_uf.strip().upper(),
    }, None


def _payload_itens(venda):
    """Cada item da venda -> {item: {codigo, descricao, ...}} via SKU do
    TinyProdutoMap no canal 'b2b' (no Tiny o B2B é outro cadastro/lista de
    preço — SKU pode diferir do site). Item sem SKU mapeado: ABORTA (não
    emite NF parcial).

    O desconto por item é aplicado no valor_unitario (a NF sai com o preço
    efetivo) — Decimal na conta, float só na borda do JSON."""
    out, faltando = [], []
    for it in venda.itens:
        kind = 'receita' if it.receita_id else 'produto'
        sku = tiny_nf.sku_do_item(kind, it.receita_id or it.produto_id,
                                  canal='b2b')
        if not sku:
            faltando.append(it.nome_item)
            continue
        preco = Decimal(it.preco_unitario or 0)
        desc = Decimal(str(it.desconto_percentual or 0))
        unitario = preco * (Decimal('1') - desc / Decimal('100'))
        out.append({'item': {
            'codigo': sku,
            'descricao': it.nome_item[:120],
            'unidade': 'UN',
            'quantidade': float(it.quantidade),
            'valor_unitario': float(unitario),
        }})
    return out, faltando


def _nota_payload(venda, itens):
    """NF pro `nota.fiscal.incluir` com cabeçalho fiscal EXPLÍCITO — mesma
    razão do site: o gerar-por-pedido não aplicava natureza/série."""
    from flask import current_app
    cfg = current_app.config
    cliente, _ = _payload_cliente(venda)
    return {
        'tipo': 'S',  # saída (venda)
        'natureza_operacao': cfg.get('NF_NATUREZA_OPERACAO',
                                     'Venda de mercadorias'),
        'serie': str(cfg.get('NF_SERIE', '1')),
        'data_emissao': agora().strftime('%d/%m/%Y'),
        'cliente': cliente,
        'itens': itens,
        # B2B não tem frete cobrado na venda; modalidade obrigatória no Tiny
        # (letra, não número — "0" vira vazio no PHP deles).
        'valor_frete': 0.0,
        'frete_por_conta': str(cfg.get('NF_FRETE_POR_CONTA', 'R')),
    }


def emitir_nf(venda, user_id=None, recriar=False):
    """Emite NF pra venda B2B. Devolve {ok, msg, nota_fiscal_id?}.
    Guard próprio do B2B: venda cancelada não emite. O fluxo (incluir →
    emitir → obter, idempotência, recriar) está no motor comum."""
    if venda.status == 'cancelada':
        return {'ok': False, 'msg': 'Venda cancelada — não emite NF.'}

    def _montar():
        cliente, erro = _payload_cliente(venda)
        if erro:
            return None, erro
        itens, faltando = _payload_itens(venda)
        if faltando:
            return None, ('Itens sem SKU B2B mapeado no Tiny: '
                          + ', '.join(faltando)
                          + '. Mapeie em B2B → SKUs do Tiny (/b2b/tiny-skus).')
        if not itens:
            return None, 'Venda sem itens — nada pra emitir.'
        return _nota_payload(venda, itens), None

    return tiny_nf.emitir_nf_generico(venda, _montar, recriar=recriar)


def link_danfe(venda):
    """URL pro DANFE em PDF (válida por tempo limitado no Tiny)."""
    if not venda.tiny_nota_fiscal_id:
        return None
    return tiny.obter_link_nota_fiscal(venda.tiny_nota_fiscal_id)
