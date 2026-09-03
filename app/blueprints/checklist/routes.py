"""Checklist de loja (03/08/2026): preencher (turno), configurar (admin) e
conferir (admin). Regras de negócio em app/services/checklist_loja.py."""
from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.blueprints.checklist import checklist_bp
from app.constants import CHECKLIST_TIPO_LABEL, CHECKLIST_TIPOS
from app.decorators import admin_required, checklist_required
from app.extensions import db
from app.models import (
    ChecklistItemModelo,
    ChecklistPreenchimento,
    ChecklistResposta,
    Loja,
)
from app.services import checklist_loja, checklist_responsaveis
from app.utils import hoje


def _lojas_escolhiveis():
    return sorted(checklist_loja.lojas_operacionais(),
                  key=lambda lj: (lj.nome or '').casefold())


def _item_do_form():
    """Resolve o item_id do POST sem estourar 500 em valor forjado."""
    try:
        iid = int(request.form.get('item_id') or 0)
    except (TypeError, ValueError):
        return None
    return db.session.get(ChecklistItemModelo, iid) if iid else None


def _resolver_loja(valor):
    try:
        lid = int(valor or 0)
    except (TypeError, ValueError):
        return None
    lj = db.session.get(Loja, lid) if lid else None
    if lj is None or not lj.ativa or lj.nome == 'Industria':
        return None
    return lj


@checklist_bp.route('/')
@login_required
@checklist_required
def index():
    """Hub: escolhe a loja (default = a do usuário) e o tipo de checklist.
    Mostra o que já foi preenchido hoje pra ninguém preencher em dobro sem
    querer (preencher de novo é permitido — vira outro registro)."""
    lojas = _lojas_escolhiveis()
    loja = (_resolver_loja(request.args.get('loja'))
            or _resolver_loja(checklist_responsaveis.loja_do_usuario(current_user))
            or _resolver_loja(current_user.loja_id)
            or (lojas[0] if lojas else None))
    tipos = checklist_loja.tipos_configurados(loja.id) if loja else {}
    feitos_hoje = {}
    if loja:
        # Janela hoje+ontem e filtro pelo "dia do turno" de cada tipo: o
        # fechamento de madrugada grava data=ontem e ainda precisa aparecer
        # como "✓ hoje" (revisão rodada 2).
        from datetime import timedelta
        for p in (ChecklistPreenchimento.query
                  .filter(ChecklistPreenchimento.loja_id == loja.id,
                          ChecklistPreenchimento.data
                          >= hoje() - timedelta(days=1))
                  .order_by(ChecklistPreenchimento.criado_em).all()):
            if p.data == checklist_loja._data_do_registro(p.tipo):
                feitos_hoje.setdefault(p.tipo, []).append(p)
    return render_template(
        'checklist/index.html', lojas=lojas, loja=loja,
        tipos=CHECKLIST_TIPOS, labels=CHECKLIST_TIPO_LABEL,
        configurados=tipos, feitos_hoje=feitos_hoje,
        equipe=checklist_responsaveis.quadro(loja.id) if loja else None)


@checklist_bp.route('/responsaveis')
@login_required
@admin_required
def responsaveis():
    return render_template('checklist/responsaveis.html',
                           equipe=checklist_responsaveis.quadro())


