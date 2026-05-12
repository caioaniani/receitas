"""Endpoints do Copilot.
Apenas admin pode usar (inicialmente)."""
import json
import logging

from flask import jsonify, request
from flask_login import current_user, login_required

from app.blueprints.copilot import copilot_bp
from app.extensions import db
from app.models import CopilotConversa
from app.services import copilot as copilot_svc

logger = logging.getLogger(__name__)


def _admin_only():
    if not current_user.is_authenticated:
        return jsonify(ok=False, erro='login obrigatorio'), 401
    if not current_user.is_admin():
        return jsonify(ok=False, erro='acesso restrito ao admin'), 403
    return None


@copilot_bp.route('/api/interpretar', methods=['POST'])
@login_required
def interpretar():
    guard = _admin_only()
    if guard:
        return guard
    data = request.get_json(silent=True) or {}
    prompt = (data.get('prompt') or '').strip()
    historico = data.get('historico') or []
    if not prompt:
        return jsonify(ok=False, erro='prompt vazio'), 400
    if len(prompt) > 2000:
        return jsonify(ok=False, erro='prompt muito longo (max 2000 chars)'), 400
    if not isinstance(historico, list):
        historico = []

    conversa = CopilotConversa(
        usuario_id=current_user.id,
        prompt=prompt,
        status='pendente',
    )
    db.session.add(conversa)
    db.session.flush()

    try:
        resultado = copilot_svc.interpretar(prompt, current_user, historico=historico)
    except Exception as exc:  # noqa: BLE001
        logger.exception('Copilot.interpretar falhou')
        conversa.status = 'falhou'
        conversa.erro = str(exc)
        db.session.commit()
        return jsonify(ok=False, erro=str(exc), conversa_id=conversa.id), 500

    conversa.tipo_acao = resultado.get('tipo')
    conversa.interpretacao_json = json.dumps(resultado, ensure_ascii=False)
    if resultado.get('tipo') == 'erro':
        conversa.status = 'falhou'
        conversa.erro = resultado.get('explicacao')
    db.session.commit()

    return jsonify(
        ok=True,
        conversa_id=conversa.id,
        tipo=resultado.get('tipo'),
        params=resultado.get('params'),
        explicacao=resultado.get('explicacao'),
        resultado=resultado.get('resultado'),
        requer_aprovacao=resultado.get('requer_aprovacao', False),
    )


@copilot_bp.route('/api/<int:conversa_id>/aprovar', methods=['POST'])
@login_required
def aprovar(conversa_id):
    guard = _admin_only()
    if guard:
        return guard
    conversa = CopilotConversa.query.get_or_404(conversa_id)
    if conversa.usuario_id != current_user.id:
        return jsonify(ok=False, erro='conversa pertence a outro usuario'), 403
    if conversa.status in ('executado', 'cancelado'):
        return jsonify(ok=False, erro=f'conversa ja {conversa.status}'), 400

    data = request.get_json(silent=True) or {}
    params = data.get('params') or {}
    # Sempre carrega params originais da conversa salva como base, e aplica
    # overrides do frontend (especialmente _itens_editados).
    base_params = {}
    if conversa.interpretacao_json:
        try:
            interp = json.loads(conversa.interpretacao_json)
            base_params = interp.get('params') or {}
        except (ValueError, TypeError):
            pass
    # Merge: base + overrides
    if params.get('_itens_editados') is not None:
        base_params['itens'] = params['_itens_editados']
    for k in ('loja_id', 'data_entrega', 'observacao'):
        if k in params and params[k] is not None:
            base_params[k] = params[k]
    params = base_params

    tipo = conversa.tipo_acao
    if tipo not in copilot_svc.REQUER_APROVACAO:
        return jsonify(ok=False, erro=f'tipo de acao nao executavel: {tipo}'), 400

    try:
        resultado = copilot_svc.executar(tipo, params, current_user)
    except Exception as exc:  # noqa: BLE001
        import traceback
        tb = traceback.format_exc()
        logger.exception('Copilot.executar falhou. tipo=%s params=%r', tipo, params)
        db.session.rollback()
        # Re-busca conversa apos rollback pra marcar status
        c = CopilotConversa.query.get(conversa_id)
        if c:
            c.status = 'falhou'
            c.erro = f'{type(exc).__name__}: {exc}\n{tb[:2000]}'
            try:
                db.session.commit()
            except Exception:  # noqa: BLE001
                db.session.rollback()
        return jsonify(ok=False, erro=f'{type(exc).__name__}: {exc}'), 500

    if not resultado.get('ok'):
        conversa.status = 'falhou'
        conversa.erro = resultado.get('erro')
        db.session.commit()
        return jsonify(ok=False, erro=resultado.get('erro')), 400

    from datetime import datetime
    conversa.status = 'executado'
    conversa.executado_em = datetime.utcnow()
    conversa.registro_tipo = resultado.get('registro_tipo')
    conversa.registro_id = resultado.get('registro_id')
    db.session.commit()

    return jsonify(ok=True, **resultado)


@copilot_bp.route('/api/lojas', methods=['GET'])
@login_required
def listar_lojas():
    """Lista lojas pra dropdown do preview de criar_pedido."""
    guard = _admin_only()
    if guard:
        return guard
    from app.models import Loja
    lojas = Loja.query.filter_by(ativa=True).order_by(Loja.nome).all()
    return jsonify(lojas=[{'id': l.id, 'nome': l.nome} for l in lojas])


@copilot_bp.route('/api/<int:conversa_id>/cancelar', methods=['POST'])
@login_required
def cancelar(conversa_id):
    guard = _admin_only()
    if guard:
        return guard
    conversa = CopilotConversa.query.get_or_404(conversa_id)
    if conversa.usuario_id != current_user.id:
        return jsonify(ok=False, erro='conversa pertence a outro usuario'), 403
    if conversa.status in ('executado', 'cancelado'):
        return jsonify(ok=False, erro=f'conversa ja {conversa.status}'), 400
    conversa.status = 'cancelado'
    db.session.commit()
    return jsonify(ok=True)
