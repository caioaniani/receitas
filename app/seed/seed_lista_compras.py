"""Seed inicial do catalogo da Lista de Compras semanal por loja.

Importa os itens das 4 unidades (Ribeiro do Vale, Anesio Pinto Rosa, Nebraska,
Industria) que vieram da planilha existente. Idempotente: rerun = no-op (chave
unica em loja_id + grupo + nome_item, ver `ItemListaCompras.__table_args__`).

Itens duplicados dentro do mesmo grupo da planilha original (ex.: 'COCA COLA KS
ZERO' listado 2x, 'GELO' 2x em FLOR COMESTIVEL, 'DINIZ' inteiro 2x na Industria)
sao deduplicados automaticamente pelo dict — sem necessidade de limpar a fonte.

Nao normaliza variacoes de nome de fornecedor entre lojas (ex.: 'FORNECEDOR DE
CAFE SAINTS' vs 'FORNECEDOR DE CAFÉ SAINTS'): cada loja tem seu proprio catalogo
e o nome do grupo eh decorativo. Pode unificar depois se quiser ligar a Fornecedor.
"""

import logging

logger = logging.getLogger(__name__)

# Mapeamento "nome da aba da planilha" → nome da Loja no sistema.
# Estes sao os nomes CANONICOS (nao os apelidos: Brooklin/Loja 2/1851).
LOJA_POR_ABA = {
    'Ribeiro do Vale': 'LISTA DE COMPAS LOJA 1',     # Brooklin
    'Anesio Pinto Rosa': 'LISTA DE COMPRAS LOJA 2',
    'Nebraska': 'LISTA DE COMPRAS 1851',
    'Industria': 'LISTA INDUSTRIA',
}

