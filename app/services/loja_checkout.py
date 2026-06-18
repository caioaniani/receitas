"""Checkout da loja online (Fase 3).

Onde mora a INTEGRIDADE DE DINHEIRO do pedido nativo do site. Regras
inegociáveis (CLAUDE.md — dinheiro tem peso especial):

- O servidor NUNCA confia em preço/frete que vem do navegador. Ao criar o
  pedido, re-busca o preço atual do catálogo (`loja_catalogo`) e recomputa
  o frete (`frete.consultar_frete`) no servidor. O carrinho client-side é
  só conveniência de UI.
- Tudo em `Decimal` (centavo exato), nunca float.
- Fase 3 NÃO cobra e NÃO baixa estoque: o pedido nasce
  'aguardando_pagamento'. Pagamento + baixa entram na Fase 4 (Pagar.me).

Modos de entrega (decisão do dono 17/06/2026):
- 'agendada': frete real dos anéis do `frete.py`; data com corte 17h.
- 'retirada': cliente escolhe a loja + data/hora; frete R$0.
- 'express': entrega em até 1h; valor é ESTIMATIVA (a equipe confirma —
  pode ser Lalamove de várias faixas de veículo ou entregador próprio,
  decidido no painel). Só disponível dentro do horário de entrega.
"""
import os
from datetime import date, timedelta
from decimal import Decimal

from app.extensions import db
from app.models import Cliente, Loja, PedidoOnline, PedidoOnlineItem
from app.services import frete as frete_svc
from app.services import loja_catalogo
from app.utils import agora

# Horário de entrega do site (8h–18h). O antigo "corte 17h do dia inteiro"
# foi trocado por filtro de janela passada + lead (ver LEAD_HORAS abaixo).
HORA_ABRE = 8
HORA_FECHA = 18

# Janelas de 1 hora, das 08:00 às 18:00 (decisão do dono 17/06/2026):
# '08:00–09:00', '09:00–10:00', … , '17:00–18:00'.
JANELAS_HORARIAS = tuple(
    f'{h:02d}:00–{h + 1:02d}:00' for h in range(HORA_ABRE, HORA_FECHA))
JANELA_EXPRESS = 'em até 1h'

# Quantos dias de agenda oferecer a partir da primeira data válida.
DIAS_AGENDA = 14


def lojas_retirada():
    """Lojas físicas onde dá pra retirar — ativas, fora a 'Industria'
    (que existe só pra RH). Espelha o filtro de lojas operacionais."""
    return (Loja.query
            .filter(Loja.ativa.is_(True), Loja.nome != 'Industria')
            .order_by(Loja.nome).all())


def express_disponivel(base=None):
    """Express só faz sentido dentro do horário de entrega e com folga pra
    chegar em ~1h (até a hora de corte do fim do expediente)."""
    base = base or agora()
    return HORA_ABRE <= base.hour < HORA_FECHA


# Antecedência mínima (horas) pra uma janela ser oferecida AINDA HOJE.
# Ex: às 13h com LEAD=2, a primeira janela de hoje é 15:00–16:00. Substitui
# o antigo "corte 17h bloqueia o dia inteiro" por filtro por janela —
# decisão 17/06/2026 (o dono quis que janelas passadas sumam, não deem erro).
LEAD_HORAS = int(os.environ.get('LOJA_LEAD_HORAS', '2') or '2')


def janelas_disponiveis(modo, data=None, base=None):
    """Janelas válidas pro modo numa data. Quando a data é HOJE, remove as
    janelas que já passaram (início < agora + LEAD_HORAS). Em dias futuros,
    todas as janelas. `data` aceita date ou str ISO."""
    if modo == 'express':
        return [JANELA_EXPRESS]
    base = base or agora()
    if isinstance(data, str):
        try:
            data = date.fromisoformat(data)
        except ValueError:
            data = None
    janelas = list(JANELAS_HORARIAS)
    if data and data == base.date():
        limite = base.hour + LEAD_HORAS
        janelas = [j for j in janelas if int(j[:2]) >= limite]
    return janelas


def janelas_do_modo(modo):
    """Lista completa de janelas do modo (sem filtro de data). Mantida por
    compat; a validação real usa janelas_disponiveis(modo, data)."""
    if modo == 'express':
        return [JANELA_EXPRESS]
    return list(JANELAS_HORARIAS)


