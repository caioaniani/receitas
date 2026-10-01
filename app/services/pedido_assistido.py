"""Pedido ASSISTIDO — a equipe monta o pedido do site em nome do cliente e
manda a ele o link em que ele mesmo paga (Pix ou cartão).

Pedido do dono (01/10/2026): "preciso criar um 'criar pedido' no sistema que
o operador faz o pedido para o cliente e envia para ele a parte que ele
insere os dados para pagamento ou pix".

Desenho (nada novo no caminho do dinheiro):
- O MOTOR é o do checkout: `loja_checkout.criar_pedido` com os MESMOS campos
  do site (nome/sobrenome, e-mail, telefone, CPF/CNPJ, modo, endereço, data,
  janela, itens, presente). Ele valida tudo o que o site valida (catálogo
  publicado e preço atual, plano do dia, data especial, janela, frete pelo
  servidor, retirada só na loja permitida), cria/reusa o `Cliente` por
  e-mail, grava o `PedidoOnline` em `aguardando_pagamento` e RESERVA o
  estoque. Baixa de estoque, NF, painel e e-mails de status seguem SÓ pelo
  webhook do Pagar.me — exatamente como um pedido do site.
- A "parte em que ele insere os dados para pagamento" JÁ EXISTE: é a página
  pública `/loja/pedido/<codigo>/pagamento` (allowlist do `_gate_acesso`,
  responde anônima pelo código). O link canônico é `LOJA_BASE_URL` + esse
  caminho (nunca `url_for(_external=True)`, que apontaria pro host gestão).
- O que o pedido do site NÃO tem fica em `PedidoOnlineAssistido` (tabela
  nova, create_all): operador, canal do pedido, prazo do link, e-mail enviado.
- PRAZO do link (`prazo_pagamento`): o checkout reserva por 35 min porque o
  cliente está na tela; aqui o cliente recebe o link e paga quando vê. O
  prazo vira `PedidoOnline.reserva_expira_em`, então o MESMO cron de
  expiração do site (`loja_estoque_reserva.liberar_expirados`) cancela o
  pedido não pago — fonte única, sem segundo relógio. A reserva física é
  contábil (não barra venda), então segurar por horas não trava ninguém.
- E-mail próprio (`email.enviar_link_pagamento_assistido`) + texto pronto
  para WhatsApp (`texto_whatsapp`/`url_whatsapp`, link `wa.me` só para
  celular BR). O "recebemos seu pedido" do checkout NÃO sai: diria que o
  cliente fez o pedido.
- Aceite dos termos: quem marca "informei o cliente" é o OPERADOR; por isso
  `criar_pedido(registrar_aceite=False)` não carimba `Cliente.aceite_lgpd_em`
  e a página de pagamento diz ao cliente que, ao pagar, ele concorda com os
  termos. A equipe nunca reescreve cadastro existente
  (`atualizar_cadastro=False`); cadastro novo nasce `origem='balcao'`.
"""
import logging
from datetime import datetime, time, timedelta
from urllib.parse import quote

from flask import current_app

from app.extensions import db
from app.models import CANAIS_PEDIDO_ASSISTIDO, PedidoOnlineAssistido
from app.utils import agora, fmt_brl, telefone_e164_whatsapp

logger = logging.getLogger(__name__)

# Quanto tempo o link de pagamento vale, a partir da criação.
PRAZO_PADRAO_HORAS = 24
# Piso: mesmo que a entrega seja daqui a pouco (hoje), o cliente tem pelo
# menos isto para abrir o link.
PRAZO_MINIMO_HORAS = 2
# EXPRESS é "sai agora": a janela ('em até 1h/2h') é calculada na criação e
# o motoboy é chamado no pagamento — um link pago 2 h depois seria um
# express já vencido. Prazo curto, próprio.
PRAZO_EXPRESS_MIN = 30
# Duplo clique / reenvio do form: o MESMO operador criando pedido para o
# MESMO e-mail dentro desta janela é recusado com o código do anterior.
JANELA_DUPLICATA_SEG = 30
CANAIS = tuple(c for c, _ in CANAIS_PEDIDO_ASSISTIDO)
MSG_CLIENTE_NAO_INFORMADO = (
    'Confirme que informou ao cliente os itens, o total, a entrega e que os '
    'termos de compra do site valem para este pedido.')


