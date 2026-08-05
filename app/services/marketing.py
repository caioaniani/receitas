"""Sincronização da base de clientes com o Listmonk (05/08/2026).

Pedido do dono: disparar propaganda e felicitação de aniversário para os
e-mails cadastrados — tanto de quem **comprou no site** quanto de quem
**usou o Wi-Fi das lojas**.

DECISÃO DO DONO (05/08/2026), registrada de propósito: o regime é
**OPT-OUT**. A base inteira entra na campanha e quem clicar em "cancelar
inscrição" para de receber. Eu levantei que o aceite dos termos do portal
Wi-Fi foi dado para *usar o Wi-Fi*, o que é base mais frágil que a de quem
comprou; ele reafirmou que quer usar as duas bases. Ficam como salvaguarda:
o link de descadastro em todo e-mail (o Postmark injeta e o Gmail mostra o
botão "Cancelar Assinatura") e o registro da data em
`Cliente.marketing_descadastro_em`.

Três listas, por ORIGEM — isso permite segmentar a campanha e mantém
rastreável de onde veio cada contato (útil justamente se um dia alguém
questionar a base):

  Clientes do site   — tem PedidoOnline PAGO
  Wi-Fi das lojas    — passou pelo portal Wi-Fi
  Sorteio 2026       — planilha do sorteio, com "Ok, concordo." explícito
                       (importada uma vez; não sincroniza)

Nada aqui pode derrubar o cron: toda falha é logada e devolvida no dict de
stats, nunca propagada.
"""
import json
import logging
import re

from sqlalchemy import func

from app.extensions import db
from app.utils import agora, hoje

logger = logging.getLogger(__name__)

LISTA_SITE = 'Clientes do site'
LISTA_WIFI = 'Wi-Fi das lojas'
LISTA_SORTEIO = 'Sorteio 2026'
# Lista TRANSIENTE: é reconstruída todo dia com quem faz aniversário hoje.
# Existe porque campanha do Listmonk mira LISTA, não consulta — não dá pra
# dizer "mande só pros aniversariantes" numa lista permanente.
LISTA_ANIVERSARIO = 'Aniversariantes de hoje'

CFG_ANIV_ATIVO = 'marketing_aniv_ativo'      # '1' = o cron dispara sozinho
CFG_ANIV_ASSUNTO = 'marketing_aniv_assunto'
CFG_ANIV_CORPO = 'marketing_aniv_corpo'
CFG_ANIV_ULTIMO = 'marketing_aniv_ultimo'    # 'AAAA-MM-DD' do último disparo

# `{{ .Subscriber.FirstName }}` é template do Listmonk (Go), não do Jinja.
ASSUNTO_PADRAO = 'Feliz aniversário, {{ .Subscriber.FirstName }}! 🎂'
CORPO_PADRAO = """<p>Oi, {{ .Subscriber.FirstName }}!</p>
<p>Hoje é o seu dia — e a gente queria ser um dos primeiros a desejar
<strong>feliz aniversário</strong>. 🎉</p>
<p>Passa numa das nossas lojas ou peça no site: tem pão fresco saindo do
forno pra deixar o seu dia ainda melhor.</p>
<p><a href="https://opao.online">Ver o que tem hoje →</a></p>
<p>Um abraço,<br>Equipe O Pão Padaria Artesanal</p>"""

# Teto de sanidade: mais gente que isso fazendo aniversário no MESMO dia
# significa que a consulta pegou o que não devia. Não envia e avisa.
TETO_ANIVERSARIANTES = 200


def _attribs(cli):
    """Atributos do contato no Listmonk. O aniversário vai aqui — é o que
    permite a campanha do dia buscar por consulta SQL."""
    a = {}
    if cli.aniversario_dia and cli.aniversario_mes:
        a['aniv_dia'] = int(cli.aniversario_dia)
        a['aniv_mes'] = int(cli.aniversario_mes)
    if cli.telefone:
        a['telefone'] = cli.telefone
    return json.dumps(a, ensure_ascii=False)


def _contato(cli):
    return {'email': (cli.email or '').strip().lower(),
            'nome': (cli.nome or '').strip(),
            'attribs_json': _attribs(cli)}


