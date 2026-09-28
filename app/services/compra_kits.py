"""Compra única de um kit por data escolhida pelo cliente."""
import logging
import re
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.exc import SQLAlchemyError

from app.extensions import db
from app.models import CompraKit, EntregaKit
from app.utils import agora

logger = logging.getLogger(__name__)
DIAS_AGENDA_KITS = 31


def grupo_do_pedido(pedido):
    pedido_id = getattr(pedido, 'id', None)
    if pedido_id is None:
        return None
    entrega = db.session.get(EntregaKit, pedido_id)
    return entrega.compra if entrega else None


def pedidos_do_grupo(compra):
    return [e.pedido for e in compra.entregas]


def reler_entregas(compra):
    """Relê marcadores após os locks dos pedidos; identity map pode ser anterior à espera."""
    return (EntregaKit.query.filter_by(compra_id=compra.id)
            .order_by(EntregaKit.ordem).populate_existing().all())


def principal_do_pedido(pedido):
    compra = grupo_do_pedido(pedido)
    return compra.pedido_principal if compra else pedido


def valor_cobranca(pedido):
    compra = grupo_do_pedido(pedido)
    return compra.valor_total if compra else pedido.valor_total


def _assinatura_itens(itens):
    return [(it.kind, it.receita_id, it.produto_id, it.nome,
             it.quantidade, it.preco_unitario, it.subtotal, bool(it.fatiado),
             sorted((c.produto_item_id, c.tipo, c.alvo_id, c.nome,
                     c.quantidade, c.preco_unitario) for c in it.componentes))
            for it in itens]


def _snapshot_composicao(itens):
    """Captura preço/composição ANTES da cotação de frete e valida todas as datas."""
    from app.models import PedidoOnlineItem
    from app.services.loja_checkout import _persistir_composicao_menu
    modelos = []
    for item in itens:
        modelo = PedidoOnlineItem(kind=item['kind'], receita_id=item['receita_id'],
                                   produto_id=item['produto_id'], nome=item['nome'],
                                   quantidade=item['qtd'], preco_unitario=item['preco'],
                                   subtotal=item['subtotal'], fatiado=item.get('fatiado'))
        if item.get('comp'):
            _persistir_composicao_menu(modelo, item['produto_id'], item['comp'])
        modelos.append(modelo)
    return _assinatura_itens(modelos)


class FormularioJaUsado(Exception):
    """O nonce do formulário já gerou uma compra que não pode ser devolvida.

    Acontece quando o cliente volta pelo histórico ou usa duas abas e envia
    o formulário ALTERADO (outra data, suco, adicional, endereço, e-mail), ou
    quando a compra do nonce já foi paga, cancelada ou expirou. Devolver a
    compra antiga em silêncio cobraria algo diferente do que o cliente acabou
    de escolher. A rota troca o nonce da sessão e reapresenta o formulário
    com os dados enviados e esta mensagem.
    """

    MENSAGENS = {
        # Paga: a rota leva à página do pedido — nunca convida a comprar de
        # novo (cliente voltando pelo histórico criava outra cobrança,
        # revisão 28/09/2026).
        'paga': ('A compra {codigo} deste formulário já foi paga. Acompanhe '
                 'as entregas nesta página; para comprar outro kit, abra o '
                 'kit de novo.'),
        'cancelada': ('A compra {codigo} deste formulário foi cancelada. Para '
                      'fazer uma nova compra, confira os dados e envie de novo.'),
        # Prazo vencido não prova que não houve pagamento (webhook atrasado):
        # o texto não afirma que "nada foi cobrado".
        'expirada': ('O prazo de pagamento da compra {codigo} deste formulário '
                     'terminou. Se você já pagou, não envie de novo: a '
                     'confirmação chega por e-mail. Para fazer uma nova compra, '
                     'confira os dados e envie de novo.'),
        'pendente': ('Este formulário já gerou a compra {codigo}. Confira os '
                     'dados e envie de novo para criar uma nova compra.'),
    }

    def __init__(self, codigo, situacao='pendente'):
        self.codigo = codigo
        self.situacao = situacao if situacao in self.MENSAGENS else 'pendente'
        super().__init__(self.MENSAGENS[self.situacao].format(codigo=codigo))