def prazo_pagamento(base, data_entrega, *, modo_entrega=None):
    """Até quando o link vale. Regra: 24 h a partir de agora, mas nunca
    depois da VÉSPERA da entrega às 23:59 (a produção/separação parte do que
    está pago), com piso de 2 h (entrega hoje, pedido lançado tarde na
    véspera). EXPRESS: `PRAZO_EXPRESS_MIN` fixos. Função pura — a tela e o
    teste a leem igual."""
    base = base or agora()
    if modo_entrega == 'express':
        return base + timedelta(minutes=PRAZO_EXPRESS_MIN)
    limite = base + timedelta(hours=PRAZO_PADRAO_HORAS)
    if data_entrega is not None:
        vespera = datetime.combine(data_entrega - timedelta(days=1),
                                   time(23, 59, 59))
        limite = min(limite, vespera)
    piso = base + timedelta(hours=PRAZO_MINIMO_HORAS)
    return max(limite, piso)


def _duplicata_recente(operador_id, email, base):
    """Pedido assistido do MESMO operador para o MESMO e-mail criado há menos
    de `JANELA_DUPLICATA_SEG` (duplo clique, F5 no POST). Devolve o pedido
    anterior ou None. O form não tem nonce; esta guarda + o botão
    desabilitado no clique cobrem o caso real."""
    from app.models import PedidoOnline
    email = (email or '').strip().lower()
    if not operador_id or not email:
        return None
    desde = base - timedelta(seconds=JANELA_DUPLICATA_SEG)
    reg = (PedidoOnlineAssistido.query
           .join(PedidoOnline, PedidoOnline.id == PedidoOnlineAssistido.pedido_id)
           .filter(PedidoOnlineAssistido.operador_id == operador_id,
                   PedidoOnlineAssistido.criado_em >= desde,
                   db.func.lower(PedidoOnline.email_cliente) == email)
           .order_by(PedidoOnlineAssistido.criado_em.desc())
           .first())
    return reg.pedido if reg else None


def link_pagamento(pedido):
    """URL pública em que o cliente paga — a MESMA do e-mail "recebemos seu
    pedido" (`email.py`): LOJA_BASE_URL + /loja/pedido/<codigo>/pagamento."""
    base = (current_app.config.get('LOJA_BASE_URL')
            or current_app.config.get('APP_BASE_URL') or '').rstrip('/')
    return f'{base}/loja/pedido/{pedido.codigo}/pagamento'


def registro_de(pedido):
    """`PedidoOnlineAssistido` do pedido, ou None (pedido comum do site)."""
    if pedido is None or pedido.id is None:
        return None
    return PedidoOnlineAssistido.query.filter_by(pedido_id=pedido.id).first()


def vencido(registro, base=None):
    """O prazo do link já passou? (o cancelamento em si é do cron)."""
    if registro is None or registro.prazo_pagamento is None:
        return False
    return registro.prazo_pagamento <= (base or agora())


