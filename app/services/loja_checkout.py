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
# Express pra cliente longe (>= limiar km) leva mais tempo — o motoboy
# percorre mais (decisão do dono 23/06/2026: >10km o express vira 2h).
JANELA_EXPRESS_LONGE = 'em até 2h'
DISTANCIA_EXPRESS_2H_KM = float(
    os.environ.get('LOJA_EXPRESS_2H_KM', '10') or '10')


def janela_express_para_distancia(distancia_km):
    """Texto da janela express conforme a distância. >= DISTANCIA_EXPRESS_2H_KM
    → 'em até 2h'; senão 'em até 1h'. Distância None (sem cotação) → 1h."""
    if (distancia_km is not None
            and distancia_km >= DISTANCIA_EXPRESS_2H_KM):
        return JANELA_EXPRESS_LONGE
    return JANELA_EXPRESS

# Quantos dias de agenda oferecer a partir da primeira data válida.
DIAS_AGENDA = 14


def lojas_retirada():
    """Lojas físicas mostradas na opção de retirada — ativas, fora a
    'Industria' (que existe só pra RH). TODAS aparecem na lista; só a
    `loja_retirada_permitida()` é selecionável (as outras vêm desabilitadas
    no template e bloqueadas no servidor)."""
    return (Loja.query
            .filter(Loja.ativa.is_(True), Loja.nome != 'Industria')
            .order_by(Loja.nome).all())


def loja_retirada_permitida():
    """ÚNICA loja que aceita retirada de pedido do site (decisão do dono
    19/06/2026 — hoje a Anésio Pinto Rosa). É a mesma loja que fulfilla o
    site (`loja_origem_site`), então fica amarrada à config existente
    (`AppConfig.loja_site_estoque_id`) em vez de hardcodar o nome — mexer
    num lugar só. Devolve a Loja ou None se não configurada."""
    from app.services.loja_pagamento import loja_origem_site
    return loja_origem_site()


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

# Distância (km) a partir da qual a PRIMEIRA janela da manhã (08:00-09:00) é
# cortada — motoboy demora pra ser alocado de manhã e cliente >10km não recebe
# a tempo (decisão do dono 23/06/2026). Default 10km; ajustável por env sem
# deploy. A loja é o Brooklin (frete.py); 'distancia_km' vem do consultar_frete.
DISTANCIA_CORTE_PRIMEIRA_JANELA_KM = float(
    os.environ.get('LOJA_CORTE_1A_JANELA_KM', '10') or '10')
# Quais janelas considerar "primeira da manhã" pra cortar quando o cliente
# está longe. Hoje só 08-09; se um dia tiver janelas <8h, listamos aqui.
# IMPORTANTE: usa en-dash (–) pra bater com JANELAS_HORARIAS (NÃO hífen).
JANELAS_CORTADAS_LONGE = ('08:00–09:00',)

# Cartinha de presente: limite de caracteres (23/06/2026, decisão do dono —
# clientes empolgavam e enchiam o cupom da entrega).
CARTINHA_MAX_CHARS = int(os.environ.get('LOJA_CARTINHA_MAX', '250') or '250')


def janelas_disponiveis(modo, data=None, base=None, *, distancia_km=None):
    """Janelas válidas pro modo numa data. Quando a data é HOJE, remove as
    janelas que já passaram (início < agora + LEAD_HORAS). Em dias futuros,
    todas as janelas. `data` aceita date ou str ISO.

    `distancia_km` (opcional, vem do `consultar_frete`): quando informado e
    >= DISTANCIA_CORTE_PRIMEIRA_JANELA_KM, corta a 1ª janela (08-09) — o
    motoboy não chega a tempo (caso real Alphaville)."""
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
    # Corte de janelas matinais por distância (só agendada — express é por
    # horário; retirada não tem distância). Aplica EM QUALQUER dia (não só
    # hoje): pra a quinta às 8h o motoboy já passa pelo mesmo gargalo de
    # alocação matinal.
    if (modo == 'agendada'
            and distancia_km is not None
            and distancia_km >= DISTANCIA_CORTE_PRIMEIRA_JANELA_KM):
        janelas = [j for j in janelas if j not in JANELAS_CORTADAS_LONGE]
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
        # Esgotou entre o carrinho e o checkout → não vende (regra do dono).
        if not loja_catalogo.tem_estoque_site(kind, item_id):
            avisos.append(f'"{cat["nome"]}" esgotou e foi removido do pedido.')
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