def _base_query():
    """Clientes elegíveis: ativos, com e-mail, que NÃO descadastraram."""
    from app.models import Cliente
    return Cliente.query.filter(
        Cliente.ativo.is_(True),
        Cliente.email.isnot(None),
        Cliente.email != '',
        Cliente.marketing_descadastro_em.is_(None))


def contatos_do_site():
    """Quem tem pedido PAGO no site. `pago_em` (não o status) é a prova de
    compra — mesma régua do faturamento; divulgação fica fora."""
    from app.models import Cliente, PedidoOnline
    emails_pagos = (db.session.query(func.lower(PedidoOnline.email_cliente))
                    .filter(PedidoOnline.pago_em.isnot(None),
                            PedidoOnline.divulgacao.is_(False))
                    .distinct().subquery())
    q = _base_query().filter(func.lower(Cliente.email).in_(
        db.session.query(emails_pagos.c[0])))
    return [_contato(c) for c in q.all()]


def contatos_do_wifi():
    """Quem se cadastrou pelo portal Wi-Fi das lojas.

    A fonte é `Cliente.origem == 'wifi'`. A sessão do portal entra como rede
    de segurança, mas NÃO pode ser a fonte principal: o portal no modo RADIUS
    (o que está vivo desde 13/07/2026) cria só o `Cliente` — quem deixa
    `WifiPortalSessao` é o fluxo antigo, de validação por WhatsApp. Derivar
    da sessão fazia a lista mostrar 1 pessoa em vez de dezenas (05/08/2026).
    """
    from app.models import Cliente, WifiPortalSessao
    emails_wifi = (db.session.query(func.lower(WifiPortalSessao.email))
                   .filter(WifiPortalSessao.email.isnot(None),
                           WifiPortalSessao.email != '')
                   .distinct().subquery())
    q = _base_query().filter(db.or_(
        Cliente.origem == 'wifi',
        func.lower(Cliente.email).in_(db.session.query(emails_wifi.c[0]))))
    return [_contato(c) for c in q.all()]


def _ids_permanentes():
    """[site, wifi, sorteio] — cria as listas se ainda não existirem."""
    from app.services import listmonk
    return [
        listmonk.garantir_lista(LISTA_SITE,
                                'Clientes com pedido pago no site'),
        listmonk.garantir_lista(LISTA_WIFI,
                                'Cadastros do portal Wi-Fi das lojas'),
        listmonk.garantir_lista(LISTA_SORTEIO,
                                'Planilha do sorteio (importada uma vez)'),
    ]


def _todas_listas():
    """As permanentes + a transiente de aniversário.

    O descadastro precisa ser colhido de TODAS: quem clica em "cancelar" no
    e-mail de aniversário cancela na lista transiente, que é apagada no dia
    seguinte — se não colhermos antes, o "não quero mais" some.
    """
    from app.services import listmonk
    return _ids_permanentes() + [
        listmonk.garantir_lista(
            LISTA_ANIVERSARIO,
            'Transiente: reconstruída todo dia pela campanha de aniversário'),
    ]


def sincronizar():
    """Empurra as duas bases vivas pro Listmonk e traz os descadastros de
    volta. Idempotente — pode rodar quantas vezes quiser.

    Ordem importa: PRIMEIRO puxa quem descadastrou e marca aqui, DEPOIS
    envia. Ao contrário, a mesma execução re-inscreveria quem acabou de
    cancelar.
    """
    from app.services import listmonk

    stats = {'site': 0, 'wifi': 0, 'descadastros': 0, 'erro': None}
    if not listmonk.disponivel():
        stats['erro'] = 'Listmonk não configurado (LISTMONK_URL/TOKEN)'
        return stats
    try:
        todas = _todas_listas()
        id_site, id_wifi = todas[0], todas[1]

        stats['descadastros'] = marcar_descadastros(todas)

        site = contatos_do_site()
        wifi = contatos_do_wifi()
        listmonk.importar([id_site], site)
        listmonk.importar([id_wifi], wifi)
        stats['site'] = len(site)
        stats['wifi'] = len(wifi)
        logger.info('marketing: sincronizado %s', stats)
    except Exception as exc:                                  # noqa: BLE001
        stats['erro'] = f'{type(exc).__name__}: {exc}'
        logger.exception('marketing: sincronização falhou')
        db.session.rollback()
    return stats


