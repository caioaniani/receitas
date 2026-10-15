"""Cadastro fiscal por pedido. Consulta externa nunca inventa inscrição/isencão."""
from app.extensions import db
from app.models import FiscalPedidoOnline
from app.utils import agora, normalizar_documento

ENDERECO = ('endereco', 'numero', 'complemento', 'bairro', 'cep', 'cidade', 'uf')
SITUACOES_IE = ('contribuinte', 'isento', 'nao_contribuinte')
LIMITES_TINY = {'nome': 50, 'endereco': 50, 'numero': 10, 'complemento': 50,
                'bairro': 30, 'cidade': 30, 'uf': 2, 'cep': 10, 'ie': 18}
# Rótulos das mensagens de pendência (conferência do dono/NF) — a chave
# interna ('endereco') nunca aparece para quem lê. O checkout passa os
# rótulos da tela dele (`checkout_fiscal.ROTULOS_CHECKOUT`).
ROTULOS = {'nome': 'razão social', 'endereco': 'logradouro', 'numero': 'número',
           'complemento': 'complemento', 'bairro': 'bairro', 'cidade': 'cidade',
           'uf': 'UF', 'cep': 'CEP', 'ie': 'inscrição estadual'}


def digitos(valor):
    """Só dígitos — CEP e inscrição estadual. Documento (CPF/CNPJ) usa
    `normalizar_documento`: o CNPJ alfanumérico perderia as letras. Só
    0-9 ASCII: `isdigit` aceita '²' e dígitos de outros alfabetos, que iam
    para a NF (mesma regra de `loja_checkout._so_digitos`)."""
    return ''.join(c for c in str(valor or '') if '0' <= c <= '9')


def cortar_aos_limites(dados):
    """Corta cada campo ao limite do Tiny (sem espaço sobrando no fim).

    Devolve ``(dados_cortados, campos_abreviados)``. Quem mostra os dados ao
    cliente precisa avisar quais campos foram abreviados.
    """
    cortados = dict(dados)
    abreviados = []
    for campo, limite in LIMITES_TINY.items():
        valor = cortados.get(campo)
        if isinstance(valor, str) and len(valor) > limite:
            cortados[campo] = valor[:limite].rstrip()
            abreviados.append(campo)
    return cortados, abreviados


def registro(pedido):
    # SimpleNamespace é usado por integrações/testes de payload sem persistência.
    from sqlalchemy import inspect
    estado = inspect(pedido, raiseerr=False)
    return (db.session.get(FiscalPedidoOnline, pedido.id)
            if estado is not None and estado.identity else None)


def documento(pedido):
    row = registro(pedido)
    if row is not None:
        return row.documento
    return normalizar_documento(getattr(getattr(pedido, 'cliente', None), 'cpf', ''))


def congelar_documento(pedido, doc):
    """Na mesma transação do checkout, antes de pagamentos e emissões futuras."""
    row = db.session.get(FiscalPedidoOnline, pedido.id)
    if row is None:
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert
        inserir = pg_insert if db.engine.dialect.name == 'postgresql' else sqlite_insert
        db.session.execute(inserir(FiscalPedidoOnline).values(
            pedido_id=pedido.id, documento=normalizar_documento(doc), dados={}, criado_em=agora(),
        ).on_conflict_do_nothing(index_elements=['pedido_id']))
        row = db.session.get(FiscalPedidoOnline, pedido.id)
    return row


def _endereco_publico(dados):
    return {campo: str(dados.get('logradouro' if campo == 'endereco' else campo) or '').strip()
            for campo in ENDERECO}


