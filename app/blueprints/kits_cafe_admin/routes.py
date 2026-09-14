"""O dono monta os kits; clientes escolhem as datas na loja pública."""
import json
import re
from decimal import Decimal

from flask import abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.blueprints.kits_cafe_admin import kits_cafe_admin_bp
from app.decorators import owner_required
from app.extensions import db
from app.models import KitCafe, KitCafeItem, KitCafeOpcao, KitCafeSuco, Produto, Receita
from app.services import kits_cafe, loja_menu
from app.utils import agora


def _estado(kit):
    itens, erros = kits_cafe.montar(kit)
    sucos = []
    grupos = []
    if not erros:
        sucos = kits_cafe.opcoes_suco(kit)
        grupos = kits_cafe.opcoes_grupos(kit)
    ids_sucos = {suco['id'] for suco in sucos}
    opcionais = {(opcao['kind'], opcao['id'])
                 for grupo in grupos for opcao in grupo['opcoes']}
    return {'kit': kit, 'itens': itens, 'erros': erros,
            'itens_fixos': [item for item in itens if not (
                (item['kind'] == 'produto' and item['produto_id'] in ids_sucos)
                or (item['kind'], item['id']) in opcionais)],
            'sucos': sucos, 'grupos': grupos,
            'preco_varia': (len({suco['preco'] for suco in sucos}) > 1
                            or any(len({o['preco'] for o in g['opcoes']}) > 1
                                   for g in grupos)),
            'preco': sum((item['subtotal'] for item in itens), Decimal('0.00'))}


def _editor(kit=None, *, erros=(), status=200):
    catalogo = kits_cafe.catalogo_para_editor()
    por_chave = {(item['kind'], item['id']): item for item in catalogo}
    selecionados = {}
    for item in (kit.itens if kit else []):
        chave = (item.kind, item.receita_id if item.kind == 'receita' else item.produto_id)
        selecionados[chave] = item.quantidade
        cat = por_chave.get(chave)
        if cat is None:
            modelo = Receita if item.kind == 'receita' else Produto
            alvo = db.session.get(modelo, chave[1])
            cat = {'kind': chave[0], 'id': chave[1],
                   'nome': alvo.nome if alvo else 'Item removido do catálogo',
                   'indisponivel': True, 'preco_kit': None}
            catalogo.insert(0, cat)
        cat['ja_selecionado'] = True
        if item.comp_json:
            try:
                comp = kits_cafe.itens_fixos_do_kit(kit)
                raw = next(raw for raw in comp
                           if (raw['kind'], raw['id']) == chave)
                prod = db.session.get(Produto, chave[1])
                cat['composicao_fixa'] = loja_menu.resumo(prod, raw['comp'])
                if loja_menu.eh_menu(prod):
                    cat['preco_kit'] = loja_menu.preco(prod, raw['comp'])
            except (ValueError, KeyError):
                cat['composicao_erro'] = 'Composição inválida; adote o padrão atual ou remova o item.'
    dados = request.form if request.method == 'POST' else {}
    sucos_selecionados = [str(suco.produto_id) for suco in (kit.sucos if kit else [])]
    if request.method == 'POST' and dados.get('configurar_sucos') == '1':
        sucos_selecionados = dados.getlist('suco_ids')
    sucos_catalogo = [item for item in catalogo
                     if item['kind'] == 'produto' and not item.get('menu')]
    disponiveis_sucos = {str(item['id']) for item in sucos_catalogo}
    for produto_id in sucos_selecionados:
        if produto_id in disponiveis_sucos or not re.fullmatch(r'[1-9][0-9]*', produto_id):
            continue
        produto = db.session.get(Produto, int(produto_id))
        sucos_catalogo.append({'id': int(produto_id),
                              'nome': produto.nome if produto else 'Produto removido',
                              'preco_kit': None, 'indisponivel': True})
    if request.method == 'POST':
        for item in catalogo:
            chave = (item['kind'], item['id'])
            selecionados[chave] = dados.get(f'qtd_{chave[0]}_{chave[1]}', '0')
    grupos_catalogo = []
    for grupo, titulo in (('croissant', 'Croissant'), ('sourdough', 'Sourdough')):
        marcados = [f'{opcao.kind}:{opcao.receita_id if opcao.kind == "receita" else opcao.produto_id}'
                    for opcao in (kit.opcoes if kit else []) if opcao.grupo == grupo]
        if request.method == 'POST' and dados.get('configurar_opcoes') == '1':
            marcados = dados.getlist(f'opcao_{grupo}')
        candidatos = [{**item, 'chave': f'{item["kind"]}:{item["id"]}'}
                      for item in catalogo if not item.get('menu')]
        chaves = {item['chave'] for item in candidatos}
        for marcada in marcados:
            if marcada in chaves:
                continue
            match = re.fullmatch(r'(receita|produto):([1-9][0-9]{0,9})', marcada)
            if not match:
                continue
            kind, item_id = match.group(1), int(match.group(2))
            alvo = db.session.get(Receita if kind == 'receita' else Produto, item_id)
            candidatos.append({'chave': marcada, 'kind': kind, 'id': item_id,
                                'nome': alvo.nome if alvo else 'Item removido do catálogo',
                                'preco_kit': None, 'indisponivel': True})
            chaves.add(marcada)
        grupos_catalogo.append({'chave': grupo, 'nome': titulo,
                                'selecionados': marcados, 'catalogo': candidatos})
    return render_template('admin/kits_cafe_editor.html', kit=kit,
                           catalogo=catalogo, selecionados=selecionados,
                           grupos_catalogo=grupos_catalogo,
                           sucos_catalogo=sucos_catalogo,
                           sucos_selecionados=sucos_selecionados,
                           dados=dados, erros=erros,
                           estado=_estado(kit) if kit else None), status


