"""Retira divulgação da cobrança, sem apagar venda, quitar ou estornar estoque."""
from app.models import Cobranca, VendaB2B, VendaB2BParcela
from app.utils import agora


def dispensar(venda_id, usuario, motivo):
    venda = VendaB2B.query.filter_by(id=venda_id).populate_existing().with_for_update().one()
    if venda.sem_cobranca:
        return venda
    if venda.status != 'ativa':
        raise ValueError('A venda precisa estar ativa para ser classificada como divulgação.')
    if venda.fatura_id or any(p.fatura_id for p in venda.parcelas):
        raise ValueError('Esta venda pertence a uma fatura. Confira o fechamento antes de retirar a cobrança.')
    if venda.valor_pago or any(p.pago_em for p in venda.parcelas):
        raise ValueError('Há pagamento registrado. Não é possível retirar a cobrança por este caminho.')
    cobrancas = (Cobranca.query.join(VendaB2BParcela)
                 .filter(VendaB2BParcela.venda_id == venda.id)
                 .populate_existing().with_for_update().all())
    if any(c.status != 'pendente' or c.nosso_numero or c.remessa_id
           or c.valor_pago or c.pago_em for c in cobrancas):
        raise ValueError('Já existe boleto numerado ou movimentado. Confira a situação no banco antes de retirar a cobrança.')
    motivo = (motivo or '').strip()
    if not motivo or len(motivo) > 300:
        raise ValueError('Informe o motivo da divulgação, com até 300 caracteres.')
    venda.dispensa_cobranca = {'motivo': motivo, 'registrado_em': agora().strftime('%d/%m/%Y às %H:%M'),
                              'usuario_id': usuario.id, 'usuario_nome': usuario.nome}
    # O caller commita. Não toca em valores, baixa bancária, NF ou estoque.
    return venda
