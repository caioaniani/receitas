"""Decisão individual do dono: revisar antes de aplicar uma promoção."""
import hashlib
import json
from datetime import date

from flask import current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from itsdangerous import BadData, URLSafeTimedSerializer
from sqlalchemy.exc import DBAPIError

from app.blueprints.rh import rh_bp
from app.decorators import owner_required
from app.extensions import db
from app.models import (
    Cargo,
    Funcionario,
    PlanoCarreiraCargoVinculo,
    PlanoCarreiraEnquadramento,
    PlanoCarreiraFaixa,
    RhMovimentacao,
)
from app.services import rh_movimentacao, rh_promocao
from app.utils import hoje

_VALIDADE_PREVIA = 30 * 60


def _assinador():
    return URLSafeTimedSerializer(current_app.config['SECRET_KEY'], salt='rh-promocao-v1')


def _estado(pessoa, cargo, previa):
    """Detecta edição após a revisão, inclusive um cargo que mudou e voltou.

    Valores brutos também entram: não basta comparar centavos arredondados.
    Só a assinatura sai para o navegador; nenhum dado de acesso é incluído.
    """
    def campos(obj, nomes):
        return {nome: getattr(obj, nome) for nome in nomes} if obj else None

    ultimo = (db.session.query(db.func.max(RhMovimentacao.id))
              .filter(RhMovimentacao.funcionario_id == pessoa.id).scalar())
    dados = {
        'pessoa': campos(pessoa, ('id', 'nome', 'ativo', 'data_admissao', 'cargo_id',
                                 'funcao', 'salario_base', 'premiacao',
                                 'tem_cargo_confianca')),
        'cargo_atual': campos(pessoa.cargo, ('id', 'nome', 'salario_base', 'ativo')),
        'destino': campos(cargo, ('id', 'nome', 'salario_base', 'ativo')),
        'plano': campos(pessoa.enquadramento_carreira, (
            'id', 'importacao_id', 'familia', 'nivel', 'cargo_proposto',
            'salario_base_alvo', 'complemento_funcao_alvo', 'total_alvo', 'decisao')),
        'faixa': campos(previa['faixa_destino'], (
            'id', 'importacao_id', 'familia', 'nivel', 'cargo_proposto',
            'salario_nivel', 'equivalente_mensal', 'complemento_funcao', 'total_alvo')),
        'previa': {k: v for k, v in previa.items() if k != 'faixa_destino'},
        'ultimo_registro': ultimo,
    }
    return hashlib.sha256(json.dumps(dados, sort_keys=True, default=str).encode()).hexdigest()


def _bloquear_dependencias(pessoa, cargo_id):
    """Mesma ordem para evitar inversão de locks em promoções simultâneas."""
    cargos = {c.id: c for c in Cargo.query.filter(
        Cargo.id.in_({cargo_id, pessoa.cargo_id} - {None}))
        .order_by(Cargo.id).populate_existing().with_for_update().all()}
    (PlanoCarreiraEnquadramento.query.filter_by(funcionario_id=pessoa.id)
     .populate_existing().with_for_update().all())
    vinculos = (PlanoCarreiraCargoVinculo.query.filter_by(cargo_id=cargo_id)
                .populate_existing().with_for_update().all())
    if vinculos:
        (PlanoCarreiraFaixa.query.filter(PlanoCarreiraFaixa.id.in_(
            [v.faixa_id for v in vinculos])).order_by(PlanoCarreiraFaixa.id)
         .populate_existing().with_for_update().all())
    return cargos.get(cargo_id)


@rh_bp.route('/funcionarios/<int:id>/promover', methods=['GET', 'POST'])
@login_required
@owner_required
def promover_funcionario(id):
    pessoa = Funcionario.query.get_or_404(id)
    if not pessoa.ativo:
        flash('Somente funcionários ativos podem ser promovidos.', 'warning')
        return redirect(url_for('rh.carreira_funcionario', id=id))
    valores = {'cargo_id': '', 'data_efetiva': hoje().isoformat(), 'observacao': ''}
    previa, confirmacao, erro = None, None, None
    if request.method == 'POST':
        etapa = request.form.get('etapa', 'revisar')
        try:
            if etapa == 'confirmar':
                try:
                    dados = _assinador().loads(request.form.get('confirmacao', ''),
                                               max_age=_VALIDADE_PREVIA)
                except BadData:
                    raise ValueError('A revisão expirou ou é inválida. Revise a promoção novamente.') from None
                if dados.get('pessoa_id') != id or dados.get('actor_id') != current_user.id:
                    raise ValueError('Esta revisão pertence a outra pessoa ou sessão. Revise novamente.')
                valores = dados['valores']
                # Uma segunda confirmação espera a primeira e relê o cadastro;
                # sua assinatura antiga não pode gerar outra movimentação.
                db.session.expire_all()
                pessoa = (Funcionario.query.filter_by(id=id).populate_existing()
                          .with_for_update().first_or_404())
                cargo = _bloquear_dependencias(pessoa, int(valores['cargo_id']))
            else:
                if etapa not in ('revisar', 'editar'):
                    raise ValueError('Escolha revisar ou confirmar a promoção.')
                valores = {k: (request.form.get(k) or '').strip() for k in valores}
                cargo = db.session.get(Cargo, int(valores['cargo_id'])) if valores['cargo_id'].isdigit() else None
            try:
                data = date.fromisoformat(valores['data_efetiva'])
            except ValueError:
                raise ValueError('Informe uma data válida para a promoção.') from None
            if etapa != 'editar':
                previa = rh_promocao.prever(pessoa, cargo, data, observacao=valores['observacao'])
                estado = _estado(pessoa, cargo, previa)
                if etapa == 'confirmar':
                    if estado != dados['estado']:
                        raise ValueError('O cadastro ou os valores mudaram desde a revisão. Revise a promoção novamente.')
                    rh_promocao.aplicar(pessoa, cargo, data, actor_id=current_user.id,
                                       observacao=valores['observacao'])
                    nome = pessoa.nome
                    db.session.commit()
                    flash(f'Promoção de {nome} registrada. Cargo e histórico atualizados.', 'success')
                    return redirect(url_for('rh.equipe', q=nome, _anchor=f'pessoa-{id}'))
                confirmacao = _assinador().dumps({
                    'pessoa_id': id, 'actor_id': current_user.id,
                    'valores': valores, 'estado': estado,
                })
        except ValueError as exc:
            db.session.rollback()
            erro = str(exc)
            previa, confirmacao = None, None
        except DBAPIError as exc:
            db.session.rollback()
            # Importação do plano/unificação também atualizam estes vínculos.
            # PostgreSQL aborta o conflito; somente esses casos são repetíveis.
            codigo = getattr(exc.orig, 'sqlstate', None) or getattr(exc.orig, 'pgcode', None)
            if codigo not in ('40P01', '40001'):
                raise
            erro = ('Outro cadastro foi atualizado ao mesmo tempo. '
                    'Revise e confirme a promoção novamente.')
            previa, confirmacao = None, None
    return render_template('rh/promover_funcionario.html', pessoa=pessoa,
                           atual=rh_movimentacao.snapshot(pessoa),
                           cargos=rh_promocao.cargos_disponiveis(pessoa),
                           valores=valores, previa=previa, confirmacao=confirmacao,
                           erro=erro, hoje=hoje()), 400 if erro else 200
