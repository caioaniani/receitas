"""Corte do fim do dia do pedido loja→indústria (10/08/2026, regra do dono).

"O pedido que as lojas fazem para receber no dia seguinte não pode ser
modificado após o corte" — é o horário de corte do PRÉ-PREPARO: no corte a
TV do padeiro calcula o que assar/adiantar pra amanhã (`preparar.json`),
e mudar o pedido de amanhã depois disso muda a lista com a produção já
adiantada.

HORÁRIO: `HORA_CORTE` (nasceu 18:00; **19:00 desde 13/08/2026**, pedido do
dono no 1º dia real — as lojas precisavam da hora extra pra revisar). Os
crons acompanham: refresh dos auto-pedidos 30min antes do corte e envio
automático da ordem NO corte (`seru_cron`) — mudar a hora aqui exige mudar
os dois jobs lá junto.

Regra: pedido com `data_entrega == amanhã` fica TRAVADO da HORA_CORTE (BRT)
em diante pra gerente/funcionário/produção/padeiro. ADMIN/OWNER passa, com
AVISO explícito (válvula de emergência — decisão do dono 10/08/2026, junto
com a automação de pedidos). O mesmo corte é o motivo de o cron de
auto-pedidos nunca tocar o D+1 depois do corte.

Defesa em profundidade (mesmo desenho da trava de MP não-pedível): web
novo/editar/cancelar + executores do copilot. LIMITAÇÃO CONHECIDA e aceita:
após a MEIA-NOITE o pedido (agora "de hoje") volta ao regime normal de
edição — o corte protege a janela HORA_CORTE–00:00, que é quando o
pré-preparo acontece; loja não opera de madrugada.
"""
from datetime import timedelta

from app.utils import agora

HORA_CORTE = 19


def corte_ativo(data_entrega, *, agora_dt=None):
    """True se ESTA data de entrega está sob o corte agora (amanhã +
    >= HORA_CORTE).

    `data_entrega` None nunca trava (pedido sem data não participa do
    pré-preparo por data). `agora_dt` injetável pra teste."""
    if data_entrega is None:
        return False
    now = agora_dt or agora()
    return (now.hour >= HORA_CORTE
            and data_entrega == now.date() + timedelta(days=1))


def bloqueio_do_corte(datas, user=None, *, agora_dt=None):
    """Verifica o corte pra um GESTO que toca as `datas` (iterável de
    date/None — na edição entram a data ATUAL e a NOVA: mover um pedido
    PRA amanhã ou TIRAR de amanhã depois do corte muda o pré-preparo
    igual).

    Retorna (bloqueado, aviso):
    - (True, msg)  -> recusar (gerente/funcionário/produção/padeiro);
    - (False, msg) -> admin/owner: prosseguir MOSTRANDO o aviso;
    - (False, None)-> fora do corte, nada a dizer.
    """
    if not any(corte_ativo(d, agora_dt=agora_dt) for d in datas):
        return False, None
    msg = ('O pedido de AMANHÃ está fechado desde as %d:00 — é o horário de '
           'corte do pré-preparo do padeiro.' % HORA_CORTE)
    if user is not None and getattr(user, 'is_admin', lambda: False)():
        return False, (msg + ' Você é admin e PODE prosseguir, mas o '
                       'pré-preparo já foi calculado — avise a produção.')
    return True, (msg + ' Mudanças só pra depois de amanhã, ou fale com um '
                  'admin.')