def _nome_valido(s):
    """Nome de pessoa: pelo menos 2 caracteres, sem dígitos, com letras.
    Bloqueia o caso real (23/06/2026) do cliente digitar o CPF no campo de
    nome — o campo aceitava qualquer coisa."""
    import re
    s = (s or '').strip()
    if len(s) < 2:
        return False
    if re.search(r'\d', s):
        return False
    return any(ch.isalpha() for ch in s)


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
            ('Esse endereço está fora da nossa área de entrega '
             f'(até {int(frete_svc.RAIO_MAX_KM)} km).')
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

    nome_dado = (form.get('nome') or '').strip()
    sobrenome_dado = (form.get('sobrenome') or '').strip()
    email = (form.get('email') or '').strip()
    telefone = (form.get('telefone') or '').strip()
    cpf = _so_digitos(form.get('cpf') or '')
    modo = (form.get('modo_entrega') or '').strip()
    cartinha = (form.get('cartinha') or '').strip() or None
    # Cartinha tem limite (23/06/2026, decisão do dono — clientes empolgavam).
    # Trunca em vez de rejeitar o pedido: o presente é opcional, melhor cortar
    # do que perder a venda. Aviso ao cliente fica no front (maxlength + contador).
    if cartinha and len(cartinha) > CARTINHA_MAX_CHARS:
        cartinha = cartinha[:CARTINHA_MAX_CHARS].rstrip()
    aceite = form.get('aceite_lgpd') in ('1', 'on', 'true', True)

    # Destinatário diferente do pagador (presente)
    e_presente = form.get('e_presente') in ('1', 'on', 'true', True)
    nome_destinatario = ((form.get('nome_destinatario') or '').strip()
                         if e_presente else None) or None
    telefone_destinatario = ((form.get('telefone_destinatario') or '').strip()
                             if e_presente else None) or None

    # Nome completo = nome + sobrenome. O servidor valida o CONJUNTO (sem
    # dígitos, com letras) — bloqueia o CPF no campo de nome. O "sobrenome
    # obrigatório" é garantido pelos 2 campos `required` do formulário web;
    # aqui aceitamos também o nome completo vindo num campo só (compat com
    # chamadas que mandam o nome inteiro).
    nome = f'{nome_dado} {sobrenome_dado}'.strip()
    if not _nome_valido(nome):
        erros.append('Informe seu nome e sobrenome (apenas letras, '
                     'sem números).')
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
    elif e_presente and not _nome_valido(nome_destinatario):
        erros.append('O nome de quem vai receber deve ter só letras '
                     '(sem números).')

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
        permitida = loja_retirada_permitida()
        if not loja or not loja.ativa or loja.nome == 'Industria':
            erros.append('Escolha uma loja válida para retirada.')
        elif not permitida or loja.id != permitida.id:
            # Trava server-side: só a loja permitida aceita retirada, mesmo
            # que alguém burle o <select> desabilitado do template.
            erros.append(f'Retirada disponível apenas em {permitida.nome}.'
                         if permitida else 'Retirada indisponível no momento.')
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
        # Express é hoje, imediato — ignora o que vier do form. A janela
        # reflete a distância (>10km = 2h; o motoboy percorre mais).
        if express_disponivel(base):
            data_entrega = base.date()
            janela = janela_express_para_distancia(distancia_km)
    else:
        disponiveis = {d.isoformat() for d in datas_disponiveis(modo, base=base)}
        if data_str not in disponiveis:
            erros.append('Escolha uma data de entrega válida.')
        else:
            data_entrega = date.fromisoformat(data_str)
            # Janela tem que ser válida PARA AQUELA DATA (janelas passadas de
            # hoje são rejeitadas — espelha o filtro do front). Distância
            # corta a 1ª janela quando o cliente está longe (motoboy não
            # chega — caso real Alphaville 23/06/2026).
            janelas_ok = janelas_disponiveis(
                modo, data_entrega, base=base,
                distancia_km=distancia_km)
            if janela not in janelas_ok:
                # Mensagem diferenciada quando o motivo é a distância (cliente
                # entende por que sumiu a 1ª janela).
                if (modo == 'agendada' and distancia_km is not None
                        and distancia_km >= DISTANCIA_CORTE_PRIMEIRA_JANELA_KM
                        and janela in JANELAS_CORTADAS_LONGE):
                    erros.append(
                        f'Para o seu endereço ({distancia_km:.1f} km da loja), '
                        'não conseguimos entregar na janela das '
                        f'{janela.split("-")[0]} — '
                        'escolha a partir das 09:00.')
                else:
                    erros.append('Escolha uma janela de horário válida '
                                 '(o horário escolhido já passou).')

    # Plano por dia (22/06/2026): valida cada item contra o saldo do plano
    # da data_entrega escolhida. Mensagem CLARA com nome do produto + data
    # pra cliente saber o que tirar/o que mudar.
    if data_entrega and itens:
        esgotados = []
        for it in itens:
            if not loja_catalogo.tem_estoque_para_dia(
                    it['kind'], it['id'], data_entrega):
                esgotados.append(it['nome'])
        if esgotados:
            data_fmt = data_entrega.strftime('%d/%m/%Y')
            if len(esgotados) == 1:
                erros.append(
                    f'"{esgotados[0]}" não está disponível pra entrega em '
                    f'{data_fmt}. Escolha outra data ou tire o item do carrinho.')
            else:
                erros.append(
                    f'Os seguintes itens não estão disponíveis pra entrega '
                    f'em {data_fmt}: {", ".join(esgotados)}. Escolha outra '
                    'data ou tire-os do carrinho.')

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
    db.session.flush()
    # Reserva estoque ANTES do commit (race condition no cutover loja
    # propria, 21/06/2026). Se um dos itens nao tem disponivel suficiente,
    # rollback de tudo e devolve a lista pra o caller mostrar o que faltou.
    # Em SQLite (dev/teste), FOR UPDATE da reserva vira no-op silencioso.
    from app.services import loja_estoque_reserva
    from app.services.loja_pagamento import _loja_baixa as _origem_baixa
    loja_origem = _origem_baixa(pedido)
    if loja_origem:
        r = loja_estoque_reserva.reservar(pedido, loja_id=loja_origem.id)
        if not r['ok']:
            db.session.rollback()
            faltas = [
                f"{f['nome']}: pedido {f['pedido']}, disponivel {f['disponivel']}"
                for f in r.get('sem_estoque', [])
            ]
            if faltas:
                erros.append(
                    'Algum item saiu de estoque enquanto voce finalizava. '
                    'Reveja seu carrinho: ' + '; '.join(faltas) + '.')
            else:
                erros.append('Nao foi possivel reservar estoque agora. '
                             'Tente novamente em alguns segundos.')
            return None, erros
    db.session.commit()
    # Auto-salva o endereço estruturado do cliente logado pra ele reusar no
    # próximo pedido. Só faz pra entrega (retirada não tem endereço).
    if (cliente.tem_conta if hasattr(cliente, 'tem_conta') else False) \
            and modo != 'retirada' and end_logradouro:
        _salvar_ou_atualizar_endereco_principal(
            cliente, dict(cep=endereco_cep, logradouro=end_logradouro,
                          numero=end_numero, complemento=end_complemento,
                          bairro=end_bairro, cidade=end_cidade, uf=end_uf,
                          lat=None, lng=None))
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


