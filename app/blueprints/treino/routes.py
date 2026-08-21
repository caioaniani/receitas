"""Sistema de treinamento GAMIFICADO (spec v1.0) — rotas §12.

Funcionário (/treino), gestor (/treino/gestor), admin (/treino/admin) e a rota
pública de verificação de certificado. Tudo keyed em Funcionario (resolvido do
Usuario de login). Toda a lógica de pontos/anti-fraude vive nos serviços
treino_*; aqui é só orquestração + gate de papel.
"""
from functools import wraps

from flask import (
    Response,
    abort,
    flash,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

from app.blueprints.treino import treino_bp
from app.decorators import admin_required
from app.extensions import db
from app.models import (
    Funcionario,
    TreinoAplicacaoPratica,
    TreinoCheckpoint,
    TreinoQuiz,
    TreinoRecompensa,
    TreinoResgate,
    TreinoSelo,
    TreinoTentativaQuiz,
    TreinoTrilha,
    TreinoVideo,
)
from app.services import treino_aplicacao as ap
from app.services import treino_certificado as cert
from app.services import treino_ledger as ledger
from app.services import treino_painel as painel
from app.services import treino_quiz as tq
from app.services import treino_ranking as rk
from app.services import treino_recompensa as rc
from app.services import treino_trilha as tt
from app.services import treino_video as tv


def _func():
    """Funcionario do usuário logado (None se a conta não é de funcionário)."""
    return tv.resolver_funcionario(current_user)


def _func_obrigatorio():
    f = _func()
    if f is None:
        abort(403)
    return f


def gestor_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if ledger.papel_treino(current_user) not in ('GESTOR', 'ADMIN'):
            abort(403)
        return fn(*a, **kw)
    return wrapper


def _temp():
    return ledger.temporada_ativa()


# ── Funcionário ─────────────────────────────────────────────────────────
@treino_bp.route('/')
@login_required
def home():
    f = _func()
    temp = _temp()
    trilhas = TreinoTrilha.query.filter_by(ativa=True).order_by(
        TreinoTrilha.ordem).all()
    ctx = {'trilhas': trilhas, 'func': f, 'temp': temp, 'onboarding_ids': set()}
    if f:
        from app.services import treino_onboarding as ob
        ctx['onboarding_ids'] = {t.id for t in ob.onboarding_do_funcionario(f)}
        ctx['progressao'] = ob.progressao(f)
    if f and temp:
        ctx['pos'] = rk.posicao_individual(f, temp.id)
        ctx['progresso'] = {
            t.id: tt.progresso_trilha(f, t, temp) for t in trilhas}
    ctx['proximo'] = painel.proximo_passo(
        f, trilhas, ctx['onboarding_ids']) if f else None
    return render_template('treino/home.html', **ctx)


@treino_bp.route('/trilha/<int:id>')
@login_required
def trilha(id):
    t = TreinoTrilha.query.filter_by(id=id, ativa=True).first_or_404()
    f = _func()
    temp = _temp()
    est = tt.progresso_trilha(f, t, temp) if (f and temp) else None
    videos = tt.videos_publicados(t)
    quizzes = tt.quizzes_publicados(t)
    video_progresso = painel.progresso_dos_videos(f, videos)
    return render_template('treino/trilha.html', t=t, est=est, videos=videos,
                           quizzes=quizzes, video_progresso=video_progresso)


@treino_bp.route('/video/<int:id>')
@login_required
def video(id):
    v = TreinoVideo.query.filter_by(id=id, ativo=True).first_or_404()
    from app.services import treinamento_stream as ts
    proc = _garantir_duracao(v)  # sem duração o progresso daria 100% falso
    pronto = proc is None or proc.get('pronto')
    embed = ts.embed_url(v.video_externo_id) \
        if v.video_externo_id and pronto else None
    quizzes = TreinoQuiz.query.filter_by(video_id=v.id, ativo=True).all()
    # Progresso JÁ salvo — pra a barra abrir no ponto certo (não em 0% quando o
    # vídeo já foi assistido/concluído).
    f = _func()
    pct_inicial, concluido = 0, False
    if f:
        from app.models import TreinoProgressoVideo
        prog = TreinoProgressoVideo.query.filter_by(
            funcionario_id=f.id, video_id=v.id, versao_video=v.versao).first()
        if prog:
            pct_inicial = float(prog.percentual or 0)
            concluido = bool(prog.concluido_em)
    # No CELULAR o vídeo NÃO pode ir a tela cheia: em fullscreen o iOS abre o
    # player nativo do sistema (camada acima da página) e a pergunta do
    # checkpoint não aparece nem dá pra sair (vídeo é cross-origin do
    # Cloudflare). Sem `allowfullscreen` ele toca embutido e o overlay funciona.
    import re as _re
    ua = request.headers.get('User-Agent', '')
    is_mobile = bool(_re.search(r'Mobi|Android|iPhone|iPad|iPod', ua, _re.I))
    resposta = make_response(render_template(
        'treino/video.html', v=v, video_embed=embed,
        checkpoints=v.checkpoints, quizzes=quizzes,
        pct_inicial=pct_inicial, concluido_inicial=concluido,
        is_mobile=is_mobile))
    if is_mobile:
        # O vídeo do Cloudflare é um iframe cross-origin. No celular, a tela
        # cheia nativa fica acima da página e esconderia a pergunta. O header é
        # a segunda trava, além da Permissions Policy aplicada no próprio iframe.
        resposta.headers['Permissions-Policy'] = 'fullscreen=()'
    return resposta


@treino_bp.route('/api/videos/<int:id>/heartbeat', methods=['POST'])
@login_required
def api_heartbeat(id):
    v = TreinoVideo.query.filter_by(id=id, ativo=True).first_or_404()
    f = _func_obrigatorio()
    try:
        pos = float(request.form.get('t') or 0)
        vel = float(request.form.get('v') or 1.0)
    except (TypeError, ValueError):
        pos, vel = 0.0, 1.0
    return jsonify(ok=True, **tv.heartbeat(f, v, pos, vel))


@treino_bp.route('/api/checkpoints/<int:id>/resposta', methods=['POST'])
@login_required
def api_checkpoint(id):
    cp = db.session.get(TreinoCheckpoint, id) or abort(404)
    f = _func_obrigatorio()
    try:
        idx = int(request.form.get('indice'))
    except (TypeError, ValueError):
        return jsonify(ok=False, erro='índice inválido'), 400
    return jsonify(ok=True, **tv.responder_checkpoint(f, cp, idx))


@treino_bp.route('/api/quiz/<int:id>/iniciar', methods=['POST'])
@login_required
def api_quiz_iniciar(id):
    quiz = TreinoQuiz.query.filter_by(id=id, ativo=True).first_or_404()
    f = _func_obrigatorio()
    try:
        t = tq.iniciar_tentativa(f, quiz)
    except tq.CooldownError as e:
        return jsonify(ok=False, erro='cooldown',
                       liberado_em=e.liberado_em.isoformat()), 429
    return jsonify(ok=True, tentativa_id=t.id, questoes=t.questoes_sorteadas,
                   nota_minima=float(quiz.nota_minima))


@treino_bp.route('/api/tentativas/<int:id>/responder', methods=['POST'])
@login_required
def api_responder(id):
    t = db.session.get(TreinoTentativaQuiz, id) or abort(404)
    f = _func_obrigatorio()
    if t.funcionario_id != f.id:
        abort(403)
    return jsonify(**tq.responder(
        t, request.form.get('questao_id'), request.form.get('alternativa_id'),
        request.form.get('segundos', 0)))


@treino_bp.route('/api/tentativas/<int:id>/finalizar', methods=['POST'])
@login_required
def api_finalizar(id):
    t = db.session.get(TreinoTentativaQuiz, id) or abort(404)
    f = _func_obrigatorio()
    if t.funcionario_id != f.id:
        abort(403)
    quiz = db.session.get(TreinoQuiz, t.quiz_id)
    res = tq.finalizar(t, quiz)
    # tenta fechar a trilha (pode emitir selo)
    temp = _temp()
    if temp and quiz.trilha_id:
        trilha_obj = db.session.get(TreinoTrilha, quiz.trilha_id)
        tt.verificar_conclusao(f, trilha_obj, temp)
    return jsonify(ok=True, **res)


@treino_bp.route('/ranking')
@login_required
def ranking():
    temp = _temp()
    f = _func()
    unidades = rk.ranking_unidades(temp.id) if temp else []
    pos = rk.posicao_individual(f, temp.id) if (f and temp) else None
    return render_template('treino/ranking.html', unidades=unidades, pos=pos)


@treino_bp.route('/recompensas')
@login_required
def recompensas():
    f = _func()
    temp = _temp()
    lista = TreinoRecompensa.query.filter_by(ativa=True).all()
    saldo = ledger.saldo(f.id, temp.id) if (f and temp) else 0
    return render_template('treino/recompensas.html', recompensas=lista,
                           saldo=saldo)


@treino_bp.route('/api/resgates', methods=['POST'])
@login_required
def api_resgatar():
    f = _func_obrigatorio()
    temp = _temp()
    rec = db.session.get(TreinoRecompensa, request.form.get('recompensa_id', 0))
    if rec is None or temp is None:
        return jsonify(ok=False, erro='indisponível'), 400
    try:
        rc.solicitar(f, rec, temp)
    except rc.ResgateError as e:
        return jsonify(ok=False, erro=str(e)), 400
    flash('Resgate solicitado — aguarde a aprovação do gestor.', 'success')
    return jsonify(ok=True)


@treino_bp.route('/extrato')
@login_required
def extrato():
    f = _func_obrigatorio()
    temp = _temp()
    from app.models import TreinoEventoPontos
    eventos = (TreinoEventoPontos.query.filter_by(funcionario_id=f.id)
               .order_by(TreinoEventoPontos.criado_em.desc()).limit(200).all()) \
        if f else []
    saldo = ledger.saldo(f.id, temp.id) if (f and temp) else 0
    return render_template('treino/extrato.html', eventos=eventos, saldo=saldo)


@treino_bp.route('/certificado/<int:selo_id>.pdf')
@login_required
def certificado(selo_id):
    selo = db.session.get(TreinoSelo, selo_id) or abort(404)
    f = _func()
    # dono do selo, ou gestor/admin
    if not (f and selo.funcionario_id == f.id) and \
            ledger.papel_treino(current_user) not in ('GESTOR', 'ADMIN'):
        abort(403)
    pdf = cert.gerar_pdf(selo, base_url=request.host_url.rstrip('/'))
    return Response(pdf, mimetype='application/pdf', headers={
        'Content-Disposition': f'inline; filename=certificado-{selo_id}.pdf'})


# ── Rota pública de verificação (§11) ───────────────────────────────────
@treino_bp.route('/verificar/<codigo>')
def verificar(codigo):
    selo = cert.por_codigo(codigo)
    dados = cert.dados_certificado(selo) if selo else None
    return render_template('treino/verificar.html', selo=selo, dados=dados)


# ── Gestor ──────────────────────────────────────────────────────────────
@treino_bp.route('/gestor/')
@login_required
@gestor_required
def gestor_home():
    gestor = _func()
    unidade = ledger.unidade_do_funcionario(gestor) if gestor else None
    is_admin = ledger.papel_treino(current_user) == 'ADMIN'
    if is_admin:
        equipe = Funcionario.query.filter_by(ativo=True).order_by(
            Funcionario.nome).all()
    else:
        equipe = [f for f in (unidade.funcionarios if unidade else [])
                  if f.ativo]
    trilhas = TreinoTrilha.query.filter_by(ativa=True).all()
    visao_equipe = painel.painel_equipe(equipe, _temp())
    return render_template('treino/gestor.html', equipe=equipe, trilhas=trilhas,
                           unidade=unidade, painel=visao_equipe,
                           is_admin=is_admin,
                           can_open_rh=current_user.is_dono())


@treino_bp.route('/gestor/api/aplicacao', methods=['POST'])
@login_required
@gestor_required
def gestor_aplicacao():
    gestor = _func_obrigatorio()
    temp = _temp()
    f = db.session.get(Funcionario, request.form.get('funcionario_id', 0))
    trilha_obj = db.session.get(TreinoTrilha, request.form.get('trilha_id', 0))
    if not (f and trilha_obj and temp):
        return jsonify(ok=False, erro='dados incompletos'), 400
    itens = request.form.getlist('itens_ok')
    try:
        ap.registrar(gestor, f, trilha_obj, temp, itens,
                     request.form.get('evidencia', ''),
                     criado_por_id=current_user.id,
                     is_admin=ledger.papel_treino(current_user) == 'ADMIN')
    except ap.AplicacaoError as e:
        return jsonify(ok=False, erro=str(e)), 400
    # pode fechar a trilha
    tt.verificar_conclusao(f, trilha_obj, temp)
    return jsonify(ok=True)


@treino_bp.route('/gestor/resgates')
@login_required
@gestor_required
def gestor_resgates():
    is_admin = ledger.papel_treino(current_user) == 'ADMIN'
    gestor = _func()
    q = TreinoResgate.query.filter_by(status='SOLICITADO')
    pendentes = q.order_by(TreinoResgate.solicitado_em).all()
    if not is_admin and gestor is not None:
        unidade = ledger.unidade_do_funcionario(gestor)
        ids = {f.id for f in (unidade.funcionarios if unidade else [])}
        pendentes = [r for r in pendentes if r.funcionario_id in ids]
    return render_template('treino/gestor_resgates.html', pendentes=pendentes)


@treino_bp.route('/gestor/api/resgates/<int:id>/decidir', methods=['POST'])
@login_required
@gestor_required
def gestor_decidir(id):
    r = db.session.get(TreinoResgate, id) or abort(404)
    aprovar = request.form.get('acao') == 'aprovar'
    try:
        if aprovar:
            rc.aprovar(r, decidido_por_id=current_user.id)
        else:
            rc.recusar(r, decidido_por_id=current_user.id)
    except rc.ResgateError as e:
        return jsonify(ok=False, erro=str(e)), 400
    return jsonify(ok=True)


# ── Admin (autoria + gestão) ────────────────────────────────────────────
@treino_bp.route('/admin/')
@login_required
@admin_required
def admin_home():
    from app.models import Cargo, TreinoTemporada, TreinoTrilhaCargo
    from app.services import treino_acessos as acessos
    from app.services import treino_exclusao as exclusao
    from app.services import treino_pontos as cfg
    trilhas = TreinoTrilha.query.order_by(TreinoTrilha.ordem).all()
    contas_livres = acessos.contas_sem_vinculo()
    # mapa trilha_id -> conjunto de cargo_ids exigidos (v2 §16.1)
    cargos_por_trilha = {}
    vinculados = set()
    for m in TreinoTrilhaCargo.query.all():
        cargos_por_trilha.setdefault(m.trilha_id, set()).add(m.cargo_id)
        vinculados.add(m.cargo_id)
    # cargos ativos + os já vinculados a alguma trilha (mesmo se desativados) —
    # sem os vinculados, salvar o form de uma trilha apagaria em silêncio o
    # vínculo a um cargo que foi desativado depois (achado da revisão).
    cargos = Cargo.query.filter(db.or_(
        Cargo.ativo.is_(True), Cargo.id.in_(vinculados) if vinculados else
        db.false())).order_by(Cargo.nome).all()
    funcionarios = Funcionario.query.filter_by(ativo=True).order_by(
        Funcionario.nome).all()
    quizzes_por_trilha = {}
    for quiz in TreinoQuiz.query.order_by(TreinoQuiz.id).all():
        if quiz.trilha_id:
            quizzes_por_trilha.setdefault(quiz.trilha_id, []).append(quiz)
    return render_template(
        'treino/admin.html', trilhas=trilhas, pontos=cfg.todos(),
        temporadas=TreinoTemporada.query.order_by(
            TreinoTemporada.inicio.desc()).all(),
        recompensas=TreinoRecompensa.query.all(),
        cargos=cargos, cargos_por_trilha=cargos_por_trilha,
        exclusao_por_trilha=exclusao.mapa_historico(trilhas),
        contas_livres=contas_livres,
        funcionarios=funcionarios, quizzes_por_trilha=quizzes_por_trilha,
        painel=painel.resumo_admin(trilhas, funcionarios),
        can_open_rh=current_user.is_dono())


@treino_bp.route('/admin/aplicacoes/<int:id>/estornar', methods=['POST'])
@login_required
@admin_required
def admin_estornar_aplicacao(id):
    a = db.session.get(TreinoAplicacaoPratica, id) or abort(404)
    ap.estornar(a, criado_por_id=current_user.id)
    flash('Aplicação estornada.', 'success')
    return redirect(request.referrer or url_for('treino.admin_home'))


# ── Admin: AUTORIA (criar conteúdo) ─────────────────────────────────────
def _int(v, d=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return d


@treino_bp.route('/admin/temporada', methods=['POST'])
@login_required
@admin_required
def admin_temporada():
    from datetime import date

    from app.models import TreinoTemporada
    try:
        ini = date.fromisoformat(request.form['inicio'])
        fim = date.fromisoformat(request.form['fim'])
    except (KeyError, ValueError):
        flash('Datas inválidas.', 'warning')
        return redirect(url_for('treino.admin_home'))
    t = TreinoTemporada(nome=request.form.get('nome', 'Temporada')[:100],
                        inicio=ini, fim=fim,
                        status=request.form.get('status', 'ATIVA'))
    db.session.add(t)
    db.session.commit()
    flash('Temporada criada.', 'success')
    return redirect(url_for('treino.admin_home'))


@treino_bp.route('/admin/trilha', methods=['POST'])
@login_required
@admin_required
def admin_trilha():
    nome = (request.form.get('nome') or '').strip()
    if not nome:
        flash('Dê um nome à trilha.', 'warning')
        return redirect(url_for('treino.admin_home'))
    ordem = (db.session.query(db.func.max(TreinoTrilha.ordem)).scalar() or 0) + 1
    db.session.add(TreinoTrilha(
        nome=nome[:150], descricao=request.form.get('descricao') or None,
        ordem=ordem, ativa=False))
    db.session.commit()
    flash('Trilha criada.', 'success')
    return redirect(url_for('treino.admin_home'))


@treino_bp.route('/admin/roteiros/importar', methods=['POST'])
@login_required
@admin_required
def admin_importar_roteiros():
    """Importa o PLANO DE CONTEÚDO (xlsx de roteiros) — 13/08/2026.

    Módulo vira trilha, aula vira vídeo RASCUNHO com o roteiro anexado.
    Tudo nasce desativado; idempotente (re-importar atualiza roteiros sem
    duplicar e sem tocar em aula com vídeo gravado)."""
    from app.services import treino_roteiros
    arq = request.files.get('arquivo')
    if not arq or not arq.filename:
        flash('Escolha a planilha de roteiros (.xlsx).', 'warning')
        return redirect(url_for('treino.admin_home'))
    raw = arq.read()
    if len(raw) > 8 * 1024 * 1024:
        flash('Arquivo maior que 8MB — confira se é a planilha certa.',
              'danger')
        return redirect(url_for('treino.admin_home'))
    try:
        stats = treino_roteiros.importar(raw)
    except Exception as e:  # noqa: BLE001 — parse de arquivo externo
        flash(f'Não consegui importar: {e}', 'danger')
        return redirect(url_for('treino.admin_home'))
    partes = []
    if stats['trilhas_criadas']:
        partes.append(f"{stats['trilhas_criadas']} trilha(s) criada(s) "
                      '(desativadas — ative quando os vídeos subirem)')
    if stats['aulas_criadas']:
        partes.append(f"{stats['aulas_criadas']} aula(s) criada(s) em "
                      'rascunho com roteiro')
    if stats['roteiros_atualizados']:
        partes.append(f"{stats['roteiros_atualizados']} roteiro(s) "
                      'atualizado(s)')
    if stats['aulas_com_video_preservadas']:
        partes.append(f"{stats['aulas_com_video_preservadas']} aula(s) já "
                      'gravada(s) preservada(s)')
    flash('Roteiros importados: ' + ('; '.join(partes) or 'nenhuma mudança')
          + '.', 'success')
    for a in stats['avisos']:
        flash(a, 'warning')
    return redirect(url_for('treino.admin_home'))


@treino_bp.route('/admin/trilha/<int:id>/video', methods=['POST'])
@login_required
@admin_required
def admin_video_novo(id):
    t = db.session.get(TreinoTrilha, id) or abort(404)
    ordem = _int(db.session.query(db.func.max(TreinoVideo.ordem)).filter_by(
        trilha_id=t.id).scalar()) + 1
    # Titulo em branco NAO pode virar '' (a tela da trilha mostraria uma aula
    # sem nome, sem jeito de corrigir) — cai no default 'Aula', editavel depois.
    titulo = (request.form.get('titulo') or '').strip()[:200] or 'Aula'
    v = TreinoVideo(trilha_id=t.id, titulo=titulo,
                    duracao_segundos=_int(request.form.get('duracao')),
                    ordem=ordem, ativo=False)
    db.session.add(v)
    db.session.commit()
    flash('Vídeo criado — suba o arquivo agora.', 'success')
    return redirect(url_for('treino.admin_video_editar', id=v.id))


@treino_bp.route('/admin/video/<int:id>/titulo', methods=['POST'])
@login_required
@admin_required
def admin_video_titulo(id):
    """Renomeia a aula (o titulo do video). So havia como setar na criacao;
    aula criada com nome em branco ficava sem titulo e sem edicao."""
    v = db.session.get(TreinoVideo, id) or abort(404)
    titulo = (request.form.get('titulo') or '').strip()
    if not titulo:
        flash('O título da aula não pode ficar em branco.', 'warning')
    else:
        v.titulo = titulo[:200]
        db.session.commit()
        flash('Título salvo.', 'success')
    return redirect(url_for('treino.admin_video_editar', id=v.id))


def _garantir_duracao(v):
    """Busca a duração no Cloudflare ENQUANTO é <=0 (recém-subido; o Cloudflare
    devolve -1 durante o processamento) e grava quando vem POSITIVA. Devolve o
    proc (status) ou None. Progresso e antifraude dependem de duração positiva —
    sem ela o cálculo de cobertura daria 100% falso. Depois de detectada não
    bate mais no Cloudflare (evita HTTP síncrono à toa)."""
    if not v.video_externo_id or v.duracao_segundos > 0:
        return None
    from app.services import treinamento_stream as ts
    try:
        proc = ts.status(v.video_externo_id)
        dur = proc.get('duracao') or 0
        if dur > 0:
            v.duracao_segundos = dur
            db.session.commit()
        return proc
    except Exception:
        return None


@treino_bp.route('/admin/video/<int:id>')
@login_required
@admin_required
def admin_video_editar(id):
    v = db.session.get(TreinoVideo, id) or abort(404)
    from app.services import treinamento_stream as ts
    proc = _garantir_duracao(v)
    pronto = proc is None or proc.get('pronto')
    return render_template('treino/admin_video.html', v=v, proc=proc,
                           stream_ok=ts.configurado(),
                           video_embed=ts.embed_url(v.video_externo_id)
                           if v.video_externo_id and pronto else None)


@treino_bp.route('/admin/video/<int:id>/upload-url', methods=['POST'])
@login_required
@admin_required
def admin_video_upload_url(id):
    v = db.session.get(TreinoVideo, id) or abort(404)
    from app.services import treinamento_stream as ts
    if not ts.configurado():
        return jsonify(ok=False, erro='Cloudflare não configurado'), 503
    try:
        d = ts.criar_upload_direto(f'treino video {v.id}')
    except ValueError as e:
        return jsonify(ok=False, erro=str(e)), 502
    return jsonify(ok=True, uid=d['uid'], uploadURL=d['uploadURL'])


@treino_bp.route('/admin/video/<int:id>/salvar', methods=['POST'])
@login_required
@admin_required
def admin_video_salvar(id):
    import re as _re
    v = db.session.get(TreinoVideo, id) or abort(404)
    uid = (request.form.get('uid') or '').strip()
    if not _re.fullmatch(r'[0-9a-f]{32}', uid):
        return jsonify(ok=False, erro='UID inválido'), 400
    from app.services import treinamento_stream as ts
    v.video_externo_id = uid
    v.provedor = 'cloudflare'
    # Upload e publicação são decisões separadas. O Cloudflare ainda precisa
    # processar o arquivo; deixar ativo aqui exporia um player quebrado.
    v.ativo = False
    v.duracao_segundos = 0
    db.session.commit()
    if not ts.subdomain():
        ts.status(uid)
    return jsonify(ok=True)


def _mmss_para_seg(valor):
    """Aceita 'min:seg' (ex '2:30' -> 150), 'hora:min:seg' (ex '1:02:30' ->
    3750) OU segundos crus (ex '150'). Campo do checkpoint é o MOMENTO do vídeo,
    e o dono pensa em min:seg. Nunca devolve negativo."""
    v = (valor or '').strip()
    if ':' in v:
        try:
            seg = 0
            for parte in v.split(':'):        # base-60: mm:ss e hh:mm:ss
                seg = seg * 60 + int(parte or 0)
            return max(0, seg)
        except ValueError:
            return 0
    return max(0, _int(v))


def _dados_checkpoint_form(v):
    """Normaliza e valida o formulário compartilhado por criar/editar."""
    segundo = _mmss_para_seg(request.form.get('segundo'))
    enunciado = (request.form.get('enunciado') or '').strip()[:500]

    # Preserva o índice marcado mesmo se uma das quatro linhas estiver vazia.
    # Sem esse mapa, marcar a 3ª opção com a 2ª vazia faria a alternativa correta
    # apontar para o lugar errado depois da remoção dos campos em branco.
    alternativas = []
    indices = {}
    for indice_original, valor in enumerate(request.form.getlist('alt[]')):
        texto = (valor or '').strip()
        if texto:
            indices[indice_original] = len(alternativas)
            alternativas.append(texto)
    correta = indices.get(_int(request.form.get('correta')), -1)

    erro = None
    if not enunciado:
        erro = 'Escreva a pergunta do checkpoint.'
    elif len(alternativas) < 2:
        erro = 'Checkpoint precisa de ao menos 2 alternativas.'
    elif correta < 0:
        erro = 'Marque a alternativa correta entre as que você preencheu.'
    elif v.duracao_segundos > 0 and segundo >= v.duracao_segundos:
        erro = 'O momento da pergunta precisa ser antes do fim do vídeo.'
    return segundo, enunciado, alternativas, correta, erro


def _checkpoint_json(cp):
    return jsonify(
        ok=True,
        id=cp.id,
        segundo=cp.segundo,
        enunciado=cp.enunciado,
        alternativas=cp.alternativas,
        n_alts=len(cp.alternativas),
        correta=cp.indice_correto,
        editar_url=url_for('treino.admin_checkpoint_editar', id=cp.id),
        excluir_url=url_for('treino.admin_checkpoint_excluir', id=cp.id),
    )


def _checkpoint_erro(v, ajax, erro):
    if ajax:
        return jsonify(ok=False, erro=erro), 400
    flash(erro, 'warning')
    return redirect(url_for('treino.admin_video_editar', id=v.id))


@treino_bp.route('/admin/video/<int:id>/checkpoint', methods=['POST'])
@login_required
@admin_required
def admin_checkpoint(id):
    v = db.session.get(TreinoVideo, id) or abort(404)
    ajax = request.form.get('ajax') == '1'   # salvar sem recarregar a página
    segundo, enunciado, alts, correta, erro = _dados_checkpoint_form(v)
    if erro:
        return _checkpoint_erro(v, ajax, erro)
    cp = TreinoCheckpoint(
        video_id=v.id, segundo=segundo, enunciado=enunciado,
        alternativas=alts, indice_correto=correta)
    db.session.add(cp)
    db.session.commit()
    if ajax:
        return _checkpoint_json(cp)
    flash('Checkpoint adicionado.', 'success')
    return redirect(url_for('treino.admin_video_editar', id=v.id))


@treino_bp.route('/admin/checkpoint/<int:id>/editar', methods=['POST'])
@login_required
@admin_required
def admin_checkpoint_editar(id):
    """Edita momento, pergunta e alternativas sem recriar o checkpoint."""
    cp = db.session.get(TreinoCheckpoint, id) or abort(404)
    ajax = request.form.get('ajax') == '1'
    segundo, enunciado, alts, correta, erro = _dados_checkpoint_form(cp.video)
    if erro:
        return _checkpoint_erro(cp.video, ajax, erro)
    cp.segundo = segundo
    cp.enunciado = enunciado
    cp.alternativas = alts
    cp.indice_correto = correta
    db.session.commit()
    if ajax:
        return _checkpoint_json(cp)
    flash('Pausa e pergunta atualizadas.', 'success')
    return redirect(url_for('treino.admin_video_editar', id=cp.video_id))


@treino_bp.route('/admin/video/<int:id>/ia-gerar', methods=['POST'])
@login_required
@admin_required
def admin_video_ia(id):
    """v2 §16.2 no CHECKPOINT: a IA PROPÕE pergunta + alternativas. Duas fontes:
    `fonte=video` usa a TRANSCRIÇÃO com tempo do Cloudflare (a IA também sugere
    o MOMENTO); qualquer outra usa o texto colado (sem momento). Não grava nada
    aqui — o admin revisa, ajusta o momento e salva pelo endpoint de checkpoint."""
    v = db.session.get(TreinoVideo, id) or abort(404)
    from app.services import treino_ia_perguntas as ia
    n = request.form.get('n', 3)
    if request.form.get('fonte') == 'video':
        if not v.video_externo_id:
            return jsonify(ok=False, erro='Envie o vídeo primeiro.'), 400
        from app.services import treinamento_stream as ts
        segs = ts.transcricao(v.video_externo_id)
        if not segs:
            # legenda ainda não pronta — dispara a geração e pede pra tentar de
            # novo (o Cloudflare leva ~1-2 min pra transcrever). Se a geração
            # falhar (ex.: vídeo ainda não transcodificado, idioma), mostra o
            # motivo real em vez de "processando" pra sempre.
            res = ts.gerar_legenda(v.video_externo_id)
            msg = ('Legenda automática do vídeo ainda sendo gerada pelo '
                   'Cloudflare. Tente de novo em 1-2 minutos.')
            if res and not res.get('ok') and res.get('erro'):
                msg = f'Não consegui gerar a legenda do vídeo: {res["erro"]}'
            return jsonify(ok=False, processando=True, erro=msg), 409
        r = ia.gerar_com_momento(segs, n)
    else:
        r = ia.gerar(request.form.get('texto', ''), n)
    if 'erro' in r:
        return jsonify(ok=False, erro=r['erro']), 400
    return jsonify(ok=True, perguntas=r['perguntas'])


@treino_bp.route('/admin/quiz', methods=['POST'])
@login_required
@admin_required
def admin_quiz_novo():
    trilha_id = _int(request.form.get('trilha_id')) or None
    video_id = _int(request.form.get('video_id')) or None
    if bool(trilha_id) == bool(video_id):
        flash('Escolha trilha OU vídeo pro quiz.', 'warning')
        return redirect(url_for('treino.admin_home'))
    q = TreinoQuiz(titulo=request.form.get('titulo', 'Quiz')[:200],
                   trilha_id=trilha_id, video_id=video_id,
                   questoes_por_tentativa=_int(request.form.get('por_tent'), 5),
                   cooldown_minutos=_int(request.form.get('cooldown'), 120))
    db.session.add(q)
    db.session.commit()
    flash('Quiz criado — adicione as questões.', 'success')
    return redirect(url_for('treino.admin_quiz_editar', id=q.id))


@treino_bp.route('/admin/quiz/<int:id>')
@login_required
@admin_required
def admin_quiz_editar(id):
    q = db.session.get(TreinoQuiz, id) or abort(404)
    return render_template('treino/admin_quiz.html', q=q,
                           pode=tq.pode_publicar(q))


@treino_bp.route('/admin/quiz/<int:id>/questao', methods=['POST'])
@login_required
@admin_required
def admin_questao(id):
    from app.models import TreinoAlternativa, TreinoQuestao
    q = db.session.get(TreinoQuiz, id) or abort(404)
    alts = request.form.getlist('alt[]')
    correta = _int(request.form.get('correta'))
    pares = [(a.strip(), i == correta) for i, a in enumerate(alts) if a.strip()]
    if not request.form.get('enunciado') or len(pares) < 2:
        flash('Questão precisa de enunciado e ao menos 2 alternativas.',
              'warning')
        return redirect(url_for('treino.admin_quiz_editar', id=q.id))
    quest = TreinoQuestao(quiz_id=q.id, enunciado=request.form['enunciado'])
    db.session.add(quest)
    db.session.flush()
    for texto, cor in pares:
        db.session.add(TreinoAlternativa(questao_id=quest.id, texto=texto,
                                         correta=cor))
    db.session.commit()
    flash('Questão adicionada.', 'success')
    return redirect(url_for('treino.admin_quiz_editar', id=q.id))


@treino_bp.route('/admin/quiz/<int:id>/ia-gerar', methods=['POST'])
@login_required
@admin_required
def admin_quiz_ia(id):
    """v2 §16.2: a IA PROPÕE perguntas a partir do conteúdo colado. Devolve as
    propostas pra revisão humana na tela — NÃO grava nada aqui (o admin edita e
    salva as escolhidas pelo endpoint normal de questão)."""
    db.session.get(TreinoQuiz, id) or abort(404)
    from app.services import treino_ia_perguntas as ia
    r = ia.gerar(request.form.get('texto', ''), request.form.get('n', 5))
    if 'erro' in r:
        return jsonify(ok=False, erro=r['erro']), 400
    return jsonify(ok=True, perguntas=r['perguntas'],
                   modelo=r.get('modelo_usado'))


@treino_bp.route('/admin/recompensa', methods=['POST'])
@login_required
@admin_required
def admin_recompensa():
    nome = (request.form.get('nome') or '').strip()
    if not nome:
        flash('Dê um nome à recompensa.', 'warning')
        return redirect(url_for('treino.admin_home'))
    est = request.form.get('estoque')
    db.session.add(TreinoRecompensa(
        nome=nome[:150], descricao=request.form.get('descricao') or None,
        custo_pontos=_int(request.form.get('custo'), 100),
        estoque=_int(est) if est else None))
    db.session.commit()
    flash('Recompensa criada.', 'success')
    return redirect(url_for('treino.admin_home'))


@treino_bp.route('/admin/ajuste', methods=['POST'])
@login_required
@admin_required
def admin_ajuste():
    f = db.session.get(Funcionario, _int(request.form.get('funcionario_id')))
    temp = _temp()
    just = (request.form.get('justificativa') or '').strip()
    if not (f and temp and just):
        flash('Funcionário, temporada ativa e justificativa são obrigatórios.',
              'warning')
        return redirect(url_for('treino.admin_home'))
    try:
        ledger.ajuste_manual(f, _int(request.form.get('pontos')), just,
                             criado_por_id=current_user.id, temporada=temp)
    except ValueError as e:
        flash(str(e), 'warning')
    else:
        flash('Ajuste lançado.', 'success')
    return redirect(url_for('treino.admin_home'))


# ── Admin: edição / desativação / exclusão ──────────────────────────────
def _voltar():
    return redirect(request.referrer or url_for('treino.admin_home'))


@treino_bp.route('/admin/trilha/<int:id>/toggle', methods=['POST'])
@login_required
@admin_required
def admin_trilha_toggle(id):
    t = db.session.get(TreinoTrilha, id) or abort(404)
    if not t.ativa and not tt.videos_publicados(t):
        flash('Publique ao menos uma aula com vídeo antes de publicar o módulo.',
              'warning')
        return _voltar()
    t.ativa = not t.ativa
    db.session.commit()
    flash('Módulo publicado.' if t.ativa else 'Módulo retirado do ar.',
          'success')
    return _voltar()


@treino_bp.route('/admin/trilha/<int:id>/excluir', methods=['POST'])
@login_required
@admin_required
def admin_trilha_excluir(id):
    from sqlalchemy.exc import IntegrityError

    from app.services import treino_exclusao as exclusao

    t = db.session.get(TreinoTrilha, id) or abort(404)
    apagar_historico = request.form.get('apagar_historico') == '1'
    if apagar_historico and (request.form.get('confirmar') or '').strip() != 'EXCLUIR':
        flash('Exclusão cancelada: digite EXCLUIR exatamente como solicitado.',
              'warning')
        return redirect(url_for('treino.admin_home', _anchor=f't{t.id}'))
    try:
        stats = exclusao.excluir(
            t, apagar_historico=apagar_historico,
            criado_por_id=current_user.id)
    except ValueError as e:
        flash(f'Trilha com histórico ({e}). Use “Limpar progresso e excluir” '
              'se estes dados forem realmente de teste.', 'warning')
        return redirect(url_for('treino.admin_home', _anchor=f't{t.id}'))
    except IntegrityError:
        # rede de segurança: qualquer vínculo não previsto nunca vira 500
        db.session.rollback()
        flash('Não deu pra excluir — há registros vinculados à trilha. '
              'Desative em vez de excluir.', 'warning')
        return _voltar()
    if apagar_historico:
        flash(f'Trilha excluída: {stats["historico"]} registro(s) de progresso '
              f'removido(s) e {stats["estornos"]} lançamento(s) de pontos '
              'estornado(s).', 'success')
    else:
        flash('Trilha excluída.', 'success')
    return redirect(url_for('treino.admin_home'))


@treino_bp.route('/admin/trilha/<int:id>/editar', methods=['POST'])
@login_required
@admin_required
def admin_trilha_editar(id):
    """Edita nome/descrição da trilha."""
    t = db.session.get(TreinoTrilha, id) or abort(404)
    nome = (request.form.get('nome') or '').strip()
    if not nome:
        flash('A trilha precisa de um nome.', 'warning')
        return _voltar()
    t.nome = nome[:150]
    t.descricao = (request.form.get('descricao') or '').strip() or None
    db.session.commit()
    flash('Trilha atualizada.', 'success')
    return _voltar()


@treino_bp.route('/admin/video/<int:id>/toggle', methods=['POST'])
@login_required
@admin_required
def admin_video_toggle(id):
    v = db.session.get(TreinoVideo, id) or abort(404)
    acao = (request.form.get('acao') or '').strip()
    publicar = acao == 'publicar' if acao else not v.ativo
    if publicar:
        if not v.video_externo_id:
            flash('Envie o arquivo do vídeo antes de publicar esta aula.',
                  'warning')
            return _voltar()
        from app.services import treinamento_stream as ts
        proc = ts.status(v.video_externo_id)
        if not proc.get('pronto'):
            detalhe = proc.get('erro') or (
                f"processamento em {proc.get('pct', 0)}%")
            flash(f'O vídeo ainda não pode ser publicado: {detalhe}.',
                  'warning')
            return _voltar()
        dur = proc.get('duracao') or 0
        if dur > 0:
            v.duracao_segundos = dur
        v.ativo = True
        mensagem = 'Aula publicada.'
    else:
        v.ativo = False
        mensagem = 'Aula retirada do ar.'
    db.session.commit()
    flash(mensagem, 'success')
    return _voltar()


@treino_bp.route('/admin/video/<int:id>/excluir', methods=['POST'])
@login_required
@admin_required
def admin_video_excluir(id):
    from sqlalchemy.exc import IntegrityError

    from app.models import TreinoProgressoVideo
    v = db.session.get(TreinoVideo, id) or abort(404)
    tid = v.trilha_id
    # Preserva histórico: vídeo já assistido ou com quiz atrelado não se apaga
    # (desative). Checa ANTES de mexer no Cloudflare pra não deixar o vídeo lá
    # órfão. Checkpoints somem por cascade.
    bloqueios = []
    if TreinoProgressoVideo.query.filter_by(video_id=v.id).first():
        bloqueios.append('já foi assistido')
    if TreinoQuiz.query.filter_by(video_id=v.id).first():
        bloqueios.append('tem quiz')
    if bloqueios:
        flash(f'Vídeo {" e ".join(bloqueios)} — desative em vez de excluir.',
              'warning')
        return redirect(url_for('treino.admin_home', _anchor=f't{tid}'))
    if v.video_externo_id:
        from app.services import treinamento_stream as ts
        ts.deletar(v.video_externo_id)
    db.session.delete(v)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash('Não deu pra excluir — há registros vinculados ao vídeo. '
              'Desative em vez de excluir.', 'warning')
        return redirect(url_for('treino.admin_home', _anchor=f't{tid}'))
    flash('Vídeo excluído.', 'success')
    return redirect(url_for('treino.admin_home', _anchor=f't{tid}'))


@treino_bp.route('/admin/checkpoint/<int:id>/excluir', methods=['POST'])
@login_required
@admin_required
def admin_checkpoint_excluir(id):
    c = db.session.get(TreinoCheckpoint, id) or abort(404)
    db.session.delete(c)
    db.session.commit()
    return _voltar()


@treino_bp.route('/admin/quiz/<int:id>/toggle', methods=['POST'])
@login_required
@admin_required
def admin_quiz_toggle(id):
    q = db.session.get(TreinoQuiz, id) or abort(404)
    if not q.ativo and not tq.pode_publicar(q):
        flash('Banco insuficiente (precisa de 3× as questões por tentativa).',
              'warning')
        return _voltar()
    q.ativo = not q.ativo
    db.session.commit()
    return _voltar()


@treino_bp.route('/admin/questao/<int:id>/excluir', methods=['POST'])
@login_required
@admin_required
def admin_questao_excluir(id):
    from app.models import TreinoQuestao
    quest = db.session.get(TreinoQuestao, id) or abort(404)
    db.session.delete(quest)
    db.session.commit()
    return _voltar()


@treino_bp.route('/admin/recompensa/<int:id>/toggle', methods=['POST'])
@login_required
@admin_required
def admin_recompensa_toggle(id):
    r = db.session.get(TreinoRecompensa, id) or abort(404)
    r.ativa = not r.ativa
    db.session.commit()
    return _voltar()


@treino_bp.route('/admin/acessos/<int:func_id>/gerar', methods=['POST'])
@login_required
@admin_required
def admin_gerar_acesso(func_id):
    """Cria/vincula o LOGIN do funcionário (onboarding). Sem esse vínculo a
    pessoa não entra no treinamento. Migrado do módulo antigo."""
    from app.services import treino_acessos as acessos
    f = Funcionario.query.get_or_404(func_id)
    r = acessos.gerar_acesso(f)
    if r['motivo'] == 'sem_email':
        flash(f'{f.nome}: cadastre o e-mail no RH antes de gerar o acesso.',
              'warning')
    elif r['motivo'] == 'ja_tem':
        flash(f'{f.nome} já tem acesso.', 'info')
    elif r['motivo'] == 'conta_de_outro_papel':
        flash(f'O e-mail de {f.nome} já é de uma conta de acesso administrativo '
              '— não vinculei (seria a conta errada). Use outro e-mail no RH.',
              'danger')
    elif r['motivo'] == 'email_em_uso':
        flash(f'O e-mail de {f.nome} já está vinculado a outro funcionário — '
              'confira o cadastro.', 'danger')
    elif r['motivo'] == 'vinculado':
        flash(f'{f.nome} vinculado a uma conta existente.', 'success')
    elif r['motivo'] == 'criado':
        if r.get('email_ok'):
            flash(f'Acesso de {f.nome} criado — senha enviada por e-mail.',
                  'success')
        else:
            flash(f'Acesso de {f.nome} criado, mas o e-mail falhou '
                  f'({r.get("email_erro")}). Senha: {r.get("senha")} — '
                  'passe manualmente.', 'warning')
    return _voltar()


@treino_bp.route('/admin/acessos/gerar-todos', methods=['POST'])
@login_required
@admin_required
def admin_gerar_acessos_todos():
    """Gera o acesso de TODO funcionário ativo com e-mail e sem login, de uma
    vez (12/08/2026 — os e-mails entraram nas fichas pela rodada do RI e
    clicar 36 vezes não é gesto). Reusa `gerar_acesso`, que é idempotente e
    tem todas as guardas (conta de outro papel / e-mail em uso = recusa).
    Cada conta criada recebe a senha no PRÓPRIO e-mail."""
    from app.services import treino_acessos as acessos
    fila = (Funcionario.query
            .filter(Funcionario.ativo.is_(True),
                    Funcionario.usuario_id.is_(None),
                    Funcionario.email.isnot(None), Funcionario.email != '')
            .order_by(Funcionario.nome).all())
    if not fila:
        flash('Ninguém pendente: todo funcionário ativo com e-mail já tem '
              'login.', 'info')
        return _voltar()
    criados, vinculados, problemas = 0, 0, []
    for f in fila:
        r = acessos.gerar_acesso(f)
        if r['motivo'] == 'criado':
            criados += 1
            if not r.get('email_ok'):
                # Sem o aviso a pessoa ficaria com conta e sem senha.
                problemas.append(f'{f.nome}: conta criada mas o e-mail '
                                 f'falhou ({r.get("email_erro")}). Senha: '
                                 f'{r.get("senha")} — passe manualmente.')
        elif r['motivo'] == 'vinculado':
            vinculados += 1
        elif r['motivo'] == 'conta_de_outro_papel':
            problemas.append(f'{f.nome}: o e-mail já é de uma conta '
                             'administrativa — não vinculei; use outro '
                             'e-mail no RH.')
        elif r['motivo'] == 'email_em_uso':
            problemas.append(f'{f.nome}: o e-mail já está vinculado a outro '
                             'funcionário — confira o cadastro.')
    partes = []
    if criados:
        partes.append(f'{criados} acesso(s) criado(s) — senha enviada por '
                      'e-mail')
    if vinculados:
        partes.append(f'{vinculados} vinculado(s) a conta existente')
    flash('Acessos em lote: ' + ('; '.join(partes) or 'nenhuma mudança') + '.',
          'success' if (criados or vinculados) else 'info')
    for p in problemas:
        flash(p, 'warning')
    return _voltar()


@treino_bp.route('/admin/acessos/<int:func_id>/vincular', methods=['POST'])
@login_required
@admin_required
def admin_vincular_acesso(func_id):
    """Liga o funcionário a uma conta de login JÁ EXISTENTE (sem duplicar) —
    pro caso de quem já tem cadastro mas sem e-mail."""
    from app.models import Usuario
    from app.services import treino_acessos as acessos
    f = Funcionario.query.get_or_404(func_id)
    u = db.session.get(Usuario, _int(request.form.get('usuario_id')))
    r = acessos.vincular_conta(f, u)
    if r['ok']:
        flash(f'{f.nome} vinculado à conta "{r["usuario"].login}".', 'success')
    elif r['motivo'] == 'conta_em_uso':
        flash('Essa conta já está vinculada a outro funcionário.', 'danger')
    elif r['motivo'] == 'owner':
        flash('Não dá pra vincular à conta do dono.', 'danger')
    elif r['motivo'] == 'ja_tem':
        flash(f'{f.nome} já tem login.', 'info')
    else:
        flash('Selecione uma conta pra vincular.', 'warning')
    return _voltar()


@treino_bp.route('/admin/trilha/<int:id>/cargos', methods=['POST'])
@login_required
@admin_required
def admin_trilha_cargos(id):
    """v2 §16.1: liga a trilha a cargos (onboarding automático por cargo).
    Suporta ajax=1 (auto-salvar ao marcar, sem recarregar) — devolve JSON."""
    from app.services import treino_onboarding as ob
    db.session.get(TreinoTrilha, id) or abort(404)
    ob.definir_cargos_da_trilha(id, request.form.getlist('cargo_ids'))
    if request.form.get('ajax') == '1':
        return jsonify(ok=True)
    flash('Cargos da trilha atualizados.', 'success')
    return _voltar()


@treino_bp.route('/gestor/progressao')
@login_required
@gestor_required
def gestor_progressao():
    """v2 §16.3: quem está apto a progredir de cargo (concluiu as trilhas
    exigidas do cargo)."""
    from app.services import treino_onboarding as ob
    gestor = _func()
    is_admin = ledger.papel_treino(current_user) == 'ADMIN'
    if is_admin:
        equipe = Funcionario.query.filter_by(ativo=True).order_by(
            Funcionario.nome).all()
    else:
        unidade = ledger.unidade_do_funcionario(gestor) if gestor else None
        equipe = [f for f in (unidade.funcionarios if unidade else [])
                  if f.ativo]
    progs = ob.progressao_lote(equipe)   # 2 queries, sem N+1
    linhas = [{'func': f, 'prog': progs[f.id]} for f in equipe]
    return render_template('treino/gestor_progressao.html', linhas=linhas)


@treino_bp.route('/admin/temporada/<int:id>/status', methods=['POST'])
@login_required
@admin_required
def admin_temporada_status(id):
    from app.models import TreinoTemporada
    t = db.session.get(TreinoTemporada, id) or abort(404)
    novo = request.form.get('status')
    if novo in ('PLANEJADA', 'ATIVA', 'ENCERRADA'):
        t.status = novo
        db.session.commit()
    return _voltar()