def datas_disponiveis(modo, base=None, dias=DIAS_AGENDA):
    """Datas válidas pro modo.

    - express: só hoje (entrega imediata, dentro do horário).
    - agendada/retirada: HOJE entra se ainda houver janela viável (lead),
      depois amanhã em diante (contíguo). Sem o antigo corte-17h-do-dia:
      janelas passadas são filtradas por `janelas_disponiveis`.
    """
    base = base or agora()
    hoje_d = base.date()
    if modo == 'express':
        return [hoje_d] if express_disponivel(base) else []
    datas = []
    if janelas_disponiveis(modo, hoje_d, base=base):
        datas.append(hoje_d)
    inicio = hoje_d + timedelta(days=1)
    datas.extend(inicio + timedelta(days=i) for i in range(dias))
    return datas


def montar_itens(itens_raw):
    """Re-valida o carrinho contra o catálogo. NUNCA usa o preço do
    cliente — pega o preço publicado atual. Devolve (itens, avisos).

    itens_raw: lista de {kind, id, qtd} (vindo do localStorage).
    item de saída: {kind, id, receita_id, produto_id, nome, preco, qtd, subtotal}
    """
    itens = []
    avisos = []
    for raw in (itens_raw or []):
        kind = (str(raw.get('kind') or '')).strip()
        try:
            item_id = int(raw.get('id'))
            qtd = int(raw.get('qtd') or 0)
        except (TypeError, ValueError):
            continue
        if qtd < 1:
            continue
        cat = loja_catalogo.por_id_publicado(kind, item_id)
        if not cat or not cat.get('preco'):
            avisos.append('Um item saiu de catálogo e foi removido do pedido.')
            continue
        preco = Decimal(str(cat['preco']))
        itens.append({
            'kind': kind,
            'id': item_id,
            'receita_id': item_id if kind == 'receita' else None,
            'produto_id': item_id if kind == 'produto' else None,
            'nome': cat['nome'],
            'preco': preco,
            'qtd': qtd,
            'subtotal': preco * qtd,
        })
    return itens, avisos


def _email_valido(email):
    email = (email or '').strip()
    return '@' in email and '.' in email.split('@')[-1] and len(email) >= 6


def _so_digitos(s):
    return ''.join(c for c in (s or '') if c.isdigit())


def _cpf_valido(cpf):
    """Valida 11 dígitos + dígitos verificadores. Algoritmo padrão da
    Receita Federal. Rejeita sequências iguais ('11111111111')."""
    cpf = _so_digitos(cpf)
    if len(cpf) != 11 or len(set(cpf)) == 1:
        return False
    for i in (9, 10):
        soma = sum(int(cpf[j]) * (i + 1 - j) for j in range(i))
        dig = (soma * 10) % 11
        if dig == 10:
            dig = 0
        if dig != int(cpf[i]):
            return False
    return True


def _montar_endereco(form):
    """Junta os campos estruturados em um texto de uma linha pra gravar
    em PedidoOnline.endereco_entrega (snapshot da entrega)."""
    partes = [
        (form.get('logradouro') or '').strip(),
        (form.get('numero') or '').strip(),
        (form.get('complemento') or '').strip(),
        (form.get('bairro') or '').strip(),
        (form.get('cidade') or '').strip(),
        (form.get('uf') or '').strip(),
    ]
    return ', '.join(p for p in partes if p)


def _frete_para(modo, endereco, base=None):
    """Calcula o frete no servidor (autoritativo). Devolve
    (valor:Decimal, distancia_km, endereco_norm, erro|None)."""
    if modo == 'retirada':
        return Decimal('0.00'), None, None, None
    if not endereco:
        return None, None, None, 'Informe o endereço de entrega.'
    r = frete_svc.consultar_frete(endereco)
    if not r.get('ok'):
        return None, None, None, 'Não consegui localizar esse endereço. '\
            'Confira o endereço ou o CEP.'
    if r.get('fora_area'):
        return None, r.get('distancia_km'), r.get('endereco'), \
            'Esse endereço está fora da nossa área de entrega (até 15 km).'
    valor = Decimal(str(r.get('valor') or 0))
    # Express: o valor dos anéis é só uma ESTIMATIVA — a equipe confirma o
    # custo real (Lalamove faixa X ou entregador próprio) no painel.
    return valor, r.get('distancia_km'), r.get('endereco'), None


