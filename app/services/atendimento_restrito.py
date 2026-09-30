"""Atendimento público limitado a informações simples e encaminhamento.

Não usa modelo, catálogo, pedidos ou carrinho. Uma frase fora da lista de
perguntas reconhecidas fica com a equipe, inclusive quando vem antes de uma
saudação na mesma conversa. O histórico recebido continua com o chamador.
"""
import logging
import re
import unicodedata
from urllib.parse import urlsplit

from flask import current_app

from app.services.atendimento_humano import POLITICA_ATENDIMENTO as POLITICA

logger = logging.getLogger(__name__)

_SAUDACOES = {'oi', 'ola', 'bom dia', 'boa tarde', 'boa noite',
              'tudo bem', 'oi tudo bem', 'ola tudo bem',
              'bom dia tudo bem', 'boa tarde tudo bem', 'boa noite tudo bem'}
_AGRADECIMENTOS = {'obrigado', 'obrigada', 'muito obrigado', 'muito obrigada',
                    'valeu', 'tchau', 'ate mais'}
_LINK = re.compile(
    r'(?:qual (?:e )?(?:o )?(?:link|site|cardapio)(?: de voces|da padaria)?|'
    r'(?:me )?(?:manda|mande|envia|envie|passa|passe) '
    r'(?:o )?(?:link(?: do (?:site|cardapio))?|site|cardapio)|'
    r'(?:quero|gostaria de) (?:ver|acessar) (?:o )?(?:site|cardapio)|'
    r'(?:link do )?(?:site|cardapio))')
_ENDERECOS = {
    'qual o endereco', 'qual e o endereco', 'qual o endereco de voces',
    'qual e o endereco de voces', 'onde voces ficam', 'onde fica a padaria',
    'quais os enderecos das lojas', 'quais sao os enderecos das lojas',
    'enderecos das lojas', 'endereco das lojas', 'endereco da loja',
}
_ENDERECO_UNIDADE = re.compile(
    r'(?:qual (?:e )?o endereco d[ao]|endereco d[ao]|onde fica a) '
    r'(?:loja |unidade )?(?P<unidade>[a-z ]+)')
_HORARIOS = {
    'qual o horario', 'qual e o horario', 'qual o horario de funcionamento',
    'qual e o horario de funcionamento', 'horario de funcionamento',
    'qual o horario das lojas', 'que horas voces abrem', 'que horas voces fecham',
}
_PENDENCIA_ASSISTANT = re.compile(
    r'\b(?:pedido|entregue|pagamento|carrinho|checkout|cartinha|'
    r'complemento|transferi|encaminhei|atendente|aguardando|confirmacao)\b')


def _normalizar(texto):
    if not isinstance(texto, str):
        return ''
    t = ''.join(c for c in unicodedata.normalize('NFKD', texto.lower())
                if not unicodedata.combining(c))
    # Pontuação só nas pontas: uma pergunta adicional não desaparece.
    t = re.sub(r'\s+', ' ', t).strip().strip('.,!?;: ').strip()
    return t


def _classificar(texto):
    t = _normalizar(texto)
    if t in _SAUDACOES:
        return 'saudacao', None
    if t in _AGRADECIMENTOS:
        return 'agradecimento', None
    # Cortesia não muda o assunto; não removemos saudações ou frases livres.
    t = re.sub(r',? por favor$', '', t).strip()
    if _LINK.fullmatch(t):
        return 'link', None
    if t in _ENDERECOS:
        return 'endereco', None
    m = _ENDERECO_UNIDADE.fullmatch(t)
    if m:
        return 'endereco', m.group('unidade')
    if t in _HORARIOS:
        return 'horario', None
    return None, None


def _tem_anexo(mensagem):
    return any(mensagem.get(chave) for chave in (
        'imagens', 'images', 'image', 'anexos', 'attachments', 'audio',
        'audios', 'video', 'videos', 'arquivo', 'files'))


def _resultado(acao, texto, motivo):
    return {'acao': acao, 'texto': texto, 'motivo': motivo,
            'tools_usadas': [], 'politica_atendimento': POLITICA}


def _encaminhar(motivo):
    from app.services.chatbot import _texto_handoff_com_horario
    texto = ('Obrigada pelo contato. Vou passar sua mensagem para nossa equipe '
             'continuar o atendimento por aqui.')
    return _resultado('handoff', _texto_handoff_com_horario(texto), motivo)


# Mensagem que o WhatsApp não entregou (`type: unsupported` — o Chatwoot
# grava só um placeholder; ver `chatwoot.MARCADOR_MENSAGEM_INDISPONIVEL`).
# Aqui "vou passar sua mensagem" seria mentira: não há mensagem. O cliente
# recebe o motivo e o gesto que resolve (reenviar como texto comum); a
# equipe recebe a conversa com o marcador e o motivo na nota do handoff.
TEXTO_MENSAGEM_INDISPONIVEL = (
    'Sua mensagem chegou sem conteúdo aqui no nosso atendimento — isso '
    'costuma acontecer com mensagens temporárias, enquetes ou conteúdo de '
    'visualização única. Pode reenviar como mensagem de texto comum? Nossa '
    'equipe continua o atendimento por aqui.')