# Catalogo: { nome_da_loja: [ (grupo, [itens, ...]), ... ] }
# Ordem dos grupos e dos itens dentro do grupo preservada (vira `ordem` na UI).
CATALOGO = {
    'Ribeiro do Vale': [
        ('FORNECEDOR DE CAFÉ SAINTS', [
            'CAFÉ EM GRÃOS', 'CAFÉ COADO', 'FILTRO', 'PRODUTO DE LIMPEZA MÁQUINA',
        ]),
        ('COPOS VIAGEM', [
            'COPO 240 ML', 'TAMPA 240 ML', 'COPO 300 ML', 'TAMPA 300 ML',
            'SAQUINHO PP COOKIE', 'SAQUINHO IFOD 35X50', 'COPO 30 ML EXPRESSO',
            'MEXEDOR DE CAFÉ',
        ]),
        ('AROMAR', [
            'PIMENTA TABASCO', 'SACHE AÇUCAR', 'SACHE AÇUCAR ORGANICO',
            'SACHE AÇUCAR MASCAVO', 'SACHE ADOÇANTE', 'SACHE SAL', 'SACHE VINAGRE',
            'SACHE AZEITE', 'PALITO', 'TODDY', 'CHOCOLATE DO PADRE', 'CANELA EM PÓ',
        ]),
        ('MERCADO ATACADÃO', [
            'CAFÉ TRÊS CORAÇÕES', 'FILTRO DE CAFÉ', 'LEITE', 'LEITE DESNATADO',
            'LEITE ZERO LACTOSE', 'PAPEL HIGIENICO FUNCIONÁRIO', 'GILETE',
            'AÇUCAR 1 KG', 'REQUEIJÃO VIGOR GRANDE', 'CLORO DE SALADA',
        ]),
        ('MATERIAL DE ESCRITÓRIO', [
            'TINTA IMPRESSORA L6171 BK', 'TINTA IMPRESSORA L6171 C',
            'TINTA IMPRESSORA L6171 M', 'TINTA IMPRESSORA L6171 Y',
            'PILOTO 1.0 PRETO', 'PILTO 2.0 PRETO', 'PILOTO 2.0 VERMELHO',
            'GRAMPOS', 'CLIPS', 'CANETA BIC', 'DUREX MEDIO', 'DUREX GRANDE',
            'SUPORTE DE DUREX', 'SULFITE AZUL', 'SULFITE BRANCO',
            'PAPEL VERGUE BRANCO', 'PAPEL VERGUE BEGE', 'BOBINA MAQUININHA',
            'MARCA TEXTO', 'TESOURA', 'PAPEL FOTO 10X15',
        ]),
        ('BOBINA CAIXA MONTEIRO', ['BOBINA CAIXA']),
        ('IRMÃOS BELLA COZZA', [
            'POTE 200 ML', 'POTE 600 ML', 'POTE MEL', 'GARRAFINHA DE ARRANJO',
        ]),
        ('SAFE BAND', [
            'SECANTE MAQUINA DE LAVAR', 'SABAO MAQUINA DE LAVAR',
            'PRODUTO DE LIMPEZA', 'DETERGENTE DESENGORDURANTE', 'LUVA VINIL G',
            'TOUCA', 'DETERGENTE YPE', 'ALCOOL GALÃO 70', 'ALCOOL EM GEL GALÃO',
            'ALCOOL EM GEL REFIL', 'CLORO GALÃO', 'DESIFETANTE GALÃO',
            'LIMPA ALUMINIO GALÃO', 'ESPONJA AMARELA', 'ESPONJA AZUL', 'BOMBRIL',
            'LAVA ROUPA', 'VEJA MULTIUSO', 'VEJA DESENGOURDURANTE', 'FIBRA',
            'PANO DE CHÃO BRANCO', 'PANO DE CHÃO AZUL',
            'SACO DE 100 LITROS REFORÇADO VERMELHO',
            'SACO DE 200 LITROS REFORÇADO PRETO', 'SACO DE 40 LITROS', 'RESINIT',
            'GARRAFA DE 300 ML', 'PERFEX', 'RODO PEQUENO', 'RODO MEDIO',
            'VASSOURA', 'VASSOURA PIAÇAVA', 'PÁ DE LIXO', 'BALDE',
            'SABÃO EM PEDRA', 'LISOFORM SPRAY', 'WD', 'REFIL BOM AR GLADE',
            'SOUDA CAUSTICA', 'BORRIFADOR', 'SACO PLASTICO TALHER 07X25X010',
            'LIMPA VIDRO', 'LUVA AMARELA', 'SABONETE DE MÃO',
        ]),
        ('AVANTI', [
            'PAPEL HIGIENICO CLIENTES', 'PAPEL TOALHA CLIENTES',
            'REFIL SABONETE', 'REFIL EM ALCOOL GEL',
        ]),
        ('COCA COLA', [
            'COCA COLA KS ZERO', 'COCA COLA LATA', 'COCA COLA LATA ZERO',
            'COCA COLA 2 LITROS',
        ]),
        ('CASTELÃO', ['GUARANÁ', 'GUARANA ZERO', 'PEITO DE PERU']),
        ('CENTRAL DE FRIOS', [
            'PRESUNTO', 'QUEIJO PRATO LANCHINHO', 'MUSSARELA DE BÚFULA',
            'PARMESÃO RALADO', 'MANTEIGA PAYSAN', 'CALABRESA', 'BATONS',
            'CALLEBAUT 823', 'MANTEIGA TRES MARIAS OU PIC NIC',
            'SICAO MEIO AMARGO LINHA MAIS MOEDAS',
            'SICAO BRANCO LINHA MAIS MOEDAS', 'MUSSARELA DE BÚFULA LEVITARE',
        ]),
        ('FLOR COMESTÍVEL', [
            'FLOR COMESTÍVEL', 'GELO', 'VINHAIS', 'NUTELLA',
            'REQUEIJÃO DANUBIO 10 G',
        ]),
        ('FAZENDA', ['LEITE', 'IOGURTE FAZENDA', 'QUEIJO BRANCO']),
        ('DINIZ', [
            'AMENDOAS LAMINADAS', 'FARINHA DE AMENDOAS', 'MANGA DE CONFEITAR',
            'CORANTE VERMELHO', 'CORANTE ROSA', 'GLITER DOURADO',
            'CORANTE VERDE', 'ESSENCIA DE BAUNILHA', 'LEITE EM PÓ',
            'AÇUCAR GELADO', 'LEITE CONDESNADO', 'CACAU EM PÓ',
        ]),
        ('RIZZO', ['CELOFANE', 'CARTÃO', 'ENVELOPE', 'SISAL', 'CONSERVANTE FLOR']),
        ('MERCADÃO DAS FLORES', [
            'CHÁ VERDE', 'CHÁ DE CAMOMILA', 'CHÁ DE ERVA DOCE',
        ]),
        ('MERCADO LIVRE', [
            'GUARDANAPOS', 'LACRE', 'GARRAFA 1 LITRO', 'CANUDO',
            'MASCARA DESCARTAVEL', 'SACO DE PLÁSTICO EMBALAGEM A VACUO 25X40',
            'LAMINAS PLASTIFICAR', 'COPO DESCARTAVEL', 'XAROPE DE COCO',
            'XAROPE DE MAÇA VERDE', 'XAROPE DE LIMÃO SICILIANO', 'LEITE VEGETAL',
            'SYNTHA - 6', 'LUVA PRETA',
        ]),
        ('EBD', ['NUTELLA', 'KINDER OVO']),
        ('BALAS/DOCES', [
            'BALA 7 BELO', 'PIRULITO 7 BELO', 'TRIDENT AZUL', 'TRIDENT VERDE',
            'TRIDENT ROSA', 'TRIDENT PRETO', 'TRIDENT VERMELHO', 'TIDENT CANELA',
            'CHICLETS', 'TRIDENT POTE VERDE', 'TRIDENT POTE BRANCO', 'FINI',
        ]),
        ('CAIXAS DE MDF FATIMA', [
            'MIMO 25X25', 'MÉDIA 30X30', 'ESPECIAL 35X35', 'SAPATO 33X24',
            'PRETA 30X30', 'BASE REDONDA', 'BANDEJA DE CAFÉ DA MANHÃ',
            'BASE DE CORAÇÃO',
        ]),
        ('EMBALAGEM SACO DE PÃO/SACOLA/SACO DE DELIVERY', [
            'SAO DE PÃO DE 4', 'SAO DE PÃO DE 10', 'GUARDANAPO', 'TALHER',
            'ROLO DE PAPEL MANTEIGA', 'PACOTE DELIVERY', 'SACOLA COM ALÇA',
        ]),
        ('EMBALAGEM CAIXINHAS', ['MALETA G', 'MALETA M']),
        ('FARMACIA', [
            'TORSILAX', 'LUFTAL', 'DIPIRONA', 'DORFLEX', 'BUSCOPAN', 'GASES',
            'ALGODÃO', 'NEBACETIM', 'MICROPOLO', 'ENO', 'FERNEGAM', 'VONA FLASH',
            'ABSORVENTE',
        ]),
        ('OUTROS', [
            'AÇAI', 'PINGO DE OURO', 'ETIQUETA DE VALIDADE SÉRGIO',
            'ETIQUETA DE VALIDADE',
        ]),
        ('GRÁFICA ADESIVO O PÃO PRINT', ['ADESIVO O PÃO']),
        ('ÁGUA', ['AGUA SEM GÁS 300 ML', 'ÁGUA COM GAS 300 ML']),
        ('OVOS ORGANICOS', ['OVOS ORGÂNICOS']),
    ],

    'Anesio Pinto Rosa': [
        ('FORNECEDOR DE CAFE SAINTS', ['CAFÉ COADO', 'CAFÉ EM GRÃOS', 'FILTRO']),
        ('OSMAR GERMANO', [
            'COPO 240 ML', 'TAMPA 240 ML', 'COPO 300 ML', 'TAMPA 200 ML',
            'SAQUINHO PP COOKIE', 'SAQUINHO IFOOD 35X50', 'COPO 30 ML EXPRESSO',
            'MEXEDOR DE CAFÉ',
        ]),
        ('AROMAR', [
            'PIMENTA TABASCO', 'SACHE AÇUCAR', 'SACHE AÇUCAR ORGANICO',
            'SACHE AÇUCAR MASCAVO', 'SACHE ADOÇANTE', 'SACHE SAL', 'SACHE VINAGRE',
            'SACHE AZEITE', 'PALITO', 'TODDY', 'CHOCOLATE DO PADRE', 'CANELA EM PÓ',
        ]),
        ('ATACADÃO', [
            'CAFE TRES CORAÇÕES', 'FILTRO DE CAFE', 'AÇUCAR', 'SAL',
            'LEITE CONDENSADO', 'PAPEL HIGIENICO FUNCIONÁRIO',
            'LEITE ZERO LACTOSE', 'LEITE DESNATADO', 'LEITE VEGETAL',
            'LEITE EM PÓ', 'REQUEIJÃO',
        ]),
        ('MATERIAL DE ESCRITÓRIO', [
            'TINTA IMPRESSORA L6171 BK', 'TINTA IMPRESSORA L6171 C',
            'TINTA IMPRESSORA L6171 M', 'TINTA IMPRESSORA L6171 Y',
            'PILOTO 1.0 PRETO', 'PILTO 2.0 PRETO', 'PILOTO 2.0 VERMELHO',
            'GRAMPOS', 'CLIPS', 'CANETA BIC', 'DUREX MEDIO', 'DUREX GRANDE',
            'SUPORTE DE DUREX', 'SULFITE AZUL', 'SULFITE BRANCO',
            'PAPEL VERGUE BRANCO', 'PAPEL VERGUE BEGE', 'BOBINA MAQUININHA',
            'MARCA TEXTO', 'TESOURA', 'PAPEL FOTO 10X15',
        ]),
        ('BOBINA CAIXA MONTEIRO', ['BOBINA CAIXA']),
        ('IRMÃOS BELLA COZZA', [
            'POTE 200 ML', 'POTE 600 ML', 'POTE MEL', 'GARRAFINHA DE ARRANJO',
        ]),
        ('SAFE BAND', [
            'SECANTE MAQUINA DE LAVAR', 'SABAO MAQUINA DE LAVAR',
            'PRODUTO DE LIMPEZA', 'DETERGENTE DESENGORDURANTE', 'LUVA VINIL G',
            'LUVA PRETA G', 'TOUCA', 'DETERGENTE YPE', 'ALCOOL GALÃO 70',
            'ALCOOL EM GEL GALÃO', 'ALCOOL EM GEL REFIL', 'CLORO GALÃO',
            'DESIFETANTE GALÃO', 'LIMPA ALUMINIO GALÃO', 'ESPONJA AMARELA',
            'ESPONJA AZUL', 'BOMBRIL', 'LAVA ROUPA', 'VEJA MULTIUSO',
            'VEJA DESENGOURDURANTE', 'FIBRA', 'PANO DE CHÃO BRANCO',
            'PANO DE CHÃO AZUL', 'SACO DE 100 LITROS REFORÇADO VERMELHO',
            'SACO DE 200 LITROS REFORÇADO PRETO', 'SACO DE 40 LITROS', 'RESINIT',
            'GARRAFA DE 300 ML', 'PERFEX', 'RODO PEQUENO', 'RODO MEDIO',
            'VASSOURA', 'VASSOURA PIAÇAVA', 'PÁ DE LIXO', 'BALDE',
            'SABÃO EM PEDRA', 'LISOFORM SPRAY', 'WD', 'REFIL BOM AR GLADE',
            'SOUDA CAUSTICA', 'BORRIFADOR', 'SACO PLASTICO TALHER 07X25X010',
            'LIMPA VIDRO', 'LUVA AMARELA', 'SABONETE DE MÃO',
        ]),
        ('AVANTI', [
            'PAPEL HIGIENICO CLIENTES', 'PAPEL TOALHA CLIENTES',
            'REFIL SABONETE', 'REFIL EM ALCOOL GEL',
        ]),
        ('COCA COLA', [
            'COCA COLA KS ZERO', 'COCA COLA LATA', 'COCA COLA LATA ZERO',
            'COCA COLA 2 LITROS',
        ]),
        ('CASTELÃO', ['PEITO DE PERU']),
        ('CENTRAL DE FRIOS', [
            'PRESUNTO', 'QUEIJO PRATO LANCHINHO', 'MANTEIGA PAYSAN',
            'CALLEBAUT 823', 'MANTEIGA TRES MARIAS OU PIC NIC',
            'SICAO MEIO AMARGO LINHA MAIS MOEDAS',
            'SICAO BRANCO LINHA MAIS MOEDAS', 'MUSSARELA TRES MARIAS',
        ]),
        ('FLOR COMESTÍVEL', [
            'FLOR COMESTÍVEL', 'GELO', 'VINHAIS', 'REQUEIJÃO DANUBIO 10 G',
        ]),
        ('RIZZO', ['CELOFANE', 'CARTÃO', 'ENVELOPE', 'SISAL']),
        ('MERCADÃO DAS FLORES', [
            'ÓLEO DE COCO', 'MEL', 'FLORES', 'CHA CAMOMILA', 'CHÁ ERVA DOCE',
            'FLORES PARA ARRANJOS DO SITE',
        ]),
        ('MERCADO LIVRE', [
            'GUARDANAPOS', 'LACRE', 'GARRAFA 1 LITRO', 'CANUDO',
            'MASCARA DESCARTAVEL', 'SACO DE PLÁSTICO EMBALAGEM A VACUO 25X40',
            'LAMINAS PLASTIFICAR', 'COPO DESCARTAVEL', 'XAROPE DE COCO',
            'XAROPE DE MAÇA VERDE', 'XAROPE DE LIMÃO SICILIANO', 'SYNTHA-6',
        ]),
        ('EBD', ['NUTELLA', 'KINDER OVO']),
        ('BALAS/DOCES', [
            'BALA 7 BELO', 'PIRULITO 7 BELO', 'TRIDENT AZUL', 'TRIDENT VERDE',
            'TRIDENT ROSA', 'TRIDENT PRETO', 'TRIDENT VERMELHO', 'TIDENT CANELA',
            'CHICLETS',
        ]),
        ('CAIXAS DE MDF FATIMA', [
            'MIMO 25X25', 'MÉDIA 30X30', 'ESPECIAL 35X35', 'SAPATO 33X24',
            'PRETA 30X30', 'BASE REDONDA', 'BANDEJA DE CAFÉ DA MANHÃ',
            'BASE DE CORAÇÃO',
        ]),
        ('EMBALAGEM EDUARDO', [
            'SAO DE PÃO DE 4', 'SAO DE PÃO DE 10', 'GUARDANAPO', 'TALHER',
            'ROLO DE PAPEL MANTEIGA', 'PACOTE DELIVERY', 'SACOLA COM ALÇA',
        ]),
        ('EMBALAGEM CAIXINHAS', ['MALETA G', 'MALETA M']),
        ('FARMACIA', [
            'TORSILAX', 'LUFTAL', 'DIPIRONA', 'DORFLEX', 'BUSCOPAN', 'GASES',
            'ALGODÃO', 'NEBACETIM', 'MICROPOLO', 'ENO', 'FERNEGAM', 'VONA FLASH',
            'ABSORVENTE',
        ]),
        ('OUTROS', [
            'AÇAI', 'PINGO DE OURO', 'ETIQUETA DE VALIDADE SÉRGIO',
            'ETIQUETA DE VALIDADE',
        ]),
        ('GRÁFICA ADESIVO O PÃO PRINT', ['ADESIVO O PÃO']),
        ('CWI', ['AGUA SEM GÁS 300 ML', 'ÁGUA COM GAS 300 ML']),
        ('FAZENDA', ['LEITE', 'QUEIJO BRANCO']),
    ],

    'Nebraska': [
        ('FORNECEDOR DE CAFE SAINTS', [
            'CAFÉ EM GRÃOS', 'CAFÉ COADO', 'FILTRO', 'NITRO', 'FANTASY',
            'OAT-CHATA', 'ENERGY', 'CHÁ DE CLITÓRIA', 'XAROPE DE BAUNILHA',
        ]),
        ('OSMAR GERMANO', ['COPO TRANSPARENTE', 'COPO BRANCO']),
        ('XAROPES MDG', [
            'CARAMELO SALGADO', 'MAÇA VERDE', 'MORANGO', 'COCO', 'NAMU MATCHA',
            'MATCHA',
        ]),
        ('AROMAR', [
            'PIMENTA TABASCO', 'SACHE AÇUCAR', 'SACHE AÇUCAR ORGANICO',
            'SACHE AÇUCAR MASCAVO', 'SACHE ADOÇANTE', 'SACHE SAL', 'SACHE VINAGRE',
            'SACHE AZEITE', 'PALITO', 'TODDY', 'CHOCOLATE DO PADRE', 'CANELA EM PÓ',
        ]),
        ('ATACADÃO', [
            'CAFE TRES CORAÇÕES', 'FILTRO DE CAFE', 'AÇUCAR', 'SAL',
            'PAPEL HIGIENICO FUNCIONÁRIO', 'LEITE ZERO LACTOSE', 'LEITE DESNATADO',
            'LEITE VEGETAL', 'LEITE EM PÓ', 'REQUEIJÃO', 'LEITE CONDENSADO',
        ]),
        ('MATERIAL DE ESCRITÓRIO', [
            'PILOTO 1.0 PRETO', 'PILTO 2.0 PRETO', 'PILOTO 2.0 VERMELHO',
            'GRAMPOS', 'CLIPS', 'CANETA BIC', 'DUREX MEDIO', 'DUREX GRANDE',
            'SUPORTE DE DUREX', 'BOBINA MAQUININHA', 'MARCA TEXTO', 'TESOURA',
        ]),
        ('BOBINA CAIXA MONTEIRO', ['BOBINA CAIXA']),
        ('IRMÃOS BELLA COZZA', [
            'POTE 200 ML', 'POTE 600 ML', 'POTE MEL', 'GARRAFINHA DE ARRANJO',
        ]),
        ('SAFE BAND', [
            'SECANTE MAQUINA DE LAVAR', 'SABAO MAQUINA DE LAVAR',
            'PRODUTO DE LIMPEZA', 'DETERGENTE DESENGORDURANTE', 'LUVA VINIL G',
            'LUVA PRETA G', 'TOUCA', 'DETERGENTE YPE', 'ALCOOL GALÃO 70',
            'ALCOOL EM GEL GALÃO', 'ALCOOL EM GEL REFIL', 'CLORO GALÃO',
            'DESIFETANTE GALÃO', 'LIMPA ALUMINIO GALÃO', 'ESPONJA AMARELA',
            'ESPONJA AZUL', 'BOMBRIL', 'LAVA ROUPA', 'VEJA MULTIUSO',
            'VEJA DESENGOURDURANTE', 'FIBRA', 'PANO DE CHÃO BRANCO',
            'PANO DE CHÃO AZUL', 'SACO DE 100 LITROS REFORÇADO VERMELHO',
            'SACO DE 200 LITROS REFORÇADO PRETO', 'SACO DE 40 LITROS', 'RESINIT',
            'GARRAFA DE 300 ML', 'PERFEX', 'RODO PEQUENO', 'RODO MEDIO',
            'VASSOURA', 'VASSOURA PIAÇAVA', 'PÁ DE LIXO', 'BALDE',
            'SABÃO EM PEDRA', 'LISOFORM SPRAY', 'WD', 'REFIL BOM AR GLADE',
            'SOUDA CAUSTICA', 'BORRIFADOR', 'SACO PLASTICO TALHER 07X25X010',
            'LIMPA VIDRO', 'LUVA AMARELA', 'SABONETE DE MÃO',
        ]),
        ('COCA COLA', ['COCA COLA LATA', 'COCA COLA LATA ZERO', 'COCA COLA 2 LITROS']),
        ('CASTELÃO', ['PEITO DE PERU']),
        ('CENTRAL DE FRIOS', [
            'PRESUNTO', 'QUEIJO PRATO LANCHINHO', 'MANTEIGA PAYSAN',
            'CALLEBAUT 823', 'MANTEIGA TRES MARIAS OU PIC NIC',
            'SICAO MEIO AMARGO LINHA MAIS MOEDAS',
            'SICAO BRANCO LINHA MAIS MOEDAS',
        ]),
        ('FLOR COMESTÍVEL', ['FLOR COMESTÍVEL', 'OVOS RAIAR', 'OVOS']),
        ('RIZZO', ['CELOFANE', 'CARTÃO', 'ENVELOPE', 'SISAL']),
        ('MERCADÃO DAS FLORES', ['CHA CAMOMILA', 'CHÁ ERVA DOCE', 'CHA VERDE']),
        ('MERCADO LIVRE', [
            'GUARDANAPOS', 'GARRAFA 1 LITRO', 'CANUDO', 'MASCARA DESCARTAVEL',
            'LAMINAS PLASTIFICAR', 'COPO DESCARTAVEL',
        ]),
        ('EBD', ['NUTELLA', 'KINDER OVO']),
        ('BALAS/DOCES', [
            'BALA 7 BELO', 'PIRULITO 7 BELO', 'TRIDENT AZUL', 'TRIDENT VERDE',
            'TRIDENT ROSA', 'TRIDENT PRETO', 'TRIDENT VERMELHO', 'TIDENT CANELA',
            'CHICLETS',
        ]),
        ('EMBALAGEM EDUARDO', [
            'SAO DE PÃO DE 4', 'SAO DE PÃO DE 10', 'GUARDANAPO', 'TALHER',
            'ROLO DE PAPEL MANTEIGA', 'PACOTE DELIVERY', 'SACOLA COM ALÇA',
        ]),
        ('EMBALAGEM CAIXINHAS', ['MALETA G', 'MALETA M']),
        ('FARMACIA', [
            'TORSILAX', 'LUFTAL', 'DIPIRONA', 'DORFLEX', 'BUSCOPAN', 'GASES',
            'ALGODÃO', 'NEBACETIM', 'MICROPOLO', 'ENO', 'FERNEGAM', 'VONA FLASH',
            'ABSORVENTE',
        ]),
        ('OUTROS', [
            'AÇAI', 'PINGO DE OURO', 'ETIQUETA DE VALIDADE SÉRGIO',
            'ETIQUETA DE VALIDADE',
        ]),
        ('GRÁFICA ADESIVO O PÃO PRINT', ['ADESIVO O PÃO']),
        ('CWI', ['AGUA SEM GÁS 300 ML', 'ÁGUA COM GAS 300 ML']),
        ('FAZENDA', ['LEITE', 'QUEIJO BRANCO']),
    ],

    'Industria': [
        ('AROMAR', [
            'SAL 1 KG', 'AZEITONA VERDE FATIADA', 'AZEITE 5 LITROS',
            'MOSTARDA GRANDE', 'AÇUCAR MASCAVO UNIÃO', 'CANELA EM PÓ',
        ]),
        ('MERCADO ATACADÃO', [
            'MAIZENA', 'ÓLEO', 'LAGARTO', 'LEITE', 'PIMENTÃO VERDE',
            'PIMENTÃO AMARELO', 'PIMENTÃO VERMELHO', 'BICARBONATO', 'AÇUCAR 1 KG',
            'CREME DE LEITE', 'CREME CHEESE',
        ]),
        ('PRODUTO DE LIMPEZA', [
            'DETERGENTE DESENGORDURANTE', 'LUVA VINIL G', 'TOUCA',
            'DETERGENTE YPE', 'ALCOOL GALÃO 70', 'ALCOOL EM GEL GALÃO',
            'ALCOOL EM GEL REFIL', 'CLORO GALÃO', 'DESIFETANTE GALÃO',
            'LIMPA ALUMINIO GALÃO', 'ESPONJA AMARELA', 'ESPONJA AZUL', 'BOMBRIL',
            'LAVA ROUPA', 'VEJA MULTIUSO', 'VEJA DESENGOURDURANTE', 'FIBRA',
            'PANO DE CHÃO BRANCO', 'PANO DE CHÃO AZUL',
            'SACO DE 100 LITROS REFORÇADO VERMELHO',
            'SACO DE 200 LITROS REFORÇADO PRETO', 'SACO DE 40 LITROS', 'RESINIT',
            'PERFEX', 'RODO PEQUENO', 'RODO MEDIO', 'VASSOURA', 'VASSOURA PIAÇAVA',
            'PÁ DE LIXO', 'BALDE', 'SABÃO EM PEDRA', 'LISOFORM SPRAY', 'WD',
            'REFIL BOM AR GLADE', 'SOUDA CAUSTICA', 'BORRIFADOR',
            'SACO PLASTICO TALHER 07X25X010', 'LIMPA VIDRO', 'LUVA AMARELA',
            'SABONETE DE MÃO', 'REFIL DE LT',
        ]),
        ('CENTRAL DE FRIOS', [
            'MUSSARELA DE BÚFULA', 'GONGORZOLA', 'PARMESÃO RALADO', 'CALABRESA',
            'BATONS', 'CALLEBAUT 811', 'MANTEIGA TRES MARIAS OU PIC NIC',
            'TOMATE SECO', 'CHOCOLATE COOKIE',
        ]),
        ('CALIMP', [
            'MANTEIGA FRANCESA', 'FRANCE', 'T 45- CROISSANT', 'T 65- PÃO',
            'INTEGRAL', 'CENTEIO',
        ]),
        ('FAZENDA', ['LEITE', 'IOGURTE FAZENDA']),
        ('DINIZ', [
            'MANGA DE CONFEITAR', 'CORANTE VERMELHO', 'CORANTE ROSA',
            'GLITER DOURADO', 'CORANTE VERDE', 'ESSENCIA DE BAUNILHA',
            'LEITE EM PÓ', 'AÇUCAR GELADO', 'LEITE CONDESNADO', 'CACAU EM PÓ',
        ]),
        ('MERCADÃO DAS FLORES', ['ÓLEO DE COCO', 'MEL PRODUÇÃO']),
        ('FERMENTO ITAIQUARA', [
            'FERMENTO', 'SORDOUGH DE GRÃOS E NOZES', 'NOZES', 'GERGILIM BRANCO',
            'GERGILIM PRETO', 'QUINOA VERMELHA', 'SEMENTE DE ABÓBORA',
            'SEMENTE DE GIRASSOL', 'AVEIA', 'CHIA',
        ]),
        ('PRODUTOS PARA CREME DE AMENDOAS', [
            'FARINHA DE AMENDOAS', 'GRANOLA', 'CRAMBERRY', 'AVEIA', 'CHIA',
            'CASTANHA', 'COCO', 'CORN FLAKES', 'AMENDOAS LAMINADAS',
            'FARINHA DE AMENDOIM', 'ETIQUETA DE VALIDADE SÉRGIO',
            'ETIQUETA DE VALIDADE', 'OVOS BRANCOS PRODUÇÃO', 'OVOS BRANCOS',
        ]),
        ('PAULISTA', [
            'REFIL DE SABONETE', 'REFIL DE ALCOOL', 'PAPEL HIGIENICO',
            'PAPEL TOALHA',
        ]),
    ],
}


