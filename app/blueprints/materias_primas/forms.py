from flask_wtf import FlaskForm
from wtforms import StringField, DecimalField, SelectField, SubmitField
from wtforms.validators import DataRequired, Optional, NumberRange, Length


class MateriaPrimaForm(FlaskForm):
    nome = StringField('Nome', validators=[DataRequired(), Length(max=100)])
    unidade = SelectField('Unidade de Medida', choices=[
        ('kg', 'kg'),
        ('g', 'g'),
        ('litro', 'Litro'),
        ('ml', 'ml'),
        ('unidade', 'Unidade'),
    ], validators=[DataRequired()])
    preco = DecimalField('Preço por Unidade (R$)', validators=[DataRequired(), NumberRange(min=0)], places=2)
    fornecedor = StringField('Fornecedor', validators=[Optional(), Length(max=100)])
    submit = SubmitField('Salvar')
