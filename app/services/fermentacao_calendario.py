"""Calendário da fermentação nas duas lojas da cidade de São Paulo.

Feriados recorrentes nacionais, estaduais e municipais. Pontos facultativos,
emendas e datas comerciais não são feriados deste calendário.
Fonte conferida em 27/09/2026: https://clic.prefeitura.sp.gov.br/calendario
"""
from datetime import date, timedelta

FIXOS = {
    (1, 1): 'Confraternização Universal',
    (1, 25): 'Aniversário da Cidade de São Paulo',
    (4, 21): 'Tiradentes',
    (5, 1): 'Dia do Trabalho',
    (7, 9): 'Data Magna do Estado de São Paulo',
    (9, 7): 'Independência do Brasil',
    (10, 12): 'Nossa Senhora Aparecida',
    (11, 2): 'Finados',
    (11, 15): 'Proclamação da República',
    (11, 20): 'Consciência Negra',
    (12, 25): 'Natal',
}


def _pascoa(ano):
    """Computus gregoriano (Meeus/Jones/Butcher), sem consulta externa."""
    ciclo = ano % 19
    seculo, resto = divmod(ano, 100)
    bissextos, ajuste = divmod(seculo, 4)
    lunar = (seculo - (seculo + 8) // 25 + 1) // 3
    epacta = (19 * ciclo + seculo - bissextos - lunar + 15) % 30
    anos_bissextos, anos_resto = divmod(resto, 4)
    semana = (32 + 2 * ajuste + 2 * anos_bissextos - epacta - anos_resto) % 7
    correcao = (ciclo + 11 * epacta + 22 * semana) // 451
    mes, dia = divmod(epacta + semana - 7 * correcao + 114, 31)
    return date(ano, mes, dia + 1)


def feriado(dia):
    nome = FIXOS.get((dia.month, dia.day))
    if nome:
        return nome
    pascoa = _pascoa(dia.year)
    if dia == pascoa - timedelta(days=2):
        return 'Paixão de Cristo'
    if dia == pascoa + timedelta(days=60):
        return 'Corpus Christi'
    return None


def selecionar_datas(data_alvo, semanas):
    """Completa a amostra sem feriados quando o alvo é um dia normal.

    Para alvo feriado, mantém a seleção anterior: não inventa um modelo de
    demanda de feriado. O consumidor deve destacar essa limitação.
    Ausência de vendas nunca é um critério para trocar a data selecionada.
    """
    if semanas not in (3, 7):
        raise ValueError('A referência deve ter três ou sete ocorrências.')
    excluir = not feriado(data_alvo)
    datas, excluidas = [], []
    dia = data_alvo
    while len(datas) < semanas:
        dia -= timedelta(weeks=1)
        motivo = feriado(dia) if excluir else None
        if motivo:
            excluidas.append({'data': dia.isoformat(), 'motivo': motivo})
        else:
            datas.append(dia)
    return sorted(datas), sorted(excluidas, key=lambda item: item['data'])
