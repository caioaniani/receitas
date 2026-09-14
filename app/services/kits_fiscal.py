"""Compatibilidade: a fila dos kits agora atende todos os pedidos do site."""
from app.services.loja_fiscal import processar_pendentes  # noqa: F401
