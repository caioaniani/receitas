"""Fase 7 (parte 2) — certificado RDC 216 (§11).

Emite o comprovante de capacitação com: nome + matrícula do funcionário, nome
e CNPJ do estabelecimento, título da trilha e CONTEÚDO PROGRAMÁTICO (vídeos),
CARGA HORÁRIA, data e código de verificação (resolve na rota pública). Base
pra apresentar em fiscalização sanitária.
"""
from fpdf import FPDF

from app.models import TreinoSelo
from app.services import treino_ledger as ledger
from app.services import treino_trilha as tt


def _s(txt):
    """Sanitiza pra fonte core do FPDF (latin-1).

    Translitera ANTES do encode o que o latin-1 não conhece mas tem
    equivalente óbvio — em/en-dash e aspas curvas (achado de revisão
    12/08/2026: os módulos da Universidade usam travessão, "Módulo 1 —
    Cultura", e o certificado RDC 216 — documento de fiscalização — sairia
    "Módulo 1 ? Cultura"; mesma classe do bug do en-dash em pdf._latin1,
    27/07/2026). O resto fora do latin-1 (emoji etc.) segue virando '?'."""
    txt = (txt or '')
    for de, para in (('—', '-'), ('–', '-'), ('“', '"'), ('”', '"'),
                     ('‘', "'"), ('’', "'")):
        txt = txt.replace(de, para)
    return txt.encode('latin-1', 'replace').decode('latin-1')


def por_codigo(codigo):
    """Selo pelo código de verificação (rota pública /verificar/<uuid>)."""
    return TreinoSelo.query.filter_by(codigo_verificacao=codigo).first()


def dados_certificado(selo):
    trilha = selo.trilha
    func = selo.funcionario
    unidade = ledger.unidade_do_funcionario(func) if func else None
    videos = tt.videos_publicados(trilha)
    return {
        'nome': func.nome if func else '—',
        'matricula': func.cpf if func else '—',
        'estabelecimento': unidade.nome if unidade else 'O Pão Padaria Artesanal',
        'cnpj': (getattr(unidade, 'cnpj', None) or '—') if unidade else '—',
        'trilha': trilha.nome,
        'conteudo': [v.titulo for v in videos],
        'carga_horaria_minutos': selo.carga_horaria_minutos,
        'emitido_em': selo.emitido_em,
        'codigo': selo.codigo_verificacao,
    }


def gerar_pdf(selo, base_url=''):
    d = dados_certificado(selo)
    pdf = FPDF(orientation='P', unit='mm', format='A4')
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=18)

    pdf.set_font('Helvetica', 'B', 22)
    pdf.cell(0, 16, _s('CERTIFICADO DE CAPACITAÇÃO'), align='C', new_x='LMARGIN',
             new_y='NEXT')
    pdf.set_font('Helvetica', '', 11)
    pdf.cell(0, 7, _s('Treinamento de manipuladores de alimentos — RDC 216/ANVISA'),
             align='C', new_x='LMARGIN', new_y='NEXT')
    pdf.ln(8)

    pdf.set_font('Helvetica', '', 13)
    pdf.multi_cell(0, 8, _s(
        f'Certificamos que {d["nome"]} (matrícula {d["matricula"]}) concluiu a '
        f'trilha de treinamento "{d["trilha"]}" no estabelecimento '
        f'{d["estabelecimento"]} (CNPJ {d["cnpj"]}).'))
    pdf.ln(4)
    pdf.set_font('Helvetica', 'B', 12)
    pdf.cell(0, 8, _s('Conteúdo programático:'), new_x='LMARGIN', new_y='NEXT')
    pdf.set_font('Helvetica', '', 11)
    for i, titulo in enumerate(d['conteudo'], 1):
        pdf.cell(0, 7, _s(f'  {i}. {titulo}'), new_x='LMARGIN', new_y='NEXT')
    pdf.ln(3)
    pdf.set_font('Helvetica', 'B', 12)
    horas = d['carga_horaria_minutos'] // 60
    minutos = d['carga_horaria_minutos'] % 60
    carga = f'{horas}h{minutos:02d}min' if horas else f'{minutos} min'
    pdf.cell(0, 8, _s(f'Carga horária: {carga}'), new_x='LMARGIN', new_y='NEXT')
    data_str = d['emitido_em'].strftime('%d/%m/%Y') if d['emitido_em'] else '—'
    pdf.cell(0, 8, _s(f'Data de conclusão: {data_str}'), new_x='LMARGIN',
             new_y='NEXT')
    pdf.ln(10)
    pdf.set_font('Helvetica', '', 9)
    verif = f'{base_url}/treino/verificar/{d["codigo"]}' if base_url \
        else f'/treino/verificar/{d["codigo"]}'
    pdf.multi_cell(0, 6, _s(
        f'Código de verificação: {d["codigo"]}\nValidar em: {verif}'))
    saida = pdf.output()
    return bytes(saida)


def concluidos_por_periodo(inicio, fim, unidade_id=None):
    """Selos emitidos no período (pra exportação de fiscalização, §11)."""
    q = TreinoSelo.query.filter(TreinoSelo.emitido_em >= inicio,
                                TreinoSelo.emitido_em < fim)
    linhas = []
    for selo in q.order_by(TreinoSelo.emitido_em).all():
        d = dados_certificado(selo)
        if unidade_id and (not selo.funcionario or
                           ledger.unidade_do_funcionario(selo.funcionario) is None
                           or ledger.unidade_do_funcionario(
                               selo.funcionario).id != unidade_id):
            continue
        linhas.append({
            'nome': d['nome'], 'matricula': d['matricula'],
            'estabelecimento': d['estabelecimento'], 'trilha': d['trilha'],
            'carga_horaria_minutos': d['carga_horaria_minutos'],
            'emitido_em': selo.emitido_em, 'codigo': d['codigo']})
    return linhas
