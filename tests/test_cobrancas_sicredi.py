"""Cobrança Sicredi CNAB400 (04/07/2026): nosso número (DV mod11), remessa,
retorno (liquidação quita a parcela B2B; tipo 8 traz o QR híbrido) e tela.
Fixture do DV vem do boleto-modelo OFICIAL do banco (manual híbrido):
ag 0101, posto 19, benef 00207, nosso nº 21/103527-5.
"""
from datetime import timedelta
from decimal import Decimal

from app.extensions import db
from app.models import ClienteB2B, Cobranca, VendaB2B, VendaB2BParcela
from app.utils import hoje


def _cliente():
    c = ClienteB2B(nome='Restaurante Bom Prato', cnpj_cpf='11222333000144',
                   endereco='Rua das Laranjeiras 100', ativo=True)
    db.session.add(c)
    db.session.commit()
    return c


def _parcela(cliente, valor='500.00', venc_dias=15):
    v = VendaB2B(cliente_id=cliente.id, valor_total=Decimal(valor))
    db.session.add(v)
    db.session.flush()
    p = VendaB2BParcela(venda_id=v.id, numero=1,
                        vencimento=hoje() + timedelta(days=venc_dias),
                        valor=Decimal(valor))
    db.session.add(p)
    db.session.commit()
    return p


def _cobranca(parcela=None, cep='04568001', venc_dias=15, valor='500.00'):
    cob = Cobranca(
        parcela_id=parcela.id if parcela else None,
        pagador_nome='Restaurante Bom Prato',
        pagador_cnpj_cpf='11.222.333/0001-44',
        pagador_endereco='Rua das Laranjeiras 100',
        pagador_cep=cep, valor=Decimal(valor),
        vencimento=hoje() + timedelta(days=venc_dias), emissao=hoje(),
        seu_numero='V1P1')
    db.session.add(cob)
    db.session.commit()
    return cob


# ── nosso número ────────────────────────────────────────────────────────────

def test_dv_nosso_numero_fixture_oficial_do_banco():
    """Boleto-modelo do Sicredi: 21/103527-5 (ag 0101, posto 19, benef 00207,
    byte 1)."""
    from app.services.sicredi_cnab import dv_nosso_numero
    assert dv_nosso_numero('0101', '19', '00207', '21', '1', '03527') == 5


def test_proximo_nosso_numero_sequencial(app, monkeypatch):
    from app.services import sicredi_cnab
    monkeypatch.setenv('SICREDI_AGENCIA', '0726')
    monkeypatch.setenv('SICREDI_POSTO', '61')
    monkeypatch.setenv('SICREDI_BENEFICIARIO', '34325')
    with app.app_context():
        n1 = sicredi_cnab.proximo_nosso_numero()
        assert len(n1) == 9
        ano2 = f'{hoje().year % 100:02d}'
        assert n1.startswith(ano2 + '2')            # byte default 2
        assert n1[3:8] == '00001'
        _cobranca().nosso_numero = n1
        db.session.commit()
        n2 = sicredi_cnab.proximo_nosso_numero()
        assert n2[3:8] == '00002'                   # nunca repete


# ── remessa ────────────────────────────────────────────────────────────────

def test_gerar_remessa_estrutura_e_status(app, admin_user):
    from app.services.sicredi_cnab import gerar_remessa
    with app.app_context():
        cli = _cliente()
        p = _parcela(cli)
        cob = _cobranca(parcela=p)
        rem, erros = gerar_remessa([cob], user_id=admin_user.id)
        assert erros == []
        assert rem.numero == 1 and rem.n_titulos == 1
        linhas = rem.conteudo.split('\r\n')
        assert linhas[-1] == ''                     # CRLF final
        header, det, trailer = linhas[0], linhas[1], linhas[2]
        for ln in (header, det, trailer):
            assert len(ln) == 400                   # CNAB não perdoa
        # Header (manual §8.1)
        assert header[0] == '0' and header[2:9] == 'REMESSA'
        assert header[26:31] == '34325'             # beneficiário
        assert header[76:79] == '748'
        assert header[110:117] == '0000001'         # nº remessa
        assert header[390:394] == '2.00'
        # Detalhe (manual §8.2)
        assert det[0] == '1' and det[5] == 'H'      # boleto HÍBRIDO
        assert det[71] == 'N' and det[73] == 'B'    # postagem/impressão nossos
        assert det[47:56] == cob.nosso_numero
        assert det[108:110] == '01'                 # cadastro de título
        assert det[126:139] == '0000000050000'      # R$ 500,00 em centavos
        assert det[218] == '2'                      # pagador PJ
        assert det[220:234] == '11222333000144'     # CNPJ só dígitos
        assert 'RESTAURANTE BOM PRATO' in det[234:274]
        assert det[326:334] == '04568001'           # CEP
        # Trailer (§8.8)
        assert trailer[0] == '9' and trailer[2:5] == '748'
        db.session.refresh(cob)
        assert cob.status == 'remessa' and cob.remessa_id == rem.id


def test_remessa_valida_cep_e_prazo(app, admin_user):
    from app.services.sicredi_cnab import gerar_remessa
    with app.app_context():
        sem_cep = _cobranca(cep='')
        rem, erros = gerar_remessa([sem_cep], user_id=admin_user.id)
        assert rem is None and any('CEP' in e for e in erros)
        curta = _cobranca(venc_dias=3)              # < 7 dias
        rem2, erros2 = gerar_remessa([curta], user_id=admin_user.id)
        assert rem2 is None and any('7 dias' in e for e in erros2)


