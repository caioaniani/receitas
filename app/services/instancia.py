"""Guarda de INSTÂNCIA CANÔNICA — só a produção fala com o mundo (20/08/2026).

POR QUE EXISTE (caso "duplo texto do bot", dono: "muito serio"): o serviço
de homologação da UI v2 ficou vivo no Railway com as envs de Chatwoot/Z-API
COPIADAS da produção e rodando os MESMOS crons. Como os jobs são cron de
HORÁRIO DE PAREDE (o vigia de conversa é `minute='*/5'`,
seru_cron.py:807), as duas instâncias disparam no MESMO minuto, leem o
MESMO Chatwoot (serviço externo, compartilhado) e mandam a MESMA mensagem:
o dono recebeu o alerta "Cliente esperando ATENDENTE" duas vezes, texto
idêntico, nas conversas #1759 (14:50) e #1760 (15:15) de 20/08/2026.

NENHUM dedupe de banco resolve isso: cada instância tem o seu banco, então
cada uma acha que é a primeira a avisar (a prova: em produção há UMA linha
de VigiaVeredito por conversa, e mesmo assim chegaram duas mensagens).

COMO DISCRIMINA: `RAILWAY_GIT_BRANCH` é injetada pelo Railway POR SERVIÇO —
diferente das envs do usuário, ela não é copiada quando se clona um serviço.
O serviço de produção deploya `BRANCH_PRODUCAO`; qualquer outro serviço
rodando este mesmo código é homologação e não deve falar com o mundo.

FAIL-OPEN por desenho, e a assimetria manda: uma cópia falando causa
mensagem duplicada (irritante); a PRODUÇÃO calada por engano é silenciosa e
cara — sem alerta de vigia, sem magic link de motorista, e o bot para de
responder cliente. Por isso a regra NÃO é "bloqueia tudo que não reconheço":

- Sem `RAILWAY_GIT_BRANCH` (dev local, testes, outro host) → LIBERA.
- Branch de produção (`BRANCH_PRODUCAO`) → LIBERA.
- Branch que CASA um padrão de cópia (`_PADROES_COPIA`: codex/, preview,
  homolog, staging, ui-simplification…) → BLOQUEIA, logando ERROR.
- Branch DESCONHECIDO → LIBERA, logando ERROR. É o caso de um branch de
  produção renomeado: melhor uma duplicata do que produção muda em
  segredo (achado de revisão 20/08/2026).
- `critico=True` (Lalamove, pedido pago) atravessa o bloqueio automático:
  se a detecção errar, o que não pode faltar continua saindo.

Escapes: `ALERTAS_INSTANCIA_CANONICA=1` força liberar (use se o branch de
produção for renomeado antes de alguém atualizar `BRANCH_PRODUCAO`), `=0`
força o silêncio TOTAL — inclusive crítico, porque aí é gesto humano
explícito de "esta cópia não fala com ninguém".

Cobre os canais que leem estado EXTERNO compartilhado — Z-API (WhatsApp),
Slack e Chatwoot. E-mail fica de fora de propósito: é dirigido por dado do
BANCO (pedido, cliente), e o banco da cópia não tem pedido real.
"""
import logging
import os

logger = logging.getLogger(__name__)

# Branch que o serviço de PRODUÇÃO deploya (confirmado pela sonda
# /api/claude/deploy em 20/08/2026). AO RENOMEAR O BRANCH DE PRODUÇÃO,
# atualize aqui — ou setar ALERTAS_INSTANCIA_CANONICA=1 no Railway.
BRANCH_PRODUCAO = 'claude/continue-controller-conversation-aGS3F'

_ENV_OVERRIDE = 'ALERTAS_INSTANCIA_CANONICA'
_ENV_BRANCH = 'RAILWAY_GIT_BRANCH'

# Loga o bloqueio uma vez por branch por processo — se uma cópia ficar
# viva, o log diz QUEM está calado sem virar enxurrada a cada mensagem.
_ja_logou = set()


def status():
    """(pode_enviar: bool, motivo: str) — motivo sempre legível pra sonda."""
    override = (os.environ.get(_ENV_OVERRIDE) or '').strip()
    if override == '1':
        return True, 'liberado por ALERTAS_INSTANCIA_CANONICA=1'
    if override == '0':
        return False, 'silenciado por ALERTAS_INSTANCIA_CANONICA=0'

    branch = (os.environ.get(_ENV_BRANCH) or '').strip()
    if not branch:
        return True, 'sem RAILWAY_GIT_BRANCH (dev/teste) — fail-open'
    if branch == BRANCH_PRODUCAO:
        return True, f'instancia de producao ({branch})'
    return False, (f'instancia NAO canonica: branch "{branch}" != '
                   f'"{BRANCH_PRODUCAO}" (copia/homologacao)')


def pode_falar_com_o_mundo(canal='', critico=False):
    """False = esta instância é uma cópia e não deve mandar mensagem.

    `critico=True` sempre libera (ver docstring do módulo). Erro aqui nunca
    bloqueia: guarda de segurança que quebra não pode calar a produção.
    """
    try:
        ok, motivo = status()
    except Exception:  # noqa: BLE001 — guarda quebrada nunca cala producao
        logger.exception('instancia: checagem falhou — liberando envio')
        return True
    if ok:
        return True
    if critico:
        logger.warning('instancia: %s — mas mensagem CRITICA (%s) segue',
                       motivo, canal or '?')
        return True
    chave = (motivo, canal)
    if chave not in _ja_logou:
        _ja_logou.add(chave)
        logger.error('instancia: envio por %s SUPRIMIDO — %s',
                     canal or '?', motivo)
    return False
