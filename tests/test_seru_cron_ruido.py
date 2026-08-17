"""Ruído do cron NÃO vira evento no Sentry (17/08/2026).

A integração de logging do sentry-sdk promove todo log ERROR a evento; a
cota grátis estourou com condições TRANSITÓRIAS do cron (deploy no meio do
ciclo → RuntimeError de shutdown do executor; rede da Seru instável →
ConnectionError/"Response ended prematurely"/5xx após os retries). O
classificador `_erro_transitorio` rebaixa SÓ essas classes pra WARNING —
qualquer outra exceção continua `logger.exception` (ERROR → Sentry).
"""
import logging

import requests

from app.services import seru_cron
from app.services.seru import _Erro5xx


def test_shutdown_do_interpretador_e_transitorio():
    exc = RuntimeError('cannot schedule new futures after interpreter shutdown')
    assert seru_cron._erro_transitorio(exc) is True


def test_shutdown_do_executor_e_transitorio():
    exc = RuntimeError('cannot schedule new futures after shutdown')
    assert seru_cron._erro_transitorio(exc) is True


def test_rede_da_seru_e_transitorio():
    assert seru_cron._erro_transitorio(
        requests.exceptions.ConnectionError('pool(host=...)')) is True
    assert seru_cron._erro_transitorio(
        requests.exceptions.ChunkedEncodingError(
            'Response ended prematurely')) is True
    assert seru_cron._erro_transitorio(
        requests.exceptions.Timeout('read timeout')) is True
    assert seru_cron._erro_transitorio(_Erro5xx('Seru /orders 502')) is True


def test_runtime_error_comum_NAO_e_transitorio():
    """RuntimeError sem 'shutdown' (ex.: 'Seru /orders 500' do
    _get_uma_vez) segue como erro de verdade — vai pro Sentry."""
    assert seru_cron._erro_transitorio(RuntimeError('Seru /orders 500')) is False
    assert seru_cron._erro_transitorio(ValueError('qualquer coisa')) is False


def test_falha_de_job_transitoria_loga_warning(caplog):
    with caplog.at_level(logging.WARNING, logger='app.services.seru_cron'):
        seru_cron._falha_de_job(
            'sync teste',
            requests.exceptions.ConnectionError('pool caiu'))
    regs = [r for r in caplog.records if 'sync teste' in r.message]
    assert regs and all(r.levelno == logging.WARNING for r in regs)


def test_falha_de_job_real_loga_error(caplog):
    with caplog.at_level(logging.WARNING, logger='app.services.seru_cron'):
        try:
            raise ValueError('bug de verdade')
        except ValueError as exc:
            seru_cron._falha_de_job('sync teste', exc)
    regs = [r for r in caplog.records if 'sync teste' in r.getMessage()]
    assert regs and all(r.levelno == logging.ERROR for r in regs)


def test_com_lock_usa_o_classificador(app, caplog):
    """`_com_lock` com fn levantando erro de REDE não pode logar ERROR
    (viraria evento no Sentry a cada ciclo com a Seru instável)."""
    def _explode():
        raise requests.exceptions.ConnectionError('Seru fora')

    with app.app_context():
        with caplog.at_level(logging.WARNING,
                             logger='app.services.seru_cron'):
            seru_cron._com_lock(999999, _explode, 'job de teste')
    regs = [r for r in caplog.records if 'job de teste' in r.message]
    assert regs
    assert all(r.levelno == logging.WARNING for r in regs)