def _salvar_ou_atualizar_endereco_principal(cliente, dados):
    """Salva o endereço como `principal` do cliente. Deduplica por
    logradouro+numero+cep — se já existe, atualiza."""
    from app.models import EnderecoCliente
    existente = EnderecoCliente.query.filter_by(
        cliente_id=cliente.id,
        logradouro=dados['logradouro'],
        numero=dados['numero'],
        cep=dados['cep'],
    ).first()
    if existente:
        end = existente
    else:
        end = EnderecoCliente(cliente_id=cliente.id)
        db.session.add(end)
    for k, v in dados.items():
        setattr(end, k, v)
    # Reset principal das outras, marca essa
    EnderecoCliente.query.filter(
        EnderecoCliente.cliente_id == cliente.id,
        EnderecoCliente.id != (end.id if existente else None),
    ).update({'principal': False})
    end.principal = True
    db.session.commit()


def endereco_principal(cliente):
    """Devolve o endereço marcado como `principal` do cliente, ou None.

    Fallback: se ainda não tem `EnderecoCliente` salvo (cliente recém
    cadastrado / pedidos antigos que rodaram como guest), usa o último
    pedido de entrega como fonte — assim a segunda compra já vem
    pré-preenchida sem precisar do cliente "passar" pelo auto-salvar."""
    if not cliente:
        return None
    from app.models import EnderecoCliente, PedidoOnline
    salvo = (EnderecoCliente.query
             .filter_by(cliente_id=cliente.id, principal=True)
             .first())
    if salvo:
        return salvo
    # Sem endereço salvo: monta um "endereço virtual" do último pedido de
    # entrega. Apenas leitura — não persiste (auto-salva só roda no fim do
    # próximo checkout).
    ultimo = (PedidoOnline.query
              .filter(PedidoOnline.cliente_id == cliente.id,
                      PedidoOnline.modo_entrega != 'retirada',
                      PedidoOnline.endereco_logradouro.isnot(None))
              .order_by(PedidoOnline.criado_em.desc())
              .first())
    if not ultimo:
        return None
    return EnderecoCliente(
        cliente_id=cliente.id,
        cep=ultimo.endereco_cep,
        logradouro=ultimo.endereco_logradouro,
        numero=ultimo.endereco_numero,
        complemento=ultimo.endereco_complemento,
        bairro=ultimo.endereco_bairro,
        cidade=ultimo.endereco_cidade,
        uf=ultimo.endereco_uf,
    )
