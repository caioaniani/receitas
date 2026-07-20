"""NF-e de TRANSFERÊNCIA indústria→loja via Tiny (20/07/2026).

Pedido do dono: quando o motorista escaneia o QR de saída do pedido de
loja, a NF de transferência sai sozinha e fica atrelada ao pedido na tela
dele (fiscalização na estrada). Decisões do dono (20/07/2026):

- VALOR dos itens = CUSTO calculado da ficha técnica (`custos.py`); MP vai
  pelo custo do cadastro na unidade dela ('un' → custo_por_kg é o custo POR
  UNIDADE — mesma semântica de `custos._custo_por_grama`).
- MATÉRIA-PRIMA entra na NF (kind 'mp' no TinyProdutoMap, canal 'transf').
- Natureza de operação: config NF_NATUREZA_TRANSFERENCIA (texto EXATO
  cadastrado no Tiny).
- SKU: canal 'transf' com FALLBACK site→b2b (mesmos produtos físicos; a
  tela própria é só pra exceções e MPs).

Motor comum `tiny_nf.emitir_nf_generico` (incluir → emitir → obter,
idempotente). A emissão na coleta do QR é BEST-EFFORT pós-commit (padrão
`loja_pagamento._emitir_nf_e_enviar`): falha NUNCA trava a saída do
caminhão — fica pro reenvio manual na lista de pedidos.
"""
import logging
from decimal import ROUND_HALF_UP, Decimal

from app.services import tiny_nf
from app.utils import agora

logger = logging.getLogger(__name__)

CANAL = 'transf'
# Ordem do fallback de SKU (decisão do dono): transf explícito ganha;
# senão herda do site e por fim do B2B — zero retrabalho de mapeamento.
_FALLBACK_CANAIS = (CANAL, 'site', 'b2b')

# Status do PedidoLoja em que a NF de transferência faz sentido (da
# separação em diante; 'entregue'/'recebido' cobrem reemissão tardia).
_STATUS_EMITIVEIS = ('separado', 'em_transporte', 'entregue', 'recebido')


def _so_digitos(s):
    return ''.join(c for c in (s or '') if c.isdigit())


def sku_transferencia(kind, item_id):
    """SKU efetivo pra NF de transferência. MP existe SÓ no canal transf
    (site/b2b nunca mapearam MP); receita/produto herdam site→b2b."""
    if kind == 'mp':
        return tiny_nf.sku_do_item('mp', item_id, canal=CANAL)
    for canal in _FALLBACK_CANAIS:
        sku = tiny_nf.sku_do_item(kind, item_id, canal=canal)
        if sku:
            return sku
    return None


def _payload_cliente_loja(loja):
    """Destinatária da NF = a LOJA (filial com CNPJ próprio). SEFAZ exige
    documento + endereço estruturado — mesma régua do ClienteB2B.
    Devolve (dict, None) ou (None, erro)."""
    if loja is None:
        return None, 'Pedido sem loja — não emite NF de transferência.'
    doc = _so_digitos(loja.cnpj)
    if len(doc) != 14:
        return None, (f'CNPJ da loja "{loja.nome}" inválido ou ausente — '
                      'preencha no cadastro de lojas (RH → Lojas) pra '
                      'emitir a NF de transferência.')
    faltam = [rotulo for rotulo, valor in (
        ('logradouro', loja.endereco_logradouro),
        ('número', loja.endereco_numero),
        ('bairro', loja.endereco_bairro),
        ('CEP', loja.endereco_cep),
        ('cidade', loja.endereco_cidade),
        ('UF', loja.endereco_uf),
    ) if not (valor or '').strip()]
    if faltam:
        return None, ('Endereço fiscal da loja incompleto pra NF (SEFAZ '
                      'exige campos separados): falta ' + ', '.join(faltam)
                      + '. Complete no cadastro de lojas (RH → Lojas).')
    out = {
        'nome': loja.nome,
        'tipo_pessoa': 'J',
        'cpf_cnpj': doc,
        'fone': (loja.telefone or '').strip(),
        'endereco': loja.endereco_logradouro.strip(),
        'numero': loja.endereco_numero.strip(),
        'complemento': (loja.endereco_complemento or '').strip(),
        'bairro': loja.endereco_bairro.strip(),
        'cep': loja.endereco_cep.strip(),
        'cidade': loja.endereco_cidade.strip(),
        'uf': loja.endereco_uf.strip().upper(),
    }
    ie = (loja.inscricao_estadual or '').strip()
    if ie:
        out['ie'] = ie
    return out, None


def _custo_unitario_mp(mp):
    """Custo unitário da MP NA UNIDADE do cadastro (espelha a semântica de
    `custos._custo_por_grama`): 'un' → custo_por_kg É o custo por unidade;
    'g'/'ml' → custo_por_kg/1000; 'kg' → custo_por_kg."""
    custo = float(mp.custo_por_kg or 0)
    if mp.unidade in ('g', 'ml'):
        return custo / 1000.0
    return custo


