"""Entrega candidata pela RUA citada — identificação INTERNA para a equipe.

Dono, 20/09/2026 (caso conv 2409, entregador da Lalamove sem número de
pedido): "o boy deveria confirmar pelo menos o nome da rua para poder
passar o número". Leitura segura adotada: a rua serve de CHAVE DE
IDENTIFICAÇÃO para a EQUIPE, nunca de credencial para o contato. O bot
segue fail-closed (só telefone verificado do canal ou CPF do comprador
abrem um pedido — `bot_tools._consultar_pedido_online`); rua e código
NÃO viram credencial: a rua é conhecida por exatamente os terceiros que
se quer barrar (entregador, portaria, vizinho), e o código circula na
via do motorista e no comprovante.

O que este módulo faz: no HANDOFF de uma conversa em que um TERCEIRO na
entrega é citado (`chatbot._TERCEIRO_ENTREGA` na fala do contato ou
`motivo_terceiro_na_entrega` no motivo), cruza as ruas ditas pelo contato
com os pedidos PAGOS de hoje/amanhã que saem para entrega e escreve o
resultado numa NOTA PRIVADA do Chatwoot (`chatwoot.enviar_nota_privada`),
junto com o motivo do handoff e o resumo das ferramentas. O contato nunca
recebe nada disto: `resultado['texto']` é a única fala pública.

Regras do match (achados da investigação de 21/09/2026):
- só `modo_entrega != 'retirada'` (na retirada o endereço estruturado é o
  do cliente para a NF, não um destino);
- só falas `role == 'user'` (o bot repete endereços de pedidos consultados
  e casaria o endereço do próprio cliente);
- tokens do logradouro do PEDIDO, sem tipo de via (rua/av/alameda...) e
  sem "de/da/do", TODOS presentes como palavras inteiras na fala do
  contato — nunca o contrário ("Rua Serra" não casa "Rua Serra da
  Bocaina"; "Rua Nova" não casa "Rua Nova York"); logradouro sem token
  de 3+ letras não casa sozinho;
- hoje antes de amanhã; corrida Lalamove em rua (ON_GOING/PICKED_UP) ou
  atribuição de motorista pendente desempatam; 2+ candidatas viram lista
  sem escolher; 0 = nada.
"""
import logging
import re

from app.utils import agora, hoje, normalizar_busca

logger = logging.getLogger(__name__)

PREFIXO_NOTA_BOT = '🤖 Bot:'
_STATUS_ENTREGA = ('pago', 'em_preparo', 'a_caminho')
_TIPOS_VIA = {'rua', 'r', 'av', 'avenida', 'al', 'alameda', 'travessa', 'tv',
              'praca', 'pc', 'estrada', 'est', 'rod', 'rodovia', 'largo',
              'viela', 'passagem', 'via', 'vl', 'jd', 'jardim'}
_STOP = {'de', 'da', 'do', 'das', 'dos', 'e', 'a', 'o'}
_MAX_CANDIDATAS = 3
_LALAMOVE_EM_RUA = {'ON_GOING', 'PICKED_UP'}


def _tokens_logradouro(logradouro):
    """['serra', 'bocaina'] de 'Rua Serra da Bocaina'. [] quando não há
    token de 3+ letras (logradouro curto demais para identificar)."""
    partes = re.split(r'[^a-z0-9]+', normalizar_busca(logradouro or ''))
    toks = [p for p in partes if p and p not in _TIPOS_VIA and p not in _STOP]
    if not any(len(t) >= 3 and t.isalpha() for t in toks):
        return []
    return toks


def _logradouro_do_pedido(p):
    if (p.endereco_logradouro or '').strip():
        return p.endereco_logradouro.strip()
    # Fallback: primeiro trecho da linha única (formato de
    # `loja_checkout._montar_endereco`: logradouro, numero, ...).
    return ((p.endereco_entrega or '').split(',')[0]).strip()


def _texto_do_contato(historico):
    from app.services.chatbot import texto_da_mensagem
    falas = [texto_da_mensagem(m) for m in (historico or [])
             if m.get('role') == 'user' and not m.get('herdada')]
    return ' ' + normalizar_busca(' '.join(f for f in falas if f)) + ' '


def _todos_presentes(tokens, texto_norm):
    return all(re.search(r'(?<![a-z0-9])' + re.escape(t) + r'(?![a-z0-9])',
                         texto_norm) for t in tokens)


def _sinal_em_rua(codigo, dia):
    """Sinal de 'entrega em curso': corrida Lalamove já chamada (status) ou
    atribuição a motorista próprio pendente. Só leitura; erro = ''."""
    from app.models import AtribuicaoEntrega, LalamoveEntrega
    try:
        corrida = (LalamoveEntrega.query
                   .filter(LalamoveEntrega.pedido_code == codigo,
                           LalamoveEntrega.order_id.isnot(None))
                   .order_by(LalamoveEntrega.criado_em.desc()).first())
        if corrida is not None:
            quem = f' ({corrida.motorista_nome})' if corrida.motorista_nome else ''
            return f'Lalamove {corrida.status}{quem}'
        atrib = (AtribuicaoEntrega.query
                 .filter(AtribuicaoEntrega.pedido_code == codigo,
                         AtribuicaoEntrega.data_entrega == dia).first())
        if atrib is not None:
            return f'motorista próprio, {atrib.status or "pendente"}'
    except Exception:  # noqa: BLE001
        logger.exception('entrega_candidata: sinal falhou codigo=%s', codigo)
    return ''


