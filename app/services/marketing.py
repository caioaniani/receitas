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

from sqlalchemy import func

from app.extensions import db
from app.utils import agora, hoje

logger = logging.getLogger(__name__)

LISTA_SITE = 'Clientes do site'
LISTA_WIFI = 'Wi-Fi das lojas'
LISTA_SORTEIO = 'Sorteio 2026'


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
    """Quem passou pelo portal Wi-Fi das lojas."""
    from app.models import Cliente, WifiPortalSessao
    emails_wifi = (db.session.query(func.lower(WifiPortalSessao.email))
                   .filter(WifiPortalSessao.email.isnot(None),
                           WifiPortalSessao.email != '')
                   .distinct().subquery())
    q = _base_query().filter(func.lower(Cliente.email).in_(
        db.session.query(emails_wifi.c[0])))
    return [_contato(c) for c in q.all()]


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
        id_site = listmonk.garantir_lista(
            LISTA_SITE, 'Clientes com pedido pago no site')
        id_wifi = listmonk.garantir_lista(
            LISTA_WIFI, 'Cadastros do portal Wi-Fi das lojas')

        stats['descadastros'] = marcar_descadastros([id_site, id_wifi])

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
    """
    from app.models import Cliente
    from app.services import listmonk

    saiu = set()
    for lid in lista_ids:
        saiu |= listmonk.descadastrados(lid)
    if not saiu:
        return 0
    n = (Cliente.query
         .filter(func.lower(Cliente.email).in_(saiu),
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