def marcar_descadastros(lista_ids):
    """Traz de volta quem clicou em "cancelar inscrição" e marca no banco.

    Sem isso a próxima sincronização re-inscreveria a pessoa — o caminho
    mais rápido pra reclamação de spam e queima do domínio.

    Faz DUAS coisas, e as duas importam:
      1. propaga o descadastro pra TODAS as listas no Listmonk (quem cancelou
         no e-mail de aniversário cancelou só na lista transiente);
      2. marca `Cliente.marketing_descadastro_em`, que tira a pessoa de toda
         sincronização futura daqui.
    """
    from app.models import Cliente
    from app.services import listmonk

    saiu = {}
    for lid in lista_ids:
        saiu.update(listmonk.descadastrados(lid))
    if not saiu:
        return 0
    # Re-aplicado a cada execução de propósito: é idempotente, e o conjunto
    # de quem saiu cresce devagar (uma chamada com algumas dezenas de ids).
    # A trava principal contra ressurreição continua sendo o
    # `marketing_descadastro_em` abaixo, que tira a pessoa da origem.
    listmonk.mudar_listas(sorted(saiu.values()), 'unsubscribe', lista_ids)
    n = (Cliente.query
         .filter(func.lower(Cliente.email).in_(set(saiu)),
                 Cliente.marketing_descadastro_em.is_(None))
         .update({'marketing_descadastro_em': agora()},
                 synchronize_session=False))
    db.session.commit()
    if n:
        logger.info('marketing: %d cliente(s) marcados como descadastrados', n)
    return n


def aniversariantes(dia=None):
    """Clientes que fazem aniversário no dia (default: hoje), elegíveis a
    receber. Base da campanha de felicitação."""
    from app.models import Cliente
    d = dia or hoje()
    q = _base_query().filter(Cliente.aniversario_dia == d.day,
                             Cliente.aniversario_mes == d.month)
    return [_contato(c) for c in q.all()]


# ── Import de planilha (sorteio, evento, lista de papel) ─────────────

_RE_EMAIL = re.compile(r'^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$')


def _linhas_da_planilha(stream, nome_arquivo):
    """Lê xlsx ou csv e devolve a lista de linhas (tuplas de células)."""
    if (nome_arquivo or '').lower().endswith('.csv'):
        import csv
        import io
        texto = stream.read()
        if isinstance(texto, bytes):
            texto = texto.decode('utf-8-sig', errors='replace')
        return [tuple(l) for l in csv.reader(io.StringIO(texto))]
    import openpyxl
    wb = openpyxl.load_workbook(stream, read_only=True, data_only=True)
    return list(wb.worksheets[0].iter_rows(values_only=True))


def _achar_colunas(cabecalho):
    """Descobre por NOME onde estão e-mail/nome/sobrenome/telefone.

    Por nome e não por posição: a planilha vem de formulário e a ordem das
    colunas muda entre um sorteio e outro.
    """
    idx = {}
    for i, c in enumerate(cabecalho or []):
        t = str(c or '').strip().lower()
        if 'mail' in t and 'email' not in idx:
            idx['email'] = i
        elif t.startswith('sobrenome') and 'sobrenome' not in idx:
            idx['sobrenome'] = i
        elif t.startswith('nome') and 'nome' not in idx:
            idx['nome'] = i
        elif ('telefone' in t or 'celular' in t or 'whats' in t) \
                and 'telefone' not in idx:
            idx['telefone'] = i
    return idx