def criar(form, itens_raw, *, operador_id, canal=None, observacao=None,
          base=None, enviar_email=True):
    """Cria o pedido assistido. Devolve (pedido|None, erros:list,
    registro|None).

    `form`: dict-like com os campos do checkout do site (ver
    `loja_checkout.criar_pedido`) mais `cliente_informado` ('1' = o operador
    confirmou ter informado o cliente). `itens_raw`: lista de
    {kind, id, qtd[, comp, fatiado]} — passa pelos mesmos tetos do carrinho
    do site (`normalizar_itens_raw`). Nada é gravado se houver erro."""
    from app.services import loja_checkout
    base = base or agora()
    erros = []
    if form.get('cliente_informado') not in ('1', 'on', 'true', True):
        erros.append(MSG_CLIENTE_NAO_INFORMADO)
    canal = (canal or '').strip() or None
    if canal and canal not in CANAIS:
        erros.append('Canal do pedido inválido.')
    observacao = (observacao or '').strip() or None
    if observacao and len(observacao) > 500:
        erros.append('A observação interna deve ter no máximo 500 caracteres.')
    anterior = _duplicata_recente(operador_id, form.get('email'), base)
    if anterior is not None:
        erros.append(f'Você acabou de criar o pedido {anterior.codigo} para '
                     'este mesmo e-mail há menos de um minuto — abra-o em vez '
                     'de criar outro (clique duplo?).')
    if erros:
        # Erro do OPERADOR: nem chama o motor — ele cotaria frete (Google) e
        # dispararia alertas antes de o pedido ser recusado.
        return None, erros, None
    # O aceite dos termos entra como "informado pelo operador" (o campo
    # `cliente_informado` acima é o que o operador marca); o motor exige
    # `aceite_lgpd`, e `registrar_aceite=False` impede carimbar o
    # consentimento no cadastro do cliente (quem clicou não foi ele).
    dados = dict(form)
    dados['aceite_lgpd'] = '1'
    itens = loja_checkout.normalizar_itens_raw(itens_raw)
    pedido, erros_motor = loja_checkout.criar_pedido(
        dados, itens, base=base, commit=False, itens_estritos=True,
        registrar_aceite=False, atualizar_cadastro=False,
        origem_cliente='balcao', alertar_dono=False)
    if erros_motor or pedido is None:
        # Com `commit=False` o motor só deu flush: nada do pedido (nem o
        # Cliente criado por ele) sobrevive ao rollback.
        db.session.rollback()
        return None, erros_motor or ['Não foi possível criar o pedido.'], None

    prazo = prazo_pagamento(base, pedido.data_entrega,
                            modo_entrega=pedido.modo_entrega)
    # Mesmo relógio do site: o cron de expiração lê `reserva_expira_em`.
    # Gravado SEMPRE (mesmo sem reserva, por loja de origem não configurada)
    # — a tela promete "sem pagamento o pedido é cancelado"; sem loja o cron
    # avisa no log e zera o campo, como faz com qualquer pedido do site.
    pedido.reserva_expira_em = prazo
    registro = PedidoOnlineAssistido(
        pedido_id=pedido.id, operador_id=operador_id,
        prazo_pagamento=prazo, canal=canal, observacao=observacao)
    db.session.add(registro)
    db.session.commit()
    logger.info('pedido assistido %s criado por usuario %s (canal=%s, '
                'prazo=%s)', pedido.codigo, operador_id, canal, prazo)
    if enviar_email:
        enviar_email_link(pedido, registro)
    return pedido, [], registro


def enviar_email_link(pedido, registro):
    """Manda (ou reenvia) o e-mail com o link de pagamento. Best-effort:
    falha nunca derruba a criação — o operador ainda tem o WhatsApp e o
    link na tela. Devolve o dict do `email.enviar`."""
    from app.services import email as email_svc
    try:
        if not email_svc.disponivel():
            return {'ok': False, 'erro': 'e-mail não configurado'}
        res = email_svc.enviar_link_pagamento_assistido(
            pedido, prazo=registro.prazo_pagamento if registro else None)
        if res and res.get('ok') and registro is not None:
            registro.email_enviado_em = agora()
            db.session.commit()
        return res
    except Exception as exc:  # noqa: BLE001 — best-effort, nunca derruba
        logger.exception('e-mail do pedido assistido %s falhou', pedido.codigo)
        return {'ok': False, 'erro': str(exc)}


def _resumo_itens(pedido, maximo=6):
    partes = []
    for it in pedido.itens[:maximo]:
        nome = f'{it.quantidade}x {it.nome}'
        if it.fatiado:
            nome += ' (fatiado)'
        partes.append(nome)
    if len(pedido.itens) > maximo:
        partes.append(f'e mais {len(pedido.itens) - maximo} item(ns)')
    return ', '.join(partes)


def _quando_onde(pedido):
    if pedido.modo_entrega == 'retirada':
        loja = pedido.loja_retirada
        onde = f'retirada na {loja.nome}' if loja else 'retirada na loja'
    elif pedido.modo_entrega == 'express':
        onde = f'entrega express em {pedido.endereco_entrega or "seu endereço"}'
    else:
        onde = f'entrega em {pedido.endereco_entrega or "seu endereço"}'
    quando = ''
    if pedido.data_entrega:
        quando = pedido.data_entrega.strftime('%d/%m')
        if pedido.janela_entrega:
            quando += f' ({pedido.janela_entrega})'
    return onde, quando


