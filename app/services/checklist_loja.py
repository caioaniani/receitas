"""Checklist de loja (03/08/2026) — abertura, troca de turno e fechamento.

Pedido do dono: o gerente/atendente chefe responsável do turno preenche o
checklist no celular e tira foto comprovando os pontos marcados como "exige
foto". Itens cadastráveis pelo admin (/checklist/config); registro em
ChecklistPreenchimento/ChecklistResposta com SNAPSHOT do texto do item.

Regras de validação (todas fail-close — o registro só nasce completo):
- TODO item ativo do tipo precisa de resposta (OK ou problema).
- Item "exige foto" sem foto = recusa (é o ponto da feature: prova).
  Vale também quando a resposta é "problema" — a foto prova o estado.
- "Problema" sem observação = recusa (problema sem explicação é inútil).
- Upload de foto falhou (Dropbox fora, imagem ilegível) = recusa com
  mensagem clara e NADA gravado — checklist sem a prova prometida seria
  o registro mentindo em silêncio.

Cobrança (pendência na home, decisão do dono 03/08/2026): abertura de hoje
ausente (só depois de HORA_COBRA_ABERTURA — cobrar às 6h da manhã seria
ruído) e fechamento de ONTEM ausente. Respeita `Loja.funciona_em` (Cantina
só sáb/dom) e só cobra depois que o dono cadastrar itens do tipo — feature
sem configuração não cobra ninguém. Troca de turno NUNCA é cobrada (nem
toda loja tem turnos; o registro existe pra quem usa).
"""
import logging
from datetime import time as _time
from datetime import timedelta

from app.constants import CHECKLIST_TIPO_LABEL, CHECKLIST_TIPOS
from app.extensions import db
from app.models import (
    ChecklistItemModelo,
    ChecklistPreenchimento,
    ChecklistResposta,
    Loja,
)
from app.utils import agora, hoje

logger = logging.getLogger(__name__)

# Antes disso a abertura ausente não vira pendência (loja abrindo às 7-8h;
# gritar de madrugada seria alarme falso todo dia).
HORA_COBRA_ABERTURA = _time(10, 0)

# Fechamento preenchido de MADRUGADA (antes desta hora) conta pro dia
# ANTERIOR: o turno de segunda fechado à 00:15 de terça é o fechamento de
# SEGUNDA — gravar `data`=terça geraria falso "devendo" de segunda na home
# e ainda calaria a cobrança de terça (mesma classe do problema da tela do
# padeiro pós-meia-noite, achado da revisão 03/08/2026).
HORA_VIRADA_FECHAMENTO = _time(4, 0)


def itens_para(loja_id, tipo):
    """Itens ativos do tipo que valem pra loja: globais (loja_id NULL) +
    específicos dela, na ordem cadastrada."""
    return (ChecklistItemModelo.query
            .filter(ChecklistItemModelo.tipo == tipo,
                    ChecklistItemModelo.ativo.is_(True),
                    db.or_(ChecklistItemModelo.loja_id.is_(None),
                           ChecklistItemModelo.loja_id == loja_id))
            .order_by(ChecklistItemModelo.ordem, ChecklistItemModelo.id)
            .all())


def tipos_configurados(loja_id):
    """{tipo: n_itens} só dos tipos com pelo menos 1 item aplicável à loja."""
    from sqlalchemy import func
    rows = (db.session.query(ChecklistItemModelo.tipo,
                             func.count(ChecklistItemModelo.id))
            .filter(ChecklistItemModelo.ativo.is_(True),
                    db.or_(ChecklistItemModelo.loja_id.is_(None),
                           ChecklistItemModelo.loja_id == loja_id))
            .group_by(ChecklistItemModelo.tipo).all())
    return {t: n for t, n in rows if t in CHECKLIST_TIPOS and n}