@checklist_bp.route('/preencher', methods=['GET', 'POST'])
@login_required
@checklist_required
def preencher():
    loja = _resolver_loja(request.values.get('loja'))
    tipo = (request.values.get('tipo') or '').strip()
    if loja is None or tipo not in CHECKLIST_TIPOS:
        flash('Escolha a loja e o tipo de checklist.', 'warning')
        return redirect(url_for('checklist.index'))
    itens = checklist_loja.itens_para(loja.id, tipo)
    if not itens:
        flash('Esse checklist ainda não tem itens cadastrados — peça ao '
              'admin pra configurar.', 'warning')
        return redirect(url_for('checklist.index', loja=loja.id))

    if request.method == 'POST':
        # Anti-duplo-submit (achado da revisão 03/08/2026): upload de foto em
        # 3G dá janela real pro segundo clique. Mesmo (loja, tipo, usuário)
        # criado há < 30s = duplicata acidental, não re-preenchimento
        # intencional (esse continua permitido depois da janela).
        from datetime import timedelta

        from app.utils import agora
        recente = (ChecklistPreenchimento.query
                   .filter(ChecklistPreenchimento.loja_id == loja.id,
                           ChecklistPreenchimento.tipo == tipo,
                           ChecklistPreenchimento.usuario_id == current_user.id,
                           ChecklistPreenchimento.criado_em
                           >= agora() - timedelta(seconds=30))
                   .first())
        if recente is not None:
            flash('Esse checklist acabou de ser registrado — não gravei em '
                  'dobro. (Pra preencher de novo de propósito, aguarde '
                  'meio minuto.)', 'info')
            return redirect(url_for('checklist.index', loja=loja.id))
        respostas = {}
        for it in itens:
            estado = request.form.get(f'ok_{it.id}')
            foto = request.files.get(f'foto_{it.id}')
            raw = foto.read() if foto and foto.filename else None
            respostas[it.id] = {
                'ok': {'ok': True, 'problema': False}.get(estado),
                'observacao': request.form.get(f'obs_{it.id}'),
                'foto': raw or None,
            }
        try:
            p = checklist_loja.registrar(
                loja, tipo, current_user.id, respostas,
                observacao=request.form.get('observacao'))
        except ValueError as exc:
            # Re-render mantém marcações e observações; fotos o navegador
            # SEMPRE descarta em file input — avisar poupa o susto.
            flash(f'{exc} (As fotos precisam ser anexadas de novo.)',
                  'danger')
            return render_template(
                'checklist/preencher.html', loja=loja, tipo=tipo,
                label=CHECKLIST_TIPO_LABEL[tipo], itens=itens,
                grupos=checklist_loja.agrupar_por_setor(itens),
                anteriores=_do_dia(loja, tipo),
                form=request.form,
                equipe=checklist_responsaveis.quadro(loja.id)), 422
        flash(f'Checklist de {CHECKLIST_TIPO_LABEL[tipo].lower()} da '
              f'{loja.nome} registrado ({len(p.respostas)} pontos'
              + (f', {p.n_problemas} com problema' if p.n_problemas else '')
              + ').', 'success')
        return redirect(url_for('checklist.index', loja=loja.id))

    return render_template(
        'checklist/preencher.html', loja=loja, tipo=tipo,
        label=CHECKLIST_TIPO_LABEL[tipo], itens=itens,
        grupos=checklist_loja.agrupar_por_setor(itens),
        anteriores=_do_dia(loja, tipo), form=None,
        equipe=checklist_responsaveis.quadro(loja.id))


def _do_dia(loja, tipo):
    """Preenchimentos do "dia do turno" corrente — pra fechamento de
    madrugada, é ONTEM (`_data_do_registro`): sem isso o registro de 00:15
    sumia do aviso "já preenchido" e o mesmo fechamento entrava em dobro
    sem alerta (regressão apontada na revisão, rodada 2)."""
    return (ChecklistPreenchimento.query
            .filter_by(loja_id=loja.id, tipo=tipo,
                       data=checklist_loja._data_do_registro(tipo))
            .order_by(ChecklistPreenchimento.criado_em).all())


# ── Configuração dos itens (admin) ───────────────────────────────────

