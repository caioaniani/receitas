"""Projeção somente leitura do contas a receber. Não gera nem quita títulos."""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import joinedload, selectinload

from app.models import ClienteB2B, Cobranca, EnvioCobranca, FaturaB2B, VendaB2B, VendaB2BParcela
from app.utils import hoje

ZERO = Decimal('0.00')
BANCARIOS = {
    'pendente': 'Boleto a preparar',
    'remessa': 'Arquivo gerado · confirmar no banco',
    'registrada': 'Registrado no banco',
    'paga': 'Liquidado no banco',
    'rejeitada': 'Rejeitado pelo banco',
    'baixada': 'Baixado no banco',
}
ENVIOS = {
    'preparando': 'Envio iniciado, sem confirmação',
    'aceito': 'Aceito pelo serviço de e-mail',
    'falha': 'Falha no envio',
    'incerto': 'Envio não confirmado',
}
ETAPAS = {
    'nf_pendente': 'Notas fiscais a conferir',
    'boleto_pendente': 'Boletos a preparar',
    'banco': 'Boletos para conferir no banco',
}


@dataclass
class Recebivel:
    tipo: str
    id: int
    documento: object
    cobranca: object
    cliente: str
    email: str
    referencia: str
    valor: Decimal
    pago: Decimal
    vencimento: object
    cancelada: bool = False
    envio: object = None
    envio_confirmado: object = None

    @property
    def saldo(self):
        if self.sem_cobranca:
            return ZERO
        return max(ZERO, self.valor - self.pago)

    @property
    def sem_cobranca(self):
        return bool(getattr(self.documento, 'sem_cobranca', False))

    @property
    def pagamento(self):
        if self.sem_cobranca:
            return 'Divulgação · sem cobrança'
        if self.cancelada:
            return 'Cancelada'
        if self.saldo == ZERO:
            return 'Paga'
        if self.vencimento < hoje():
            return 'Vencida' if not self.pago else 'Vencida · parcial'
        return 'Parcial' if self.pago else 'A receber'

    @property
    def nf_pronta(self):
        d = self.documento
        return bool(d and getattr(d, 'tiny_nota_fiscal_id', None)
                    and getattr(d, 'nf_emitida_em', None)
                    and (getattr(d, 'nf_status', '') or '').lower()
                    not in ('cancelada', 'rejeitada', 'denegada'))

    @property
    def nf_label(self):
        if self.sem_cobranca:
            return 'Fora das pendências de cobrança'
        if not self.documento:
            return 'NF não vinculada'
        if self.nf_pronta:
            return f'NF {self.documento.nf_numero or "autorizada"}'
        return 'Verificar NF' if getattr(self.documento, 'tiny_nota_fiscal_id', None) else 'NF a emitir'

    @property
    def banco_label(self):
        if self.sem_cobranca:
            return 'Não cobrar'
        return BANCARIOS.get(self.cobranca.status, self.cobranca.status) if self.cobranca else 'Boleto a gerar'

    @property
    def envio_label(self):
        if self.envio_confirmado:
            return 'NF + boleto enviados'
        return ENVIOS.get(self.envio.status, 'Envio não confirmado') if self.envio else 'Sem histórico'

    @property
    def bloqueio(self):
        if self.sem_cobranca:
            return 'Divulgação — sem cobrança. A venda e o estoque foram preservados; não há envio de NF + boleto.'
        if self.cancelada:
            return 'Cobrança cancelada. Não envie documentos de cobrança.'
        if not self.saldo:
            return 'Pagamento já registrado. Não há saldo a cobrar.'
        if not self.documento:
            return 'Este boleto avulso não tem uma venda ou fatura vinculada. Consulte a origem antes de enviar documentos juntos.'
        if not self.nf_pronta:
            return 'Emita ou verifique a autorização da NF antes do envio conjunto.'
        c = self.cobranca
        if not c or not c.nosso_numero:
            return 'Prepare o boleto e gere a remessa na área do banco.'
        if c.status not in ('registrada', 'remessa'):
            return 'Verifique a situação do boleto na área do banco antes de enviar.'
        # Uma quitação parcial não reduz o PDF já registrado no banco.
        if c.valor != self.saldo:
            return 'O valor do boleto difere do saldo. Confira o recebimento e o título no banco antes de enviar.'
        return ''

    @property
    def acao(self):
        if self.cancelada or not self.saldo:
            return 'Ver detalhes'
        if not self.documento:
            return 'Conferir origem'
        if not self.nf_pronta:
            return 'Preparar documentos'
        if self.bloqueio:
            return 'Conferir boleto'
        if self.envio_confirmado:
            return 'Enviar novamente'
        return 'Conferir tentativa' if self.envio else 'Enviar NF + boleto'