def contatos_de_planilha(stream, nome_arquivo='planilha.xlsx', origem='sorteio'):
    """Extrai contatos de uma planilha. Devolve (contatos, stats).

    Descarta linha sem e-mail, e-mail malformado e repetido — o import não é
    lugar de "quase e-mail": endereço inválido vira bounce, e bounce em
    volume queima a reputação do domínio (o transacional de pedido sai do
    mesmo Postmark).
    """
    linhas = _linhas_da_planilha(stream, nome_arquivo)
    st = {'linhas': max(0, len(linhas) - 1), 'validos': 0,
          'invalidos': 0, 'repetidos': 0, 'sem_email': 0}
    if not linhas:
        return [], st
    idx = _achar_colunas(linhas[0])
    if 'email' not in idx:
        raise ValueError('A planilha não tem uma coluna de e-mail no '
                         'cabeçalho da primeira linha.')
    def cel(linha, chave):
        i = idx.get(chave)
        if i is None or i >= len(linha):
            return ''
        return str(linha[i] or '').strip()

    vistos = {}
    for linha in linhas[1:]:
        email = cel(linha, 'email').lower()
        if not email:
            st['sem_email'] += 1
            continue
        if not _RE_EMAIL.match(email):
            st['invalidos'] += 1
            continue
        if email in vistos:
            st['repetidos'] += 1
            continue
        nome = ' '.join(x for x in (cel(linha, 'nome'),
                                    cel(linha, 'sobrenome')) if x)
        attrs = {'origem': origem}
        tel = re.sub(r'\D', '', cel(linha, 'telefone'))
        if tel:
            attrs['telefone'] = tel
        vistos[email] = {'email': email, 'nome': nome or email.split('@')[0],
                         'attribs_json': json.dumps(attrs, ensure_ascii=False)}
    st['validos'] = len(vistos)
    return list(vistos.values()), st


def importar_planilha(stream, nome_arquivo='planilha.xlsx',
                      lista_nome=LISTA_SORTEIO):
    """Sobe a planilha pra uma lista do Listmonk. Devolve stats + `erro`."""
    from app.services import listmonk

    st = {'validos': 0, 'erro': None}
    if not listmonk.disponivel():
        st['erro'] = 'Listmonk não configurado (LISTMONK_URL/TOKEN)'
        return st
    try:
        contatos, st = contatos_de_planilha(stream, nome_arquivo)
        st['erro'] = None
        if not contatos:
            st['erro'] = 'Nenhum e-mail válido na planilha.'
            return st
        lid = listmonk.garantir_lista(lista_nome, 'Importada de planilha')
        listmonk.importar([lid], contatos)
        logger.info('marketing: %d contato(s) importados em %r',
                    len(contatos), lista_nome)
    except Exception as exc:                                  # noqa: BLE001
        st['erro'] = f'{type(exc).__name__}: {exc}'
        logger.exception('marketing: import de planilha falhou')
    return st


# ── Campanha de aniversário ──────────────────────────────────────────

def _txt_cfg(chave, padrao):
    from app.models import AppConfig
    v = AppConfig.get(chave)
    return v if (v or '').strip() else padrao


def envio_automatico_ligado():
    """O disparo automático nasce DESLIGADO de propósito: o primeiro e-mail
    de marketing pra base real é gesto do dono, na tela, não efeito colateral
    de um deploy."""
    from app.models import AppConfig
    return AppConfig.get(CFG_ANIV_ATIVO) == '1'


def _teto():
    import os
    try:
        return max(1, int(os.environ.get('MARKETING_ANIV_TETO',
                                         TETO_ANIVERSARIANTES)))
    except (TypeError, ValueError):
        return TETO_ANIVERSARIANTES