def registrar(loja, tipo, usuario_id, respostas, observacao=None):
    """Grava um checklist completo. `respostas` = {item_id: {'ok': bool,
    'observacao': str|None, 'foto': bytes|None}}.

    Levanta ValueError com mensagem legível em QUALQUER problema — e nesse
    caso NADA é gravado (os uploads acontecem antes do primeiro add).
    """
    if tipo not in CHECKLIST_TIPOS:
        raise ValueError('Tipo de checklist inválido.')
    itens = itens_para(loja.id, tipo)
    if not itens:
        raise ValueError(
            'Nenhum item cadastrado pra esse checklist — peça ao admin '
            'pra configurar em Checklist → Configurar itens.')

    sem_resposta = [it.texto for it in itens if it.id not in respostas
                    or respostas[it.id].get('ok') is None]
    if sem_resposta:
        raise ValueError('Responda todos os pontos. Faltou: '
                         + '; '.join(sem_resposta[:5])
                         + (' …' if len(sem_resposta) > 5 else ''))
    sem_foto = [it.texto for it in itens
                if it.exige_foto and not respostas[it.id].get('foto')]
    if sem_foto:
        raise ValueError('Estes pontos exigem FOTO comprovando: '
                         + '; '.join(sem_foto[:5])
                         + (' …' if len(sem_foto) > 5 else ''))
    sem_obs = [it.texto for it in itens
               if respostas[it.id].get('ok') is False
               and not (respostas[it.id].get('observacao') or '').strip()]
    if sem_obs:
        raise ValueError('Ponto marcado com problema precisa da observação '
                         '(o que está errado?): ' + '; '.join(sem_obs[:5]))

    # Uploads ANTES de qualquer INSERT: falha de foto = nada gravado.
    fotos_up = _subir_fotos(loja, itens, respostas)

    p = ChecklistPreenchimento(
        loja_id=loja.id, tipo=tipo, data=_data_do_registro(tipo),
        usuario_id=usuario_id,
        observacao=(observacao or '').strip()[:500] or None)
    db.session.add(p)
    db.session.flush()
    for it in itens:
        r = respostas[it.id]
        info = fotos_up.get(it.id) or {}
        db.session.add(ChecklistResposta(
            preenchimento_id=p.id, item_id=it.id, item_texto=it.texto,
            exigia_foto=it.exige_foto, ok=bool(r.get('ok')),
            observacao=(r.get('observacao') or '').strip()[:500] or None,
            foto_url=info.get('url'),
            foto_storage_path=info.get('storage_path')))
    db.session.commit()
    return p


def _data_do_registro(tipo):
    """Fechamento preenchido de madrugada (< HORA_VIRADA_FECHAMENTO) pertence
    ao dia ANTERIOR; o resto é do dia corrente."""
    if tipo == 'fechamento' and agora().time() < HORA_VIRADA_FECHAMENTO:
        return hoje() - timedelta(days=1)
    return hoje()


def _subir_fotos(loja, itens, respostas):
    """Comprime e sobe pro Dropbox as fotos enviadas (exigidas ou extras).

    Fail-close DELIBERADO: se o Dropbox está fora ou a imagem é ilegível,
    o checklist NÃO fecha — a foto é a prova pedida pelo dono; aceitar sem
    ela seria o registro mentindo em silêncio.
    """
    from app.services import dropbox_storage
    from app.utils import comprimir_imagem

    out = {}
    com_foto = [it for it in itens if respostas[it.id].get('foto')]
    if not com_foto:
        return out
    if not dropbox_storage.disponivel():
        raise ValueError('Não consegui guardar as fotos agora (armazenamento '
                         'indisponível). Tente de novo em instantes.')
    for it in com_foto:
        raw = respostas[it.id]['foto']
        try:
            comprimida = comprimir_imagem(raw)
        except ValueError as exc:
            raise ValueError(f'A foto de "{it.texto}" não pôde ser lida '
                             '(formato não suportado?). Tire de novo.') from exc
        path = (f'/checklists/{loja.id}/{hoje().isoformat()}/'
                f'{it.id}_{int(agora().timestamp() * 1000)}.jpg')
        try:
            out[it.id] = dropbox_storage.upload_publico(comprimida, path)
        except Exception as exc:                           # noqa: BLE001
            # Broad DE PROPÓSITO (achado da revisão 03/08/2026): além do
            # RuntimeError do serviço, o retry de rede re-levanta
            # requests.ConnectionError/Timeout e o r.json() pode levantar
            # JSONDecodeError — qualquer uma escapando viraria 500 genérico
            # e o funcionário perderia as marcações. O fail-close se mantém
            # (nada gravado); o erro real fica no log.
            logger.warning('checklist: upload de foto falhou (%s): %s',
                           type(exc).__name__, exc)
            raise ValueError('Falha ao subir a foto de '
                             f'"{it.texto}". Tente de novo.') from exc
    return out