MOTIVO_MENSAGEM_INDISPONIVEL = (
    'mensagem indisponível: o WhatsApp não entregou o conteúdo (tipo não '
    'suportado pela API — mensagem temporária, enquete, visualização única '
    'ou similar). O cliente foi orientado a reenviar em texto; se o número '
    'for conhecido, ligar.')


def _mensagem_indisponivel(texto):
    """True quando TODA a fala do cliente (uma mensagem, ou a rajada que o
    webhook juntou com quebras de linha) é o marcador de conteúdo não
    entregue. Texto real misturado segue o fluxo normal."""
    from app.services.chatwoot import MARCADOR_MENSAGEM_INDISPONIVEL
    linhas = [ln.strip() for ln in (texto or '').splitlines() if ln.strip()]
    return bool(linhas) and all(ln == MARCADOR_MENSAGEM_INDISPONIVEL for ln in linhas)


def _encaminhar_indisponivel():
    from app.services.chatbot import _texto_handoff_com_horario
    return _resultado('handoff', _texto_handoff_com_horario(TEXTO_MENSAGEM_INDISPONIVEL),
                      MOTIVO_MENSAGEM_INDISPONIVEL)


def _enderecos(unidade=None):
    """Cadastro das lojas, sem assumir horário ou habilitação para retirada."""
    from app.models import Loja
    lojas = [loja for loja in Loja.query.filter(Loja.ativa.is_(True))
             .order_by(Loja.nome).all() if _normalizar(loja.nome) != 'industria']
    if unidade:
        # Nome/parte do nome cadastrados, sem escolher entre resultados ambíguos.
        lojas = [loja for loja in lojas
                 if unidade in (_normalizar(loja.nome),
                                _normalizar(loja.nome).removeprefix('loja '))]
        if len(lojas) != 1:
            return None
    if not lojas or any(not (loja.endereco or '').strip() for loja in lojas):
        return None
    return '\n'.join(f'{loja.nome}: {loja.endereco.strip()}' for loja in lojas)


def _link_site():
    base = (current_app.config.get('LOJA_BASE_URL') or '').strip().rstrip('/')
    try:
        url = urlsplit(base)
        if (url.scheme != 'https' or not url.hostname or url.username
                or url.password or url.query or url.fragment):
            return None
    except ValueError:
        return None
    return base


def responder(historico, *, telefone_contato=None, conversa_id=None):
    """FAQ com correspondência da mensagem inteira; demais casos são humanos.

    Não há chave, configuração ou argumento para habilitar o motor antigo.
    Telefone e conversa mantêm a assinatura dos canais; não autorizam consultas.
    """
    atuais = [m for m in (historico or [])
              if isinstance(m, dict) and not m.get('herdada')]
    clientes = [m for m in atuais if m.get('role') == 'user']
    if not clientes:
        return _encaminhar('atendimento restrito: mensagem sem contexto suficiente')
    # Conteúdo que o WhatsApp não entregou: o texto genérico ("vou passar
    # sua mensagem") não serve — vem ANTES da continuidade com a equipe,
    # porque o cliente precisa saber o que fazer mesmo numa conversa já
    # encaminhada (caso 2339: sete vezes o mesmo placeholder em 15 dias).
    if (_mensagem_indisponivel(clientes[-1].get('content'))
            or clientes[-1].get('indisponivel')):
        return _encaminhar_indisponivel()
    # Uma saudação/agradecimento não apaga venda, ajuste ou dúvida pendente.
    for m in atuais:
        if m.get('handoff_em'):
            return _encaminhar('atendimento restrito: continuidade com a equipe')
        if _tem_anexo(m) or not isinstance(m.get('content', ''), str):
            return _encaminhar('atendimento restrito: anexo ou mensagem não textual')
        if m.get('role') == 'user' and _classificar(m.get('content'))[0] is None:
            return _encaminhar('atendimento restrito: solicitação requer equipe')
        if m.get('role') == 'assistant' and (
                m.get('handoff') or m.get('humano') is True
                or _PENDENCIA_ASSISTANT.search(_normalizar(m.get('content')))):
            return _encaminhar('atendimento restrito: continuidade com a equipe')

    tipo, unidade = _classificar(clientes[-1].get('content'))
    if tipo == 'saudacao':
        return _resultado('responder', 'Olá! Você está falando com a O Pão. '
                          'Como podemos ajudar?', 'saudação simples')
    if tipo == 'agradecimento':
        return _resultado('responder', 'Obrigada pelo contato.', 'agradecimento simples')
    try:
        if tipo == 'link':
            link = _link_site()
            if link:
                return _resultado('responder', f'Nosso site: {link}',
                                  'link solicitado explicitamente')
        if tipo == 'endereco':
            enderecos = _enderecos(unidade)
            if enderecos:
                return _resultado('responder', enderecos, 'endereço cadastrado')
    except Exception:  # noqa: BLE001
        logger.exception('atendimento restrito: informação institucional indisponível')
    # O cadastro de Loja contém dias, mas não horário de abertura/fechamento.
    # O texto do prompt antigo não é uma fonte atualizada para esse dado.
    return _encaminhar('atendimento restrito: informação depende de confirmação da equipe')