def texto_whatsapp(pedido, registro=None):
    """Mensagem pronta para o operador mandar ao cliente (copiar ou abrir no
    WhatsApp). Diz o que foi montado, o total, a entrega, o link e o prazo
    — sem pedir dado nenhum de volta (o pagamento é na página)."""
    primeiro_nome = (pedido.nome_cliente or '').strip().split(' ')[0]
    onde, quando = _quando_onde(pedido)
    linhas = [
        f'Olá{", " + primeiro_nome if primeiro_nome else ""}! Aqui é da O Pão '
        'Padaria Artesanal.',
        f'Montamos o seu pedido {pedido.codigo}: {_resumo_itens(pedido)}.',
    ]
    if pedido.frete_valor:
        linhas.append(f'Total {fmt_brl(pedido.valor_total)} '
                      f'(itens {fmt_brl(pedido.subtotal)} + frete '
                      f'{fmt_brl(pedido.frete_valor)}).')
    else:
        linhas.append(f'Total {fmt_brl(pedido.valor_total)}.')
    linhas.append(f'{onde[0].upper() + onde[1:]}{" em " + quando if quando else ""}.')
    linhas.append('Para pagar com Pix ou cartão, é só abrir o link:')
    linhas.append(link_pagamento(pedido))
    if registro is not None and registro.prazo_pagamento:
        linhas.append('O link vale até '
                      f'{registro.prazo_pagamento.strftime("%d/%m às %H:%M")}.')
    linhas.append('Qualquer dúvida, é só responder por aqui.')
    return '\n'.join(linhas)


def url_whatsapp(pedido, texto=None):
    """Link `wa.me` já com a mensagem, SÓ para celular brasileiro (o único
    destino que recebe WhatsApp — `telefone_e164_whatsapp`); fixo/
    internacional/inválido devolve None e a tela oferece só o copiar."""
    e164 = telefone_e164_whatsapp(pedido.telefone_cliente)
    if not e164:
        return None
    return f'https://wa.me/{e164.lstrip("+")}?text={quote(texto or texto_whatsapp(pedido))}'


def cancelar(pedido, *, usuario_id=None):
    """Cancela um pedido assistido ainda NÃO pago: encerra a cobrança no
    gateway e libera a reserva pelo mesmo caminho do admin
    (`loja_pagamento.cancelar_aguardando_pelo_admin`). Só pedido assistido
    em `aguardando_pagamento` — pago/cancelado/comum ficam com os botões de
    sempre (reembolso é do dono). Devolve (ok, mensagem)."""
    from app.services import kits_pagamento, loja_pagamento
    if registro_de(pedido) is None:
        return False, f'Pedido {pedido.codigo} não é um pedido assistido.'
    # Trava o pedido ANTES de decidir (mesma ordem de locks do cancelar do
    # dono): dois cliques, ou clique + webhook de pagamento, ficam em fila e
    # o segundo lê o status já mudado.
    kits_pagamento.travar(pedido, todos=True)
    if pedido.status != 'aguardando_pagamento':
        return False, (f'Pedido {pedido.codigo} não está aguardando pagamento '
                       f'({pedido.status}) — use os botões de sempre do pedido.')
    ok, msg = loja_pagamento.cancelar_aguardando_pelo_admin(pedido)
    if ok:
        logger.info('pedido assistido %s cancelado pelo usuario %s',
                    pedido.codigo, usuario_id)
    return ok, msg


def ids_assistidos(pedidos):
    """Conjunto dos `pedido_id` que são assistidos, numa consulta só (lista
    de pedidos do site, badge por linha)."""
    ids = [p.id for p in pedidos if p.id is not None]
    if not ids:
        return set()
    return {pid for (pid,) in db.session.query(PedidoOnlineAssistido.pedido_id)
            .filter(PedidoOnlineAssistido.pedido_id.in_(ids)).all()}