def candidatas_por_rua(historico):
    """Pedidos pagos de hoje/amanhã, para ENTREGA, cuja rua aparece inteira
    na fala do contato. Lista (até _MAX_CANDIDATAS) de dicts prontos para a
    nota interna; [] sem match ou sem rua identificável. Nunca levanta."""
    from datetime import timedelta

    from app.models import PedidoOnline
    texto = _texto_do_contato(historico)
    if len(texto.strip()) < 3:
        return []
    h = hoje()
    dias = (h, h + timedelta(days=1))
    try:
        pedidos = (PedidoOnline.query
                   .filter(PedidoOnline.data_entrega.in_(dias),
                           PedidoOnline.status.in_(_STATUS_ENTREGA),
                           PedidoOnline.modo_entrega != 'retirada')
                   .order_by(PedidoOnline.data_entrega.asc(),
                             PedidoOnline.criado_em.asc()).all())
    except Exception:  # noqa: BLE001
        logger.exception('entrega_candidata: consulta falhou')
        return []
    achadas = []
    for p in pedidos:
        toks = _tokens_logradouro(_logradouro_do_pedido(p))
        if not toks or not _todos_presentes(toks, texto):
            continue
        sinal = _sinal_em_rua(p.codigo, p.data_entrega)
        achadas.append({
            'codigo': p.codigo,
            'rua': _logradouro_do_pedido(p),
            'numero': (p.endereco_numero or '').strip(),
            'bairro': (p.endereco_bairro or '').strip(),
            'data': p.data_entrega,
            'hoje': p.data_entrega == h,
            'modo': p.modo_entrega,
            'destinatario': (p.nome_destinatario or '').strip(),
            'sinal': sinal,
            'em_rua': any(s in sinal for s in _LALAMOVE_EM_RUA),
        })
    achadas.sort(key=lambda c: (not c['hoje'], not c['em_rua']))
    return achadas[:_MAX_CANDIDATAS]


def _linha_candidata(c):
    dia = 'hoje' if c['hoje'] else 'amanhã'
    end = c['rua'] + (f', {c["numero"]}' if c['numero'] else '')
    if c['bairro']:
        end += f' — {c["bairro"]}'
    extras = [dia, c['modo']]
    if c['destinatario']:
        extras.append(f'p/ {c["destinatario"]}')
    if c['sinal']:
        extras.append(c['sinal'])
    return f'{c["codigo"]} — {end} ({", ".join(extras)})'


def terceiro_na_conversa(historico, motivo=None):
    """True se a fala do contato cita entregador/portaria/vizinho/Lalamove
    ou se o motivo do handoff descreve terceiro com problema em curso."""
    from app.services.chatbot import _TERCEIRO_ENTREGA, motivo_terceiro_na_entrega
    if motivo_terceiro_na_entrega(motivo or ''):
        return True
    return bool(_TERCEIRO_ENTREGA.search(_texto_do_contato(historico)))


def nota_de_handoff(resultado, historico):
    """Texto da nota privada do handoff (uso interno). Sempre traz o motivo
    e o resumo das ferramentas; a entrega candidata só entra quando há
    terceiro na conversa e a rua casa. '' quando não há o que anotar."""
    motivo = (resultado or {}).get('motivo') or ''
    linhas = [f'{PREFIXO_NOTA_BOT} transferido para a equipe às '
              f'{agora().strftime("%H:%M")} — motivo: {motivo or "(sem motivo)"}']
    resumo = [r for r in ((resultado or {}).get('tools_resumo') or []) if r]
    if resumo:
        linhas.append('Ferramentas: ' + ' | '.join(str(r)[:160] for r in resumo[:4]))
    if terceiro_na_conversa(historico, motivo):
        try:
            cands = candidatas_por_rua(historico)
        except Exception:  # noqa: BLE001
            logger.exception('entrega_candidata: match por rua falhou')
            cands = []
        if len(cands) == 1:
            linhas.append('Entrega candidata pela rua citada: ' + _linha_candidata(cands[0]))
        elif cands:
            linhas.append(f'{len(cands)} entregas na rua citada (conferir qual): '
                          + '; '.join(_linha_candidata(c) for c in cands))
        else:
            linhas.append('Terceiro na entrega sem rua reconhecida nos pedidos '
                          'de hoje/amanhã — conferir o painel de entregas.')
    linhas.append('(uso interno — nunca repassar ao contato)')
    return '\n'.join(linhas)


def anotar_handoff(conv_id, resultado, historico):
    """Posta a nota privada do handoff. Best-effort: nunca levanta, nunca
    fala com o contato."""
    try:
        texto = nota_de_handoff(resultado, historico)
        if not texto:
            return {'ok': False, 'pulou': 'vazio'}
        from app.services import chatwoot
        res = chatwoot.enviar_nota_privada(conv_id, texto)
        if not res.get('ok'):
            logger.warning('entrega_candidata: nota de handoff nao postada conv=%s (%s)',
                           conv_id, res.get('erro') or res)
        return res
    except Exception:  # noqa: BLE001
        logger.exception('entrega_candidata: anotar_handoff falhou conv=%s', conv_id)
        return {'ok': False, 'erro': 'excecao'}