def pendencias(row, rotulos=None):
    """O que falta para a NF, com os rótulos de quem lê (``rotulos``
    sobrepõe ``ROTULOS``; a chave interna nunca vai para a mensagem)."""
    rotulos = {**ROTULOS, **(rotulos or {})}
    dados = row.dados or {}
    erros = []
    for campo in ('nome', 'endereco', 'numero', 'bairro', 'cidade', 'uf', 'cep'):
        if not str(dados.get(campo) or '').strip():
            erros.append(rotulos[campo])
    if dados.get('cep') and len(digitos(dados['cep'])) != 8:
        erros.append('CEP válido')
    if dados.get('uf') and len(dados['uf']) != 2:
        erros.append('UF válida')
    if row.situacao_ie not in SITUACOES_IE:
        erros.append('inscrição estadual ou condição de contribuinte confirmada')
    elif row.situacao_ie == 'contribuinte' and not digitos(dados.get('ie')):
        erros.append(rotulos['ie'])
    for campo, limite in LIMITES_TINY.items():
        if len(str(dados.get(campo) or '')) > limite:
            erros.append(f'{rotulos[campo]} com até {limite} caracteres (limite da nota fiscal)')
    return erros


# ── Cadastro DECLARADO pelo cliente resolvido sem o dono (15/10/2026) ─────
# Dono: "Por que fica aguardando minha conferência? Deveria já buscar
# automaticamente e emitir". Caso ca37c72e: 17 dias sem nota porque a
# consulta do checkout só "confirma" quando acha IE ativa — empresa SEM
# inscrição (serviços) nunca era confirmada. Agora a fila reconsulta a base
# pública (CNPJ.ws, a mesma do checkout) e decide sozinha quando a resposta
# é clara; o dono só é chamado no que a base não resolve.
UF_EMITENTE = 'SP'                 # emissão intraestadual; fora disso, dono
REAVALIAR_DECLARADO_MIN = 30       # não reconsulta a cada tentativa da fila
BASE_VALIDADE_DIAS = 365           # base pública mais velha que isso não decide
ORIGEM_AUTO = 'auto_consulta'
# Erro que a própria fila resolve (base/Tiny fora do ar): não é pendência do
# dono e não entra no aviso "precisa conferir". Fonte única: `aguarda_dono`.
SUFIXO_TRANSITORIO = ' A fila tenta de novo automaticamente.'


def aguarda_dono(row):
    """O cadastro fiscal trava a nota até o dono conferir (qualquer origem)."""
    return bool(row is not None and row.erro and not row.confirmado_em
                and not row.erro.endswith(SUFIXO_TRANSITORIO))


def _pendente_dono(row, motivo):
    row.erro = (f'{motivo} O dono precisa conferir o cadastro fiscal '
                'antes de autorizar a nota.')
    row.consultado_em = agora()
    return False


def _pendente_transitorio(row, motivo):
    """Base ou Tiny indisponível: sem carimbar `consultado_em`, a próxima
    tentativa da fila (com o backoff dela) consulta de novo."""
    row.erro = motivo + SUFIXO_TRANSITORIO
    return False


def _base_recente(atualizado_em):
    """A base pública foi atualizada há menos de BASE_VALIDADE_DIAS? Data
    ausente ou ilegível = não sabe (False)."""
    from datetime import datetime, timedelta
    texto = str(atualizado_em or '').strip()
    if not texto:
        return False
    try:
        quando = datetime.fromisoformat(texto.replace('Z', '+00:00'))
    except ValueError:
        return False
    if quando.tzinfo is not None:
        quando = quando.replace(tzinfo=None)
    return quando >= agora() - timedelta(days=BASE_VALIDADE_DIAS)