def test_remessa_exige_endereco_do_pagador(app, admin_user):
    """Relatório da homologação (06/07/2026): enderecoPagador (275-314) é
    obrigatório — o primeiro arquivo foi devolvido por ele ir em branco."""
    from app.services.sicredi_cnab import gerar_remessa
    with app.app_context():
        cob = _cobranca()
        cob.pagador_endereco = ''
        db.session.commit()
        rem, erros = gerar_remessa([cob], user_id=admin_user.id)
        assert rem is None
        assert any('endereço' in e for e in erros)
        db.session.refresh(cob)
        assert cob.status == 'pendente'             # nada foi gravado


def test_remessa_grava_endereco_nas_posicoes_275_314(app, admin_user):
    from app.services.sicredi_cnab import gerar_remessa
    with app.app_context():
        cob = _cobranca()
        rem, erros = gerar_remessa([cob], user_id=admin_user.id)
        assert erros == []
        det = rem.conteudo.split('\r\n')[1]
        assert det[274:314] == 'RUA DAS LARANJEIRAS 100'.ljust(40)


def test_remessa_sequencial_incrementa(app, admin_user):
    from app.services.sicredi_cnab import gerar_remessa
    with app.app_context():
        r1, _ = gerar_remessa([_cobranca()], user_id=admin_user.id)
        r2, _ = gerar_remessa([_cobranca()], user_id=admin_user.id)
        assert (r1.numero, r2.numero) == (1, 2)
        assert r2.conteudo.split('\r\n')[0][110:117] == '0000002'


# ── retorno ────────────────────────────────────────────────────────────────

def _linha_retorno_tipo1(nosso9, ocorr, valor_pago_centavos=0,
                         data='040726'):
    buf = [' '] * 400
    buf[0] = '1'
    buf[47:62] = list(('000000' + nosso9))          # 15 posições sem edição
    buf[108:110] = list(ocorr)
    buf[110:116] = list(data)                       # DDMMAA
    buf[253:266] = list(str(valor_pago_centavos).rjust(13, '0'))
    return ''.join(buf)


def _linha_retorno_tipo8(nosso9, txid='TX123', url='pix-qrcode-h.sicredi/x',
                         copia='000201PIXCOPIA'):
    buf = [' '] * 400
    buf[0] = '8'
    buf[1:16] = list(('000000' + nosso9))
    buf[20:55] = list(txid.ljust(35))
    buf[56:133] = list(url.ljust(77))
    buf[134:390] = list(copia.ljust(256))
    return ''.join(buf)


def test_retorno_liquidacao_quita_cobranca_e_parcela(app, admin_user):
    from app.services.sicredi_cnab import gerar_remessa, processar_retorno
    with app.app_context():
        cli = _cliente()
        p = _parcela(cli)
        cob = _cobranca(parcela=p)
        gerar_remessa([cob], user_id=admin_user.id)
        texto = _linha_retorno_tipo1(cob.nosso_numero, '06',
                                     valor_pago_centavos=50000) + '\r\n'
        res = processar_retorno(texto)
        assert res['pagas'] == 1
        db.session.refresh(cob)
        assert cob.status == 'paga'
        assert cob.valor_pago == Decimal('500.00')
        db.session.refresh(p)
        assert p.pago_em is not None                # parcela quitada junto
        assert p.forma_pagamento == 'boleto'
        assert p.valor_pago == Decimal('500.00')
        # Reprocessar o mesmo retorno NÃO re-quita (idempotente)
        res2 = processar_retorno(texto)
        assert res2['pagas'] == 0


def test_retorno_registrada_e_qrcode(app, admin_user):
    from app.services.sicredi_cnab import gerar_remessa, processar_retorno
    with app.app_context():
        cob = _cobranca()
        gerar_remessa([cob], user_id=admin_user.id)
        texto = (_linha_retorno_tipo1(cob.nosso_numero, '02') + '\r\n'
                 + _linha_retorno_tipo8(cob.nosso_numero) + '\r\n')
        res = processar_retorno(texto)
        assert res['registradas'] == 1 and res['qrcode'] == 1
        db.session.refresh(cob)
        assert cob.status == 'registrada'
        assert cob.pix_txid == 'TX123'
        assert cob.pix_copia_cola.startswith('000201PIX')


def test_retorno_rejeitada_guarda_motivo(app, admin_user):
    from app.services.sicredi_cnab import gerar_remessa, processar_retorno
    with app.app_context():
        cob = _cobranca()
        gerar_remessa([cob], user_id=admin_user.id)
        linha = list(_linha_retorno_tipo1(cob.nosso_numero, '03'))
        linha[318:328] = list('48        ')          # motivo CEP irregular
        processar_retorno(''.join(linha))
        db.session.refresh(cob)
        assert cob.status == 'rejeitada'
        assert '48' in cob.motivo_retorno


# ── tela ───────────────────────────────────────────────────────────────────

def _login(client, uid):
    with client.session_transaction() as s:
        s['_user_id'] = str(uid)
        s['_fresh'] = True


def test_tela_gerar_da_parcela_e_download(app, admin_user):
    with app.app_context():
        cli = _cliente()
        p = _parcela(cli)
        pid = p.id
    c = app.test_client()
    _login(c, admin_user.id)
    r = c.get('/cobrancas/')
    assert r.status_code == 200
    assert 'Restaurante Bom Prato' in r.get_data(as_text=True)
    # Gera cobrança da parcela (snapshot do pagador)
    r2 = c.post(f'/cobrancas/gerar-da-parcela/{pid}', follow_redirects=True)
    assert r2.status_code == 200
    with app.app_context():
        cob = Cobranca.query.filter_by(parcela_id=pid).first()
        assert cob is not None
        assert cob.pagador_nome == 'Restaurante Bom Prato'
        assert (cob.vencimento - cob.emissao).days >= 7   # regra Sicredi
