"""Cada job do cron precisa de advisory lock UNICO.

Caso real (12/06/2026, achado em auditoria): `LOCK_KEY_VNDA_CARD = 7736`
estava sendo usado tambem em `_run_zapi_digest_saude` (linha 521). Quando
o digest de saude (07:30 BRT) coincidia com a janela horaria do vnda-card
sync, um dos dois falhava SILENCIOSAMENTE em pegar o lock e skipava sem
log de alarme — o pg_try_advisory_lock devolve False e o `_com_lock`
trata como 'outro worker ja pegou', sai limpo. O job ficava sem rodar
naquele ciclo.

Este teste varre o seru_cron.py procurando todas as constantes
`LOCK_KEY_*` e todas as chamadas `_com_lock(N, ...)` com inteiro
literal, e garante que NENHUMA chave aparece em dois jobs distintos."""
import pathlib
import re


def _ler_seru_cron():
    return pathlib.Path('app/services/seru_cron.py').read_text()


def test_cada_advisory_lock_e_unico():
    src = _ler_seru_cron()
    chaves = set()
    duplicadas = []

    # Constantes LOCK_KEY_X = NNNN
    for m in re.finditer(r'^(LOCK_KEY\w*)\s*=\s*(\d+)\s*(?:#.*)?$',
                          src, re.MULTILINE):
        nome, valor = m.group(1), int(m.group(2))
        if valor in chaves:
            duplicadas.append(f'constante {nome}={valor}')
        chaves.add(valor)

    # Inteiros literais usados direto em _com_lock(N, ...)
    for m in re.finditer(r'_com_lock\(\s*(\d+)\s*,', src):
        valor = int(m.group(1))
        if valor in chaves:
            duplicadas.append(f'literal _com_lock({valor}, ...)')
        chaves.add(valor)

    assert not duplicadas, (
        'advisory locks com chave repetida — cada job precisa de chave '
        'unica, senao pg_try_advisory_lock devolve False e o job '
        'skipa silenciosamente. Duplicatas: ' + ', '.join(duplicadas))


def test_todas_as_constantes_de_lock_sao_usadas():
    """Constante LOCK_KEY_X declarada sem uso = lixo que confunde a
    proxima auditoria (vai parecer que esta tomado mas nao esta)."""
    src = _ler_seru_cron()
    decls = {m.group(1) for m in re.finditer(
        r'^(LOCK_KEY\w*)\s*=\s*\d+', src, re.MULTILINE)}
    naodefinidas = []
    for nome in decls:
        # Conta usos alem da declaracao
        ocorrencias = len(re.findall(r'\b' + re.escape(nome) + r'\b', src))
        if ocorrencias < 2:
            naodefinidas.append(nome)
    assert not naodefinidas, (
        'constantes de lock declaradas mas nunca usadas (lixo que '
        'confunde auditoria): ' + ', '.join(naodefinidas))