def campanha_aniversario(dia=None, enviar=None, forcar=False):
    """Monta (e opcionalmente dispara) a campanha de aniversário do dia.

    Passos, nesta ordem — a ordem é a proteção:
      1. colhe quem descadastrou (inclusive na lista transiente de ontem) e
         propaga, ANTES de reconstruir qualquer coisa;
      2. esvazia a lista transiente;
      3. enche com os aniversariantes do dia, consultando `attribs` no
         Postgres do Listmonk e limitando o universo às nossas listas;
      4. confere o tamanho (0 = não há campanha; acima do teto = a consulta
         está errada, não dispara);
      5. cria a campanha e, se autorizado, inicia.

    Best-effort: qualquer falha volta em `erro`, nunca sobe pro cron.
    """
    from app.models import AppConfig
    from app.services import listmonk

    d = dia or hoje()
    st = {'dia': d.isoformat(), 'n': 0, 'campanha_id': None,
          'enviada': False, 'pulou': None, 'erro': None}
    if enviar is None:
        enviar = envio_automatico_ligado()
    if not listmonk.disponivel():
        st['erro'] = 'Listmonk não configurado (LISTMONK_URL/TOKEN)'
        return st
    if enviar and not forcar and AppConfig.get(CFG_ANIV_ULTIMO) == d.isoformat():
        st['pulou'] = 'já enviada hoje'
        return st
    try:
        permanentes = _ids_permanentes()
        id_aniv = listmonk.garantir_lista(
            LISTA_ANIVERSARIO,
            'Transiente: reconstruída todo dia pela campanha de aniversário')

        marcar_descadastros(permanentes + [id_aniv])

        # `subscribers.id > 0` = todos; o alvo é só a lista transiente, então
        # o "remove" nunca alcança as listas de origem.
        listmonk.mudar_listas_por_query(
            'subscribers.id > 0', 'remove', [id_aniv])
        listmonk.mudar_listas_por_query(
            f"subscribers.attribs->>'aniv_dia' = '{d.day}' "
            f"AND subscribers.attribs->>'aniv_mes' = '{d.month}'",
            'add', [id_aniv], listas_origem=permanentes)

        st['n'] = listmonk.contar(id_aniv)
        if st['n'] == 0:
            st['pulou'] = 'ninguém faz aniversário hoje'
            return st
        if st['n'] > _teto():
            st['erro'] = (f'{st["n"]} aniversariantes num dia só passa do teto '
                          f'({_teto()}) — não enviei. Confira os cadastros.')
            logger.error('marketing: %s', st['erro'])
            return st

        st['campanha_id'] = listmonk.criar_campanha(
            f'Aniversário {d.strftime("%d/%m/%Y")}',
            _txt_cfg(CFG_ANIV_ASSUNTO, ASSUNTO_PADRAO),
            _txt_cfg(CFG_ANIV_CORPO, CORPO_PADRAO),
            [id_aniv], tags=['opao', 'aniversario'])
        if not enviar:
            st['pulou'] = 'envio automático desligado — campanha ficou em rascunho'
            return st
        # Marca o dia ANTES de disparar: se o "iniciar" falhar no meio, hoje
        # fica sem e-mail (o erro vai pro log e pra tela) em vez de arriscar
        # dois "parabéns" pra mesma pessoa na retentativa.
        AppConfig.set(CFG_ANIV_ULTIMO, d.isoformat())
        db.session.commit()
        listmonk.iniciar_campanha(st['campanha_id'])
        st['enviada'] = True
        logger.info('marketing: campanha de aniversário enviada (%d pessoas)',
                    st['n'])
    except Exception as exc:                                  # noqa: BLE001
        st['erro'] = f'{type(exc).__name__}: {exc}'
        logger.exception('marketing: campanha de aniversário falhou')
        db.session.rollback()
    return st


LISTA_TESTE = 'Testes internos'


def enviar_teste(assunto, corpo, email, nome_peca='Peça'):
    """Manda a peça pro e-mail informado SEM disparar campanha nenhuma.

    A campanha nasce em rascunho mirando a lista `Testes internos` (que só
    tem quem foi cadastrado aqui) — mesmo que alguém a inicie por engano no
    Listmonk, ela não alcança a base.
    """
    from app.services import listmonk

    st = {'enviado': False, 'erro': None, 'campanha_id': None}
    email = (email or '').strip().lower()
    if not _RE_EMAIL.match(email):
        st['erro'] = 'E-mail inválido.'
        return st
    if not (assunto or '').strip() or not (corpo or '').strip():
        st['erro'] = 'Assunto e mensagem são obrigatórios.'
        return st
    if not listmonk.disponivel():
        st['erro'] = 'Listmonk não configurado (LISTMONK_URL/TOKEN)'
        return st
    try:
        lid = listmonk.garantir_lista(
            LISTA_TESTE, 'Destinatários das mensagens de teste')
        # O Listmonk só testa pra quem já é assinante.
        listmonk.garantir_assinante(email, None, [lid])
        payload = listmonk.montar_campanha(
            f'[teste] {nome_peca} — {agora():%d/%m %H:%M}',
            assunto, corpo, [lid], content_type='html',
            tags=['opao', 'teste'])
        st['campanha_id'] = listmonk.criar_campanha(
            payload['name'], assunto, corpo, [lid],
            content_type='html', tags=['opao', 'teste'])
        listmonk.enviar_teste(st['campanha_id'], payload, [email])
        st['enviado'] = True
    except Exception as exc:                                  # noqa: BLE001
        st['erro'] = f'{type(exc).__name__}: {exc}'
        logger.exception('marketing: envio de teste falhou')
    return st