def _situacao_da_compra(compra, base):
    """'paga' | 'cancelada' | 'expirada' | 'pendente' — para a mensagem do
    formulário reenviado com o nonce de uma compra que não será devolvida."""
    pedidos = [entrega.pedido for entrega in compra.entregas]
    if compra.pago_em is not None or any(p.pago_em for p in pedidos):
        return 'paga'
    if any(p.status == 'cancelado' for p in pedidos):
        return 'cancelada'
    if compra.expira_em <= base:
        return 'expirada'
    return 'pendente'


def _valores_do_campo(form, campo):
    return (form.getlist(campo) if hasattr(form, 'getlist') else
            [form[campo]] if campo in form else [])


def _ler_escolhas(kit, form):
    """Suco e opções de cada grupo enviados pelo cliente: (suco_id, escolhas, erros)."""
    suco_id = None
    valores_suco = _valores_do_campo(form, 'suco_id')
    if kit.sucos:
        if (len(valores_suco) != 1 or not isinstance(valores_suco[0], str)
                or not re.fullmatch(r'[1-9][0-9]{0,9}', valores_suco[0])):
            return None, None, ['Escolha um dos sucos disponíveis neste kit.']
        suco_id = int(valores_suco[0])
    elif valores_suco:
        return None, None, ['Este kit não oferece escolha de suco.']
    grupos = {opcao.grupo for opcao in kit.opcoes}
    campos_escolha = {f'escolha_{grupo}' for grupo in grupos}
    if any(campo.startswith('escolha_') and campo not in campos_escolha for campo in form):
        return None, None, ['Este kit não oferece uma das escolhas recebidas. Reabra o kit.']
    escolhas = {}
    for grupo in grupos:
        valores = _valores_do_campo(form, f'escolha_{grupo}')
        if (len(valores) != 1 or not isinstance(valores[0], str)
                or not re.fullmatch(r'(receita|produto):[1-9][0-9]{0,9}', valores[0])):
            return None, None, ['Escolha uma opção de cada grupo disponível neste kit.']
        escolhas[grupo] = valores[0]
    return suco_id, escolhas, []


def _texto(valor):
    return ' '.join(str(valor or '').split()).casefold()


def _itens_agregados(itens):
    """(kind, id, qtd, comp) → multiconjunto somado, independente da ordem das linhas."""
    agregados = {}
    for kind, item_id, qtd, comp in itens:
        chave = (kind, int(item_id or 0),
                 tuple(sorted((int(k), int(v)) for k, v in (comp or {}).items())))
        agregados[chave] = agregados.get(chave, 0) + int(qtd or 0)
    return sorted(agregados.items())


def _contato_normalizado(nome, email, telefone, documento, cep, logradouro, numero,
                         complemento, bairro, cidade, uf):
    from app.services.fiscal_online import digitos
    from app.utils import normalizar_documento
    # Documento pela fonte única: `digitos` apagava as letras do CNPJ
    # alfanumérico e trocar só uma letra passava como "mesmo envio".
    return (_texto(nome), _texto(email), digitos(telefone), normalizar_documento(documento),
            digitos(cep), _texto(logradouro), _texto(numero), _texto(complemento),
            _texto(bairro), _texto(cidade), _texto(str(uf or '').strip().upper()[:2]))


def _fiscal_do_form(form):
    """Dados fiscais do bloco de CNPJ como o checkout os grava, ou None
    (CPF, ou PJ sem o bloco). Mesma normalização de `checkout_fiscal.validar`."""
    from app.services import checkout_fiscal
    from app.utils import normalizar_documento
    if (len(normalizar_documento(form.get('cpf'))) != 14
            or form.get('fiscal_checkout') != '1'):
        return None
    dados = checkout_fiscal._normalizar(
        {campo: form.get('fiscal_' + campo) for campo in checkout_fiscal.CAMPOS})
    return tuple(sorted(dados.items())), str(form.get('fiscal_situacao_ie') or 'desconhecida')


def _fiscal_da_compra(pedido):
    """A mesma forma de `_fiscal_do_form`, lida do cadastro fiscal gravado."""
    from app.services.fiscal_online import registro
    row = registro(pedido)
    dados = {campo: valor for campo, valor in ((row.dados or {}) if row else {}).items()
             if not campo.startswith('_')}
    if not dados:
        return None
    return tuple(sorted(dados.items())), row.situacao_ie


