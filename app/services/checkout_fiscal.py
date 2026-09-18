"""Dados públicos apresentados no checkout, com origem assinada pelo servidor."""
from types import SimpleNamespace

from flask import current_app
from itsdangerous import BadData, URLSafeTimedSerializer

from app.services.fiscal_online import ENDERECO, LIMITES_TINY, digitos, pendencias
from app.utils import agora

CAMPOS = ('nome', *ENDERECO, 'ie')
UFS = set('AC AL AP AM BA CE DF ES GO MA MT MS MG PA PB PR PE PI RJ RN RS RO RR SC SP SE TO'.split())


def _assinador():
    return URLSafeTimedSerializer(current_app.config['SECRET_KEY'], salt='checkout-fiscal-v1')


def _normalizar(dados):
    res = {campo: str(dados.get(campo) or '').strip() for campo in CAMPOS}
    res['cep'] = digitos(res['cep'])
    res['uf'] = res['uf'].upper()
    res['ie'] = digitos(res['ie'])
    return res


def resposta_consulta(doc, consulta):
    """Lista explícita de campos públicos: nunca devolve cadastro interno do ERP."""
    dados = _normalizar(consulta.get('dados') or {})
    situacao = (consulta.get('dados') or {}).get('situacao_ie')
    # A consulta pública só atesta uma IE ativa; ausência não atesta isenção.
    situacao = 'contribuinte' if situacao == 'contribuinte' and dados['ie'] else 'desconhecida'
    origem = str(consulta.get('origem') or '')[:40]
    atualizado = str(consulta.get('atualizado_em') or '')[:40]
    token = _assinador().dumps(dict(cnpj=doc, dados=dados, situacao_ie=situacao,
                                   origem=origem, atualizado_em=atualizado))
    return dict(dados={**dados, 'situacao_ie': situacao}, token=token,
                origem=origem, atualizado_em=atualizado,
                aviso=str(consulta.get('aviso') or 'Confira os dados da empresa antes de continuar.'))


def validar(form, doc):
    """Sem rede ou commit: kits validam cada entrega na mesma transação."""
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
    faltando = pendencias(candidato)
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
    return dict(dados=dados, situacao_ie=situacao, verificado=verificado,
                origem=prova.get('origem', ''), atualizado_em=prova.get('atualizado_em', '')), erros


def salvar(pedido, snapshot):
    from app.services.fiscal_online import congelar_documento

    row = congelar_documento(pedido, pedido.cliente.cpf)
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