def filtrar_etapa(linhas, etapa):
    """Atalhos de consulta; não emite documentos nem altera os títulos."""
    if etapa not in ETAPAS:
        return linhas
    abertas = [r for r in linhas if r.saldo and not r.cancelada]
    if etapa == 'nf_pendente':
        return [r for r in abertas if r.documento and not r.nf_pronta]
    if etapa == 'boleto_pendente':
        return [r for r in abertas if not r.cobranca or not r.cobranca.nosso_numero
                or r.cobranca.status == 'pendente']
    return [r for r in abertas if r.cobranca and r.cobranca.nosso_numero
            and r.cobranca.status != 'pendente'
            and (r.cobranca.status != 'registrada' or r.cobranca.valor != r.saldo)]


def resumo_dashboard(linhas):
    abertas = [r for r in linhas if r.saldo and not r.cancelada]
    vencidas = [r for r in abertas if r.vencimento < hoje()]
    # Mesmos critérios de elegibilidade da tela de fechamento, em uma consulta.
    # Contas de valor zero não viram alertas nem somam ao contas a receber.
    contas = (VendaB2B.query.with_entities(VendaB2B.cliente_id)
              .join(ClienteB2B)
              .filter(ClienteB2B.ativo.is_(True), ClienteB2B.faturamento_mensal.is_(True),
                      VendaB2B.status == 'ativa', VendaB2B.fatura_id.is_(None),
                      VendaB2B.dispensa_cobranca.is_(None),
                      VendaB2B.data_venda >= date(2000, 1, 1), VendaB2B.data_venda <= hoje(),
                      ~VendaB2B.parcelas.any())
              .group_by(VendaB2B.cliente_id).having(func.sum(VendaB2B.valor_total) > ZERO).all())
    return {
        'aberto': sum((r.saldo for r in abertas), ZERO),
        'vencido': sum((r.saldo for r in vencidas), ZERO),
        'pagas': sum(not r.saldo and not r.cancelada and not r.sem_cobranca for r in linhas),
        'nf_pendente': len(filtrar_etapa(linhas, 'nf_pendente')),
        'boleto_pendente': len(filtrar_etapa(linhas, 'boleto_pendente')),
        'banco': len(filtrar_etapa(linhas, 'banco')),
        'sem_historico': sum(r.envio is None for r in abertas),
        'fechamentos': len(contas),
    }


def de_fatura(f):
    c = f.cobrancas[0] if f.cobrancas else None
    pago = sum((p.valor_pago or ZERO for p in f.parcelas), ZERO)
    if not f.parcelas and c:
        pago = c.valor_pago or ZERO
    elif not f.parcelas and f.status == 'paga':
        pago = f.valor_total
    return Recebivel('fatura', f.id, f, c, f.cliente.nome, f.cliente.email or '',
                    f.codigo, f.valor_total, pago, c.vencimento if c else f.vencimento,
                    f.status == 'cancelada')


def de_parcela(p):
    c = p.cobranca[0] if p.cobranca else None
    v = p.venda
    return Recebivel('parcela', p.id, v, c, v.cliente_display,
                    (v.cliente.email or '') if v.cliente else '',
                    f'Venda #{v.id} · parcela {p.numero}', p.valor, p.valor_pago or ZERO,
                    c.vencimento if c else p.vencimento, v.status == 'cancelada')


def de_boleto(c):
    return Recebivel('boleto', c.id, None, c, c.pagador_nome, '',
                    c.seu_numero, c.valor, c.valor_pago or ZERO, c.vencimento,
                    c.status == 'baixada')