@kits_cafe_admin_bp.route('')
@kits_cafe_admin_bp.route('/')
@login_required
@owner_required
def index():
    kits = KitCafe.query.order_by(KitCafe.id).all()
    return render_template('admin/kits_cafe.html', estados=[_estado(kit) for kit in kits])


@kits_cafe_admin_bp.route('/novo')
@login_required
@owner_required
def novo():
    return _editor()


@kits_cafe_admin_bp.route('/<int:kit_id>')
@login_required
@owner_required
def editar(kit_id):
    return _editor(KitCafe.query.get_or_404(kit_id))


@kits_cafe_admin_bp.route('/salvar', methods=['POST'])
@login_required
@owner_required
def salvar():
    kit = None
    kit_id = request.form.get('kit_id', '').strip()
    if kit_id:
        if not kit_id.isdecimal() or int(kit_id) < 1:
            abort(400)
        kit = KitCafe.query.get_or_404(int(kit_id))
        db.session.refresh(kit, with_for_update=True)
        db.session.expire(kit, ['itens', 'sucos', 'opcoes'])
    nome = request.form.get('nome', '').strip()
    descricao = request.form.get('descricao', '').strip()
    erros = []
    acoes = request.form.getlist('acao')
    acao = acoes[0] if len(acoes) == 1 else None
    if acao not in ('publicar', 'rascunho'):
        erros.append('Escolha salvar e publicar no site ou salvar como rascunho.')
    if not nome or len(nome) > 150:
        erros.append('Informe um nome com até 150 caracteres.')
    if len(descricao) > 600:
        erros.append('A descrição pode ter até 600 caracteres.')
    selecao = []
    atualizar_menus = set()
    for campo in request.form:
        if not campo.startswith('qtd_'):
            continue
        match = re.fullmatch(r'qtd_(receita|produto)_([1-9][0-9]*)', campo)
        valor = request.form.get(campo, '').strip()
        if (not match or len(request.form.getlist(campo)) != 1
                or not re.fullmatch(r'[0-9]{1,3}', valor)):
            erros.append('Use quantidades inteiras de 0 a 999; zero remove o item.')
            continue
        kind, item_id = match.group(1), int(match.group(2))
        if int(valor):
            selecao.append({'kind': kind, 'id': item_id, 'qtd': int(valor)})
            if request.form.get(f'atualizar_menu_{kind}_{item_id}') == '1':
                atualizar_menus.add((kind, item_id))
    itens, avisos = kits_cafe.preparar_itens(selecao, kit=kit,
                                           atualizar_menus=atualizar_menus)
    erros.extend(avisos)
    suco_ids = [suco.produto_id for suco in (kit.sucos if kit else [])]
    if request.form.get('configurar_sucos') == '1':
        recebidos = request.form.getlist('suco_ids')
        if (len(recebidos) > 10 or any(
                not re.fullmatch(r'[1-9][0-9]{0,9}', valor) for valor in recebidos)):
            erros.append('Selecione de 2 a 10 sucos do catálogo, ou deixe todos desmarcados.')
        else:
            suco_ids = [int(valor) for valor in recebidos]
    sucos, avisos_sucos = kits_cafe.preparar_sucos(suco_ids, itens)
    erros.extend(avisos_sucos)
    selecao_opcoes = {}
    for opcao in (kit.opcoes if kit else []):
        item_id = opcao.receita_id if opcao.kind == 'receita' else opcao.produto_id
        selecao_opcoes.setdefault(opcao.grupo, []).append(f'{opcao.kind}:{item_id}')
    if request.form.get('configurar_opcoes') == '1':
        selecao_opcoes = {grupo: request.form.getlist(f'opcao_{grupo}')
                         for grupo in ('croissant', 'sourdough')}
        if any(campo.startswith('opcao_') and campo not in (
                'opcao_croissant', 'opcao_sourdough') for campo in request.form):
            erros.append('Grupo de opções inválido.')
    grupos, avisos_opcoes = kits_cafe.preparar_opcoes(selecao_opcoes, itens, suco_ids=suco_ids)
    erros.extend(avisos_opcoes)
    if erros:
        return _editor(kit, erros=erros, status=400)
    if kit is None:
        kit = KitCafe(usuario_id=current_user.id)
        db.session.add(kit)
    kit.nome = nome
    kit.descricao = descricao or None
    kit.ativo = acao == 'publicar'
    kit.atualizado_em = agora()
    kit.itens[:] = [KitCafeItem(
        kind=item['kind'], receita_id=item['receita_id'], produto_id=item['produto_id'],
        quantidade=item['qtd'], comp_json=(json.dumps(item['comp'], sort_keys=True)
                                           if item.get('comp') else None))
        for item in itens]
    atuais_sucos = {suco.produto_id: suco for suco in kit.sucos}
    kit.sucos[:] = [atuais_sucos.get(suco['id']) or KitCafeSuco(produto_id=suco['id'])
                   for suco in sucos]
    atuais_opcoes = {(o.kind, o.receita_id if o.kind == 'receita' else o.produto_id): o
                    for o in kit.opcoes}
    novas_opcoes = []
    for grupo in grupos:
        for opcao in grupo['opcoes']:
            registro = atuais_opcoes.get((opcao['kind'], opcao['id']))
            if registro is None:
                registro = KitCafeOpcao(
                    kind=opcao['kind'],
                    receita_id=opcao['id'] if opcao['kind'] == 'receita' else None,
                    produto_id=opcao['id'] if opcao['kind'] == 'produto' else None)
            registro.grupo = grupo['chave']
            novas_opcoes.append(registro)
    kit.opcoes[:] = novas_opcoes
    db.session.commit()
    flash('Kit publicado no site.' if kit.ativo else 'Kit salvo como rascunho.', 'success')
    return redirect(url_for('kits_cafe_admin.editar', kit_id=kit.id))


@kits_cafe_admin_bp.route('/<int:kit_id>/pausar', methods=['POST'])
@login_required
@owner_required
def pausar(kit_id):
    kit = KitCafe.query.get_or_404(kit_id)
    db.session.refresh(kit, with_for_update=True)
    kit.ativo = False
    db.session.commit()
    flash('Venda do kit pausada. As compras já realizadas continuam na agenda.', 'success')
    return redirect(url_for('kits_cafe_admin.index'))