def resolver_declarado(row):
    """Tenta resolver um cadastro `checkout_declarado` pela base pública.

    Regras (só a CNPJ.ws lista as inscrições; outra fonte = não sabe):
    - empresa fora de SP (`UF_EMITENTE`) → dono: a natureza de venda usa
      CFOP de dentro do estado e a SEFAZ rejeita destino em outra UF.
    - base com EXATAMENTE uma IE ativa na UF → contribuinte com os dados da
      base; se o cliente ou o cadastro do Tiny têm OUTRA IE → dono.
    - base listando ZERO IE ativa na UF → não contribuinte (ou isento, se o
      cliente declarou isento ou o Tiny tem ISENTO); IE declarada pelo
      cliente ou no Tiny → dono.
    - base sem a lista, com inscrição de situação desconhecida, com várias
      IEs ou desatualizada há mais de BASE_VALIDADE_DIAS → dono.
    - base ou Tiny fora do ar → a fila tenta de novo (não é pendência do dono).
    Faz rede (CNPJ.ws e Tiny) com a trava fiscal do chamador, como a consulta
    do owner. Sem commit. Devolve True se resolveu.
    """
    from app.services import consulta_empresa, tiny
    from app.services.checkout_fiscal import normalizar_dados

    consulta = consulta_empresa.consultar(row.documento)
    bruto = consulta.get('dados') or {}
    if not bruto or consulta.get('origem') != consulta_empresa._PROVEDOR:
        return _pendente_transitorio(row, 'A base pública não respondeu com a lista de '
                                     'inscrições estaduais.')
    pub, _ = cortar_aos_limites(normalizar_dados(bruto))
    declarado = row.dados or {}
    ie_decl = digitos(declarado.get('ie'))
    if pub['uf'] != UF_EMITENTE:
        return _pendente_dono(row, f'Empresa de outro estado ({pub["uf"] or "?"}): a nota '
                              'de venda usa CFOP de dentro do estado e a SEFAZ rejeita.')
    n = consulta.get('ie_ativas_uf')
    if n is None:
        return _pendente_dono(row, 'A base pública não trouxe as inscrições '
                              'estaduais desta empresa (ou alguma está sem situação).')
    if not _base_recente(consulta.get('atualizado_em')):
        return _pendente_dono(row, 'A base pública está desatualizada para decidir a '
                              'inscrição estadual.')
    contato_res = tiny.buscar_contato_por_documento(row.documento)
    if contato_res.get('erro'):
        return _pendente_transitorio(row, 'O cadastro do Tiny não respondeu.')
    ie_tiny = str((contato_res.get('contato') or {}).get('ie') or '').strip()
    tiny_isento = ie_tiny.upper() == 'ISENTO'
    ie_tiny = '' if tiny_isento else digitos(ie_tiny)
    if n == 1 and bruto.get('situacao_ie') == 'contribuinte' and pub['ie']:
        for fonte, ie in (('O cliente informou', ie_decl), ('O cadastro do Tiny tem', ie_tiny)):
            if ie and ie != pub['ie']:
                return _pendente_dono(row, f'{fonte} a IE {ie}, mas a base pública '
                                      f'mostra a IE {pub["ie"]}.')
        situacao = 'contribuinte'
    elif n == 0:
        if ie_decl or row.situacao_ie == 'contribuinte':
            return _pendente_dono(row, 'O cliente informou ser contribuinte, mas a base '
                                  'pública não mostra inscrição estadual ativa.')
        if ie_tiny:
            return _pendente_dono(row, f'O cadastro do Tiny tem a IE {ie_tiny}, mas a base '
                                  'pública não mostra inscrição estadual ativa.')
        pub['ie'] = ''
        situacao = ('isento' if row.situacao_ie == 'isento' or tiny_isento
                    else 'nao_contribuinte')
    else:
        return _pendente_dono(row, f'A base pública lista {n} inscrições estaduais '
                              'ativas; não dá para escolher sozinho.')
    candidato = FiscalPedidoOnline(documento=row.documento, dados=pub, situacao_ie=situacao)
    faltando = pendencias(candidato)
    if faltando:
        return _pendente_dono(row, 'A base pública não tem: ' + ', '.join(faltando) + '.')
    row.dados = {**pub,
                 # metadados anteriores (ex.: `_checkout`) ficam na trilha
                 **{k: v for k, v in declarado.items() if str(k).startswith('_')},
                 '_auto': {
                     'resolvido_em': agora().isoformat(), 'fonte': consulta.get('origem'),
                     'base_atualizada_em': consulta.get('atualizado_em'),
                     'declarado': {k: v for k, v in declarado.items()
                                   if not str(k).startswith('_')},
                     'situacao_declarada': row.situacao_ie,
                     'ie_tiny': 'ISENTO' if tiny_isento else ie_tiny,
                 }}
    row.situacao_ie = situacao
    row.origem = ORIGEM_AUTO
    row.consultado_em = agora()
    row.erro = None
    db.session.flush()
    return True