def seed_lista_compras():
    """Popula `ItemListaCompras` com o catalogo das 4 unidades. Idempotente."""
    from app.extensions import db
    from app.models import ItemListaCompras, Loja

    total_criados = 0
    total_existentes = 0
    lojas_faltando = []

    for nome_loja, grupos in CATALOGO.items():
        loja = Loja.query.filter_by(nome=nome_loja).first()
        if not loja:
            lojas_faltando.append(nome_loja)
            continue
        ordem_global = 0
        for grupo, itens in grupos:
            # Dedup dentro do grupo (preserva primeira ocorrencia).
            vistos = set()
            for nome in itens:
                if nome in vistos:
                    continue
                vistos.add(nome)
                existe = (ItemListaCompras.query
                          .filter_by(loja_id=loja.id, grupo=grupo, nome_item=nome)
                          .first())
                if existe:
                    total_existentes += 1
                else:
                    db.session.add(ItemListaCompras(
                        loja_id=loja.id, grupo=grupo, nome_item=nome,
                        ordem=ordem_global, ativo=True,
                    ))
                    total_criados += 1
                ordem_global += 1
    if db.session.dirty or db.session.new:
        db.session.commit()

    if lojas_faltando:
        logger.warning('seed_lista_compras: lojas nao encontradas no banco: %s',
                       lojas_faltando)
    logger.info('seed_lista_compras: %d itens novos, %d ja existiam',
                total_criados, total_existentes)
    return {'criados': total_criados, 'existentes': total_existentes,
            'lojas_faltando': lojas_faltando}
