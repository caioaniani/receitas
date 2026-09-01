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
import logging
import os
from datetime import date, timedelta
from decimal import Decimal

from app.extensions import db
from app.models import Cliente, Loja, PedidoOnline, PedidoOnlineItem
from app.services import frete as frete_svc
from app.services import loja_catalogo
from app.utils import agora

logger = logging.getLogger(__name__)

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

# Sob encomenda (dono 21/07/2026): item marcado `sob_encomenda` só pode ser
# entregue/retirado a partir de D+2 (dois dias à frente, desde a janela das
# 08:00). Ex: comprou na segunda → entrega/retirada válida a partir de
# quarta. FIXO (decisão do dono: não varia por receita). O carrinho todo
# herda o MAIOR lead dos seus itens (uma data de entrega por pedido).
ENCOMENDA_LEAD_DIAS = 2


def lead_do_carrinho(itens_raw):
    """Lead mínimo (em dias) que o carrinho exige: ENCOMENDA_LEAD_DIAS se
    QUALQUER item for `sob_encomenda`, senão 0. Recebe a lista crua
    [{kind,id,...}] (mesma de `montar_itens`) e consulta o catálogo. Best-
    effort: item inválido/fora de catálogo é ignorado (não força lead)."""
    for raw in (itens_raw or []):
        kind = (str(raw.get('kind') or '')).strip()
        try:
            item_id = int(raw.get('id'))
        except (TypeError, ValueError):
            continue
        if loja_catalogo.item_e_sob_encomenda(kind, item_id):
            return ENCOMENDA_LEAD_DIAS
    return 0


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
    chegar em ~1h (até a hora de corte do fim do expediente).

    DATA ESPECIAL (27/07/2026): o dia cadastrado com `express_bloqueado`
    não tem express NENHUM — nem dentro do horário. É o que faz "só uma
    janela no Dia dos Pais" ser verdade: sem isto, o cliente pediria entrega
    imediata às 15h e alguém teria que sair pra rua fora da leva única.
    Fica aqui, e não só na tela, porque `criar_pedido` valida por esta mesma
    função (ver o guard de modo e o ramo express em `criar_pedido`) — POST
    forjado também bate na trava."""
    from app.services import loja_data_especial
    base = base or agora()
    if loja_data_especial.express_bloqueado_em(base.date()):
        return False
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
# O separador das janelas, pra quem precisa fatiar a string (mensagem de
# erro). Fatiar com hífen NÃO funciona — é en-dash.
TRACO_JANELA = '–'

# Cartinha de presente: limite de caracteres (23/06/2026, decisão do dono —
# clientes empolgavam e enchiam o cupom da entrega).
CARTINHA_MAX_CHARS = int(os.environ.get('LOJA_CARTINHA_MAX', '250') or '250')

# Limites que existem no banco e precisam ser conhecidos ANTES do flush.
# Incidente 01/09/2026: o navegador enviou um texto longo no telefone e o
# PostgreSQL derrubou o checkout ao tentar gravar `varchar(30)`. O checkout
# deve devolver uma orientação legível, nunca deixar o banco validar PII.
NOME_MAX_CHARS = 150
EMAIL_MAX_CHARS = 200
TELEFONE_MAX_DIGITOS = 15  # E.164 (código do país + DDD + número)
ENDERECO_LIMITES = {
    'logradouro': 200,
    'numero': 20,
    'complemento': 100,
    'bairro': 100,
    'cidade': 100,
}


def janelas_disponiveis(modo, data=None, base=None, *, distancia_km=None):
    """Janelas válidas pro modo numa data. Quando a data é HOJE, remove as
    janelas que já passaram (início < agora + LEAD_HORAS). Em dias futuros,
    todas as janelas. `data` aceita date ou str ISO.

    `distancia_km` (opcional, vem do `consultar_frete`): quando informado e
    >= DISTANCIA_CORTE_PRIMEIRA_JANELA_KM, corta a 1ª janela (08-09) — o
    motoboy não chega a tempo (caso real Alphaville).

    DATA ESPECIAL (27/07/2026, `loja_data_especial`): dia cadastrado usa as
    janelas DELE no lugar das normais — vale pra agendada E pra retirada
    (decisão do dono). Esta função é o ponto único por onde o site oferece e
    o `criar_pedido` valida janela, então a regra especial entra aqui e
    todos os caminhos herdam de uma vez.
    """
    base = base or agora()
    if isinstance(data, str):
        try:
            data = date.fromisoformat(data)
        except ValueError:
            data = None
    if modo == 'express':
        # Express é sempre HOJE. Bloqueado na data → nenhuma janela.
        from app.services import loja_data_especial
        if loja_data_especial.express_bloqueado_em(data or base.date()):
            return []
        return [JANELA_EXPRESS]
    especiais = _janelas_especiais(data)
    if especiais is not None:
        # Dia especial: as janelas do dono SUBSTITUEM as normais. Lista vazia
        # = dia fechado (e não "cai no normal" — ver loja_data_especial).
        # O corte da 1ª janela por distância NÃO se aplica: aquelas janelas
        # foram escolhidas a dedo pra esse dia, e cortá-las poderia zerar o
        # dia inteiro pra quem mora longe (o 06:00–10:00 do Dia dos Pais tem
        # 4h de folga — o gargalo de alocação matinal não vale aqui).
        janelas = list(especiais)
        if data and data == base.date():
            janelas = _sem_janelas_passadas(janelas, base)
        return janelas
    janelas = list(JANELAS_HORARIAS)
    if data and data == base.date():
        janelas = _sem_janelas_passadas(janelas, base)
    # Corte de janelas matinais por distância (só agendada — express é por
    # horário; retirada não tem distância). Aplica EM QUALQUER dia (não só
    # hoje): pra a quinta às 8h o motoboy já passa pelo mesmo gargalo de
    # alocação matinal.
    if (modo == 'agendada'
            and distancia_km is not None
            and distancia_km >= DISTANCIA_CORTE_PRIMEIRA_JANELA_KM):
        janelas = [j for j in janelas if j not in JANELAS_CORTADAS_LONGE]
    return janelas


def _janelas_especiais(data):
    """Janelas cadastradas pra essa data, ou None se o dia é normal.

    `[]` (dia fechado) é DIFERENTE de None (sem cadastro) — por isso não dá
    pra usar lista vazia como sentinela."""
    from app.services import loja_data_especial
    tem, janelas = loja_data_especial.janelas_do_dia(data)
    return janelas if tem else None


def _sem_janelas_passadas(janelas, base):
    """Tira as janelas de HOJE que já passaram — viável = o FIM da janela
    ainda está além de agora + LEAD_HORAS (dá tempo de produzir e entregar
    DENTRO dela).

    Pra janela de 1h o corte é IDÊNTICO ao histórico (fim = início + 1, então
    fim <= limite ⇔ início < limite). A diferença é a FAIXA LARGA de dia
    especial ('06:00–10:00'): o corte antigo pelo INÍCIO fechava o dia
    INTEIRO às ~4h da manhã (06 < 4+2), com 6h de janela pela frente — caso
    real 09/08/2026 às 07:29, Dia dos Pais, venda barrada com "entrega só
    amanhã" (dono: "é algo sobre data especial?"). Sem o fim legível, cai no
    início + 1h (comportamento antigo).

    Janela ilegível é MANTIDA em vez de derrubar a página: a coluna
    `LojaDataEspecial.janelas` é texto e só o cadastro pela tela normaliza —
    uma linha escrita por fora (psql, import) com '6:00-10:00' faria
    `int('6:')` estourar DENTRO do render do checkout, ou seja o site
    inteiro em 500 (achado de revisão 27/07/2026)."""
    limite = base.hour + LEAD_HORAS
    out = []
    for j in janelas:
        try:
            inicio = int(j[:2])
            partes = j.split(TRACO_JANELA)
            try:
                fim = int(partes[1].strip()[:2]) if len(partes) > 1 else inicio + 1
            except (TypeError, ValueError, IndexError):
                fim = inicio + 1
            passou = fim <= limite
        except (TypeError, ValueError):
            logger.warning('janela ilegível no filtro de hora: %r', j)
            passou = False
        if not passou:
            out.append(j)
    return out


def janelas_especiais_do_periodo(datas, base=None):
    """`{'2026-08-09': ['06:00–10:00']}` pras datas com horário especial.

    Pro CHECKOUT do site, cujo seletor é montado no cliente a partir da lista
    global (`checkout.js::popularJanelas`): sem este mapa o site mostraria
    08:00–18:00 num dia especial e só o POST recusaria — com a mensagem
    errada. Só entram as datas que TÊM regra; dia normal fica de fora e o JS
    usa a lista global.

    Cobre o intervalo inteiro `[datas[0], datas[-1]]`, e não só as datas da
    lista: o `<input type=date>` do checkout é um intervalo contíguo
    (min/max), então um dia FECHADO — que `datas_disponiveis` já removeu da
    lista — continua clicável e precisa aparecer aqui com `[]` pra o cliente
    ver "não entregamos nesse dia" em vez de um seletor mentindo.

    Devolve as janelas CRUAS (sem o filtro de hora passada / distância): o JS
    aplica os mesmos filtros que aplica na lista global, e o servidor
    revalida tudo em `criar_pedido`."""
    from app.services import loja_data_especial
    if not datas:
        return {}
    base = base or agora()
    out = {}
    dia = min(datas)
    ultimo = max(datas)
    while dia <= ultimo:
        tem, janelas = loja_data_especial.janelas_do_dia(dia)
        if tem:
            out[dia.isoformat()] = list(janelas)
        dia += timedelta(days=1)
    return out


def datas_disponiveis(modo, base=None, dias=DIAS_AGENDA, *, lead_dias=0):
    """Datas válidas pro modo.

    - express: só hoje (entrega imediata, dentro do horário).
    - agendada/retirada: HOJE entra se ainda houver janela viável (lead),
      depois amanhã em diante (contíguo). Sem o antigo corte-17h-do-dia:
      janelas passadas são filtradas por `janelas_disponiveis`.

    `lead_dias` (sob encomenda, dono 21/07/2026): quando > 0, a PRIMEIRA data
    válida vira `hoje + lead_dias` (D+2 pro mini pain), sem hoje nem os dias
    intermediários — o item precisa de antecedência pra ser produzido. Como
    é dia futuro, todas as janelas (a partir das 08:00) valem.
    """
    base = base or agora()
    hoje_d = base.date()
    if modo == 'express':
        # Express é same-day; item sob encomenda (lead>0) não pode express.
        return [hoje_d] if (lead_dias <= 0 and express_disponivel(base)) else []
    datas = []
    if lead_dias > 0:
        inicio = hoje_d + timedelta(days=lead_dias)
        datas.extend(inicio + timedelta(days=i) for i in range(dias))
        return _sem_dias_fechados(datas)
    if janelas_disponiveis(modo, hoje_d, base=base):
        datas.append(hoje_d)
    inicio = hoje_d + timedelta(days=1)
    datas.extend(inicio + timedelta(days=i) for i in range(dias))
    return _sem_dias_fechados(datas)


def _sem_dias_fechados(datas):
    """Tira do calendário as datas cadastradas SEM nenhuma janela.

    Sem isto, o dia fechado (Natal) apareceria no seletor e o cliente
    escolheria uma data cujo seletor de horário vem vazio — beco sem saída
    no checkout. Só mexe em dia CADASTRADO como fechado; dia normal passa
    intacto (não vale a pena consultar janela de 14 datas aqui, e o dia de
    HOJE já é filtrado por janela logo acima)."""
    from app.services import loja_data_especial
    regras = loja_data_especial.regras_do_periodo(datas)   # 1 query, não N
    return [d for d in datas
            if not (d in regras and regras[d].fechado)]


def montar_itens(itens_raw):
    """Re-valida o carrinho contra o catálogo. NUNCA usa o preço do
    cliente — pega o preço publicado atual. Devolve (itens, avisos).

    itens_raw: lista de {kind, id, qtd, fatiado, comp} (do localStorage/sessão).
    item de saída: {kind, id, receita_id, produto_id, nome, preco, qtd,
                    subtotal, fatiado, comp}

    MENU CONFIGURÁVEL (26/07/2026): item cujo Produto é `menu_configuravel`
    tem a escolha do cliente (`comp` = {produto_item_id: qtd}) re-sanitizada
    contra o cadastro, o TOTAL obrigatório validado (regra do dono: 30 minis
    exatos) e o preço RECALCULADO pela soma do preço por mini. `comp` ausente
    = pré-seleção do cadastro.
    """
    from app.models import Produto
    from app.services import loja_menu
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
        sob_encomenda = bool(cat.get('sob_encomenda'))
        # Esgotou entre o carrinho e o checkout → não vende (regra do dono).
        # Sob encomenda TAMBÉM passa por aqui desde 07/08/2026 (o plano-do-
        # dia vale pra encomenda — decisão do dono, SUBSTITUI 21/07): o
        # "esgotado duro" (plano zerado na janela toda) remove do carrinho
        # com aviso, igual aos demais; a checagem POR DATA segue no
        # criar_pedido.
        if not loja_catalogo.tem_estoque_site(kind, item_id):
            avisos.append(f'"{cat["nome"]}" esgotou e foi removido do pedido.')
            continue
        preco = Decimal(str(cat['preco']))
        # ── Menu configurável: escolha do cliente é lei do SERVIDOR ──
        # Re-sanitiza contra o cadastro (slot de outro menu / qtd acima do
        # teto caem), valida o total obrigatório e RECALCULA o preço pela
        # soma do preço por mini. Nunca conserta em silêncio: escolha errada
        # (aba parada, carrinho velho, POST forjado) sai do pedido com aviso.
        comp = None
        if kind == 'produto' and cat.get('menu'):
            menu_prod = Produto.query.get(item_id)
            if not loja_menu.eh_menu(menu_prod):
                avisos.append('Um item saiu de catálogo e foi removido do '
                              'pedido.')
                continue
            comp = loja_menu.normalizar(menu_prod, raw.get('comp'))
            erro = loja_menu.validar(menu_prod, comp)
            if erro:
                avisos.append(erro)
                continue
            preco_menu = loja_menu.preco(menu_prod, comp)
            if preco_menu is None:
                avisos.append(f'"{cat["nome"]}" está sem preço configurado e '
                              'foi removido do pedido.')
                continue
            preco = preco_menu
        # "Fatiado?" sanitizado no SERVIDOR: só vale quando o cliente pediu E
        # o item de fato oferece a opção (sourdough, `cat['fatiavel']`) — não
        # confia no navegador (um POST forjado com fatiado=true num item que
        # não é sourdough é ignorado). NULL/False = inteiro.
        fatiado = bool(raw.get('fatiado')) and bool(cat.get('fatiavel'))
        itens.append({
            'kind': kind,
            'id': item_id,
            'receita_id': item_id if kind == 'receita' else None,
            'produto_id': item_id if kind == 'produto' else None,
            'nome': cat['nome'],
            'preco': preco,
            'qtd': qtd,
            'subtotal': preco * qtd,
            'fatiado': fatiado,
            # Sob encomenda (dono 21/07): pro checkout saber o lead do
            # carrinho (D+2). Desde 07/08/2026 a checagem do plano-do-dia
            # vale pra encomenda tambem (não é mais pulada).
            'sob_encomenda': sob_encomenda,
            # Menu configurável (26/07): a escolha JÁ sanitizada. None em
            # item comum. É ela que o pedido persiste e que a baixa de
            # estoque explode — nunca o cadastro da cesta.
            'comp': comp,
        })
    return itens, avisos


def _persistir_composicao_menu(poi, produto_id, comp):
    """Congela a composição escolhida do menu como filhos do item do pedido
    (`PedidoOnlineItemComponente`). Snapshot de nome e preço por unidade —
    reajuste posterior do cadastro não reescreve o que foi cobrado.

    Slot que sumiu do cadastro entre o carrinho e o commit é ignorado (o
    `montar_itens` já re-sanitizou contra o cadastro imediatamente antes)."""
    from app.models import PedidoOnlineItemComponente, Produto
    from app.services import loja_menu
    prod = Produto.query.get(produto_id) if produto_id else None
    if prod is None:
        return
    por_id = {s['pi_id']: s for s in loja_menu.slots(prod)}
    for pi_id, qtd in sorted(comp.items()):
        s = por_id.get(int(pi_id))
        if not s or qtd <= 0:
            continue
        poi.componentes.append(PedidoOnlineItemComponente(
            produto_item_id=s['pi_id'],
            tipo={'receita_id': 'receita', 'produto_id': 'produto',
                  'materia_prima_id': 'mp'}[s['col']],
            receita_id=s['alvo_id'] if s['col'] == 'receita_id' else None,
            produto_componente_id=(s['alvo_id'] if s['col'] == 'produto_id'
                                   else None),
            materia_prima_id=(s['alvo_id'] if s['col'] == 'materia_prima_id'
                              else None),
            nome=s['nome'][:200], quantidade=int(qtd),
            preco_unitario=(Decimal(str(s['preco']))
                            if s['preco'] is not None else None),
        ))


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


def _normalizar_telefone_checkout(valor):
    """Normaliza telefone digitado/colado sem deixar o banco derrubar a venda.

    Aceita formatação e até frases coladas por autofill (ex.: ``WhatsApp:
    (11) 98888-7777``), guardando só os dígitos. Mais de 15 dígitos quase
    sempre significa que dois campos foram colados juntos; nesse caso o
    cliente recebe erro de formulário e pode corrigir sem perder o carrinho.
    Telefone continua opcional por compatibilidade com os pedidos existentes.
    """
    bruto = (valor or '').strip()
    if not bruto:
        return '', None
    digitos = _so_digitos(bruto)
    if not digitos or len(digitos) > TELEFONE_MAX_DIGITOS:
        return '', 'Revise o telefone: informe apenas um número com DDD.'
    return digitos, None


def _validar_limite(erros, rotulo, valor, limite):
    """Adiciona erro amigável antes de um texto exceder o varchar do banco."""
    if valor and len(valor) > limite:
        erros.append(
            f'{rotulo} está muito longo (máximo de {limite} caracteres).')


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


def _cnpj_valido(cnpj):
    """Valida 14 dígitos + dígitos verificadores (mod 11 com os pesos da
    Receita Federal). Rejeita sequências iguais ('11111111111111')."""
    cnpj = _so_digitos(cnpj)
    if len(cnpj) != 14 or len(set(cnpj)) == 1:
        return False
    pesos = [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    for i in (12, 13):
        soma = sum(int(cnpj[j]) * pesos[len(pesos) - i + j] for j in range(i))
        dig = 11 - (soma % 11)
        if dig >= 10:
            dig = 0
        if dig != int(cnpj[i]):
            return False
    return True


def _cpf_cnpj_valido(doc):
    """CPF (11 dígitos) ou CNPJ (14 dígitos) válido — o campo de documento
    do checkout aceita os dois (pedido do dono, 13/07/2026: cliente PJ
    compra pelo site e precisa da NF no CNPJ)."""
    doc = _so_digitos(doc)
    if len(doc) == 11:
        return _cpf_valido(doc)
    if len(doc) == 14:
        return _cnpj_valido(doc)
    return False


def _montar_endereco(form, incluir_complemento=True):
    """Junta os campos estruturados em um texto de uma linha.

    `incluir_complemento=True` (default): snapshot da entrega gravado em
    `PedidoOnline.endereco_entrega` — o motorista PRECISA do apto/bloco.

    `incluir_complemento=False`: string PRA GEOCODE. O complemento (apto,
    bloco, nome do prédio) NÃO ajuda o geocoder e ATRAPALHA: nome de prédio
    ('Positano') e 'Ape 502' fazem o Google devolver `partial_match`
    (rejeitado como impreciso) e derrubam o Nominatim — barrando venda de
    endereço VÁLIDO (caso Mooca, CEP 03111-010, 11/07/2026). Rua + número +
    bairro + cidade + CEP é o que localiza."""
    partes = [
        (form.get('logradouro') or '').strip(),
        (form.get('numero') or '').strip(),
        (form.get('complemento') or '').strip() if incluir_complemento else '',
        (form.get('bairro') or '').strip(),
        (form.get('cidade') or '').strip(),
        (form.get('uf') or '').strip(),
    ]
    return ', '.join(p for p in partes if p)


def _frete_para(modo, endereco, base=None, contato=None):
    """Calcula o frete no servidor (autoritativo). Devolve
    (valor:Decimal, distancia_km, endereco_norm, erro|None)."""
    if modo == 'retirada':
        return Decimal('0.00'), None, None, None
    if not endereco:
        return None, None, None, 'Informe o endereço de entrega.'
    r = frete_svc.consultar_frete(endereco)
    from app.services import frete_sensor, loja_alerta
    if not r.get('ok'):
        if r.get('erro') == 'nao_encontrado':
            # Cliente prestes a comprar e barrado por endereço não localizado:
            # alerta o dono COM o contato pra chamar e fechar a venda + sensor.
            loja_alerta.alertar_endereco_falho(endereco, contato=contato)
            frete_sensor.registrar('checkout', 'barrado', endereco=endereco,
                                   contato=contato)
        return None, None, None, frete_svc.mensagem_erro(r.get('erro'))
    if r.get('fora_area'):
        # Além do raio = venda barrada. Painel registra TODOS; WhatsApp pra
        # quem ficou perto da borda (decisão do dono 09/07 — "quase comprou")
        # OU quando o km é INCERTO (impreciso = veio do centroide do CEP, pode
        # estar dentro da área na verdade — decisão do dono 09/07 pós-revisão).
        km = r.get('distancia_km')
        frete_sensor.registrar('checkout', 'fora_area', endereco=endereco,
                               contato=contato, fonte=r.get('fonte'), km=km)
        perto = km is not None and km <= frete_svc.RAIO_MAX_KM + frete_svc.MARGEM_ALERTA_FORA_KM
        if perto or r.get('impreciso'):
            loja_alerta.alertar_endereco_falho(endereco, contato=contato,
                                               motivo='fora_area')
        return None, km, r.get('endereco'), \
            ('Esse endereço está fora da nossa área de entrega '
             f'(até {int(frete_svc.RAIO_MAX_KM)} km).')
    if r.get('impreciso'):
        # Cotou só pelo centroide do CEP: a venda passa, mas o frete pode
        # estar errado — alerta o dono COM o contato pra conferir/ajustar.
        loja_alerta.alertar_endereco_falho(endereco, contato=contato,
                                           motivo='impreciso')
        frete_sensor.registrar('checkout', 'impreciso', endereco=endereco,
                               contato=contato, fonte=r.get('fonte'),
                               km=r.get('distancia_km'), valor=r.get('valor'))
    elif r.get('fonte') == 'google':
        # Google resolveu um pedido REAL (baixo volume no checkout) — registra
        # pro dono ver o Google enabling vendas.
        frete_sensor.registrar('checkout', 'resolvido_google', endereco=endereco,
                               contato=contato, fonte='google',
                               km=r.get('distancia_km'), valor=r.get('valor'))
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
    telefone, telefone_erro = _normalizar_telefone_checkout(
        form.get('telefone'))
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
    telefone_destinatario, telefone_destinatario_erro = (
        _normalizar_telefone_checkout(form.get('telefone_destinatario'))
        if e_presente else ('', None))
    telefone_destinatario = telefone_destinatario or None

    # Nome completo = nome + sobrenome. O servidor valida o CONJUNTO (sem
    # dígitos, com letras) — bloqueia o CPF no campo de nome. O "sobrenome
    # obrigatório" é garantido pelos 2 campos `required` do formulário web;
    # aqui aceitamos também o nome completo vindo num campo só (compat com
    # chamadas que mandam o nome inteiro).
    nome = f'{nome_dado} {sobrenome_dado}'.strip()
    _validar_limite(erros, 'O nome do comprador', nome, NOME_MAX_CHARS)
    _validar_limite(erros, 'O email', email, EMAIL_MAX_CHARS)
    if telefone_erro:
        erros.append(telefone_erro)
    if len(nome) <= NOME_MAX_CHARS and not _nome_valido(nome):
        erros.append('Informe seu nome e sobrenome (apenas letras, '
                     'sem números).')
    if len(email) <= EMAIL_MAX_CHARS and not _email_valido(email):
        erros.append('Informe um email válido.')
    # CPF/CNPJ é exigência do Pagar.me pra Pix e da NF-e (Fase 5) — pedir
    # aqui já é mais barato que voltar pro cliente depois. CNPJ aceito
    # desde 13/07/2026 (cliente PJ compra pelo site; NF sai no CNPJ).
    if not _cpf_cnpj_valido(cpf):
        erros.append('Informe um CPF ou CNPJ válido.')
    if not aceite:
        erros.append('É preciso aceitar os termos para concluir o pedido.')
    if modo not in ('agendada', 'retirada', 'express'):
        erros.append('Escolha um modo de entrega.')
    if e_presente and not nome_destinatario:
        erros.append('Informe o nome de quem vai receber.')
    elif e_presente:
        _validar_limite(erros, 'O nome de quem vai receber',
                        nome_destinatario, NOME_MAX_CHARS)
        if (len(nome_destinatario) <= NOME_MAX_CHARS
                and not _nome_valido(nome_destinatario)):
            erros.append('O nome de quem vai receber deve ter só letras '
                         '(sem números).')
    if telefone_destinatario_erro:
        erros.append('Revise o telefone de quem vai receber: informe apenas '
                     'um número com DDD.')

    itens, avisos = montar_itens(itens_raw)
    if not itens:
        erros.append('Seu carrinho está vazio ou os itens saíram de catálogo.')

    # Sob encomenda (dono 21/07/2026): se QUALQUER item é sob encomenda, o
    # pedido inteiro só entrega/retira a partir de D+2 (o item precisa de
    # antecedência pra ser produzido) e NÃO pode express (same-day). O lead
    # do carrinho é o MAIOR dos itens — uma data de entrega por pedido.
    lead_encomenda = (ENCOMENDA_LEAD_DIAS
                      if any(it.get('sob_encomenda') for it in itens) else 0)
    if lead_encomenda > 0 and modo == 'express':
        erros.append('Este pedido tem item sob encomenda e não pode ser '
                     'entrega express (no mesmo dia). Escolha entrega '
                     'agendada ou retirada, a partir de D+2.')

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
        # Endereco do cliente pra NOTA FISCAL (dono 20/07/2026): a retirada
        # tambem precisa do endereco estruturado, senao a NF-e sai com o
        # destinatario em branco e a SEFAZ rejeita (endereco/bairro/UF em
        # branco). NAO recalcula frete nem geocodifica — o endereco serve SO
        # pra nota; a retirada continua na loja e o frete fica R$0. Mesma
        # exigencia de campos da entrega (numero obrigatorio, decisao do
        # dono); a linha `endereco_entrega` acima segue mostrando a loja pra
        # operacao. Reusa os MESMOS campos do form (o checkout mostra o bloco
        # de endereco na retirada tambem).
        logradouro = (form.get('logradouro') or '').strip()
        numero = (form.get('numero') or '').strip()
        bairro = (form.get('bairro') or '').strip()
        cidade = (form.get('cidade') or '').strip()
        uf = (form.get('uf') or '').strip().upper()[:2]
        # Exige o CONJUNTO que a SEFAZ pede (bairro/UF inclusos): no caminho
        # feliz vêm READONLY do CEP; exigir aqui fecha a armadilha do
        # fail-open (CEP fora do ar / CEP sem rua) — sem isso o pedido pago
        # ficava com a NF travada pra sempre (guard da emissão) e a retirada
        # não tem editor de endereço no admin (achado de revisão 20/07/2026).
        if not endereco_cep:
            erros.append('Informe o CEP para a nota fiscal.')
        if not logradouro:
            erros.append('Informe o logradouro (rua/avenida) para a nota fiscal.')
        if not numero:
            erros.append('Informe o número do endereço para a nota fiscal.')
        elif not numero.isdigit():
            # Mesma regra da entrega (dono 09/08/2026): numero so digitos.
            erros.append('O número do endereço deve conter apenas números '
                         '(apto/bloco vão no campo complemento).')
        if not bairro:
            erros.append('Informe o bairro para a nota fiscal.')
        if not cidade:
            erros.append('Informe a cidade para a nota fiscal.')
        if not uf:
            erros.append('Informe o estado (UF) para a nota fiscal.')
        end_logradouro = logradouro or None
        end_numero = numero or None
        end_complemento = (form.get('complemento') or '').strip() or None
        end_bairro = bairro or None
        end_cidade = cidade or None
        end_uf = uf or None
        _validar_limite(erros, 'O logradouro', end_logradouro,
                        ENDERECO_LIMITES['logradouro'])
        _validar_limite(erros, 'O número do endereço', end_numero,
                        ENDERECO_LIMITES['numero'])
        _validar_limite(erros, 'O complemento', end_complemento,
                        ENDERECO_LIMITES['complemento'])
        _validar_limite(erros, 'O bairro', end_bairro,
                        ENDERECO_LIMITES['bairro'])
        _validar_limite(erros, 'A cidade', end_cidade,
                        ENDERECO_LIMITES['cidade'])
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
        # Numero SO DIGITOS (dono 09/08/2026, pos-Dia dos Pais: "muitos
        # clientes colocaram errado o numero ou o complemento, foi caotico"
        # — "deve ser obrigatoriamente NUMEROS, nao aceitar vazio"). Letra/
        # texto no numero ("123 apto 4", "s/n") quebrava geocode e rota.
        if not numero:
            erros.append('Informe o número do endereço.')
        elif not numero.isdigit():
            erros.append('O número do endereço deve conter apenas números '
                         '(apto/bloco vão no campo complemento).')
        if not cidade:
            erros.append('Informe a cidade.')
        # Snapshot estruturado pra NF-e (alem da linha unica abaixo).
        end_logradouro = logradouro or None
        end_numero = numero or None
        end_complemento = (form.get('complemento') or '').strip() or None
        end_bairro = (form.get('bairro') or '').strip() or None
        end_cidade = cidade or None
        end_uf = ((form.get('uf') or '').strip().upper()[:2]) or None
        _validar_limite(erros, 'O logradouro', end_logradouro,
                        ENDERECO_LIMITES['logradouro'])
        _validar_limite(erros, 'O número do endereço', end_numero,
                        ENDERECO_LIMITES['numero'])
        _validar_limite(erros, 'O complemento', end_complemento,
                        ENDERECO_LIMITES['complemento'])
        _validar_limite(erros, 'O bairro', end_bairro,
                        ENDERECO_LIMITES['bairro'])
        _validar_limite(erros, 'A cidade', end_cidade,
                        ENDERECO_LIMITES['cidade'])
        endereco_txt = _montar_endereco(form)            # snapshot (c/ complemento)
        # geocoding usa rua+numero+bairro+cidade (SEM complemento — ele derruba
        # o geocoder) + CEP concatenado pra desambiguar bairros homonimos.
        geo_txt = _montar_endereco(form, incluir_complemento=False)
        geo = geo_txt
        if endereco_cep and endereco_cep not in geo:
            geo = f'{geo_txt}, {endereco_cep}' if geo_txt else endereco_cep
        _contato = ' · '.join(p for p in (
            f'{nome_dado} {sobrenome_dado}'.strip(), telefone, email) if p)
        valor, dist, end_norm, erro_frete = _frete_para(
            modo, geo, base=base, contato=_contato)
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
        # `lead_encomenda` (D+2) empurra a 1ª data válida quando o carrinho
        # tem item sob encomenda — mesma conta que o front usa pro `min` do
        # calendário. Servidor é a autoridade.
        disponiveis = {d.isoformat() for d in datas_disponiveis(
            modo, base=base, lead_dias=lead_encomenda)}
        if data_str not in disponiveis:
            if lead_encomenda > 0:
                erros.append('Item sob encomenda: escolha uma data a partir '
                             'de D+2 (dois dias à frente).')
            else:
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
                especiais = _janelas_especiais(data_entrega)
                if (modo == 'agendada' and distancia_km is not None
                        and distancia_km >= DISTANCIA_CORTE_PRIMEIRA_JANELA_KM
                        and janela in JANELAS_CORTADAS_LONGE):
                    erros.append(
                        f'Para o seu endereço ({distancia_km:.1f} km da loja), '
                        'não conseguimos entregar na janela das '
                        # split no EN-DASH: a janela usa '–', não '-'. Com o
                        # hífen o split não achava nada e imprimia a janela
                        # inteira (defeito antigo, achado em 27/07/2026).
                        f'{janela.split(TRACO_JANELA)[0]} — '
                        'escolha a partir das 09:00.')
                elif especiais is not None:
                    # DIA ESPECIAL (27/07/2026): "o horário já passou" seria
                    # mentira — o horário nem existe nesse dia. O cliente
                    # precisa saber QUAL é o horário, senão fica tentando.
                    if especiais:
                        erros.append(
                            'Nesse dia entregamos só em '
                            + ', '.join(especiais)
                            + ' — escolha um desses horários.')
                    else:
                        erros.append('Não entregamos nesse dia — escolha '
                                     'outra data.')
                else:
                    erros.append('Escolha uma janela de horário válida '
                                 '(o horário escolhido já passou).')

    # Plano por dia (22/06/2026): valida cada item contra o saldo do plano
    # da data_entrega escolhida. Mensagem CLARA com nome do produto + data
    # pra cliente saber o que tirar/o que mudar.
    if data_entrega and itens:
        esgotados = []
        for it in itens:
            # Sob encomenda TAMBÉM valida contra o plano-do-dia desde
            # 07/08/2026 (decisão do dono — SUBSTITUI o pulo de 21/07):
            # o dono zera o item no plano do dia curado e a encomenda é
            # barrada igual aos demais. O D+2 segue validado acima.
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
            # Alerta IMEDIATO ao dono (WhatsApp): o cliente ia comprar e foi
            # barrado por esgotado. Best-effort/async — nunca afeta o
            # checkout. FICA DENTRO do `if esgotados:` — a 1ª versão do
            # bloco de data especial abaixo o engoliu sem querer e o alerta
            # de esgotado morreu (achado CRÍTICO do revisor 07/08/2026,
            # pego pelo teste de test_loja_alerta no CI).
            try:
                from app.services import loja_alerta
                loja_alerta.alertar_esgotado(
                    nome, telefone, email, esgotados, data_entrega)
            except Exception:  # noqa: BLE001
                pass

    # Bloqueio de itens por DATA ESPECIAL (07/08/2026, caso "Caixa de Mini
    # vendida pro Dia dos Pais"): a data cadastrada pode barrar categorias/
    # itens específicos (LojaDataEspecial.bloquear_itens). Vale pra entrega
    # agendada, retirada E express (no express a data_entrega é hoje — se
    # hoje for a data especial, a curadoria vale igual). Diferente do
    # esgotado (estado transitório de estoque), aqui é curadoria do dono —
    # a mensagem diz isso pro cliente não ficar re-tentando.
    if data_entrega and itens:
        from app.services import loja_data_especial
        barrados = loja_data_especial.itens_bloqueados(data_entrega, itens)
        if barrados:
            regra = loja_data_especial.regra_do_dia(data_entrega)
            rotulo = (regra.rotulo if regra and regra.rotulo
                      else data_entrega.strftime('%d/%m/%Y'))
            nomes = '", "'.join(barrados)
            erros.append(
                f'Para {rotulo} trabalhamos com um cardápio especial — '
                f'"{nomes}" não está disponível pra entrega nessa data. '
                'Escolha outra data de entrega ou tire o item do carrinho.')

    if erros:
        return None, erros

    # ── Cria/reusa cliente (guest por email) ───────────────────────────
    cliente = Cliente.query.filter(
        db.func.lower(Cliente.email) == email.lower()).first()
    if not cliente:
        cliente = Cliente(nome=nome, email=email, telefone=telefone, cpf=cpf,
                          origem='site')
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
        poi = PedidoOnlineItem(
            kind=it['kind'],
            receita_id=it['receita_id'], produto_id=it['produto_id'],
            nome=it['nome'], preco_unitario=it['preco'],
            quantidade=it['qtd'], subtotal=it['subtotal'],
            fatiado=it.get('fatiado') or None,   # None = inteiro (compat)
        )
        # Menu configurável: congela a composição ESCOLHIDA no pedido. É ela
        # que a baixa de estoque e a produção vão ler — o cadastro da cesta
        # guarda só a pré-seleção (26/07/2026).
        if it.get('comp'):
            _persistir_composicao_menu(poi, it['produto_id'], it['comp'])
        pedido.itens.append(poi)
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
    # próximo pedido. Só pra ENTREGA: o endereço da retirada é coletado só
    # pra NF (20/07/2026) e NÃO é destino de entrega — não sobrescreve o
    # endereço principal de entrega do cliente.
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