def _payload_itens(pedido):
    """Itens do pedido → payload do Tiny, com valor = CUSTO calculado.

    Aborta (não emite NF parcial) quando: item sem SKU mapeado/herdado,
    ou custo <= 0 (NF com item a R$ 0 é rejeitada e transferência a custo
    zero é mentira fiscal — melhor corrigir a ficha/cadastro)."""
    from app.services import custos as custos_svc

    res = custos_svc.calcular_custos_receitas()
    receita_custos = res['custos']
    produto_custos = custos_svc.calcular_custos_produtos(
        receita_custos, res['mp_info'])

    out, sem_sku, sem_custo = [], [], []
    for it in pedido.itens:
        qtd = int(it.quantidade or 0)
        if qtd <= 0:
            continue
        if it.receita_id:
            kind, iid = 'receita', it.receita_id
            custo = float(receita_custos.get(
                it.receita.nome if it.receita else it.nome_item, 0) or 0)
        elif it.produto_id:
            kind, iid = 'produto', it.produto_id
            custo = float(produto_custos.get(
                it.produto.nome if it.produto else it.nome_item, 0) or 0)
        elif it.materia_prima_id:
            kind, iid = 'mp', it.materia_prima_id
            custo = _custo_unitario_mp(it.materia_prima) \
                if it.materia_prima else 0
        else:
            continue

        sku = sku_transferencia(kind, iid)
        if not sku:
            sem_sku.append(it.nome_item)
            continue
        if custo <= 0:
            sem_custo.append(it.nome_item)
            continue
        unitario = Decimal(str(custo)).quantize(Decimal('0.01'),
                                                rounding=ROUND_HALF_UP)
        if unitario <= 0:
            # custo < meio centavo quantizado pra 0 — mesma classe do <= 0
            sem_custo.append(it.nome_item)
            continue
        out.append({'item': {
            'codigo': sku,
            'descricao': it.nome_item[:120],
            'unidade': 'UN',
            'quantidade': float(qtd),
            'valor_unitario': float(unitario),
        }})
    return out, sem_sku, sem_custo


def _nota_payload(cliente, itens):
    """Cabeçalho fiscal EXPLÍCITO com a natureza de TRANSFERÊNCIA (texto
    exato cadastrado no Tiny — CFOP de transferência sai da combinação
    natureza × cadastro do produto no Tiny, não daqui)."""
    from flask import current_app
    cfg = current_app.config
    return {
        'tipo': 'S',  # saída (do estabelecimento emitente)
        'natureza_operacao': cfg.get(
            'NF_NATUREZA_TRANSFERENCIA',
            'TRANSFERÊNCIA DE PRODUÇÃO DO ESTABELECIMENTO'),
        'serie': str(cfg.get('NF_SERIE', '1')),
        'data_emissao': agora().strftime('%d/%m/%Y'),
        'cliente': cliente,
        'itens': itens,
        'valor_frete': 0.0,
        'frete_por_conta': str(cfg.get('NF_FRETE_POR_CONTA', 'R')),
    }


def emitir_nf(pedido, user_id=None, recriar=False):
    """Emite a NF de transferência do pedido. {ok, msg, nota_fiscal_id?}.
    Guards próprios; fluxo/idempotência no motor comum."""
    if pedido.status not in _STATUS_EMITIVEIS:
        return {'ok': False,
                'msg': (f'Pedido em status "{pedido.status}" — a NF de '
                        'transferência sai da separação em diante.')}

    def _montar():
        cliente, erro = _payload_cliente_loja(pedido.loja)
        if erro:
            return None, erro
        itens, sem_sku, sem_custo = _payload_itens(pedido)
        if sem_sku:
            return None, ('Itens sem SKU do Tiny (nem herdado do site/B2B): '
                          + ', '.join(sem_sku)
                          + '. Mapeie em Pedidos → SKUs de transferência '
                          '(/pedidos/tiny-skus-transferencia).')
        if sem_custo:
            return None, ('Itens com CUSTO zerado (a NF de transferência '
                          'sai pelo custo da ficha): ' + ', '.join(sem_custo)
                          + '. Corrija a ficha técnica/custo do cadastro.')
        if not itens:
            return None, 'Pedido sem itens — nada pra emitir.'
        return _nota_payload(cliente, itens), None

    return tiny_nf.emitir_nf_generico(pedido, _montar, recriar=recriar)


def emitir_apos_coleta(pedido):
    """Chamada BEST-EFFORT logo após o COMMIT da coleta do QR (padrão
    `loja_pagamento._emitir_nf_e_enviar`): a saída do caminhão NUNCA trava
    por causa do Tiny/SEFAZ — falha vira log + reenvio manual na lista de
    pedidos. Devolve o resultado pra auditoria do handshake (informativo).
    """
    from app.extensions import db
    try:
        res = emitir_nf(pedido)
        if not res.get('ok'):
            logger.warning('NF transferência pedido %s não emitida: %s',
                           pedido.id, res.get('msg'))
        return res
    except Exception:  # noqa: BLE001 — nunca derruba a coleta
        db.session.rollback()
        logger.exception('NF transferência pedido %s: erro inesperado',
                         pedido.id)
        return {'ok': False, 'msg': 'erro inesperado (ver logs)'}