@checklist_bp.route('/config', methods=['GET', 'POST'])
@login_required
@admin_required
def config():
    if request.method == 'POST':
        acao = (request.form.get('acao') or '').strip()
        if acao == 'novo':
            texto = (request.form.get('texto') or '').strip()[:300]
            tipo = (request.form.get('tipo') or '').strip()
            if not texto or tipo not in CHECKLIST_TIPOS:
                flash('Informe o texto do item e o tipo.', 'danger')
                return redirect(url_for('checklist.config'))
            loja_raw = (request.form.get('loja_id') or '').strip()
            loja = _resolver_loja(loja_raw)
            if loja_raw and loja is None:
                # Loja informada mas inválida (desativada no meio-tempo,
                # POST velho): criar como GLOBAL em silêncio cobraria TODAS
                # as lojas — recusa com aviso (achado da revisão).
                flash('A loja escolhida não está mais disponível — item '
                      'não criado. Escolha de novo.', 'danger')
                return redirect(url_for('checklist.config'))
            try:
                ordem = int(request.form.get('ordem') or 0)
            except ValueError:
                ordem = 0
            db.session.add(ChecklistItemModelo(
                tipo=tipo, texto=texto,
                setor=(request.form.get('setor') or '').strip()[:60] or None,
                exige_foto=bool(request.form.get('exige_foto')),
                ordem=ordem, loja_id=loja.id if loja else None))
            db.session.commit()
            flash('Item adicionado.', 'success')
        elif acao == 'editar':
            it = _item_do_form()
            if it is None:
                flash('Item não encontrado.', 'danger')
                return redirect(url_for('checklist.config'))
            texto = (request.form.get('texto') or '').strip()[:300]
            if texto:
                it.texto = texto
            it.exige_foto = bool(request.form.get('exige_foto'))
            try:
                it.ordem = int(request.form.get('ordem') or it.ordem)
            except ValueError:
                pass
            db.session.commit()
            flash('Item atualizado.', 'success')
        elif acao == 'toggle':
            it = _item_do_form()
            if it is None:
                flash('Item não encontrado.', 'danger')
            else:
                it.ativo = not it.ativo
                db.session.commit()
                flash(('Item reativado.' if it.ativo
                       else 'Item desativado — sai dos próximos checklists; '
                            'o histórico não muda.'), 'success')
        elif acao == 'excluir':
            it = _item_do_form()
            if it is None:
                flash('Item não encontrado.', 'danger')
            else:
                usado = (ChecklistResposta.query
                         .filter_by(item_id=it.id).first() is not None)
                if usado:
                    # Item com história nunca some — desativa (a FK das
                    # respostas antigas fica viva; snapshot cobre o texto).
                    it.ativo = False
                    db.session.commit()
                    flash('Esse item já tem respostas no histórico — foi '
                          'DESATIVADO em vez de excluído.', 'warning')
                else:
                    db.session.delete(it)
                    db.session.commit()
                    flash('Item excluído.', 'success')
        return redirect(url_for('checklist.config'))

    itens = (ChecklistItemModelo.query
             .order_by(ChecklistItemModelo.ordem, ChecklistItemModelo.id)
             .all())
    # Por tipo e, dentro dele, agrupado por setor (mesma ordem da tela de
    # preenchimento — com 169 pontos, uma lista corrida era ilegível).
    por_tipo = {t: checklist_loja.agrupar_por_setor(
        [i for i in itens if i.tipo == t]) for t in CHECKLIST_TIPOS}
    n_por_tipo = {t: sum(1 for i in itens if i.tipo == t)
                  for t in CHECKLIST_TIPOS}
    setores = []
    for i in itens:
        if i.setor and i.setor not in setores:
            setores.append(i.setor)
    return render_template('checklist/config.html', por_tipo=por_tipo,
                           n_por_tipo=n_por_tipo, setores=setores,
                           tipos=CHECKLIST_TIPOS,
                           labels=CHECKLIST_TIPO_LABEL,
                           lojas=_lojas_escolhiveis())


# ── Conferência (admin) ──────────────────────────────────────────────

@checklist_bp.route('/conferencia')
@login_required
@admin_required
def conferencia():
    """Histórico: quem preencheu, quando, problemas e fotos — e quem está
    DEVENDO hoje/ontem (mesma conta da pendência da home)."""
    from datetime import timedelta

    try:
        dias = min(max(int(request.args.get('dias') or 7), 1), 90)
    except (TypeError, ValueError):
        dias = 7
    from sqlalchemy.orm import joinedload, selectinload

    loja = _resolver_loja(request.args.get('loja'))
    di = hoje() - timedelta(days=dias - 1)
    q = (ChecklistPreenchimento.query
         .options(joinedload(ChecklistPreenchimento.loja),
                  joinedload(ChecklistPreenchimento.usuario),
                  # selectinload: o template lê respostas + n_problemas de
                  # cada linha — sem isso, ~3 lazy-loads × 400 linhas.
                  selectinload(ChecklistPreenchimento.respostas))
         .filter(ChecklistPreenchimento.data >= di,
                 ChecklistPreenchimento.data <= hoje()))
    if loja:
        q = q.filter(ChecklistPreenchimento.loja_id == loja.id)
    preenchidos = (q.order_by(ChecklistPreenchimento.data.desc(),
                              ChecklistPreenchimento.criado_em.desc())
                   .limit(400).all())
    faltando = {
        'abertura_hoje': checklist_loja.lojas_faltando('abertura', hoje()),
        'fechamento_ontem': checklist_loja.lojas_faltando(
            'fechamento', hoje() - timedelta(days=1)),
    }
    return render_template(
        'checklist/conferencia.html', preenchidos=preenchidos,
        labels=CHECKLIST_TIPO_LABEL, lojas=_lojas_escolhiveis(),
        loja=loja, dias=dias, faltando=faltando)
