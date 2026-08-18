import difflib
import io
import os
import zipfile

from flask import (
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required
from sqlalchemy import func

from app.blueprints.receitas import receitas_bp
from app.decorators import admin_required, owner_required
from app.extensions import db
from app.models import (
    Atribuicao,
    MassaBase,
    MassaBaseItem,
    MateriaPrima,
    Produto,
    ProdutoItem,
    Receita,
    ReceitaEtapa,
    ReceitaIngrediente,
)
from app.services.custos import calcular_custos_produtos, calcular_custos_receitas
from app.utils import (
    SUB_RECEITA_TIPOS,
    agora,
    dividir_etapas_preparo,
    parse_float_br,
)


@receitas_bp.route('/<int:id>')
@login_required
def ficha(id):
    receita = Receita.query.get_or_404(id)

    # Funcionário só acessa fichas atribuídas
    if not current_user.is_admin():
        atribuida = Atribuicao.query.filter_by(
            receita_id=id, usuario_id=current_user.id
        ).first()
        if not atribuida:
            abort(403)

    mp_dict = {mp.nome: mp for mp in MateriaPrima.query.all()}

    resultado = calcular_custos_receitas()

    # Lista de usuarios pra dropdown "Atribuir" (admin so ve)
    from app.models import Usuario
    funcionarios = []
    receitas_retorno = []
    if current_user.is_admin():
        funcionarios = (Usuario.query
                        .filter(Usuario.papel != 'admin')
                        .order_by(Usuario.nome)
                        .all())
        # Dropdown "Receita de retorno" (devolucao loja->industria): qualquer
        # receita ativa menos a propria.
        receitas_retorno = (Receita.query
                            .filter(Receita.arquivada_em.is_(None),
                                    Receita.id != receita.id)
                            .order_by(Receita.nome).all())

    # Galeria de fotos extras do site (26/07/2026) — a capa continua sendo
    # `imagem_dropbox_url`; estas sao as SEGUINTES.
    from app.blueprints.main.routes import GALERIA_MAX_FOTOS
    from app.models import CatalogoFoto
    galeria_fotos = (CatalogoFoto.query
                     .filter_by(kind='receita', item_id=receita.id)
                     .order_by(CatalogoFoto.ordem.asc(),
                               CatalogoFoto.id.asc()).all())
    return render_template('receitas/ficha.html', receita=receita, mp_dict=mp_dict,
                           funcionarios=funcionarios,
                           receitas_retorno=receitas_retorno,
                           etapas_preparo=dividir_etapas_preparo(receita.modo_preparo),
                           receita_custos=resultado['custos'],
                           receita_pesos=resultado['pesos'],
                           carga_impostos=_carga_impostos_venda(),
                           galeria_fotos=galeria_fotos,
                           galeria_max=GALERIA_MAX_FOTOS)


def _carga_impostos_venda():
    """Fração do preço que vira imposto (PIS/COFINS/ICMS) — o resumo de
    rentabilidade da ficha mostra margem LÍQUIDA (fonte única em
    app/services/impostos.py)."""
    from app.services import impostos
    return impostos.carga_venda()


@receitas_bp.route('/padeiro')
@login_required
def padeiro_lista():
    receitas = (Receita.query.filter(Receita.arquivada_em.is_(None))
                .order_by(Receita.categoria, Receita.nome).all())
    categorias = {}
    for r in receitas:
        cat = r.categoria or 'Outros'
        categorias.setdefault(cat, []).append(r)
    arquivadas = (Receita.query.filter(Receita.arquivada_em.isnot(None))
                  .order_by(Receita.nome).all())
    return render_template('receitas/padeiro_lista.html', categorias=categorias,
                           arquivadas=arquivadas)


@receitas_bp.route('/familias', methods=['GET', 'POST'])
@login_required
@admin_required
def familias():
    """Tela bulk pra atribuir Receita.familia em lote.

    GET: lista todas as receitas com dropdown de familia, agrupadas por
    categoria. Botoes "Aplicar X a todos da categoria" pra rapido.
    POST: salva todas as familias enviadas (input name=`familia_<id>`).
    Familia vazia = limpa (NULL).
    """
    if request.method == 'POST':
        atualizados = 0
        permitidos = {'viennoiserie', 'pao_sourdough', 'fornada_especial'}
        for key, val in request.form.items():
            if not key.startswith('familia_'):
                continue
            try:
                rid = int(key[len('familia_'):])
            except ValueError:
                continue
            r = Receita.query.get(rid)
            if not r:
                continue
            v = (val or '').strip().lower() or None
            nova = v if v in permitidos else None
            if r.familia != nova:
                r.familia = nova
                atualizados += 1
        if atualizados:
            db.session.commit()
            flash(f'{atualizados} receita(s) atualizada(s).', 'success')
        else:
            flash('Nenhuma mudança.', 'info')
        return redirect(url_for('receitas.familias'))

    receitas = (Receita.query.filter(Receita.arquivada_em.is_(None))
                .order_by(Receita.categoria, Receita.nome).all())
    categorias = {}
    for r in receitas:
        cat = r.categoria or 'Outros'
        categorias.setdefault(cat, []).append(r)
    return render_template('receitas/familias.html', categorias=categorias)


@receitas_bp.route('/amassadeira', methods=['GET', 'POST'])
@login_required
@admin_required
def amassadeira():
    """Configura a capacidade da amassadeira POR CATEGORIA (mais rapido que
    receita a receita).

    GET: lista as categorias com a capacidade atual (ou 'misto' se as receitas
    da categoria divergem). POST: aplica a capacidade informada a TODAS as
    receitas da categoria. Campo vazio = nao mexe. 0 = a categoria NAO passa
    pela amassadeira (o plano mostra unidades, nao fornadas).

    O campo por receita na ficha continua valendo como override pontual.
    """
    if request.method == 'POST':
        atualizados = 0
        i = 0
        while f'categoria_{i}' in request.form:
            cat = request.form.get(f'categoria_{i}')
            cap_raw = (request.form.get(f'cap_{i}') or '').strip()
            lead_raw = (request.form.get(f'lead_{i}') or '').strip()
            i += 1
            cap = lead = None
            if cap_raw != '':
                try:
                    cap = max(0, min(int(cap_raw), 1000000))
                except (TypeError, ValueError):
                    cap = None
            if lead_raw != '':
                try:
                    lead = max(0, min(int(lead_raw), 14))
                except (TypeError, ValueError):
                    lead = None
            if cap is None and lead is None:
                continue   # ambos em branco = nao altera essa categoria
            q = Receita.query.filter(Receita.arquivada_em.is_(None))
            if cat:
                q = q.filter(Receita.categoria == cat)
            else:
                q = q.filter((Receita.categoria.is_(None))
                             | (Receita.categoria == ''))
            for r in q.all():
                mudou = False
                if cap is not None and r.capacidade_amassadeira_g != cap:
                    r.capacidade_amassadeira_g = cap
                    mudou = True
                if lead is not None and r.dias_producao != lead:
                    r.dias_producao = lead
                    mudou = True
                if mudou:
                    atualizados += 1
        if atualizados:
            db.session.commit()
            flash(f'{atualizados} receita(s) atualizada(s).', 'success')
        else:
            flash('Nenhuma mudança.', 'info')
        return redirect(url_for('receitas.amassadeira'))

    receitas = (Receita.query.filter(Receita.arquivada_em.is_(None))
                .order_by(Receita.categoria, Receita.nome).all())
    grupos = {}
    for r in receitas:
        grupos.setdefault(r.categoria or '', []).append(r)

    categorias = []
    for cat in sorted(grupos, key=lambda c: (c == '', c.lower())):
        recs = grupos[cat]
        caps = sorted({int(r.capacidade_amassadeira_g or 0) for r in recs})
        leads = sorted({int(r.dias_producao or 0) for r in recs})
        categorias.append({
            'nome': cat,
            'label': cat or '(sem categoria)',
            'qtd': len(recs),
            'atual': caps[0] if len(caps) == 1 else None,
            'misto': len(caps) > 1,
            'lead_atual': leads[0] if len(leads) == 1 else None,
            'lead_misto': len(leads) > 1,
            'nomes': [r.nome for r in recs],
        })
    # quantas receitas de cada categoria ja tem etapas de producao cadastradas
    # (pro botao "Aplicar etapas padrao" mostrar o estado atual).
    com_etapas = dict(
        db.session.query(Receita.categoria, func.count(func.distinct(Receita.id)))
        .join(ReceitaEtapa, ReceitaEtapa.receita_id == Receita.id)
        .filter(Receita.arquivada_em.is_(None))
        .group_by(Receita.categoria).all())
    for c in categorias:
        c['com_etapas'] = com_etapas.get(c['nome'] or None, 0) or com_etapas.get(c['nome'], 0)

    return render_template('receitas/amassadeira.html', categorias=categorias)


@receitas_bp.route('/amassadeira/etapas-padrao', methods=['POST'])
@login_required
@admin_required
def amassadeira_etapas_padrao():
    """Aplica as etapas de producao padrao (pesquisadas) a uma categoria.
    Substitui as etapas existentes das receitas da categoria e preenche o
    modo_preparo quando vazio. O dono ajusta receita a receita depois."""
    from app.services.producao import seed_etapas_categoria

    cat = request.form.get('categoria')
    if cat is None:
        abort(400)
    n = seed_etapas_categoria(cat or '')
    flash('Etapas padrão aplicadas a %d receita(s) de "%s".'
          % (n, cat or '(sem categoria)'), 'success')
    return redirect(url_for('receitas.amassadeira'))


# Tipo de trabalho da etapa -> (equipamento, ativa). UM só campo no editor:
#  - padeiro:     mão de obra (a pessoa trabalhando) — ocupa o padeiro.
#  - amassadeira/forno: MÁQUINA trabalha sozinha — ocupa o equipamento, padeiro
#    livre pra adiantar outra receita (correção do dono: amassar não prende a
#    pessoa no pé da amassadeira).
#  - camara_fria/descanso: fermentação/descanso passivo — não ocupa ninguém.
#  - congelar: passo FINAL (freezer) — o produto fica pronto e congelado; não é
#    fermentação (não vira marcador de câmara fria nem antecipa a produção).
# Parse/salvamento de etapas centralizados em app/services/etapas_receita.py
# (14/07/2026) — a ficha do padeiro (/padeiro/fichas) grava as MESMAS etapas;
# manter dois parsers divergiria. Os nomes locais viram aliases pra não mexer
# em todos os call sites deste arquivo.
from app.services.etapas_receita import (  # noqa: E402
    de_tuplas as _etapas_de_tuplas,
)
from app.services.etapas_receita import (
    parse_etapas_form as _parse_etapas_form,
)
from app.services.etapas_receita import (
    recurso_de_etapa as _recurso_de_etapa,
)
from app.services.etapas_receita import (
    set_etapas as _set_etapas,
)


@receitas_bp.route('/<int:id>/etapas', methods=['GET', 'POST'])
@login_required
@admin_required
def etapas(id):
    """Editor manual das etapas de produção (fluxograma/Gantt) de uma receita:
    cada etapa tem nome, duração e o tipo de trabalho (padeiro / máquina /
    descanso). POST salva a lista inteira (substitui, na ordem das linhas — o
    arrastar reordena). "padrão da categoria" preenche ESTA com o modelo
    pesquisado; "aplicar à categoria" copia ESTAS etapas pra todos os produtos
    da categoria."""
    from app.constants import etapas_padrao_categoria
    from app.services.producao import _fmt_dur

    receita = Receita.query.get_or_404(id)

    if request.method == 'POST':
        acao = request.form.get('acao')

        if acao == 'padrao':
            # preenche SÓ esta receita com o padrão (pesquisado) da categoria.
            _set_etapas(receita.id,
                        _etapas_de_tuplas(etapas_padrao_categoria(receita.categoria)))
            db.session.commit()
            flash('Etapas preenchidas com o padrão da categoria. Ajuste e salve.',
                  'info')
            return redirect(url_for('receitas.etapas', id=receita.id))

        if acao == 'aplicar_categoria':
            # Aplica ESTAS etapas (as da tela) a TODOS os produtos ativos da
            # mesma categoria (inclui esta). Sobrescreve as etapas de cada um.
            cat = receita.categoria
            if not cat:
                flash('Esta receita não tem categoria — não dá pra aplicar à '
                      'categoria.', 'warning')
                return redirect(url_for('receitas.etapas', id=receita.id))
            etapas_form = _parse_etapas_form(request.form)
            alvos = (Receita.query
                     .filter(Receita.categoria == cat,
                             Receita.arquivada_em.is_(None)).all())
            for r in alvos:
                _set_etapas(r.id, etapas_form)
            db.session.commit()
            flash(f'{len(etapas_form)} etapa(s) aplicadas a {len(alvos)} '
                  f'produto(s) da categoria "{cat}".', 'success')
            return redirect(url_for('receitas.etapas', id=receita.id))

        # Salvar normal: só esta receita.
        etapas_form = _parse_etapas_form(request.form)
        _set_etapas(receita.id, etapas_form)
        db.session.commit()
        flash(f'{len(etapas_form)} etapa(s) salva(s) para "{receita.nome}".',
              'success')
        return redirect(url_for('receitas.etapas', id=receita.id))

    etapas_atuais = (ReceitaEtapa.query.filter_by(receita_id=receita.id)
                     .order_by(ReceitaEtapa.ordem).all())
    n_categoria = 0
    if receita.categoria:
        n_categoria = (Receita.query
                       .filter(Receita.categoria == receita.categoria,
                               Receita.arquivada_em.is_(None)).count())
    return render_template('receitas/etapas.html', receita=receita,
                           etapas=etapas_atuais, fmt_dur=_fmt_dur,
                           recurso_de=_recurso_de_etapa, n_categoria=n_categoria)


@receitas_bp.route('/massa-base', methods=['GET', 'POST'])
@login_required
@admin_required
def massa_base_lista():
    """Lista os grupos de massa-base e cria novos. Cada grupo = receitas que
    saem de uma amassada comum (cascata)."""
    from app.services.massa_base import calcular_cascata

    if request.method == 'POST':
        nome = (request.form.get('nome') or '').strip()
        if not nome:
            flash('Dê um nome ao grupo.', 'warning')
            return redirect(url_for('receitas.massa_base_lista'))
        mb = MassaBase(nome=nome)
        db.session.add(mb)
        db.session.commit()
        return redirect(url_for('receitas.massa_base_editar', id=mb.id))

    grupos = []
    for mb in MassaBase.query.order_by(MassaBase.nome).all():
        calc = calcular_cascata(mb)
        grupos.append({
            'mb': mb, 'qtd': len(mb.itens),
            'nomes': [it.receita.nome for it in mb.itens if it.receita],
            'fornadas': calc['fornadas'] if calc else None,
            'base_massa': calc['base_massa'] if calc else 0,
            'avisos': calc['avisos'] if calc else [],
        })
    return render_template('receitas/massa_base_lista.html', grupos=grupos)


@receitas_bp.route('/massa-base/<int:id>', methods=['GET', 'POST'])
@login_required
@admin_required
def massa_base_editar(id):
    """Editor de um grupo: adiciona/remove receitas, arrasta pra ordenar a
    cascata e mostra o cálculo (base, acréscimos, massa, fornadas)."""
    from app.services.massa_base import calcular_cascata

    mb = MassaBase.query.get_or_404(id)

    if request.method == 'POST':
        acao = request.form.get('acao')
        if acao == 'excluir':
            db.session.delete(mb)
            db.session.commit()
            flash('Grupo excluído.', 'info')
            return redirect(url_for('receitas.massa_base_lista'))
        if acao == 'renomear':
            nome = (request.form.get('nome') or '').strip()
            if nome:
                mb.nome = nome
                db.session.commit()
            return redirect(url_for('receitas.massa_base_editar', id=mb.id))
        if acao == 'add':
            rid = request.form.get('receita_id', type=int)
            r = Receita.query.get(rid) if rid else None
            ja = MassaBaseItem.query.filter_by(receita_id=rid).first()
            if r is None:
                flash('Receita não encontrada.', 'warning')
            elif ja is not None:
                flash('Essa receita já está em um grupo de massa-base.', 'warning')
            else:
                prox = max([it.ordem for it in mb.itens], default=-1) + 1
                db.session.add(MassaBaseItem(massa_base_id=mb.id, receita_id=r.id,
                                             ordem=prox))
                db.session.commit()
            return redirect(url_for('receitas.massa_base_editar', id=mb.id))
        if acao == 'remover':
            # a ordem da cascata é calculada sozinha; remoção é por item.
            rid = request.form.get('receita_id', type=int)
            MassaBaseItem.query.filter_by(massa_base_id=mb.id,
                                          receita_id=rid).delete()
            db.session.commit()
            return redirect(url_for('receitas.massa_base_editar', id=mb.id))
        return redirect(url_for('receitas.massa_base_editar', id=mb.id))

    calc = calcular_cascata(mb)
    em_grupo = {row.receita_id for row in MassaBaseItem.query.all()}
    disponiveis = (Receita.query.filter(Receita.arquivada_em.is_(None),
                                        ~Receita.id.in_(em_grupo or {0}))
                   .order_by(Receita.nome).all())
    return render_template('receitas/massa_base_editar.html', mb=mb, calc=calc,
                           disponiveis=disponiveis)


@receitas_bp.route('/<int:id>/padeiro')
@login_required
def padeiro(id):
    receita = Receita.query.get_or_404(id)
    resultado = calcular_custos_receitas()
    return render_template('receitas/padeiro.html', receita=receita,
                           etapas_preparo=dividir_etapas_preparo(receita.modo_preparo),
                           receita_custos=resultado['custos'],
                           receita_pesos=resultado['pesos'])


@receitas_bp.route('/precos.xlsx')
@login_required
@owner_required
def precos_xlsx():
    """Exporta a tabela de precos em XLSX: PRODUTO | CUSTO | PRECO LOJA |
    PRECO SITE | PRECO INTERNO | ATACADO (+ tipo/categoria pra filtrar).
    Mesmas fontes e filtros da tela /receitas/precos. **Owner**."""
    import io

    from flask import send_file

    from app.services.precos_export import gerar_xlsx_precos
    from app.utils import hoje

    dados = gerar_xlsx_precos()
    return send_file(
        io.BytesIO(dados),
        mimetype=('application/vnd.openxmlformats-officedocument'
                  '.spreadsheetml.sheet'),
        as_attachment=True,
        download_name='precos_%s.xlsx' % hoje().isoformat())


@receitas_bp.route('/precos/reajuste/previa', methods=['POST'])
@login_required
@owner_required
def precos_reajuste_previa():
    """Passo 1 do reajuste em massa em REAIS: mostra a tabela atual → novo
    item a item (avulso +valor; cesta +valor + valor×unidades; sem preço =
    intocado) pro dono conferir ANTES de aplicar. **Owner**."""
    from app.services.precos_reajuste import (
        CAMPO_LABEL,
        CAMPOS_REAJUSTE,
        previa_reajuste,
    )

    campo = request.form.get('campo') or 'preco_site'
    if campo not in CAMPOS_REAJUSTE:
        flash('Campo de preço inválido.', 'warning')
        return redirect(url_for('receitas.precos'))
    valor = parse_float_br(request.form.get('valor', ''))
    if not valor or valor <= 0:
        flash('Informe o valor do reajuste em reais (ex: 2,00).', 'warning')
        return redirect(url_for('receitas.precos'))
    previa = previa_reajuste(campo, valor)
    return render_template('receitas/precos_reajuste_previa.html',
                           previa=previa, campo=campo, valor=valor,
                           campo_label=CAMPO_LABEL[campo])


@receitas_bp.route('/precos/reajuste/aplicar', methods=['POST'])
@login_required
@owner_required
def precos_reajuste_aplicar():
    """Passo 2: aplica os aumentos DA PRÉVIA (a coluna Aumento é editável —
    o dono corrige exceções tipo 'Granola 500g é 5x100g técnica, não cesta'
    antes de confirmar). Campos: 'aum|<receita|produto>|<id>' em R$; linha
    zerada/apagada não é alterada. **Owner**."""
    from app.services.precos_reajuste import CAMPO_LABEL, CAMPOS_REAJUSTE
    from app.services.precos_reajuste import aplicar_aumentos as _aplicar

    campo = request.form.get('campo') or ''
    if campo not in CAMPOS_REAJUSTE:
        flash('Parâmetros do reajuste inválidos.', 'warning')
        return redirect(url_for('receitas.precos'))
    aumentos = {}
    for key, bruto in request.form.items():
        if not key.startswith('aum|'):
            continue
        try:
            _, tipo, iid = key.split('|', 2)
            iid = int(iid)
        except (TypeError, ValueError):
            continue
        if tipo not in ('receita', 'produto'):
            continue
        aum = parse_float_br(bruto)
        if aum and aum > 0:
            aumentos[(tipo, iid)] = aum
    if not aumentos:
        flash('Nenhum aumento informado — nada foi alterado.', 'warning')
        return redirect(url_for('receitas.precos'))
    alterados = _aplicar(campo, aumentos)
    db.session.commit()
    flash(f'Reajuste aplicado: {alterados} item(ns) com o preço '
          f'{CAMPO_LABEL[campo]} reajustado.', 'success')
    return redirect(url_for('receitas.precos'))


@receitas_bp.route('/precos', methods=['GET', 'POST'])
@login_required
@owner_required
def precos():
    """Tela bulk pra editar precos de Receitas, Produtos simples e Cestas.

    Owner-only — mexe em dinheiro em massa.

    Receitas: preco_loja, preco_site, preco_venda (atacado/B2B), preco_interno.
    Produtos (simples e cestas): preco_loja, preco_site, preco_atacado, preco_interno.

    Naming de campo distingue tipo: `preco_loja_<id>` = Receita;
    `preco_loja_p<id>` = Produto/Cesta. Item ausente no form NAO eh zerado
    (arquivados ficam de fora da tela e nao podem perder preco silenciosamente).
    """
    if request.method == 'POST':
        atualizados = 0
        for r in Receita.query.all():
            if f'preco_loja_{r.id}' not in request.form:
                continue
            antes = (r.preco_loja, r.preco_site, r.preco_venda, r.preco_interno)
            r.preco_loja = parse_float_br(request.form.get(f'preco_loja_{r.id}', ''))
            r.preco_site = parse_float_br(request.form.get(f'preco_site_{r.id}', ''))
            r.preco_venda = parse_float_br(request.form.get(f'preco_venda_{r.id}', ''))
            r.preco_interno = parse_float_br(
                request.form.get(f'preco_interno_{r.id}', ''))
            if antes != (r.preco_loja, r.preco_site, r.preco_venda, r.preco_interno):
                atualizados += 1
        for p in Produto.query.filter_by(ativo=True).all():
            if f'preco_loja_p{p.id}' not in request.form:
                continue
            antes = (p.preco_loja, p.preco_site, p.preco_atacado, p.preco_interno)
            p.preco_loja = parse_float_br(request.form.get(f'preco_loja_p{p.id}', ''))
            p.preco_site = parse_float_br(request.form.get(f'preco_site_p{p.id}', ''))
            p.preco_atacado = parse_float_br(
                request.form.get(f'preco_atacado_p{p.id}', ''))
            p.preco_interno = parse_float_br(
                request.form.get(f'preco_interno_p{p.id}', ''))
            if antes != (p.preco_loja, p.preco_site, p.preco_atacado, p.preco_interno):
                atualizados += 1
        if atualizados:
            db.session.commit()
            flash(f'{atualizados} item(ns) com preço atualizado.', 'success')
        else:
            flash('Nenhuma mudança.', 'info')
        return redirect(url_for('receitas.precos'))

    # Custo unitario (referencia read-only pra precificar). Indexado por nome
    # — Receita.nome e Produto.nome sao unique. Anexa `custo_unit` transiente
    # em cada objeto pra simplificar o template.
    res_custos = calcular_custos_receitas()
    custos_receita = res_custos['custos']  # {nome: custo_unitario}
    custos_produto = calcular_custos_produtos(
        custos_receita, res_custos['mp_info'])  # {nome: custo}

    receitas = (Receita.query.filter(Receita.arquivada_em.is_(None))
                .order_by(Receita.categoria, Receita.nome).all())
    categorias = {}
    for r in receitas:
        r.custo_unit = custos_receita.get(r.nome)
        cat = r.categoria or 'Outros'
        categorias.setdefault(cat, []).append(r)

    produtos = (Produto.query.filter_by(ativo=True)
                .order_by(Produto.categoria, Produto.nome).all())
    cestas = []
    simples = []
    for p in produtos:
        p.custo_unit = custos_produto.get(p.nome)
        (cestas if p.itens else simples).append(p)
    categorias_produtos = {}
    for p in simples:
        cat = p.categoria or 'Outros'
        categorias_produtos.setdefault(cat, []).append(p)

    return render_template('receitas/precos.html',
                           categorias=categorias,
                           categorias_produtos=categorias_produtos,
                           cestas=cestas)


_PRECOS_CAMPOS = {
    'receita': {'preco_loja', 'preco_site', 'preco_venda', 'preco_interno'},
    'produto': {'preco_loja', 'preco_site', 'preco_atacado', 'preco_interno'},
}


@receitas_bp.route('/precos/salvar-campo', methods=['POST'])
@login_required
@owner_required
def precos_salvar_campo():
    """Auto-save de UM campo de preço (AJAX). JSON: {tipo, id, campo, valor}.

    Usado pela tela de preços em massa pra salvar assim que o usuário sai do
    campo — sem depender do botão "Salvar todos". `valor` vazio => NULL.
    Owner-only (dinheiro), mesmo gate da tela.
    """
    dados = request.get_json(silent=True) or {}
    tipo = dados.get('tipo')
    campo = dados.get('campo')
    if tipo not in _PRECOS_CAMPOS or campo not in _PRECOS_CAMPOS[tipo]:
        return jsonify(ok=False, erro='campo inválido'), 400
    try:
        obj_id = int(dados.get('id'))
    except (TypeError, ValueError):
        return jsonify(ok=False, erro='id inválido'), 400

    obj = (Receita if tipo == 'receita' else Produto).query.get(obj_id)
    if not obj:
        return jsonify(ok=False, erro='item não encontrado'), 404

    raw = dados.get('valor')
    if raw is None or str(raw).strip() == '':
        setattr(obj, campo, None)
        valor_fmt = None
    else:
        val = parse_float_br(str(raw))
        if val is None:
            return jsonify(ok=False, erro='valor inválido'), 400
        if val < 0 or val > 9999:
            return jsonify(ok=False, erro='fora da faixa (0 a 9999)'), 400
        setattr(obj, campo, val)
        valor_fmt = f'{val:.2f}'
    db.session.commit()
    return jsonify(ok=True, valor=valor_fmt)


@receitas_bp.route('/reaproveitavel', methods=['GET', 'POST'])
@login_required
@admin_required
def reaproveitavel():
    """Tela bulk pra marcar Receita.reaproveitavel e Produto.reaproveitavel.

    Item reaproveitavel: desperdicio com motivo='validade' nao baixa estoque
    (vira outra coisa — ex: croissant vencido vira croissant amande)."""
    if request.method == 'POST':
        atualizados_r = 0
        atualizados_p = 0
        marcados_r = {int(k[len('reap_r_'):]) for k in request.form.keys()
                      if k.startswith('reap_r_')}
        marcados_p = {int(k[len('reap_p_'):]) for k in request.form.keys()
                      if k.startswith('reap_p_')}
        # Checkbox desmarcado nao vem no form — arquivada (fora da tela)
        # nao pode ser "desmarcada" por ausencia.
        for r in Receita.query.filter(Receita.arquivada_em.is_(None)).all():
            novo = r.id in marcados_r
            if bool(r.reaproveitavel) != novo:
                r.reaproveitavel = novo
                atualizados_r += 1
        for p in Produto.query.all():
            novo = p.id in marcados_p
            if bool(p.reaproveitavel) != novo:
                p.reaproveitavel = novo
                atualizados_p += 1
        if atualizados_r or atualizados_p:
            db.session.commit()
            flash(f'{atualizados_r} receita(s) + {atualizados_p} produto(s) atualizados.',
                  'success')
        else:
            flash('Nenhuma mudança.', 'info')
        return redirect(url_for('receitas.reaproveitavel'))

    receitas = (Receita.query.filter(Receita.arquivada_em.is_(None))
                .order_by(Receita.categoria, Receita.nome).all())
    produtos = Produto.query.order_by(Produto.categoria, Produto.nome).all()
    receitas_por_cat = {}
    for r in receitas:
        cat = r.categoria or 'Outros'
        receitas_por_cat.setdefault(cat, []).append(r)
    produtos_por_cat = {}
    for p in produtos:
        cat = p.categoria or 'Outros'
        produtos_por_cat.setdefault(cat, []).append(p)
    return render_template('receitas/reaproveitavel.html',
                           receitas_por_cat=receitas_por_cat,
                           produtos_por_cat=produtos_por_cat)


@receitas_bp.route('/imagens/upload', methods=['GET', 'POST'])
@login_required
@admin_required
def imagens_upload():
    """Upload em massa de fotos de receita via .zip.

    Cada arquivo .jpg/.png/.webp no zip eh casado contra Receita.nome
    (exato case-insensitive, fallback fuzzy via difflib). Casou -> popula
    imagem_blob + imagem_mimetype. Nao casou -> aparece no relatorio.
    """
    if request.method == 'GET':
        return render_template('receitas/imagens_upload.html')

    arquivo = request.files.get('zipfile')
    if not arquivo or not arquivo.filename:
        flash('Selecione um arquivo .zip.', 'warning')
        return redirect(url_for('receitas.imagens_upload'))

    EXT_OK = {'.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
              '.png': 'image/png', '.webp': 'image/webp'}
    MAX_IMG = 5 * 1024 * 1024  # 5 MB por imagem

    receitas = Receita.query.order_by(Receita.nome).all()
    por_nome_lower = {r.nome.lower(): r for r in receitas}
    nomes_lower = list(por_nome_lower.keys())

    casados = []        # [(nome_arquivo, receita)]
    nao_casados = []    # [(nome_arquivo, motivo)]
    atualizadas = 0

    try:
        bruto = arquivo.read()
        zf = zipfile.ZipFile(io.BytesIO(bruto))
    except zipfile.BadZipFile:
        flash('Arquivo invalido — nao parece ser um .zip.', 'danger')
        return redirect(url_for('receitas.imagens_upload'))

    for info in zf.infolist():
        if info.is_dir():
            continue
        nome_base = os.path.basename(info.filename)
        if not nome_base or nome_base.startswith('.') or nome_base.startswith('__'):
            continue  # .DS_Store, __MACOSX/
        raiz, ext = os.path.splitext(nome_base)
        ext = ext.lower()
        if ext not in EXT_OK:
            nao_casados.append((nome_base, f'extensao {ext or "sem"} nao suportada'))
            continue
        if info.file_size > MAX_IMG:
            nao_casados.append((nome_base, f'arquivo > 5 MB ({info.file_size // 1024} KB)'))
            continue

        raiz_l = raiz.lower().strip()
        r = por_nome_lower.get(raiz_l)
        if not r:
            sugest = difflib.get_close_matches(raiz_l, nomes_lower, n=1, cutoff=0.85)
            if sugest:
                r = por_nome_lower[sugest[0]]
        if not r:
            nao_casados.append((nome_base, 'nao casou com nenhuma receita'))
            continue

        with zf.open(info) as f:
            raw_bytes = f.read()
        from app.services import dropbox_storage
        from app.utils import comprimir_imagem
        if dropbox_storage.disponivel():
            try:
                comprimida = comprimir_imagem(raw_bytes)
                path = f'/cardapio/receita/{r.id}.jpg'
                upload_info = dropbox_storage.upload_publico(
                    comprimida, path, mode='overwrite', autorename=False)
                r.imagem_dropbox_url = upload_info['url']
                r.imagem_storage_path = upload_info['storage_path']
                r.imagem_blob = None
                r.imagem_mimetype = 'image/jpeg'
            except (ValueError, RuntimeError):
                # Fallback BLOB se Dropbox falhar
                r.imagem_blob = raw_bytes
                r.imagem_mimetype = EXT_OK[ext]
        else:
            r.imagem_blob = raw_bytes
            r.imagem_mimetype = EXT_OK[ext]
        casados.append((nome_base, r))
        atualizadas += 1

    db.session.commit()
    return render_template('receitas/imagens_relatorio.html',
                           casados=casados, nao_casados=nao_casados,
                           atualizadas=atualizadas)


@receitas_bp.route('/modos-preparo')
@login_required
@admin_required
def modos_preparo():
    """Tela em lote pra cadastrar o modo de preparo de cada receita.

    Filtros: pendentes (sem texto), preenchidas (com texto), todas.
    Auto-save por textarea via POST /receitas/modos-preparo/salvar.json.
    """
    filtro = request.args.get('filtro', 'pendentes')
    q = Receita.query
    vazio = db.or_(Receita.modo_preparo.is_(None), Receita.modo_preparo == '')
    if filtro == 'pendentes':
        q = q.filter(vazio)
    elif filtro == 'preenchidas':
        q = q.filter(db.not_(vazio))
    receitas = q.order_by(Receita.categoria, Receita.nome).all()
    total = Receita.query.count()
    preenchidas = Receita.query.filter(db.not_(vazio)).count()
    return render_template('receitas/modos_preparo.html',
                           receitas=receitas, filtro=filtro,
                           total=total, preenchidas=preenchidas)


@receitas_bp.route('/modos-preparo/salvar.json', methods=['POST'])
@login_required
def modos_preparo_salvar():
    if not (current_user.is_admin()
            or current_user.is_owner
            or current_user.is_padeiro()):
        return jsonify({'ok': False, 'erro': 'sem permissao'}), 403
    receita_id = request.form.get('receita_id', type=int)
    if not receita_id:
        return jsonify({'ok': False, 'erro': 'receita_id ausente'}), 400
    receita = Receita.query.get(receita_id)
    if not receita:
        return jsonify({'ok': False, 'erro': 'receita não encontrada'}), 404
    receita.modo_preparo = (request.form.get('texto', '') or '').strip() or None
    db.session.commit()
    return jsonify({'ok': True})


@receitas_bp.route('/<int:id>/salvar', methods=['POST'])
@login_required
def salvar(id):
    receita = Receita.query.get_or_404(id)

    # Funcionário só pode salvar fichas atribuídas
    if not current_user.is_admin():
        atribuida = Atribuicao.query.filter_by(
            receita_id=id, usuario_id=current_user.id
        ).first()
        if not atribuida:
            abort(403)

    nome_antigo = receita.nome
    receita.nome = request.form.get('nome', receita.nome).strip() or nome_antigo
    if receita.nome != nome_antigo:
        # Rename: sincroniza os nomes-fallback que apontam pra esta receita.
        # A FK (sub_receita_id / ProdutoItem.receita_id) e quem manda, mas o
        # nome gravado desatualizado zerava custo de cesta/sub-receita em
        # silencio (caso iogurte 03/07/2026) e, na tela da cesta, um Salvar
        # com o nome velho no input orfanava o vinculo.
        ReceitaIngrediente.query.filter_by(sub_receita_id=receita.id) \
            .update({'ingrediente_nome': receita.nome})
        ProdutoItem.query.filter_by(receita_id=receita.id) \
            .update({'item_nome': receita.nome})
    receita.categoria = request.form.get('categoria', '').strip() or None
    fam = (request.form.get('familia') or '').strip().lower() or None
    if fam in ('viennoiserie', 'pao_sourdough', 'fornada_especial'):
        receita.familia = fam
    elif fam is None or fam == '':
        receita.familia = None
    receita.preco_venda = parse_float_br(request.form.get('preco_venda', ''))
    receita.preco_loja = parse_float_br(request.form.get('preco_loja', ''))
    receita.preco_site = parse_float_br(request.form.get('preco_site', ''))
    receita.preco_interno = parse_float_br(
        request.form.get('preco_interno', ''))
    # Rendimento = unidades que UMA fornada rende (divisor de custo unitario e
    # base da producao via qtd_alvo/rendimento). Caso especial: receita MONTADA
    # (so MP g/un, sem % de padeiro) lancada por "Quantidade de Produtos" — ali
    # cada linha ja e "por unidade" e a Quantidade e so preview de quantas
    # produzir, entao a fornada rende 1. Salvar a Quantidade como rendimento
    # dividiria o custo da unidade pela propria quantidade (custo -> ~0) e
    # furaria a producao. Por isso forcamos 1 nesse caso.
    _modo_lanc = (request.form.get('modo_lancamento') or 'farinha').strip()
    _tem_pct = any((t or 'mp') == 'mp'
                   for t in request.form.getlist('ingrediente_tipo[]'))
    if _modo_lanc == 'quantidade' and not _tem_pct:
        receita.rendimento_qtd = 1
    else:
        receita.rendimento_qtd = parse_float_br(
            request.form.get('rendimento_qtd', ''), default=1)
    receita.rendimento_unidade = request.form.get('rendimento_unidade', 'unidades').strip()
    receita.peso_base = parse_float_br(request.form.get('peso_base', ''), default=1000)
    receita.peso_unitario = parse_float_br(request.form.get('peso_unitario', ''))
    receita.perda_percentual = parse_float_br(request.form.get('perda_percentual', ''), default=0)
    receita.custo_embalagem = parse_float_br(request.form.get('custo_embalagem', ''), default=0)
    # Modo de preparo: a ficha nova manda etapas separadas (1 modulo por
    # etapa); junta com linha em branco — mesmo separador que a leitura usa
    # (dividir_etapas_preparo). Forms antigos/lote seguem mandando o texto
    # inteiro em `modo_preparo`.
    if request.form.get('tem_etapas'):
        etapas = [e.replace('\r\n', '\n').replace('\r', '\n').strip()
                  for e in request.form.getlist('modo_preparo_etapa[]')]
        receita.modo_preparo = '\n\n'.join(e for e in etapas if e) or None
    else:
        receita.modo_preparo = request.form.get('modo_preparo', '').strip() or None
    receita.observacao = request.form.get('observacao', '').strip() or None
    # Descricao do cardapio de atacado (dono 20/07/2026): texto curto e
    # sincero (ingredientes + como e vendido). Vazio = sem descricao no
    # cardapio. Forms antigos sem o campo nao apagam o gravado.
    if 'descricao_atacado' in request.form:
        receita.descricao_atacado = (
            request.form.get('descricao_atacado', '').strip() or None)
    ep = (request.form.get('estado_padrao') or '').strip().lower()
    receita.estado_padrao = ep if ep in ('assado', 'backup') else None
    receita.reaproveitavel = bool(request.form.get('reaproveitavel'))
    receita.sub_na_amassadeira = bool(request.form.get('sub_na_amassadeira'))
    # Estoque fisico nao abate a producao sugerida (balanco/cronograma) —
    # so a producao ja mandada conta. Caso Massa para folhar (dono 19/07/2026).
    receita.estoque_nao_abate = bool(request.form.get('estoque_nao_abate'))
    # Antecedencia maxima do nivelador POR receita (dono 18/08/2026,
    # brioche fresco): vazio = NULL = regra global; invalido nao mexe.
    _ant_raw = (request.form.get('antecedencia_max_dias') or '').strip()
    if _ant_raw == '':
        receita.antecedencia_max_dias = None
    else:
        try:
            receita.antecedencia_max_dias = max(0, min(7, int(_ant_raw)))
        except ValueError:
            pass
    # Sob encomenda D+2 (dono 21/07/2026): no site so vende pra data >= D+2,
    # e produzido pro pedido (nao abate prateleira) e vira producao do padeiro.
    receita.sob_encomenda = bool(request.form.get('sob_encomenda'))
    # Cobranca de sobra POR ITEM no alerta das 20h (01/08/2026, caso
    # croissant tradicional).
    receita.cobra_sobra_diaria = bool(request.form.get('cobra_sobra_diaria'))
    # Receita de retorno (devolucao loja->industria): sobras devolvidas creditam
    # esta receita. Valida existencia e evita auto-referencia; vazio = NULL.
    try:
        ret_id = int(request.form.get('retorno_receita_id') or 0)
    except (TypeError, ValueError):
        ret_id = 0
    if ret_id and ret_id != receita.id and Receita.query.get(ret_id):
        receita.retorno_receita_id = ret_id
    else:
        receita.retorno_receita_id = None
    # Insumo/etapa de producao (ex: Creme de Amendoas) — a loja nao pede direto,
    # entao some da sugestao de pedido semanal. Checkbox guarda o INVERSO pra o
    # default (desmarcado) manter a receita pedivel.
    receita.sugerir_pedido_loja = not bool(request.form.get('nao_pedir_loja'))
    # Fornada especial: vendida só sáb/dom (o forecast não sugere nos outros
    # dias). Ex: Focaccia Gorgonzola.
    receita.fornada_especial = bool(request.form.get('fornada_especial'))
    # Lead time de producao (dias). Vazio/invalido -> 0. Limite defensivo de
    # 0..14 (nada na padaria leva mais que 2 semanas pra ficar pronto).
    try:
        dias_prod = int(request.form.get('dias_producao') or 0)
    except (TypeError, ValueError):
        dias_prod = 0
    receita.dias_producao = max(0, min(dias_prod, 14))
    # Capacidade da amassadeira (g de farinha/batida). 0 = nao usa amassadeira.
    # Vazio/invalido -> 50000 (padrao 50kg). Teto defensivo generoso.
    try:
        cap_amass = int(request.form.get('capacidade_amassadeira_g') or 50000)
    except (TypeError, ValueError):
        cap_amass = 50000
    receita.capacidade_amassadeira_g = max(0, min(cap_amass, 1000000))

    # Padronizacao do pedido (a loja pede em pacotes, nao picado). Vazio -> NULL
    # (sem padronizacao). 0 tambem vira NULL.
    def _int_opt(campo):
        try:
            v = int(request.form.get(campo) or 0)
        except (TypeError, ValueError):
            return None
        return v if v > 0 else None
    receita.lote_pedido = _int_opt('lote_pedido')
    receita.minimo_pedido = _int_opt('minimo_pedido')
    receita.lote_producao = _int_opt('lote_producao')
    # Piso do estoque da industria (freezer): a previsao de producao nunca
    # sugere um alvo menor que este. Vazio/0 -> NULL (sem piso).
    receita.estoque_minimo_industria = _int_opt('estoque_minimo_industria')

    receita.imagem_url = request.form.get('imagem_url', '').strip() or None

    # Atualiza ingredientes
    ReceitaIngrediente.query.filter_by(receita_id=receita.id).delete()

    tipos = request.form.getlist('ingrediente_tipo[]')
    nomes = request.form.getlist('ingrediente_nome[]')
    porcentagens = request.form.getlist('porcentagem[]')
    bases = request.form.getlist('eh_base[]')
    notas = request.form.getlist('nota[]')

    for i in range(len(nomes)):
        nome = nomes[i].strip()
        pct_str = porcentagens[i].replace(',', '.').strip()
        if not nome or not pct_str:
            continue
        tipo = tipos[i] if i < len(tipos) else 'mp'
        # sub-receita ('receita' = quantidade absoluta; 'sub_pct' = % da base):
        # resolve a FK pelo nome agora, pra a baixa de estoque ser confiável
        # (não depender só do backfill por nome).
        sub_id = None
        if tipo in SUB_RECEITA_TIPOS:
            sub = Receita.query.filter(Receita.nome.ilike(nome)).first()
            sub_id = sub.id if sub else None
        ing = ReceitaIngrediente(
            receita_id=receita.id,
            tipo=tipo,
            ingrediente_nome=nome,
            porcentagem=float(pct_str),
            eh_base=(bases[i] == '1') if i < len(bases) else False,
            nota=notas[i].strip() if i < len(notas) else None,
            sub_receita_id=sub_id,
        )
        db.session.add(ing)

    db.session.commit()
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify(success=True)
    flash('Ficha salva com sucesso!', 'success')
    return redirect(url_for('receitas.ficha', id=receita.id))


@receitas_bp.route('/nova', methods=['POST'])
@login_required
@admin_required
def nova():
    receita = Receita(
        nome='Novo Produto',
        categoria='',
        rendimento_qtd=1,
        rendimento_unidade='unidades',
        peso_base=1000,
    )
    db.session.add(receita)
    db.session.commit()
    flash('Novo produto criado!', 'success')
    return redirect(url_for('receitas.ficha', id=receita.id))


@receitas_bp.route('/<int:id>/duplicar', methods=['POST'])
@login_required
@admin_required
def duplicar(id):
    original = Receita.query.get_or_404(id)
    copia = Receita(
        nome=f'Cópia de {original.nome}',
        categoria=original.categoria,
        preco_venda=original.preco_venda,
        preco_loja=original.preco_loja,
        preco_site=original.preco_site,
        rendimento_qtd=original.rendimento_qtd,
        rendimento_unidade=original.rendimento_unidade,
        peso_base=original.peso_base,
        peso_unitario=original.peso_unitario,
        perda_percentual=original.perda_percentual,
        custo_embalagem=original.custo_embalagem,
        modo_preparo=original.modo_preparo,
        descricao_atacado=original.descricao_atacado,
        # Sob encomenda D+2: a cópia de um item sob encomenda também nasce
        # sob encomenda (21/07/2026).
        sob_encomenda=original.sob_encomenda,
        antecedencia_max_dias=original.antecedencia_max_dias,
        cobra_sobra_diaria=original.cobra_sobra_diaria,
    )
    db.session.add(copia)
    db.session.flush()

    for ing in original.ingredientes:
        novo_ing = ReceitaIngrediente(
            receita_id=copia.id,
            tipo=ing.tipo or 'mp',
            ingrediente_nome=ing.ingrediente_nome,
            porcentagem=ing.porcentagem,
            eh_base=ing.eh_base,
            nota=ing.nota,
            # FK manda, nome e so fallback: sem copiar sub_receita_id a
            # copia nascia orfa e dependia do match por nome (quebra apos
            # rename da sub-receita).
            sub_receita_id=ing.sub_receita_id,
        )
        db.session.add(novo_ing)

    db.session.commit()
    flash(f'Receita duplicada: "{copia.nome}"', 'success')
    return redirect(url_for('receitas.ficha', id=copia.id))


def _vinculos_receita(receita):
    """Agrupa tudo que referencia a receita, separando o que tem resolucao
    automatica SEGURA (configuracao: cestas, mapeamentos de PDV, precos,
    atribuicoes, uso como ingrediente) do que e HISTORICO e nunca se apaga
    por aqui (pedidos, vendas, estoque, desperdicio — peso especial).
    Retorna (grupos, pode_excluir)."""
    from app.models import (
        Atribuicao,
        Desperdicio,
        EstoqueLoja,
        EstoqueProducao,
        PedidoItem,
        PlanejamentoItem,
        PrecoLojaReceita,
        VendaB2BItem,
        VendaManualLoja,
        VendaMapa,
    )
    rid = receita.id
    grupos = []

    def _grupo(chave, titulo, resolvivel, descricao, itens, qtd=None):
        if qtd or itens:
            grupos.append({'chave': chave, 'titulo': titulo,
                           'resolvivel': resolvivel, 'descricao': descricao,
                           'qtd': qtd if qtd is not None else len(itens),
                           'itens': itens[:10]})

    # ── Resolviveis (configuracao, nao historico) ──
    itens_cesta = ProdutoItem.query.filter_by(receita_id=rid).all()
    _grupo('cestas', 'Componente de cestas/produtos', True,
           'Remove esta receita da composição das cestas listadas.',
           [{'label': (i.produto.nome if getattr(i, 'produto', None)
                       else f'cesta #{i.produto_id}'),
             'url': url_for('produtos.detalhe', id=i.produto_id)}
            for i in itens_cesta])

    usos = (ReceitaIngrediente.query
            .filter(ReceitaIngrediente.tipo.in_(SUB_RECEITA_TIPOS),
                    ReceitaIngrediente.ingrediente_nome == receita.nome,
                    ReceitaIngrediente.receita_id != rid).all())
    _grupo('ingrediente_em_fichas', 'Usada como ingrediente em outras fichas',
           True,
           'Remove o ingrediente das fichas listadas — a composição e o '
           'custo DELAS mudam.',
           [{'label': (u.receita.nome if u.receita else f'ficha #{u.receita_id}'),
             'url': url_for('receitas.ficha', id=u.receita_id)} for u in usos])

    maps = []
    for m in VendaMapa.query.filter_by(receita_id=rid).all():
        if m.canal == 'seru':
            maps.append({'label': f'Seru: {m.nome_externo}',
                         'url': url_for('pdv.mapeamentos')})
        else:
            maps.append({'label': f'Lote: {m.nome_externo}', 'url': None})
    _grupo('mapeamentos', 'Mapeamentos de PDV/site/loja', True,
           'Desfaz os vínculos — os nomes voltam pra fila de pendentes.',
           maps)

    precos = PrecoLojaReceita.query.filter_by(receita_id=rid).count()
    _grupo('precos_loja', 'Preços por loja', True,
           'Apaga os preços específicos por loja desta receita.',
           [], qtd=precos)

    atribs = Atribuicao.query.filter_by(receita_id=rid).count()
    _grupo('atribuicoes', 'Atribuições a funcionários', True,
           'Apaga as atribuições de preparo desta receita.',
           [], qtd=atribs)

    # ── Historico: NUNCA apagavel por aqui ──
    historicos = (
        ('Pedidos de loja', PedidoItem),
        ('Vendas B2B', VendaB2BItem),
        ('Vendas manuais de loja', VendaManualLoja),
        ('Estoque de produção/congelados', EstoqueProducao),
        ('Estoque de loja', EstoqueLoja),
        ('Registros de desperdício', Desperdicio),
        ('Planos de produção', PlanejamentoItem),
    )
    for titulo, modelo in historicos:
        n = modelo.query.filter_by(receita_id=rid).count()
        _grupo(f'hist_{modelo.__tablename__}', titulo, False,
               'Histórico — não se apaga. Dois caminhos: TRANSFERIR os '
               'vínculos pra outra receita (campo abaixo — pedidos, vendas e '
               'estoque passam a contar lá) e excluir esta; ou ARQUIVAR '
               '(botão abaixo) — ela some das listas e o histórico fica.',
               [], qtd=n)

    pode_excluir = not grupos
    return grupos, pode_excluir


@receitas_bp.route('/<int:id>/vinculos')
@login_required
@admin_required
def vinculos(id):
    """JSON pro modal de exclusão: o que ainda referencia a receita."""
    receita = Receita.query.get_or_404(id)
    grupos, pode = _vinculos_receita(receita)
    return jsonify(grupos=grupos, pode_excluir=pode)


@receitas_bp.route('/<int:id>/vinculos/resolver', methods=['POST'])
@login_required
@admin_required
def vinculos_resolver(id):
    """Resolve UM grupo de vínculos (ação explícita do admin no modal).
    Só grupos de configuração — histórico nunca passa por aqui."""
    from app.models import (
        Atribuicao,
        PrecoLojaReceita,
        VendaMapa,
    )
    receita = Receita.query.get_or_404(id)
    chave = request.form.get('chave') or ''
    if chave == 'cestas':
        ProdutoItem.query.filter_by(receita_id=receita.id).delete()
    elif chave == 'ingrediente_em_fichas':
        ReceitaIngrediente.query.filter(
            ReceitaIngrediente.tipo.in_(SUB_RECEITA_TIPOS),
            ReceitaIngrediente.ingrediente_nome == receita.nome,
            ReceitaIngrediente.receita_id != receita.id).delete()
    elif chave == 'mapeamentos':
        # Volta pra pendente (receita_id NULL) — nao apaga o nome mapeado.
        for m in VendaMapa.query.filter_by(receita_id=receita.id).all():
            m.receita_id = None
            m.confirmado_em = None
    elif chave == 'precos_loja':
        PrecoLojaReceita.query.filter_by(receita_id=receita.id).delete()
    elif chave == 'atribuicoes':
        Atribuicao.query.filter_by(receita_id=receita.id).delete()
    else:
        return jsonify(erro=f'grupo "{chave}" não tem resolução automática'), 400
    db.session.commit()
    grupos, pode = _vinculos_receita(receita)
    return jsonify(grupos=grupos, pode_excluir=pode)


def _transferir_para_mp(origem, mp):
    """Transfere pra uma MATÉRIA-PRIMA os vínculos que suportam MP — o caso da
    receita que na verdade é insumo COMPRADO (ex: "pão de queijo (saco)").

    Passam a apontar pra MP: pedidos de loja, vendas manuais, desperdício,
    estoque de loja (funde com a linha MP equivalente), cestas e mapeamentos;
    ingrediente em outras fichas vira tipo='mp'. O que NÃO tem coluna de MP
    (planos de produção, vendas B2B, atribuições, preços por loja, estoque de
    produção) FICA na receita e é reportado em `ficaram` — o caminho pra esses
    é ARQUIVAR a receita depois (histórico preservado). 1 commit no fim."""
    from app.models import (
        Atribuicao,
        Desperdicio,
        EstoqueLoja,
        EstoqueProducao,
        MovEstoqueLoja,
        PedidoItem,
        PlanejamentoItem,
        PrecoLojaReceita,
        VendaB2BItem,
        VendaManualLoja,
        VendaMapa,
    )
    movidos = {}

    def _conta(chave, n):
        if n:
            movidos[chave] = movidos.get(chave, 0) + n

    swap = {'receita_id': None, 'materia_prima_id': mp.id}

    # FKs simples com coluna de MP: histórico intacto, só muda o alvo.
    for chave, modelo in (('pedidos', PedidoItem),
                          ('vendas_manuais', VendaManualLoja),
                          ('desperdicio', Desperdicio)):
        _conta(chave, modelo.query.filter_by(receita_id=origem.id)
               .update(dict(swap), synchronize_session=False))

    # Cestas: vira componente de MP (FK + tipo + nome humano-legível).
    _conta('cestas', ProdutoItem.query.filter_by(receita_id=origem.id)
           .update({**swap, 'tipo': 'mp', 'item_nome': mp.nome},
                   synchronize_session=False))

    # Ingrediente em outras fichas: vira ingrediente de MP (por nome; o FK
    # sub_receita_id é limpo — MP resolve por nome no custeio da ficha).
    _conta('ingrediente_em_fichas', ReceitaIngrediente.query
           .filter(ReceitaIngrediente.tipo.in_(SUB_RECEITA_TIPOS),
                   db.or_(ReceitaIngrediente.ingrediente_nome == origem.nome,
                          ReceitaIngrediente.sub_receita_id == origem.id),
                   ReceitaIngrediente.receita_id != origem.id)
           .update({'tipo': 'mp', 'ingrediente_nome': mp.nome,
                    'sub_receita_id': None}, synchronize_session=False))

    # Mapeamentos PDV/site/loja (mantém confirmação e fator).
    _conta('mapeamentos', VendaMapa.query.filter_by(receita_id=origem.id)
           .update(dict(swap), synchronize_session=False))

    # Estoque de loja: funde com a linha MP equivalente (mesma loja/estado).
    # Movimentações reapontadas ANTES de apagar a linha da origem.
    from app.services.estoque_helpers import serializar_lojas
    _els_fusao = EstoqueLoja.query.filter_by(receita_id=origem.id).all()
    serializar_lojas({e.loja_id for e in _els_fusao})  # lock ascendente multi-loja
    for e in _els_fusao:
        alvo = EstoqueLoja.query.filter_by(
            materia_prima_id=mp.id, loja_id=e.loja_id, estado=e.estado).first()
        if alvo:
            alvo.quantidade = (alvo.quantidade or 0) + (e.quantidade or 0)
            MovEstoqueLoja.query.filter_by(estoque_loja_id=e.id).update(
                {'estoque_loja_id': alvo.id}, synchronize_session=False)
            db.session.delete(e)
        else:
            e.receita_id = None
            e.materia_prima_id = mp.id
        _conta('estoque_loja', 1)

    # Sem coluna de MP — fica na receita (arquivar depois preserva histórico).
    ficaram = {}
    for chave, modelo in (('planejamento', PlanejamentoItem),
                          ('vendas_b2b', VendaB2BItem),
                          ('atribuicoes', Atribuicao),
                          ('precos_loja', PrecoLojaReceita),
                          ('estoque_producao', EstoqueProducao)):
        n = modelo.query.filter_by(receita_id=origem.id).count()
        if n:
            ficaram[chave] = n

    db.session.commit()
    current_app.logger.info(
        'vinculos de receita transferidos pra MP: "%s" (#%s) -> "%s" (#%s) '
        'por %s: movidos=%s ficaram=%s',
        origem.nome, origem.id, mp.nome, mp.id,
        current_user.login, movidos, ficaram)
    grupos, pode = _vinculos_receita(origem)
    return jsonify(grupos=grupos, pode_excluir=pode, movidos=movidos,
                   ficaram=ficaram, destino=mp.nome, tipo_destino='mp')


@receitas_bp.route('/<int:id>/vinculos/transferir', methods=['POST'])
@login_required
@admin_required
def vinculos_transferir(id):
    """Transfere TODOS os vínculos da receita pra outra (fusão de duplicata,
    ex: "Molho Pesto 100g" -> "Molho Pesto"). Histórico não se apaga — se
    REAPONTA: pedidos/vendas/desperdício mudam a FK; estoque FUNDE com a
    linha equivalente do destino (mesma loja/estado) somando quantidades e
    reapontando as movimentações pra linha que fica — nada se perde.
    Estoque/dinheiro têm peso especial: tudo explícito, 1 commit no fim."""
    from sqlalchemy import func

    from app.models import (
        Atribuicao,
        Desperdicio,
        EstoqueLoja,
        EstoqueProducao,
        MateriaPrima,
        MovEstoqueLoja,
        MovEstoqueProducao,
        PedidoItem,
        PlanejamentoItem,
        PrecoLojaReceita,
        VendaB2BItem,
        VendaManualLoja,
        VendaMapa,
    )
    origem = Receita.query.get_or_404(id)
    nome_destino = (request.form.get('destino') or '').strip()
    tipo_destino = (request.form.get('tipo_destino') or 'receita').strip()
    if tipo_destino not in ('receita', 'mp'):
        return jsonify(erro=f'tipo de destino "{tipo_destino}" inválido'), 400

    if tipo_destino == 'mp':
        mp_destino = (MateriaPrima.query
                      .filter(func.lower(MateriaPrima.nome) == nome_destino.lower())
                      .first()) if nome_destino else None
        if not mp_destino:
            return jsonify(erro=f'matéria-prima "{nome_destino}" não encontrada '
                                '— use o nome exato (o campo autocompleta)'), 400
        return _transferir_para_mp(origem, mp_destino)

    destino = (Receita.query
               .filter(func.lower(Receita.nome) == nome_destino.lower())
               .first()) if nome_destino else None
    if not destino:
        return jsonify(erro=f'receita "{nome_destino}" não encontrada — '
                            'use o nome exato (o campo autocompleta)'), 400
    if destino.id == origem.id:
        return jsonify(erro='o destino é a própria receita'), 400

    movidos = {}

    def _conta(chave, n):
        if n:
            movidos[chave] = movidos.get(chave, 0) + n

    # FKs simples: o registro histórico fica intacto, só muda o alvo.
    for chave, modelo in (('pedidos', PedidoItem),
                          ('vendas_b2b', VendaB2BItem),
                          ('vendas_manuais', VendaManualLoja),
                          ('desperdicio', Desperdicio),
                          ('planejamento', PlanejamentoItem),
                          ('atribuicoes', Atribuicao)):
        _conta(chave, modelo.query.filter_by(receita_id=origem.id)
               .update({'receita_id': destino.id}, synchronize_session=False))

    # Cestas: reaponta a FK e corrige o nome humano-legível.
    _conta('cestas', ProdutoItem.query.filter_by(receita_id=origem.id)
           .update({'receita_id': destino.id, 'item_nome': destino.nome},
                   synchronize_session=False))

    # Uso como ingrediente em outras fichas — vínculo por NOME e/ou por FK
    # (`sub_receita_id`, que o MRP/BOM usa e que bloqueia a exclusão; antes
    # só o nome era atualizado e o FK ficava preso na origem).
    _conta('ingrediente_em_fichas', ReceitaIngrediente.query
           .filter(ReceitaIngrediente.tipo.in_(SUB_RECEITA_TIPOS),
                   db.or_(ReceitaIngrediente.ingrediente_nome == origem.nome,
                          ReceitaIngrediente.sub_receita_id == origem.id),
                   ReceitaIngrediente.receita_id != origem.id)
           .update({'ingrediente_nome': destino.nome,
                    'sub_receita_id': destino.id},
                   synchronize_session=False))

    # Mapeamentos de PDV/site/loja (mantém confirmação e fator).
    _conta('mapeamentos', VendaMapa.query.filter_by(receita_id=origem.id)
           .update({'receita_id': destino.id}, synchronize_session=False))

    # Preços por loja: unique (loja, receita) — se o destino já tem preço
    # naquela loja, o preço dele prevalece e o da origem é descartado.
    for p in PrecoLojaReceita.query.filter_by(receita_id=origem.id).all():
        ja_tem = PrecoLojaReceita.query.filter_by(
            receita_id=destino.id, loja_id=p.loja_id).first()
        if ja_tem:
            db.session.delete(p)
        else:
            p.receita_id = destino.id
        _conta('precos_loja', 1)

    # Estoque: funde com a linha equivalente do destino (mesmo estado/loja).
    # As movimentações são reapontadas ANTES de apagar a linha da origem —
    # o histórico de movimento sobrevive inteiro na linha que fica.
    for e in EstoqueProducao.query.filter_by(receita_id=origem.id).all():
        alvo = EstoqueProducao.query.filter_by(
            receita_id=destino.id, estado=e.estado).first()
        if alvo:
            alvo.quantidade = (alvo.quantidade or 0) + (e.quantidade or 0)
            MovEstoqueProducao.query.filter_by(estoque_producao_id=e.id).update(
                {'estoque_producao_id': alvo.id}, synchronize_session=False)
            db.session.delete(e)
        else:
            e.receita_id = destino.id
        _conta('estoque_producao', 1)

    from app.services.estoque_helpers import serializar_lojas
    _els_vinc = EstoqueLoja.query.filter_by(receita_id=origem.id).all()
    serializar_lojas({e.loja_id for e in _els_vinc})  # lock ascendente multi-loja
    for e in _els_vinc:
        alvo = EstoqueLoja.query.filter_by(
            receita_id=destino.id, loja_id=e.loja_id, estado=e.estado).first()
        if alvo:
            alvo.quantidade = (alvo.quantidade or 0) + (e.quantidade or 0)
            MovEstoqueLoja.query.filter_by(estoque_loja_id=e.id).update(
                {'estoque_loja_id': alvo.id}, synchronize_session=False)
            db.session.delete(e)
        else:
            e.receita_id = destino.id
        _conta('estoque_loja', 1)

    db.session.commit()
    current_app.logger.info(
        'vinculos de receita transferidos: "%s" (#%s) -> "%s" (#%s) por %s: %s',
        origem.nome, origem.id, destino.nome, destino.id,
        current_user.login, movidos)
    grupos, pode = _vinculos_receita(origem)
    return jsonify(grupos=grupos, pode_excluir=pode, movidos=movidos,
                   destino=destino.nome)


@receitas_bp.route('/<int:id>/arquivar', methods=['POST'])
@login_required
@admin_required
def arquivar(id):
    """Arquiva/desarquiva. Arquivada = fora das listas e seletores (padeiro,
    datalists, copilot, vendas), historico 100% preservado — e o caminho pra
    receita descontinuada que tem pedidos/vendas/estoque e nao pode ser
    excluida nem faz sentido transferir."""
    receita = Receita.query.get_or_404(id)
    if receita.arquivada_em:
        receita.arquivada_em = None
        receita.arquivada_por_id = None
        db.session.commit()
        flash(f'"{receita.nome}" desarquivada — voltou pras listas.', 'success')
        return redirect(url_for('receitas.ficha', id=id))
    receita.arquivada_em = agora()
    receita.arquivada_por_id = current_user.id
    db.session.commit()
    flash(f'"{receita.nome}" arquivada. O histórico fica intacto; ela só '
          'sai das listas. Dá pra desarquivar na própria ficha.', 'success')
    return redirect(url_for('receitas.padeiro_lista'))


@receitas_bp.route('/<int:id>/excluir', methods=['POST'])
@login_required
@admin_required
def excluir(id):
    from sqlalchemy.exc import IntegrityError
    receita = Receita.query.get_or_404(id)
    nome = receita.nome
    # Delete cru estourava 500 quando a receita era referenciada (pedidos,
    # estoque, produtos/cestas, mapeamentos de PDV) — FKs sem cascade. Aborta
    # de forma limpa com mensagem em vez de 500; o historico fica intacto.
    try:
        # Galeria some junto (CatalogoFoto nao tem FK — ninguem apagaria).
        from app.blueprints.main.routes import apagar_galeria_do_item
        apagar_galeria_do_item('receita', receita.id)
        db.session.delete(receita)
        db.session.commit()
        flash(f'"{nome}" excluído com sucesso!', 'success')
    except IntegrityError:
        db.session.rollback()
        flash(f'Não é possível excluir "{nome}": há pedidos, estoque, produtos '
              f'ou mapeamentos de PDV vinculados a ela. Desvincule-os primeiro '
              f'(ou me peça para arquivar a receita).', 'danger')
    return redirect(url_for('receitas.padeiro_lista'))


@receitas_bp.route('/api/nova-mp', methods=['POST'])
@login_required
@admin_required
def nova_mp():
    """Cria matéria-prima via AJAX (sem sair da ficha técnica)."""
    nome = request.form.get('mp_nome', '').strip()
    custo = request.form.get('mp_custo', '').replace(',', '.').strip()

    if not nome or not custo:
        return jsonify(success=False, error='Preencha nome e custo.')

    if MateriaPrima.query.filter_by(nome=nome).first():
        return jsonify(success=False, error=f'"{nome}" ja existe no banco de MP.')

    try:
        custo_float = float(custo)
    except ValueError:
        return jsonify(success=False, error='Custo invalido.')

    mp = MateriaPrima(nome=nome, unidade='g', custo_por_kg=custo_float)
    db.session.add(mp)
    db.session.commit()

    return jsonify(success=True, nome=nome, custo=custo_float)
