from flask_wtf import FlaskForm
from wtforms import StringField, DecimalField, SelectField, SubmitField
from wtforms.validators import DataRequired, Optional, NumberRange, Length


class ReceitaForm(FlaskForm):
    nome = StringField('Nome da Receita', validators=[DataRequired(), Length(max=150)])
    categoria = SelectField('Categoria', choices=[
        ('pao', 'Pão'),
        ('bolo', 'Bolo'),
        ('doce', 'Doce'),
        ('salgado', 'Salgado'),
        ('outro', 'Outro'),
    ], validators=[DataRequired()])
    rendimento_qtd = DecimalField('Rendimento (Quantidade)', validators=[DataRequired(), NumberRange(min=0.01)], places=2)
    rendimento_unidade = StringField('Unidade do Rendimento', validators=[DataRequired(), Length(max=30)])
    margem_lucro = DecimalField('Margem de Lucro (%)', validators=[Optional(), NumberRange(min=0)], places=2)
    custo_adicional_pct = DecimalField('Custos Adicionais (%)', validators=[Optional(), NumberRange(min=0)], places=2)
    custo_adicional_fixo = DecimalField('Custos Adicionais Fixos (R$)', validators=[Optional(), NumberRange(min=0)], places=2)
    submit = SubmitField('Salvar')


class PrecificacaoForm(FlaskForm):
    margem_lucro = DecimalField('Margem de Lucro (%)', validators=[DataRequired(), NumberRange(min=0)], places=2)
    custo_adicional_pct = DecimalField('Custos Adicionais (%)', validators=[Optional(), NumberRange(min=0)], places=2)
    custo_adicional_fixo = DecimalField('Custos Adicionais Fixos (R$)', validators=[Optional(), NumberRange(min=0)], places=2)
    submit = SubmitField('Calcular')
