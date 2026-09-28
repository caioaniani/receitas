"""Dados públicos apresentados no checkout, com origem assinada pelo servidor."""
from types import SimpleNamespace

from flask import current_app
from itsdangerous import BadData, URLSafeTimedSerializer

from app.services.fiscal_online import (
    ENDERECO,
    LIMITES_TINY,
    cortar_aos_limites,
    digitos,
    pendencias,
)
from app.utils import agora, normalizar_documento

CAMPOS = ('nome', *ENDERECO, 'ie')
UFS = set('AC AL AP AM BA CE DF ES GO MA MT MS MG PA PB PR PE PI RJ RN RS RO RR SC SP SE TO'.split())
# Rótulos da TELA do checkout (app/templates/loja/_fiscal_cnpj.html), em
# minúsculas para caber na frase. Mudou o rótulo lá, muda aqui: há teste que
# confere os dois (tests/test_checkout_fiscal_auditoria.py).
ROTULOS_CHECKOUT = {'nome': 'razão social', 'endereco': 'rua ou avenida',
                    'numero': 'número', 'complemento': 'complemento',
                    'bairro': 'bairro', 'cidade': 'cidade', 'uf': 'estado (UF)',
                    'cep': 'CEP fiscal', 'ie': 'inscrição estadual (IE)'}


def _assinador():
    return URLSafeTimedSerializer(current_app.config['SECRET_KEY'], salt='checkout-fiscal-v1')


def _texto(valor):
    # Espaços internos colapsados dos dois lados (consulta e POST): o
    # <input type=text> descarta quebras de linha do valor, então um
    # complemento "SALA 1\nANDAR 2" da base pública nunca voltaria igual.
    return ' '.join(str(valor or '').split())


def _normalizar(dados):
    res = {campo: _texto(dados.get(campo)) for campo in CAMPOS}
    res['cep'] = digitos(res['cep'])
    res['uf'] = res['uf'].upper()
    res['ie'] = digitos(res['ie'])
    return res


def resposta_consulta(doc, consulta):
    """Lista explícita de campos públicos: nunca devolve cadastro interno do ERP.

    Cada campo sai já cortado ao limite do Tiny (auditoria 27/09/2026): a
    tela preenche por atribuição (o ``maxlength`` não corta), e um valor
    longo era recusado no POST — o cliente encurtava à mão, o dado deixava de
    bater com o assinado e a compra virava declaração. Assinado, preenchido e
    comparado são o MESMO valor; ``abreviados`` diz o que foi cortado para a
    tela pedir conferência.
    """
    doc = normalizar_documento(doc)
    dados, abreviados = cortar_aos_limites(_normalizar(consulta.get('dados') or {}))
    situacao = (consulta.get('dados') or {}).get('situacao_ie')
    # A consulta pública só atesta uma IE ativa; ausência não atesta isenção.
    situacao = 'contribuinte' if situacao == 'contribuinte' and dados['ie'] else 'desconhecida'
    origem = str(consulta.get('origem') or '')[:40]
    atualizado = str(consulta.get('atualizado_em') or '')[:40]
    token = _assinador().dumps(dict(cnpj=doc, dados=dados, situacao_ie=situacao,
                                   origem=origem, atualizado_em=atualizado))
    return dict(dados={**dados, 'situacao_ie': situacao}, token=token,
                origem=origem, atualizado_em=atualizado,
                aviso=str(consulta.get('aviso') or 'Confira os dados da empresa antes de continuar.'),
                abreviados=[{'campo': campo, 'rotulo': ROTULOS_CHECKOUT[campo]}
                            for campo in abreviados])


def validar(form, doc):
    """Sem rede ou commit: kits validam cada entrega na mesma transação."""
    doc = normalizar_documento(doc)
    if len(doc) != 14 or form.get('fiscal_checkout') != '1':
        return None, []  # Chamadores legados continuam sujeitos à conferência na emissão.
    dados = _normalizar({k: form.get('fiscal_' + k) for k in CAMPOS})
    situacao = str(form.get('fiscal_situacao_ie') or 'desconhecida')
    erros = []
    if situacao not in ('contribuinte', 'isento', 'nao_contribuinte', 'desconhecida'):
        erros.append('Confira a condição de inscrição estadual da empresa.')
        situacao = 'desconhecida'
    # Desconhecida não impede a compra, mas bloqueia autorização da NF até conferência.
    candidato = SimpleNamespace(dados=dados, situacao_ie=(
        'nao_contribuinte' if situacao == 'desconhecida' else situacao))
    faltando = pendencias(candidato, ROTULOS_CHECKOUT)
    if dados['uf'] not in UFS:
        faltando.append('UF válida')
    if faltando:
        erros.append('Confira os dados fiscais da empresa: ' + ', '.join(dict.fromkeys(faltando)) + '.')
    if form.get('fiscal_confirmado') != '1':
        erros.append('Confirme os dados fiscais da empresa para continuar.')
    # Antes de normalizar, não aceita IE alfanumérica convertida silenciosamente.
    ie_original = str(form.get('fiscal_ie') or '').strip()
    if situacao == 'contribuinte' and any(c not in '0123456789.-/ ' for c in ie_original):
        erros.append('A inscrição estadual deve conter apenas números e pontuação.')
    if len(ie_original) > LIMITES_TINY['ie']:
        erros.append('A inscrição estadual excede o tamanho permitido.')
    prova = {}
    token = str(form.get('fiscal_token') or '')
    if token and len(token) <= 16000:
        try:
            valor = _assinador().loads(token, max_age=7200)
            if isinstance(valor, dict) and valor.get('cnpj') == doc:
                prova = valor
        except BadData:
            pass  # Token ausente/expirado/adulterado vira declaração, nunca comprovação.
    verificado = (prova.get('dados') == dados and
                  prova.get('situacao_ie') == situacao == 'contribuinte')
    return dict(documento=doc, dados=dados, situacao_ie=situacao, verificado=verificado,
                origem=prova.get('origem', ''), atualizado_em=prova.get('atualizado_em', '')), erros


def salvar(pedido, snapshot):
    from app.services.fiscal_online import congelar_documento

    # O documento DESTA compra, não o do cadastro: e-mail não prova
    # identidade, e o Cliente pode ser de outra pessoa (checkout anônimo).
    row = congelar_documento(pedido, snapshot.get('documento') or pedido.cliente.cpf)
    row.dados = {**snapshot['dados'], '_checkout': {
        'conferido_em': agora().isoformat(), 'origem_consulta': snapshot['origem'],
        'base_atualizada_em': snapshot['atualizado_em'],
    }}
    row.situacao_ie = snapshot['situacao_ie']
    row.origem = 'checkout_consulta' if snapshot['verificado'] else 'checkout_declarado'
    row.consultado_em = agora() if snapshot['verificado'] else None
    # A conferência do cliente não se passa pela conferência do owner.
    row.erro = (None if snapshot['verificado'] else
                'Dados informados pelo cliente. O dono precisa conferir o cadastro fiscal '
                'e a inscrição estadual antes de autorizar a nota.')
    return row