def consultar(pedido, *, forcar=False):
    """Somente leitura nos provedores; persiste a consulta no nosso pedido.

    Chamador segura a trava fiscal. Não reconsulta dados confirmados pelo owner.
    Contato Tiny serve como fonte da IE, a base CNPJ como fonte cadastral;
    falha do Tiny não é interpretada como ausência de inscrição.
    """
    from app.services import cnpj, tiny

    row = congelar_documento(pedido, documento(pedido))
    if len(row.documento) != 14 or (row.confirmado_em and not forcar):
        return row
    # Cadastro declarado pelo cliente: a fila resolve pela base pública
    # (`resolver_declarado`), no máximo a cada REAVALIAR_DECLARADO_MIN.
    if not forcar and row.origem == 'checkout_declarado':
        from datetime import timedelta
        if (row.consultado_em is None
                or row.consultado_em <= agora() - timedelta(minutes=REAVALIAR_DECLARADO_MIN)):
            resolver_declarado(row)
            db.session.flush()
        return row
    # Preserva o que o comprador conferiu e a consulta confirmou (e o que a
    # resolução automática decidiu). Só a consulta explícita do owner troca.
    if not forcar and row.origem in ('checkout_consulta', ORIGEM_AUTO):
        return row
    if not forcar and row.consultado_em:
        from datetime import timedelta
        if not row.erro or row.consultado_em > agora() - timedelta(minutes=15):
            return row
    if forcar:
        row.confirmado_em = None
        row.confirmado_por_id = None
    do_checkout = row.origem in ('checkout_consulta', 'checkout_declarado', ORIGEM_AUTO)
    contato_res = tiny.buscar_contato_por_documento(row.documento)
    contato = contato_res.get('contato') or {}
    consulta_checkout = None
    if do_checkout:
        from app.services import consulta_empresa
        consulta_checkout = consulta_empresa.consultar(row.documento)
        dados_checkout = consulta_checkout.get('dados') or {}
        publico = {**dados_checkout, 'razao_social': dados_checkout.get('nome'),
                   'logradouro': dados_checkout.get('endereco')}
    else:
        publico = cnpj.consultar(row.documento)
    dados = {}
    origem = []
    if contato:
        dados = {campo: str(contato.get(campo) or '').strip()
                 for campo in ('nome', 'codigo', 'ie', *ENDERECO)}
        origem.append('tiny')
    if not publico.get('erro') and publico.get('razao_social'):
        dados.update(_endereco_publico(publico))
        dados['nome'] = publico['razao_social']
        origem.append(consulta_checkout['origem'] if consulta_checkout else 'cnpj')
    ie_publica = bool(consulta_checkout and
                      dados_checkout.get('situacao_ie') == 'contribuinte' and
                      digitos(dados_checkout.get('ie')))
    if ie_publica:
        dados['ie'] = digitos(dados_checkout['ie'])
    row.dados = dados
    row.origem = '+'.join(origem) or 'pendente'
    ie = str(dados.get('ie') or '').strip()
    row.situacao_ie = ('isento' if ie.upper() == 'ISENTO' else
                       'contribuinte' if digitos(ie) else None)
    if ie and not ie_publica and contato.get('uf', '').upper() != dados.get('uf', '').upper():
        row.situacao_ie = None  # IE é estadual; não transporta inscrição entre UFs.
    row.consultado_em = agora()
    faltando = pendencias(row)
    row.erro = ('Confira os dados fiscais: ' + ', '.join(faltando) + '.'
                if faltando else None)
    if contato_res.get('erro') and not ie_publica:
        row.erro = (row.erro or '') + ' Consulta ao cadastro Tiny indisponível; tente consultar novamente.'
    db.session.flush()
    return row


def payload_cliente(pedido):
    """Retorna (cadastro, erro). CPF mantém o fluxo atual de endereço do pedido."""
    doc = documento(pedido)
    if len(doc) != 14:
        return None, None
    row = consultar(pedido)
    faltando = pendencias(row)
    if faltando or row.erro:
        return None, row.erro or ('Confira os dados fiscais: ' + ', '.join(faltando) + '.')
    cliente = {campo: row.dados[campo] for campo in ('nome', 'codigo', 'ie', *ENDERECO)
               if campo in row.dados}
    cliente.update(tipo_pessoa='J', cpf_cnpj=row.documento, atualizar_cliente='N',
                   email=pedido.email_cliente, fone=pedido.telefone_cliente or '')
    cliente['ie'] = ('ISENTO' if row.situacao_ie == 'isento' else
                     digitos(cliente.get('ie')) if row.situacao_ie == 'contribuinte' else '')
    return cliente, None