def criar_rascunho(assunto, corpo, lista_nome, nome_peca=None):
    """Cria a campanha no Listmonk em RASCUNHO, mirando uma lista de verdade.

    Fecha o vazio entre "escrevi a peça" e "ela existe no Listmonk": sem
    isso o dono teria que recortar e colar o HTML lá dentro. NÃO envia — o
    disparo continua sendo um gesto dele, na tela do Listmonk, depois de
    conferir.
    """
    from flask import current_app

    from app.services import listmonk

    st = {'campanha_id': None, 'url': None, 'erro': None, 'lista': lista_nome}
    if not (assunto or '').strip() or not (corpo or '').strip():
        st['erro'] = 'Assunto e mensagem são obrigatórios.'
        return st
    if not listmonk.disponivel():
        st['erro'] = 'Listmonk não configurado (LISTMONK_URL/TOKEN)'
        return st
    if lista_nome == LISTA_ANIVERSARIO:
        # A transiente é esvaziada e reconstruída todo dia — uma campanha
        # apontada pra ela sairia pra quem fizer aniversário, não pra base.
        st['erro'] = (f'"{LISTA_ANIVERSARIO}" é a lista automática do '
                      f'aniversário. Escolha uma das listas de origem.')
        return st
    try:
        atuais = listmonk.listas_detalhe()
        lid = (atuais.get(lista_nome) or {}).get('id')
        if not lid:
            st['erro'] = f'Lista "{lista_nome}" não existe no Listmonk.'
            return st
        st['campanha_id'] = listmonk.criar_campanha(
            nome_peca or assunto, assunto, corpo, [lid],
            content_type='html', tags=['opao'])
        base = (current_app.config.get('LISTMONK_URL') or '').rstrip('/')
        st['url'] = f'{base}/admin/campaigns/{st["campanha_id"]}'
    except Exception as exc:                                  # noqa: BLE001
        st['erro'] = f'{type(exc).__name__}: {exc}'
        logger.exception('marketing: criação de rascunho falhou')
    return st


def resumo():
    """Estado da integração pra tela do dono. Nunca levanta."""
    from app.models import AppConfig
    from app.services import listmonk

    r = {'disponivel': False, 'url': '', 'listas': [], 'erro': None,
         'auto': envio_automatico_ligado(),
         'ultimo_envio': AppConfig.get(CFG_ANIV_ULTIMO),
         'assunto': _txt_cfg(CFG_ANIV_ASSUNTO, ASSUNTO_PADRAO),
         'corpo': _txt_cfg(CFG_ANIV_CORPO, CORPO_PADRAO),
         'aniversariantes_hoje': len(aniversariantes()),
         'teto': _teto()}
    from flask import current_app
    r['url'] = (current_app.config.get('LISTMONK_URL') or '').rstrip('/')
    r['disponivel'] = listmonk.disponivel()
    if not r['disponivel']:
        r['erro'] = 'Configure LISTMONK_URL e LISTMONK_API_TOKEN no Railway.'
        return r
    try:
        atuais = listmonk.listas_detalhe()
        for nome in (LISTA_SITE, LISTA_WIFI, LISTA_SORTEIO, LISTA_ANIVERSARIO):
            l = atuais.get(nome) or {}
            r['listas'].append({
                'nome': nome, 'id': l.get('id'), 'n': l.get('n') or 0,
                'transiente': nome == LISTA_ANIVERSARIO})
    except Exception as exc:                                  # noqa: BLE001
        r['erro'] = f'{type(exc).__name__}: {exc}'
        logger.exception('marketing: resumo falhou')
    return r