# ── Cobrança (pendência na home do dono) ─────────────────────────────

def lojas_operacionais():
    """Mesma régua do desperdicio_alerta: ativa e != 'Industria'."""
    return Loja.query.filter(Loja.ativa.is_(True),
                             Loja.nome != 'Industria').all()


def lojas_faltando(tipo, dia):
    """Lojas que FUNCIONAM no dia, TÊM item do tipo cadastrado e NÃO
    preencheram. Devolve a lista de nomes (a pendência mostra quem).

    Item criado DEPOIS do dia cobrado não conta pra ele: cadastrar o
    primeiro item de fechamento hoje de manhã não pode acusar todas as
    lojas de "fechamento de ontem ausente" retroativo (achado da revisão
    03/08/2026). `criado_em` compara com o FIM do dia cobrado.
    """
    from datetime import datetime as _dt
    from datetime import time as _t

    lojas = [lj for lj in lojas_operacionais() if lj.funciona_em(dia)]
    if not lojas:
        return []
    ids = [lj.id for lj in lojas]
    fim_do_dia = _dt.combine(dia + timedelta(days=1), _t.min)
    base = (ChecklistItemModelo.tipo == tipo,
            ChecklistItemModelo.ativo.is_(True),
            db.or_(ChecklistItemModelo.criado_em.is_(None),
                   ChecklistItemModelo.criado_em < fim_do_dia))
    # Item global ativo do tipo cobre todas; específico cobre a dele.
    tem_global = (db.session.query(ChecklistItemModelo.id)
                  .filter(*base, ChecklistItemModelo.loja_id.is_(None))
                  .first() is not None)
    com_item = set(ids) if tem_global else {
        lid for (lid,) in db.session.query(ChecklistItemModelo.loja_id)
        .filter(*base,
                ChecklistItemModelo.loja_id.in_(ids)).distinct().all()}
    if not com_item:
        return []
    preenchidas = {lid for (lid,) in db.session.query(
        ChecklistPreenchimento.loja_id)
        .filter(ChecklistPreenchimento.tipo == tipo,
                ChecklistPreenchimento.data == dia,
                ChecklistPreenchimento.loja_id.in_(ids)).distinct().all()}
    return [lj.nome for lj in lojas
            if lj.id in com_item and lj.id not in preenchidas]


def pendencias_checklist():
    """Itens pro "Precisa de você hoje" (forma do briefing_dono.pendencias).

    Abertura: cobrada HOJE, só depois de HORA_COBRA_ABERTURA.
    Fechamento: cobrado o de ONTEM (o turno fecha à noite; cobrar hoje de
    manhã é o primeiro momento útil). Troca de turno nunca é cobrada.
    """
    # Curto-circuito: sem NENHUM item ativo cadastrado (o estado até o dono
    # configurar), a home não paga as queries de loja/preenchimento — um
    # EXISTS e pronto (achado da revisão 03/08/2026).
    if (db.session.query(ChecklistItemModelo.id)
            .filter(ChecklistItemModelo.ativo.is_(True)).first() is None):
        return []
    out = []
    hj = hoje()
    if agora().time() >= HORA_COBRA_ABERTURA:
        faltam = lojas_faltando('abertura', hj)
        if faltam:
            out.append({
                'chave': 'checklist_abertura',
                'rotulo': ('Checklist de %s de hoje não preenchido: %s'
                           % (CHECKLIST_TIPO_LABEL['abertura'].lower(),
                              ', '.join(sorted(faltam)))),
                'qtd': len(faltam), 'url': '/checklist/conferencia'})
    faltam = lojas_faltando('fechamento', hj - timedelta(days=1))
    if faltam:
        out.append({
            'chave': 'checklist_fechamento',
            'rotulo': ('Checklist de %s de ontem não preenchido: %s'
                       % (CHECKLIST_TIPO_LABEL['fechamento'].lower(),
                          ', '.join(sorted(faltam)))),
            'qtd': len(faltam), 'url': '/checklist/conferencia'})
    return out