def _envio_normalizado(kit, form, agenda):
    """O que o cliente escolheu neste envio, sem cotação de frete nem rede.

    None quando o envio nem chega a ser uma escolha válida: para comparar com
    uma compra existente isso conta como "diferente" (nunca como igual).
    """
    from app.services import kits_adicionais, kits_cafe
    suco_id, escolhas, erros = _ler_escolhas(kit, form)
    if erros:
        return None
    try:
        raw = kits_cafe.itens_do_kit(kit, suco_id, escolhas)
    except ValueError:
        return None
    adicionais, erros = kits_adicionais.ler(form, conferir_catalogo=False)
    if erros:
        return None
    itens = _itens_agregados((it['kind'], it['id'], it['qtd'], it.get('comp'))
                             for it in [*raw, *adicionais])
    nome = f"{(form.get('nome') or '').strip()} {(form.get('sobrenome') or '').strip()}"
    contato = _contato_normalizado(
        nome, form.get('email'), form.get('telefone'), form.get('cpf'), form.get('cep'),
        form.get('logradouro'), form.get('numero'), form.get('complemento'),
        form.get('bairro'), form.get('cidade'), form.get('uf'))
    datas = tuple((item['data'], item['janela'].strip()) for item in agenda)
    # Razão social, IE e endereço fiscal corrigidos no reenvio não podem
    # devolver a compra com a NF dos dados antigos (revisão 28/09/2026).
    return datas, itens, contato, _fiscal_do_form(form)


def _compra_normalizada(compra):
    """A mesma forma de `_envio_normalizado`, lida do que foi gravado na compra."""
    from app.services.fiscal_online import documento
    pedidos = [entrega.pedido for entrega in reler_entregas(compra)]
    if not pedidos:
        return None
    itens_por_entrega = [
        _itens_agregados((it.kind, it.receita_id if it.kind == 'receita' else it.produto_id,
                          it.quantidade,
                          {c.produto_item_id: c.quantidade for c in it.componentes} or None)
                         for it in pedido.itens)
        for pedido in pedidos]
    if any(itens != itens_por_entrega[0] for itens in itens_por_entrega):
        return None
    principal = compra.pedido_principal
    contato = _contato_normalizado(
        principal.nome_cliente, principal.email_cliente, principal.telefone_cliente,
        documento(principal), principal.endereco_cep, principal.endereco_logradouro,
        principal.endereco_numero, principal.endereco_complemento,
        principal.endereco_bairro, principal.endereco_cidade, principal.endereco_uf)
    datas = tuple(sorted((p.data_entrega.isoformat() if p.data_entrega else '',
                          (p.janela_entrega or '').strip()) for p in pedidos))
    return datas, itens_por_entrega[0], contato, _fiscal_da_compra(principal)


def _pode_devolver_existente(compra, kit, form, agenda, base):
    """Só o duplo clique legítimo recebe de volta a compra do mesmo nonce.

    Exige o MESMO conteúdo e uma compra que ainda aguarda pagamento dentro do
    prazo. Formulário alterado (histórico, duas abas) ou compra encerrada
    precisa de um nonce novo para virar uma compra nova.
    """
    if compra.pago_em is not None or compra.expira_em <= base:
        return False
    pedidos = [entrega.pedido for entrega in compra.entregas]
    if not pedidos or any(p.pago_em or p.status != 'aguardando_pagamento' for p in pedidos):
        return False
    envio = _envio_normalizado(kit, form, agenda)
    return envio is not None and envio == _compra_normalizada(compra)


def validar_agenda(agenda):
    if not isinstance(agenda, list) or not 1 <= len(agenda) <= DIAS_AGENDA_KITS:
        return [], ['Escolha entre 1 e 31 datas de entrega. Cada data corresponde a um kit.']
    datas = set()
    out = []
    for item in agenda:
        if not isinstance(item, dict):
            return [], ['Revise as datas e os horários das entregas.']
        raw = item.get('data')
        janela = item.get('janela')
        try:
            dia = date.fromisoformat(raw)
        except (ValueError, TypeError):
            return [], ['Informe uma data válida para cada entrega.']
        if dia.isoformat() != raw or not isinstance(janela, str) or not janela or len(janela) > 40:
            return [], ['Escolha o horário de cada entrega.']
        if dia in datas:
            return [], ['Escolha cada data uma única vez: cada dia corresponde a um kit.']
        datas.add(dia)
        out.append({'data': raw, 'janela': janela})
    return sorted(out, key=lambda item: item['data']), []


