"""Configuração privada do owner; credenciais nunca retornam na resposta."""

import base64
import io
import json
import os
import secrets
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from flask import (
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from flask_login import current_user, login_required
from itsdangerous import BadData, URLSafeTimedSerializer

from app.blueprints.fiserv import fiserv_bp
from app.decorators import owner_required
from app.extensions import db, limiter
from app.models.fiserv import ArquivoFiservRecebido, EventoFiserv, IntegracaoFiserv, PendenciaFiserv
from app.services.fiserv_integracao import (
    ColetaEmAndamento,
    coleta_disponivel,
    consultar_arquivos_disponiveis,
    obter_chave_apresentada,
    receber_arquivo_selecionado,
    trava,
)
from app.services.fiserv_segredos import (
    ErroSegredoFiserv,
    carregar_chave,
    cifrar_json,
    decifrar_bytes,
)
from app.services.fiserv_sftp import (
    HOST_PADRAO,
    HOSTS_PERMITIDOS,
    ConfigFiservSFTP,
    ErroEtapaAutenticacaoFiservSFTP,
    ErroFiservSFTP,
    ErroOperacaoFiservSFTP,
    MetadadosArquivoFiserv,
)
from app.utils import agora


@fiserv_bp.before_request
@login_required
@owner_required
def proteger_area():
    import sentry_sdk
    sentry_sdk.get_isolation_scope().set_tag('fiserv_privado', True)


@fiserv_bp.after_request
def proteger_resposta(response):
    response.headers['Cache-Control'] = 'no-store, private'
    response.headers['Pragma'] = 'no-cache'
    # Flask-WTF exige Referer da mesma origem nos POSTs HTTPS. Mantém essa
    # validação sem enviar a URL desta área a destinos externos.
    response.headers['Referrer-Policy'] = 'same-origin'
    return response


def _assinador():
    __tracebackhide__ = True
    return URLSafeTimedSerializer(current_app.config['SECRET_KEY'], salt='fiserv-host-owner-v1')


def _assinador_arquivo():
    __tracebackhide__ = True
    return URLSafeTimedSerializer(current_app.config['SECRET_KEY'], salt='fiserv-arquivo-owner-v1')


def _painel(proposta=None, confirmacao=None, remotos=None):
    __tracebackhide__ = True
    registro = db.session.get(IntegracaoFiserv, 1)
    # Nunca enviar ORM da configuração ou ciphertext ao template.
    estado = None if registro is None else {
        'ativa': registro.ativa, 'estado': registro.estado,
        'solicitada': registro.coleta_solicitada,
        'ultima_tentativa': registro.ultima_tentativa_em,
        'ultimo_sucesso': registro.ultimo_sucesso_em, 'mensagem': registro.mensagem,
    }
    pagina = ArquivoFiservRecebido.query.order_by(
        ArquivoFiservRecebido.recebido_em.desc(), ArquivoFiservRecebido.id.desc(),
    ).paginate(page=request.args.get('pagina', 1, type=int), per_page=20, error_out=False)
    pendencias = PendenciaFiserv.query.filter_by(
        versao_configuracao=registro.versao if registro is not None else 0,
    ).order_by(PendenciaFiserv.ultima_tentativa_em.desc(), PendenciaFiserv.id.desc()).paginate(
        page=request.args.get('pagina_pendencias', 1, type=int), per_page=20, error_out=False,
    )
    return render_template(
        'fiserv/index.html', estado=estado, pagina=pagina, pendencias=pendencias,
        hosts=sorted(HOSTS_PERMITIDOS), host_padrao=HOST_PADRAO,
        proposta=proposta, confirmacao=confirmacao, remotos=remotos,
        coleta_disponivel=coleta_disponivel(),
        chave_persistente=bool(os.environ.get('SECRET_KEY')),
    )


@fiserv_bp.get('')
def index():
    return _painel()


@fiserv_bp.get('/resumo')
def resumo():
    __tracebackhide__ = True
    from app.services.fiserv_financeiro import obter_painel
    filtros = {chave: request.args.get(chave, '').strip()
               for chave in ('inicio', 'fim', 'tipo', 'documento')}
    try:
        inicio = date.fromisoformat(filtros['inicio']) if filtros['inicio'] else None
        fim = date.fromisoformat(filtros['fim']) if filtros['fim'] else None
        if inicio and fim and inicio > fim:
            raise ValueError
        if filtros['tipo'] not in {'', 'S', 'P', 'R', 'PIX', 'V'} or len(filtros['documento']) > 30:
            raise ValueError
    except ValueError:
        flash('Confira as datas e os filtros selecionados.', 'warning')
        return redirect(url_for('fiserv.resumo'))
    painel = obter_painel(inicio=inicio, fim=fim, tipo=filtros['tipo'] or None,
                          documento=filtros['documento'] or None)
    registro = db.session.get(IntegracaoFiserv, 1)
    pendencias_recebimento = PendenciaFiserv.query.filter_by(
        versao_configuracao=registro.versao if registro else 0,
    ).count()
    hoje = agora().date()
    periodos = [
        {'nome': 'Este mês', 'inicio': hoje.replace(day=1).isoformat(), 'fim': hoje.isoformat()},
        {'nome': 'Últimos 7 dias', 'inicio': (hoje - timedelta(days=6)).isoformat(), 'fim': hoje.isoformat()},
        {'nome': 'Todo o histórico', 'inicio': '', 'fim': ''},
    ]
    return render_template('fiserv/resumo.html', painel=painel, filtros=filtros,
                           pendencias_recebimento=pendencias_recebimento,
                           periodos=periodos, inicio=inicio, fim=fim,
                           automatico=bool(registro and registro.ativa))


@fiserv_bp.post('/processar')
@limiter.limit('2 per minute')
def processar():
    __tracebackhide__ = True
    from app.services.fiserv_financeiro import ErroProcessamentoFiserv, processar_pendentes
    try:
        resultado = processar_pendentes(max_arquivos=20, prazo=25)
        if resultado.get('ocupada'):
            flash('Há uma coleta ou leitura em andamento. Aguarde a próxima atualização.', 'info')
        else:
            flash(f"Leitura concluída: {resultado['processados']} arquivo(s) processado(s), "
                  f"{resultado['duplicados']} repetido(s) e {resultado['erros']} para conferência.",
                  'warning' if resultado['erros'] else 'success')
    except ErroProcessamentoFiserv:
        db.session.rollback()
        flash('Não foi possível concluir a leitura. Os originais continuam guardados.', 'warning')
    return redirect(url_for('fiserv.resumo'))


@fiserv_bp.post('/servidor')
@limiter.limit('3 per minute')
def consultar_servidor():
    __tracebackhide__ = True
    host = request.form.get('host', '')
    if host not in HOSTS_PERMITIDOS:
        abort(400)
    try:
        proposta = obter_chave_apresentada(host)
        nonce = secrets.token_urlsafe(24)
        session['fiserv_nonce'] = nonce
        token = _assinador().dumps({**proposta, 'owner_id': current_user.id, 'nonce': nonce})
        return _painel(proposta=proposta, confirmacao=token)
    except ErroFiservSFTP:
        flash('Não foi possível consultar o servidor da Fiserv. Tente novamente.', 'warning')
        return redirect(url_for('fiserv.index'))


@fiserv_bp.post('/configurar')
@limiter.limit('5 per minute')
def configurar():
    __tracebackhide__ = True
    if not os.environ.get('SECRET_KEY'):
        flash('A chave permanente do servidor precisa ser configurada antes de salvar o acesso.', 'danger')
        return redirect(url_for('fiserv.index'))
    try:
        proposta = _assinador().loads(request.form.get('confirmacao', ''), max_age=600)
        if (proposta.get('owner_id') != current_user.id
                or not session.get('fiserv_nonce')
                or proposta.get('nonce') != session['fiserv_nonce']
                or proposta.get('host') not in HOSTS_PERMITIDOS
                or request.form.get('servidor_confirmado') != '1'):
            raise ValueError
        usuario = request.form.get('usuario', '').strip()
        senha = request.form.get('senha', '') or None
        passphrase = request.form.get('passphrase', '') or None
        if (not usuario or len(usuario) > 256 or any(ord(c) < 32 for c in usuario)
                or len(senha or '') > 1024 or len(passphrase or '') > 1024):
            raise ValueError
        upload = request.files.get('chave')
        if upload is None:
            raise ValueError
        conteudo = upload.read(16 * 1024 + 1)
        if not conteudo or len(conteudo) > 16 * 1024:
            raise ValueError
        chave = carregar_chave(conteudo, passphrase)
        ConfigFiservSFTP(usuario=usuario, host=proposta['host'], chave_privada=chave,
                        known_hosts_text=proposta['known_hosts_text'], senha=senha)
        dados = {
            'host': proposta['host'], 'usuario': usuario, 'senha': senha,
            'passphrase': passphrase, 'chave_base64': base64.b64encode(conteudo).decode(),
            'known_hosts_text': proposta['known_hosts_text'],
        }
        cifrado = cifrar_json(dados)
        with trava():
            registro = db.session.get(IntegracaoFiserv, 1)
            if registro is None:
                registro = IntegracaoFiserv(id=1, versao=1)
                db.session.add(registro)
            else:
                registro.versao += 1
            registro.acesso_cifrado = cifrado
            registro.atualizado_em = agora()
            registro.atualizado_por_id = current_user.id
            registro.ativa = False
            registro.coleta_solicitada = True
            registro.estado = 'aguardando'
            registro.mensagem = 'Aguardando a primeira coleta para confirmar o acesso.'
            registro.ultima_tentativa_em = None
            registro.ultimo_sucesso_em = None
            db.session.add(EventoFiserv(acao='acesso_configurado', usuario_id=current_user.id))
            db.session.commit()
        session.pop('fiserv_nonce', None)
        flash('Acesso salvo de forma protegida. A primeira coleta será iniciada automaticamente.', 'success')
    except ColetaEmAndamento:
        db.session.rollback()
        flash('Há uma coleta em andamento. Aguarde e salve novamente.', 'warning')
    except BadData:
        flash('A confirmação do servidor expirou. Consulte o servidor novamente antes de salvar.', 'warning')
    except ErroSegredoFiserv as exc:
        db.session.rollback()
        # ErroSegredoFiserv contém somente mensagens fixas, nunca entrada privada.
        flash(str(exc), 'warning')
    except (ValueError, ErroFiservSFTP):
        db.session.rollback()
        flash('Confira os campos, a confirmação do servidor e o arquivo da chave. Nenhum acesso foi alterado.', 'warning')
    return redirect(url_for('fiserv.index'))


@fiserv_bp.post('/coletar')
@limiter.limit('3 per minute')
def solicitar_coleta():
    __tracebackhide__ = True
    try:
        with trava():
            registro = db.session.get(IntegracaoFiserv, 1)
            if registro is None:
                abort(404)
            registro.coleta_solicitada = True
            registro.estado = 'aguardando'
            registro.mensagem = 'Nova coleta solicitada pelo owner.'
            db.session.add(EventoFiserv(acao='coleta_solicitada', usuario_id=current_user.id))
            db.session.commit()
        flash('Coleta solicitada. Atualize esta tela em alguns minutos.', 'success')
    except ColetaEmAndamento:
        flash('Já existe uma coleta em andamento.', 'info')
    return redirect(url_for('fiserv.index'))


@fiserv_bp.post('/pausar')
def pausar():
    __tracebackhide__ = True
    try:
        with trava():
            registro = db.session.get(IntegracaoFiserv, 1)
            if registro is None:
                abort(404)
            registro.ativa = False
            registro.coleta_solicitada = False
            registro.estado = 'pausado'
            registro.mensagem = 'Coleta automática pausada pelo owner.'
            db.session.add(EventoFiserv(acao='coleta_pausada', usuario_id=current_user.id))
            db.session.commit()
        flash('Coleta automática pausada.', 'info')
    except ColetaEmAndamento:
        flash('A coleta atual ainda está em andamento. Aguarde e pause novamente.', 'warning')
    return redirect(url_for('fiserv.index'))


def _informar_erro_consulta(exc, *, recebimento=False):
    __tracebackhide__ = True
    db.session.rollback()
    operacao = 'receber o arquivo' if recebimento else 'consultar os arquivos disponíveis'
    if isinstance(exc, (ErroOperacaoFiservSFTP, ErroEtapaAutenticacaoFiservSFTP)):
        # Categoria fixa, sem texto remoto e sem prometer uma nova tentativa.
        flash(f'Não foi possível {operacao}. [{exc.etapa}/{exc.codigo}] '
              'A coleta automática continua pausada.', 'warning')
    elif isinstance(exc, (ErroFiservSFTP, ErroSegredoFiserv, ColetaEmAndamento)):
        flash(str(exc), 'warning')
    else:
        flash(f'Não foi possível {operacao}. Nenhuma nova coleta automática foi solicitada.', 'warning')
        current_app.logger.error('Operação manual Fiserv não concluída; detalhes privados omitidos.')


@fiserv_bp.post('/arquivos-remotos')
@limiter.limit('3 per minute')
def consultar_arquivos():
    __tracebackhide__ = True
    try:
        versao, arquivos = consultar_arquivos_disponiveis()
        total_paginas = max(1, (len(arquivos) + 19) // 20)
        pagina = min(max(request.form.get('pagina_remota', 1, type=int), 1), total_paginas)
        nonce = session.setdefault('fiserv_arquivos_nonce', secrets.token_urlsafe(24))
        itens = []
        for arquivo in arquivos[(pagina - 1) * 20:pagina * 20]:
            token = _assinador_arquivo().dumps({
                'owner_id': current_user.id, 'nonce': nonce, 'versao': versao,
                'nome': arquivo.nome, 'tamanho': arquivo.tamanho, 'mtime': arquivo.modificado_em,
            })
            itens.append({'nome': arquivo.nome, 'tamanho': arquivo.tamanho, 'selecao': token})
        return _painel(remotos={'itens': itens, 'total': len(arquivos),
                               'pagina': pagina, 'paginas': total_paginas})
    except Exception as exc:
        _informar_erro_consulta(exc)
        return redirect(url_for('fiserv.index'))


@fiserv_bp.post('/receber-arquivo')
@limiter.limit('3 per minute')
def receber_arquivo():
    __tracebackhide__ = True
    try:
        selecao = _assinador_arquivo().loads(request.form.get('selecao', ''), max_age=600)
        if (not isinstance(selecao, dict) or selecao.get('owner_id') != current_user.id
                or not session.get('fiserv_arquivos_nonce')
                or selecao.get('nonce') != session['fiserv_arquivos_nonce']):
            raise BadData('Seleção inválida.')
        esperado = MetadadosArquivoFiserv(nome=selecao['nome'], tamanho=selecao['tamanho'],
                                         modificado_em=selecao['mtime'])
        receber_arquivo_selecionado(esperado, selecao['versao'], current_user.id)
        flash('Arquivo recebido e guardado no sistema. A coleta automática continua pausada.', 'success')
    except (BadData, KeyError, TypeError):
        flash('A seleção expirou ou é inválida. Consulte os arquivos disponíveis novamente.', 'warning')
    except Exception as exc:
        _informar_erro_consulta(exc, recebimento=True)
    return redirect(url_for('fiserv.index'))


@fiserv_bp.get('/arquivos/<int:arquivo_id>')
def baixar(arquivo_id):
    __tracebackhide__ = True
    arquivo = db.session.get(ArquivoFiservRecebido, arquivo_id)
    if arquivo is None:
        abort(404)
    try:
        conteudo = decifrar_bytes(arquivo.conteudo_cifrado)
    except ErroSegredoFiserv:
        flash('Não foi possível abrir o arquivo com a chave atual do servidor.', 'danger')
        return redirect(url_for('fiserv.index'))
    db.session.add(EventoFiserv(acao='arquivo_baixado', usuario_id=current_user.id))
    db.session.commit()
    return send_file(io.BytesIO(conteudo), mimetype='application/octet-stream',
                     as_attachment=True, download_name=arquivo.nome, max_age=0)


@fiserv_bp.get('/arquivos/<int:arquivo_id>/visualizar')
def visualizar(arquivo_id):
    __tracebackhide__ = True
    registro = db.session.get(ArquivoFiservRecebido, arquivo_id)
    if registro is None:
        abort(404)
    arquivo = {'id': registro.id, 'nome': registro.nome, 'tamanho': registro.tamanho}
    limite = 512 * 1024
    conteudo = None
    mensagem = None

    def recusar_constante(_valor):
        __tracebackhide__ = True
        raise ValueError

    if registro.tamanho > limite:
        mensagem = 'Este arquivo ultrapassa o limite de visualização de 512 KiB. Baixe o original para conferi-lo.'
    else:
        try:
            dados = decifrar_bytes(registro.conteudo_cifrado)
            if len(dados) > limite:
                mensagem = 'Este arquivo ultrapassa o limite de visualização de 512 KiB. Baixe o original para conferi-lo.'
            else:
                texto = dados.decode('utf-8-sig', errors='strict')
                # Valida sem converter dinheiro em float nem reescrever os
                # números, a ordem ou as strings do documento original.
                json.loads(texto, parse_float=Decimal, parse_constant=recusar_constante)
                conteudo = texto
        except ErroSegredoFiserv:
            mensagem = 'Não foi possível abrir o arquivo com a chave atual do servidor.'
        except (UnicodeError, ValueError, InvalidOperation, RecursionError):
            mensagem = 'Este arquivo não contém JSON válido em UTF-8 para visualização. Baixe o original para conferi-lo.'
    return render_template('fiserv/arquivo.html', arquivo=arquivo, conteudo=conteudo, mensagem=mensagem)
