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
        # 340-353: CNPJ/CPF do beneficiário final. Sem beneficiário final,
        # o Sicredi exige ZEROS, não branco (crítica da homologação,
        # 07/07/2026 — o arquivo foi devolvido por esse campo vazio).
        assert det[339:353] == '0' * 14
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


def test_voltar_pendente_corrige_e_gera_nova_remessa(app, admin_user):
    """Fluxo REAL da homologação devolvida: remessa 1 foi com dado errado →
    voltar pra pendente → corrigir endereço → NOVA remessa (sequencial 2)
    com o MESMO nosso número (o título nunca foi registrado)."""
    from app.services.sicredi_cnab import gerar_remessa
    with app.app_context():
        cob = _cobranca()
        rem1, erros = gerar_remessa([cob], user_id=admin_user.id)
        assert erros == []
        cid, nosso = cob.id, cob.nosso_numero
    c = app.test_client()
    _login(c, admin_user.id)
    r = c.post(f'/cobrancas/{cid}/voltar-pendente', follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        cob = db.session.get(Cobranca, cid)
        assert cob.status == 'pendente'
        assert cob.remessa_id is None
        assert cob.nosso_numero == nosso            # mantém o título
    r2 = c.post(f'/cobrancas/{cid}/editar',
                data={'pagador_endereco': 'Av. Nova Independencia 1050'},
                follow_redirects=True)
    assert r2.status_code == 200
    r3 = c.post('/cobrancas/remessa', data={'ids': [str(cid)]},
                follow_redirects=True)
    assert r3.status_code == 200
    with app.app_context():
        cob = db.session.get(Cobranca, cid)
        assert cob.status == 'remessa'
        rem2 = cob.remessa
        assert rem2.numero == 2                     # novo sequencial
        det = rem2.conteudo.split('\r\n')[1]
        assert det[47:56] == nosso                  # mesmo nosso número
        assert det[274:314].strip() == 'AV. NOVA INDEPENDENCIA 1050'


def test_voltar_pendente_recusa_registrada(app, admin_user):
    """Título já REGISTRADO no banco não pode simplesmente voltar — seria
    dessincronizar com o Sicredi (precisa de instrução de baixa)."""
    from app.services.sicredi_cnab import gerar_remessa
    with app.app_context():
        cob = _cobranca()
        gerar_remessa([cob], user_id=admin_user.id)
        cob.status = 'registrada'
        db.session.commit()
        cid = cob.id
    c = app.test_client()
    _login(c, admin_user.id)
    c.post(f'/cobrancas/{cid}/voltar-pendente', follow_redirects=True)
    with app.app_context():
        assert db.session.get(Cobranca, cid).status == 'registrada'


# ── boleto (fase 2): código de barras + linha digitável + PDF ──────────────

def test_mod11_campo_livre_exemplo_do_manual():
    """§10.4 do manual: campo livre 1 1 072000031 0165 02 00623 1 0 soma
    223 → DV 8."""
    from app.services.sicredi_boleto import _mod11
    assert _mod11('110720000310165020062310') == 8


def test_campo_livre_do_boleto_modelo_oficial():
    """Decodificado da linha digitável do boleto-modelo do banco: nosso nº
    21/103527-5, ag 0101, posto 19, benef 00207."""
    from app.services.sicredi_boleto import campo_livre
    assert (campo_livre('211035275', '0101', '19', '00207')
            == '1121103527501011900207104')


def test_fator_vencimento_ciclos():
    from datetime import date

    from app.services.sicredi_boleto import fator_vencimento
    assert fator_vencimento(date(2007, 12, 20)) == 3726   # exemplo §10.7
    assert fator_vencimento(date(2025, 2, 21)) == 9999    # fim do ciclo 1
    assert fator_vencimento(date(2025, 2, 22)) == 1000    # reinício FEBRABAN
    assert fator_vencimento(date(2025, 2, 23)) == 1001


def test_codigo_barras_e_linha_digitavel_do_boleto_modelo():
    """Fixture OFICIAL (linha digitável impressa no boleto-modelo do manual
    híbrido): 74891.12115 03527.501013 19002.071041 6 85810000018000."""
    from datetime import date

    from app.services.sicredi_boleto import (
        linha_digitavel,
        montar_codigo_barras,
    )
    cfg = {'agencia': '0101', 'posto': '19', 'beneficiario': '00207'}
    cb = montar_codigo_barras('211035275', Decimal('180.00'),
                              date(2021, 4, 5), cfg=cfg)     # fator 8581
    assert len(cb) == 44
    assert cb == '7489' + '6' + '8581' + '0000018000' + \
        '1121103527501011900207104'
    assert (linha_digitavel(cb)
            == '74891.12115 03527.501013 19002.071041 6 85810000018000')


def test_boleto_pdf_gera_com_e_sem_qr(app, admin_user):
    from app.services.sicredi_boleto import gerar_boleto_pdf
    from app.services.sicredi_cnab import gerar_remessa
    with app.app_context():
        cob = _cobranca()
        gerar_remessa([cob], user_id=admin_user.id)
        pdf = bytes(gerar_boleto_pdf(cob))
        assert pdf.startswith(b'%PDF') and len(pdf) > 2000
        # Com o QR do híbrido (chega no retorno tipo 8)
        cob.pix_copia_cola = ('00020101021226870014br.gov.bcb.pix2565pix-qr'
                              'code-h.sicredi.com.br/qr/v2/cobv/TESTE52040000'
                              '5303986540518000')
        db.session.commit()
        pdf2 = bytes(gerar_boleto_pdf(cob))
        assert pdf2.startswith(b'%PDF') and len(pdf2) > len(pdf)


def test_rota_boleto_pdf(app, admin_user):
    from app.services.sicredi_cnab import gerar_remessa
    with app.app_context():
        sem_nosso = _cobranca()
        cob = _cobranca()
        gerar_remessa([cob], user_id=admin_user.id)
        cid, cid_sem = cob.id, sem_nosso.id
    c = app.test_client()
    _login(c, admin_user.id)
    r = c.get(f'/cobrancas/{cid}/boleto.pdf')
    assert r.status_code == 200
    assert r.mimetype == 'application/pdf'
    assert r.data.startswith(b'%PDF')
    # Sem nosso número ainda não há boleto — volta pra lista com aviso
    r2 = c.get(f'/cobrancas/{cid_sem}/boleto.pdf')
    assert r2.status_code == 302


# ── QR Pix no PDF (homologação 07/07/2026) ─────────────────────────────────

_PIX_MOCK = ('00020101021226910014br.gov.bcb.pix2569pix-qrcode.sicredi.com.'
             'br/qr/v2/cobv/f07b5c44acfa4644ba7bc85f22501ed052040000530398'
             '654040.105802BR5913XXXXXXXXXXXXX6008BRASILIA62070503***6304'
             '6C42')


def test_qr_pix_nas_dimensoes_exigidas_pela_homologacao():
    """O Sicredi exige o QR da impressão Normal com 3,599 cm × 3,422 cm
    (e-mail da homologação, 07/07/2026) — as medidas são conferidas na
    validação do PDF. O desenho precisa cravar as DUAS."""
    from app.services.sicredi_boleto import (
        QR_ALTURA_MM,
        QR_LARGURA_MM,
        _desenhar_qr,
    )

    class _PdfStub:
        def __init__(self):
            self.rects = []

        def set_fill_color(self, *a):
            pass

        def rect(self, x, y, w, h, style=None):
            self.rects.append((x, y, w, h))

    stub = _PdfStub()
    _desenhar_qr(stub, _PIX_MOCK, x=0.0, y=0.0)
    # Os finders do QR garantem módulo preenchido nas 4 bordas — a
    # extensão desenhada é exatamente a área exigida.
    assert abs(max(x + w for x, y, w, h in stub.rects) - QR_LARGURA_MM) < 1e-9
    assert abs(max(y + h for x, y, w, h in stub.rects) - QR_ALTURA_MM) < 1e-9
    assert min(x for x, *_ in stub.rects) == 0.0
    assert QR_LARGURA_MM == 35.99 and QR_ALTURA_MM == 34.22


def test_boleto_pdf_com_pix_gera_e_sem_pix_tambem(app):
    from app.services.sicredi_boleto import gerar_boleto_pdf
    with app.app_context():
        cob = _cobranca()
        cob.nosso_numero = '252000041'
        pdf_sem = bytes(gerar_boleto_pdf(cob))
        cob.pix_copia_cola = _PIX_MOCK
        pdf_com = bytes(gerar_boleto_pdf(cob))
    assert pdf_sem.startswith(b'%PDF')
    assert pdf_com.startswith(b'%PDF')
    assert len(pdf_com) > len(pdf_sem)      # QR desenhado (mais conteúdo)


def test_rota_definir_pix_e_owner_only(app, owner_user):
    with app.app_context():
        cob = _cobranca()
        cob.nosso_numero = '252000041'
        db.session.commit()
        cid = cob.id
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(owner_user.id)
        s['_fresh'] = True
    r = c.post(f'/cobrancas/{cid}/definir-pix', data={'pix': _PIX_MOCK},
               follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        assert db.session.get(Cobranca, cid).pix_copia_cola == _PIX_MOCK
    c.post(f'/cobrancas/{cid}/definir-pix', data={'pix': ''},
           follow_redirects=True)
    with app.app_context():
        assert db.session.get(Cobranca, cid).pix_copia_cola is None


def test_rota_definir_pix_admin_comum_403(app, admin_user):
    with app.app_context():
        cob = _cobranca()
        cid = cob.id
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(admin_user.id)
        s['_fresh'] = True
    assert c.post(f'/cobrancas/{cid}/definir-pix',
                  data={'pix': 'x'}).status_code == 403


# ── pós-homologação (07/07/2026): nomenclatura do arquivo de remessa ───────

def test_nome_arquivo_remessa_padrao_do_banco(app, admin_user):
    """Sicredi Internet exige CCCCCmdd.CRM (cedente + mês 1-9/O/N/D + dia);
    2º arquivo do MESMO dia vira .RM2, 3º .RM3 (e-mail da homologação).
    O REMnnnnn.CRM antigo era só do envio por e-mail."""
    from app.services.sicredi_cnab import gerar_remessa
    with app.app_context():
        r1, _ = gerar_remessa([_cobranca()], user_id=admin_user.id)
        r2, _ = gerar_remessa([_cobranca()], user_id=admin_user.id)
        d = r1.gerado_em
        mes = '123456789OND'[d.month - 1]
        assert r1.nome_arquivo == f'34325{mes}{d.day:02d}.CRM'
        assert r2.nome_arquivo == f'34325{mes}{d.day:02d}.RM2'


def test_gerar_da_parcela_puxa_cep_e_endereco_do_cadastro(app, admin_user):
    """07/08/2026 (dono: "por que não puxa o CEP direto do cadastro?"): a
    rota de parcela avulsa gravava pagador_cep='' fixo — agora usa o MESMO
    snapshot da fatura mensal (_snapshot_pagador): CEP só dígitos e
    endereço com fallback pros campos estruturados."""
    with app.app_context():
        cli = ClienteB2B(nome='United Coffee', cnpj_cpf='44737537000104',
                         endereco='', endereco_logradouro='Rua Cel Otaviano',
                         endereco_numero='55', endereco_bairro='Centro',
                         endereco_cep='04005-001', ativo=True)
        db.session.add(cli)
        db.session.commit()
        p = _parcela(cli)
        pid = p.id
    c = app.test_client()
    _login(c, admin_user.id)
    c.post(f'/cobrancas/gerar-da-parcela/{pid}', follow_redirects=True)
    with app.app_context():
        cob = Cobranca.query.filter_by(parcela_id=pid).first()
        assert cob.pagador_cep == '04005001'
        assert cob.pagador_endereco == 'Rua Cel Otaviano 55 - Centro'


def test_lista_tem_link_pra_venda(app, admin_user):
    """07/08/2026 (dono: "deveria ter um hiperlink pra clicar e abrir o
    pedido"): o seu_numero da cobrança vira link pro detalhe da venda B2B
    (ou da fatura), em outra aba."""
    with app.app_context():
        cli = _cliente()
        p = _parcela(cli)
        pid, vid = p.id, p.venda_id
    c = app.test_client()
    _login(c, admin_user.id)
    c.post(f'/cobrancas/gerar-da-parcela/{pid}')
    body = c.get('/cobrancas/banco').get_data(as_text=True)
    assert f'/b2b/vendas/{vid}' in body
    assert 'target="_blank"' in body