def carregar(tipo, id):
    if tipo == 'fatura':
        return de_fatura(FaturaB2B.query.get_or_404(id))
    if tipo == 'parcela':
        p = VendaB2BParcela.query.get_or_404(id)
        # Links antigos nunca podem permitir cobrança individual de fechamento.
        if p.fatura_id or p.venda.fatura_id:
            return de_fatura(FaturaB2B.query.get_or_404(p.fatura_id or p.venda.fatura_id))
        return de_parcela(p)
    c = Cobranca.query.get_or_404(id)
    if c.fatura_id:
        return de_fatura(c.fatura)
    if c.parcela_id:
        return carregar('parcela', c.parcela_id)
    return de_boleto(c)


def historico(r):
    q = EnvioCobranca.query
    if r.tipo == 'fatura':
        q = q.filter_by(fatura_id=r.id)
    elif r.tipo == 'parcela':
        q = q.filter_by(venda_id=r.documento.id)
    else:
        q = q.filter(EnvioCobranca.fatura_id.is_(None), EnvioCobranca.venda_id.is_(None))
    return [e for e in q.order_by(EnvioCobranca.id.desc()).all() if pertence(e, r)]


def pertence(e, r):
    if r.tipo == 'fatura':
        return e.fatura_id == r.id
    if r.tipo == 'parcela' and e.venda_id != r.documento.id:
        return False
    return (r.tipo == 'parcela' and e.documentos == 'nf') or bool(
        r.cobranca and r.cobranca.id in (e.cobranca_ids or []))


def conjunto_confirmado(e, r):
    """Uma NF isolada, outra parcela ou uma NF substituída não confirma o conjunto."""
    return bool(e.status == 'aceito' and e.documentos == 'nf_boleto'
                and r.documento and e.nf_id
                and str(e.nf_id) == str(r.documento.tiny_nota_fiscal_id)
                and r.cobranca and r.cobranca.id in (e.cobranca_ids or [])
                and pertence(e, r))


def atribuir_envios(r, envios):
    r.envio = envios[0] if envios else None
    r.envio_confirmado = next((e for e in envios if conjunto_confirmado(e, r)), None)


def painel():
    """Uma linha por fatura/parcela, nunca as parcelas E a fatura.

    Sem LIMIT antes dos totais. Paginação aplicada pela rota após filtros.
    Relações carregadas em lote evitam N+1 de clientes/boletos/parcelas.
    """
    faturas = FaturaB2B.query.options(
        joinedload(FaturaB2B.cliente), selectinload(FaturaB2B.cobrancas),
        selectinload(FaturaB2B.parcelas)).all()
    parcelas = (VendaB2BParcela.query.join(VendaB2B)
                .filter(VendaB2BParcela.fatura_id.is_(None), VendaB2B.fatura_id.is_(None))
                .options(joinedload(VendaB2BParcela.venda).joinedload(VendaB2B.cliente),
                         selectinload(VendaB2BParcela.cobranca)).all())
    avulsas = Cobranca.query.filter(Cobranca.parcela_id.is_(None), Cobranca.fatura_id.is_(None)).all()
    linhas = [de_fatura(f) for f in faturas if f.valor_total > ZERO]
    linhas += [de_parcela(p) for p in parcelas if p.valor > ZERO]
    linhas += [de_boleto(c) for c in avulsas if c.valor > ZERO]
    envios = EnvioCobranca.query.order_by(EnvioCobranca.id.desc()).all()
    por_fatura, por_venda, por_boleto = {}, {}, {}
    for e in envios:
        if e.fatura_id:
            por_fatura.setdefault(e.fatura_id, []).append(e)
        if e.venda_id and e.documentos == 'nf':
            por_venda.setdefault(e.venda_id, []).append(e)
        for cid in e.cobranca_ids or []:
            por_boleto.setdefault(cid, []).append(e)
    for r in linhas:
        if r.tipo == 'fatura':
            candidatos = por_fatura.get(r.id, [])
        else:
            candidatos = ((por_boleto.get(r.cobranca.id, []) if r.cobranca else [])
                          + (por_venda.get(r.documento.id, []) if r.documento else []))
        candidatos = {e.id: e for e in candidatos if pertence(e, r)}
        atribuir_envios(r, sorted(candidatos.values(), key=lambda e: e.id, reverse=True))
    return sorted(linhas, key=lambda r: (r.vencimento, r.tipo, r.id))