def _composicao_da_compra(kit, form, base):
    """(raw, itens, erros) da escolha do cliente: suco, opções e adicionais.

    Só leitura. Chamada sem trava (antes de cotar o frete) e de novo sob a
    trava do kit, onde o resultado é o que vale."""
    from app.services import kits_adicionais, kits_cafe, loja_checkout

    suco_id, escolhas, erros = _ler_escolhas(kit, form)
    if erros:
        return None, None, erros
    try:
        # Captura o produto escolhido antes de qualquer cotação externa.
        raw = kits_cafe.itens_do_kit(kit, suco_id, escolhas)
    except ValueError as exc:
        return None, None, [str(exc)]
    itens, erros = kits_cafe.montar(kit, suco_id, escolhas)
    if erros or not itens:
        return None, None, erros or ['Este kit está indisponível.']
    adicionais, erros = kits_adicionais.ler(form)
    if erros:
        return None, None, erros
    raw.extend(adicionais)
    if adicionais:
        itens, erros = loja_checkout.montar_itens(
            raw, dias_disponibilidade=DIAS_AGENDA_KITS, base=base)
        if erros or len(itens) != len(raw):
            return None, None, erros or ['Revise os produtos adicionais do kit.']
    return raw, itens, []


def criar_compra(kit, form, agenda, *, checkout_token, base=None):
    """Reutiliza as validações do checkout para cada data sem commits parciais.

    O nonce vem da sessão assinada; a rota confere sua origem. A unicidade e
    o lock do kit impedem duas compras por um duplo clique do mesmo formulário.
    Os valores individuais são os únicos pedidos que entram no faturamento.

    Nonce já usado: devolve a compra existente SÓ quando o envio é o mesmo e
    ela ainda aguarda pagamento no prazo (duplo clique). Caso contrário
    levanta `FormularioJaUsado`, sem cotar frete nem gravar nada.
    """
    from app.services import loja_checkout, loja_plano_dia

    if not re.fullmatch(r'[0-9a-f]{64}', str(checkout_token or '')):
        return None, ['Reabra o kit para iniciar uma nova compra.']
    agenda, erros = validar_agenda(agenda)
    if erros:
        return None, erros
    base = base or agora()
    # O frete (rede: Google/BrasilAPI/Nominatim, segundos) é cotado ANTES de
    # travar o kit — cotado sob o FOR UPDATE, prendia as outras compras e a
    # edição do dono durante toda a rede (revisão 28/09/2026). Só cota quando
    # a composição já passa numa conferência SEM trava (escolha inválida não
    # gasta cotação); sob a trava tudo é conferido de novo, e é essa conferência
    # que vale. Reenvio do mesmo formulário não cota (a compra existente é
    # decidida sob a trava).
    frete_validado = None
    snapshot_previo = None
    if not CompraKit.query.filter_by(checkout_token=checkout_token).first():
        # Relê o kit do banco: relações carregadas antes (outra aba, edição
        # do dono) não podem aprovar uma cotação que a trava depois recusa.
        db.session.refresh(kit)
        db.session.expire(kit, ['itens', 'sucos', 'opcoes'])
        if not kit.ativo:
            return None, ['Este kit não está disponível para compra.']
        _, itens_previos, erros = _composicao_da_compra(kit, form, base)
        if erros:
            return None, erros
        snapshot_previo = _snapshot_composicao(itens_previos)
        geo, cep_invalido = loja_checkout.texto_de_geocode(form)
        if not cep_invalido:
            contato = ' · '.join(p for p in (
                (form.get('nome') or '').strip(),
                (form.get('telefone') or '').strip(),
                (form.get('email') or '').strip()) if p)
            frete_validado = loja_checkout._frete_para(
                'agendada', geo, base=base, contato=contato)
    try:
        db.session.refresh(kit, with_for_update=True)
        # A relação pode ter sido carregada antes de aguardar o lock do owner.
        db.session.expire(kit, ['itens', 'sucos', 'opcoes'])
        existente = CompraKit.query.filter_by(checkout_token=checkout_token).first()
        if existente:
            if existente.kit_id != kit.id:
                return None, ['Este formulário pertence a outro kit. Reabra a compra.']
            if _pode_devolver_existente(existente, kit, form, agenda, base):
                return existente, []
            codigo = existente.pedido_principal.codigo
            situacao = _situacao_da_compra(existente, base)
            # Solta a trava do kit antes de a rota reapresentar o formulário.
            db.session.rollback()
            raise FormularioJaUsado(codigo, situacao)
        if not kit.ativo:
            return None, ['Este kit não está disponível para compra.']
        raw, itens, erros = _composicao_da_compra(kit, form, base)
        if erros:
            return None, erros
        snapshot = _snapshot_composicao(itens)
        if snapshot_previo is not None and snapshot != snapshot_previo:
            # Preço/composição mudou enquanto o frete era cotado: o cliente
            # reabre o kit e vê o total novo antes de pagar.
            db.session.rollback()
            return None, ['O preço ou a composição do kit mudou durante a compra. '
                          'Reabra o kit para conferir os itens e o total atualizados.']
        pedidos = []
        for agendamento in agenda:
            dados = dict(form)
            dados.update(modo_entrega='agendada', data_entrega=agendamento['data'],
                         janela_entrega=agendamento['janela'])
            pedido, erros = loja_checkout.criar_pedido(
                dados, raw, base=base, commit=False,
                dias_agenda=DIAS_AGENDA_KITS, reservar_estoque=False,
                frete_validado=frete_validado, itens_estritos=True,
                dias_disponibilidade=DIAS_AGENDA_KITS)
            if erros:
                db.session.rollback()
                return None, [f"{agendamento['data']}: {erro}" for erro in erros]
            if _assinatura_itens(pedido.itens) != snapshot:
                db.session.rollback()
                return None, ['O preço ou a composição do kit mudou durante a compra. '
                              'Reabra o kit para conferir os itens e o total atualizados.']
            # Uma cotação por endereço. Frete agendado usa a tabela por distância,
            # não uma corrida contratada; cada entrega recebe o mesmo valor.
            frete_validado = (pedido.frete_valor, pedido.distancia_km,
                              pedido.endereco_entrega, None)
            for it in pedido.itens:
                saldo = loja_plano_dia.saldo(it.kind, it.receita_id or it.produto_id,
                                           pedido.data_entrega)
                if saldo is not None and saldo < it.quantidade:
                    db.session.rollback()
                    return None, [f'{agendamento["data"]}: quantidade de {it.nome} '
                                  'indisponível para essa data. Escolha outro dia.']
            pedidos.append(pedido)
        compra = CompraKit(
            kit_id=kit.id, kit_nome=kit.nome, pedido_principal_id=pedidos[0].id,
            subtotal=sum((p.subtotal for p in pedidos), Decimal('0.00')),
            frete_total=sum((p.frete_valor for p in pedidos), Decimal('0.00')),
            valor_total=sum((p.valor_total for p in pedidos), Decimal('0.00')),
            expira_em=base + timedelta(minutes=35), checkout_token=checkout_token)
        db.session.add(compra)
        db.session.flush()
        for ordem, pedido in enumerate(pedidos, 1):
            db.session.add(EntregaKit(compra_id=compra.id, pedido_id=pedido.id, ordem=ordem))
        db.session.flush()
        from app.services.kits_capacidade import reservar_compra
        ok, erros = reservar_compra(compra)
        if not ok:
            db.session.rollback()
            return None, erros
        db.session.commit()
        return compra, []
    except SQLAlchemyError:
        db.session.rollback()
        logger.exception('Falha ao salvar compra de kit %s', kit.id)
        return None, ['Não foi possível salvar a compra. Tente novamente. Nenhuma cobrança foi feita.']


def cancelar_pendentes(pedido):
    """Cancela uma compra ainda não paga por inteiro, sem alterar outras reservas."""
    compra = grupo_do_pedido(pedido)
    if not compra:
        return False, 'Pedido não pertence a uma compra de kits.'
    db.session.refresh(compra, with_for_update=True)
    pedidos = sorted(pedidos_do_grupo(compra), key=lambda p: p.id)
    for p in pedidos:
        db.session.refresh(p, with_for_update=True)
    reler_entregas(compra)
    if compra.pago_em or any(p.pago_em for p in pedidos):
        return False, 'A compra já foi paga. Use o reembolso da entrega desejada.'
    for p in pedidos:
        if p.status not in ('aguardando_pagamento', 'cancelado'):
            return False, 'Esta compra já avançou na operação. Confira as entregas.'
    from app.services.kits_capacidade import liberar_compra
    liberar_compra(compra)
    for p in pedidos:
        p.status = 'cancelado'
        p.motivo_cancelamento = 'cancelado_admin'
        p.cancelado_em = agora()
    db.session.commit()
    return True, 'Compra ainda não paga cancelada, incluindo todas as datas agendadas.'