def criar_pedido(form, itens_raw, *, base=None):
    """Valida tudo e cria o PedidoOnline. Devolve (pedido|None, erros:list).

    `form`: dict-like (request.form). `itens_raw`: lista de {kind,id,qtd}.
    Não faz commit parcial: ou cria o pedido inteiro, ou devolve erros.
    """
    base = base or agora()
    erros = []

    nome = (form.get('nome') or '').strip()
    email = (form.get('email') or '').strip()
    telefone = (form.get('telefone') or '').strip()
    cpf = _so_digitos(form.get('cpf') or '')
    modo = (form.get('modo_entrega') or '').strip()
    cartinha = (form.get('cartinha') or '').strip() or None
    aceite = form.get('aceite_lgpd') in ('1', 'on', 'true', True)

    # Destinatário diferente do pagador (presente)
    e_presente = form.get('e_presente') in ('1', 'on', 'true', True)
    nome_destinatario = ((form.get('nome_destinatario') or '').strip()
                         if e_presente else None) or None
    telefone_destinatario = ((form.get('telefone_destinatario') or '').strip()
                             if e_presente else None) or None

    if not nome:
        erros.append('Informe seu nome.')
    if not _email_valido(email):
        erros.append('Informe um email válido.')
    # CPF é exigência do Pagar.me pra Pix e da NF-e (Fase 5) — pedir aqui
    # já é mais barato que voltar pro cliente depois.
    if not _cpf_valido(cpf):
        erros.append('Informe um CPF válido (11 dígitos).')
    if not aceite:
        erros.append('É preciso aceitar os termos para concluir o pedido.')
    if modo not in ('agendada', 'retirada', 'express'):
        erros.append('Escolha um modo de entrega.')
    if e_presente and not nome_destinatario:
        erros.append('Informe o nome de quem vai receber.')

    itens, avisos = montar_itens(itens_raw)
    if not itens:
        erros.append('Seu carrinho está vazio ou os itens saíram de catálogo.')

    # ── Por modo: endereço/loja + frete (servidor manda) ───────────────
    loja_retirada_id = None
    endereco_entrega = None
    endereco_cep = (form.get('cep') or '').strip() or None
    distancia_km = None
    frete_valor = Decimal('0.00')
    # Endereco ESTRUTURADO (snapshot pra NF-e). So a entrega preenche; a
    # linha unica `endereco_entrega` acima continua sendo a versao legivel.
    end_logradouro = end_numero = end_complemento = None
    end_bairro = end_cidade = end_uf = None

    if modo == 'retirada':
        try:
            loja_retirada_id = int(form.get('loja_id'))
        except (TypeError, ValueError):
            loja_retirada_id = None
        loja = Loja.query.get(loja_retirada_id) if loja_retirada_id else None
        if not loja or not loja.ativa or loja.nome == 'Industria':
            erros.append('Escolha uma loja válida para retirada.')
        else:
            endereco_entrega = f'Retirada: {loja.nome} — {loja.endereco or ""}'.strip()
    elif modo in ('agendada', 'express'):
        if modo == 'express' and not express_disponivel(base):
            erros.append('Express indisponível agora (fora do horário de '
                         'entrega). Escolha entrega agendada.')
        # Endereco estruturado (CEP + logradouro auto + numero/complemento
        # digitados). Numero e' obrigatorio pra entrega — sem ele a equipe
        # nao consegue entregar.
        logradouro = (form.get('logradouro') or '').strip()
        numero = (form.get('numero') or '').strip()
        cidade = (form.get('cidade') or '').strip()
        if not endereco_cep:
            erros.append('Informe o CEP de entrega.')
        if not logradouro:
            erros.append('Informe o logradouro (rua/avenida).')
        if not numero:
            erros.append('Informe o número do endereço.')
        if not cidade:
            erros.append('Informe a cidade.')
        # Snapshot estruturado pra NF-e (alem da linha unica abaixo).
        end_logradouro = logradouro or None
        end_numero = numero or None
        end_complemento = (form.get('complemento') or '').strip() or None
        end_bairro = (form.get('bairro') or '').strip() or None
        end_cidade = cidade or None
        end_uf = ((form.get('uf') or '').strip().upper()[:2]) or None
        endereco_txt = _montar_endereco(form)
        # geocoding usa o endereco completo (mais preciso que CEP só); CEP
        # entra concatenado pra desambiguar bairros homonimos.
        geo = endereco_txt
        if endereco_cep and endereco_cep not in geo:
            geo = f'{endereco_txt}, {endereco_cep}' if endereco_txt else endereco_cep
        valor, dist, end_norm, erro_frete = _frete_para(modo, geo, base=base)
        if erro_frete:
            erros.append(erro_frete)
        else:
            frete_valor = valor
            distancia_km = dist
            endereco_entrega = endereco_txt or end_norm

    # ── Data + janela ──────────────────────────────────────────────────
    data_str = (form.get('data_entrega') or '').strip()
    janela = (form.get('janela_entrega') or '').strip()
    data_entrega = None
    if modo == 'express':
        # Express é hoje, imediato — ignora o que vier do form.
        if express_disponivel(base):
            data_entrega = base.date()
            janela = JANELA_EXPRESS
    else:
        disponiveis = {d.isoformat() for d in datas_disponiveis(modo, base=base)}
        if data_str not in disponiveis:
            erros.append('Escolha uma data de entrega válida.')
        else:
            data_entrega = date.fromisoformat(data_str)
            # Janela tem que ser válida PARA AQUELA DATA (janelas passadas de
            # hoje são rejeitadas — espelha o filtro do front).
            if janela not in janelas_disponiveis(modo, data_entrega, base=base):
                erros.append('Escolha uma janela de horário válida '
                             '(o horário escolhido já passou).')

    if erros:
        return None, erros

    # ── Cria/reusa cliente (guest por email) ───────────────────────────
    cliente = Cliente.query.filter(
        db.func.lower(Cliente.email) == email.lower()).first()
    if not cliente:
        cliente = Cliente(nome=nome, email=email, telefone=telefone, cpf=cpf)
        db.session.add(cliente)
    else:
        # Atualiza dados de contato com o que o cliente acabou de informar.
        cliente.nome = nome or cliente.nome
        cliente.telefone = telefone or cliente.telefone
        cliente.cpf = cpf or cliente.cpf
    if aceite and not cliente.aceite_lgpd_em:
        cliente.aceite_lgpd_em = base
    db.session.flush()  # garante cliente.id

    pedido = PedidoOnline(
        cliente_id=cliente.id,
        nome_cliente=nome, email_cliente=email, telefone_cliente=telefone,
        nome_destinatario=nome_destinatario,
        telefone_destinatario=telefone_destinatario,
        modo_entrega=modo,
        loja_retirada_id=loja_retirada_id,
        endereco_entrega=endereco_entrega,
        endereco_cep=endereco_cep,
        endereco_logradouro=end_logradouro,
        endereco_numero=end_numero,
        endereco_complemento=end_complemento,
        endereco_bairro=end_bairro,
        endereco_cidade=end_cidade,
        endereco_uf=end_uf,
        distancia_km=distancia_km,
        data_entrega=data_entrega,
        janela_entrega=janela,
        frete_valor=frete_valor,
        cartinha=cartinha,
        status='aguardando_pagamento',
    )
    db.session.add(pedido)
    db.session.flush()
    for it in itens:
        pedido.itens.append(PedidoOnlineItem(
            kind=it['kind'],
            receita_id=it['receita_id'], produto_id=it['produto_id'],
            nome=it['nome'], preco_unitario=it['preco'],
            quantidade=it['qtd'], subtotal=it['subtotal'],
        ))
    pedido.recalcular_total()
    db.session.commit()
    # E-mail "recebemos seu pedido" — best-effort (não derruba o checkout).
    try:
        from app.services import email as email_svc
        if email_svc.disponivel():
            email_svc.enviar_pedido_recebido(pedido)
    except Exception:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).exception(
            'email pedido recebido falhou')
    return pedido, []