def salvar_conferencia(pedido, form, usuario_id):
    """Owner confirma antes da autorização; não altera nota existente no Tiny."""
    from app.models import Usuario
    usuario = db.session.get(Usuario, usuario_id)
    if not usuario or not usuario.is_owner:
        return 'Somente o dono pode confirmar o cadastro fiscal.'
    if pedido.nf_status == 'inclusao_iniciada' or pedido.nf_emitida_em:
        return 'A NF já foi autorizada ou sua inclusão está incerta. Confira a nota no Tiny.'
    if form.get('conferido') != '1':
        return 'Confirme que conferiu os dados fiscais da empresa.'
    doc = documento(pedido)
    if len(doc) != 14:
        return 'Este pedido não possui CNPJ.'
    dados = {campo: str(form.get(campo) or '').strip()
             for campo in ('nome', 'ie', *ENDERECO)}
    dados['cep'] = digitos(dados['cep'])
    dados['uf'] = dados['uf'].upper()
    estado_ie = form.get('situacao_ie')
    candidato = FiscalPedidoOnline(documento=doc, dados=dados, situacao_ie=estado_ie)
    faltando = pendencias(candidato)
    if faltando:
        return 'Confira: ' + ', '.join(faltando) + '.'
    limites = {'nome': 150, 'ie': 18, 'endereco': 200, 'numero': 20,
               'complemento': 100, 'bairro': 100, 'cidade': 100, 'uf': 2, 'cep': 8}
    if any(len(dados[k]) > tamanho for k, tamanho in limites.items()):
        return 'Um dos campos fiscais excede o tamanho permitido.'
    row = congelar_documento(pedido, doc)
    row.dados = dados
    row.situacao_ie = estado_ie
    row.origem = 'owner'
    row.confirmado_por_id = usuario_id
    row.confirmado_em = agora()
    row.erro = None
    return None


def conferir_rascunho(pedido):
    """Tiny pode carregar cadastro prévio. Confere o resultado antes da autorização."""
    import unicodedata

    from app.services import tiny
    if len(documento(pedido)) != 14:
        return None
    esperado, erro = payload_cliente(pedido)
    if erro:
        return erro
    nota = tiny.obter_nota_fiscal(pedido.tiny_nota_fiscal_id)
    recebido = (nota or {}).get('cliente')
    if not isinstance(recebido, dict):
        return 'Não foi possível conferir os dados fiscais do rascunho no Tiny. A autorização não foi solicitada.'

    def normalizar(campo, valor):
        if campo == 'cpf_cnpj':
            return normalizar_documento(valor)
        if campo == 'cep' or (campo == 'ie' and digitos(valor)):
            return digitos(valor)
        texto = unicodedata.normalize('NFKD', str(valor or '').upper())
        return ' '.join(''.join(c for c in texto if not unicodedata.combining(c)).split())

    divergencias = [campo for campo in ('cpf_cnpj', 'nome', 'ie', *ENDERECO)
                   if normalizar(campo, esperado.get(campo)) != normalizar(campo, recebido.get(campo))]
    if divergencias:
        return ('O rascunho no Tiny diverge do cadastro fiscal conferido (' + ', '.join(divergencias)
                + '). Corrija a nota existente no Tiny antes de autorizar; não foi criada outra nota.')
    return None


def contexto(pedido):
    row = registro(pedido)
    return {'documento': documento(pedido), 'dados': (row.dados or {}) if row else {},
            'situacao_ie': row.situacao_ie if row else '',
            'origem': row.origem if row else '', 'erro': row.erro if row else '',
            'editavel': not (pedido.nf_emitida_em
                             or pedido.nf_status == 'inclusao_iniciada')}
