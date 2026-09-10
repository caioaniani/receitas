"""SDK assíncrono e rede lenta: executar o JS real e conferir no serviço."""
import json
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.extensions import db
from app.models import (
    Funcionario,
    TreinoCheckpoint,
    TreinoProgressoVideo,
    TreinoTemporada,
    TreinoTrilha,
    TreinoVideo,
)
from app.services import treino_video
from app.utils import hoje

_HARNESS = r"""
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
const eventos = {}, pedidos = [], estadosEspera = [];
let agora = 0, timer, rejeitarPlay = false;
const player = {currentTime: 0, playbackRate: 1, paused: true, seeking: false,
  addEventListener: (nome, fn) => { eventos[nome] = fn; },
  play: () => Promise.resolve().then(() => {
    if (rejeitarPlay) { rejeitarPlay = false; throw new Error('Autoplay bloqueado'); }
    if (player.paused) { player.paused = false; eventos.play(); }
  }),
  pause: () => { Promise.resolve().then(() => {
    if (!player.paused) { player.paused = true; eventos.pause(); }
  }); }
};
const bar = {style: {}}, pct = {}, msg = {};
const sandbox = {window: {}, Promise, encodeURIComponent,
  setInterval: (fn, ms) => { assert.equal(ms, 15000); timer = fn; },
  fetch: (url, opcoes) => new Promise(resolve => {
    const corpo = new URLSearchParams(opcoes.body);
    pedidos.push({recebido: agora, posicao: Number(corpo.get('t')),
      evento: corpo.get('evento'), velocidade: Number(corpo.get('v')), resolve});
  })};
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), sandbox);
sandbox.window.TreinoProgressoPlayer({player, bar, pct, msg,
  url: '/heartbeat', csrf: 'token', onAguardar: valor => estadosEspera.push(valor)});
const flush = async () => { for (let i = 0; i < 40; i++) await Promise.resolve(); };
const responder = async (indice, dados = {}, ok = true) => {
  assert.ok(pedidos[indice], 'Pedido ausente: ' + indice);
  pedidos[indice].resolve({ok, json: async () => ({ok: true, pct: 0, ...dados})});
  await flush();
};
const avancar = instante => {
  if (!player.paused) player.currentTime += (instante - agora) * player.playbackRate;
  agora = instante;
};
const clicar = async () => { await player.play(); await flush(); };
const terminar = async () => { player.pause(); await flush(); eventos.ended(); await flush(); };
const buscar = async posicao => {
  player.seeking = true; eventos.seeking(); await flush();
  player.currentTime = posicao; player.seeking = false; eventos.seeked(); await flush();
};
"""


def _executar_js(script):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js não disponível')
    arquivo = (Path(__file__).resolve().parents[1]
               / 'app/static/js/treino-progresso.js')
    codigo = _HARNESS + '\n(async () => {\n' + script + r"""
})().catch(erro => { console.error(erro); process.exitCode = 1; });
"""
    resultado = subprocess.run(
        [node, '-e', codigo, str(arquivo)], text=True,
        capture_output=True, check=False)
    assert resultado.returncode == 0, resultado.stdout + resultado.stderr
    return json.loads(resultado.stdout) if resultado.stdout else None


def test_sdk_assincrono_aguarda_baseline_falha_e_tenta_novamente():
    _executar_js(r"""
      timer(); await flush();
      assert.equal(pedidos.length, 0); // Nenhum autoplay ao carregar.
      await clicar();
      assert.equal(player.paused, true);
      assert.equal(pedidos.length, 1);
      assert.equal(pedidos[0].evento, 'play'); // A pausa interna não vira POST.
      assert.match(msg.textContent, /Aguarde/);
      avancar(20); timer(); await flush();
      assert.equal(player.currentTime, 0); // Não assiste antes de confirmar.
      assert.equal(pedidos.length, 1);
      await responder(0, {}, false);
      assert.equal(player.paused, true);
      assert.match(msg.textContent, /Não foi possível salvar/);
      await clicar();
      rejeitarPlay = true;
      await responder(1);
      assert.equal(player.paused, true);
      assert.match(msg.textContent, /Toque em Reproduzir/);
      assert.equal(estadosEspera.at(-1), false);
      await clicar(); await responder(2);
      assert.equal(player.paused, false);
      assert.equal(pedidos.length, 3); // Play autorizado não recursa.
      avancar(35); timer(); await flush();
      assert.equal(pedidos[3].posicao, 15);
      await responder(3, {pct: 60});
      avancar(44); await terminar();
      await responder(4, {pct: 95});
      await responder(5, {pct: 95, concluido: true});
      assert.equal(bar.style.width, '100%');
      assert.equal(pct.textContent, '100%');
      assert.match(msg.textContent, /Concluído/);
    """)


def test_cliques_repetidos_e_busca_invalidam_confirmacao_antiga():
    _executar_js(r"""
      await clicar();
      avancar(5); await clicar(); // Outra intenção enquanto a primeira aguarda.
      assert.equal(player.currentTime, 0);
      avancar(6); await buscar(50); // A confirmação de 0 não pode tocar em 50.
      avancar(20); await responder(0);
      assert.equal(player.paused, true);
      assert.equal(pedidos.length, 2);
      await responder(1);
      assert.equal(player.paused, true);
      assert.equal(pedidos[2].posicao, 50);
      await responder(2);
      assert.equal(player.paused, false);
      assert.equal(pedidos.length, 3);
      // Buscar enquanto já reproduz também exige uma confirmação nova.
      avancar(23); await buscar(10);
      assert.equal(player.paused, true);
      assert.equal(pedidos[3].posicao, 10);
      await responder(3);
      assert.equal(player.paused, false);
    """)


def test_heartbeat_pendente_coalesce_e_amostra_ao_enviar():
    _executar_js(r"""
      await clicar(); await responder(0);
      avancar(15); timer(); await flush(); // Pedido em andamento.
      avancar(30); timer();
      avancar(35); timer(); // Coalesce o pendente.
      avancar(36); await responder(1);
      assert.equal(pedidos.length, 3);
      assert.equal(pedidos[2].posicao, 36);
      avancar(40); timer();
      avancar(42); await terminar(); // Pausa substitui periódico ainda pendente.
      await responder(2);
      assert.equal(pedidos.length, 4);
      assert.equal(pedidos[3].evento, 'pause');
      assert.equal(pedidos[3].posicao, 42);
      await responder(3); await responder(4);
    """)


@pytest.mark.parametrize('repetir_clique, pausa_lenta', [(False, False), (True, False), (False, True)])
def test_baseline_lenta_24s_com_relogio_real_do_servico(
        app, monkeypatch, repetir_clique, pausa_lenta):
    """O clique inicial recebe resposta em 20s; só então os 24s são assistidos.

    Cliques aos 5/6s não fazem a pessoa assistir enquanto o SDK está pausado.
    Os instantes de recebimento alimentam exclusivamente o relógio do servidor.
    """
    amostras = _executar_js(r"""
      await clicar();
      avancar(5); player.pause(); await flush();
      avancar(6);
    """ + ('await clicar();' if repetir_clique else '') + r"""
      avancar(15); timer(); await flush();
      assert.equal(player.currentTime, 0);
      assert.equal(player.paused, true);
      avancar(20); await responder(0);
    """ + (r"""
      assert.equal(player.paused, true);
      assert.equal(pedidos[1].recebido, 20);
      await responder(1);
    """ if repetir_clique else '') + r"""
      assert.equal(player.paused, false);
      const base = pedidos.length;
    """ + (r"""
      avancar(25); player.pause(); await flush(); await responder(base);
      assert.equal(player.currentTime, 5); // Pausa para responder o checkpoint.
      avancar(30); await clicar();
      avancar(50); timer(); await flush();
      assert.equal(player.currentTime, 5);
      assert.equal(player.paused, true); // Retomada também aguarda 20s.
      await responder(base + 1);
      avancar(60); timer(); await flush(); await responder(base + 2);
      avancar(69); await terminar();
      assert.equal(player.currentTime, 24);
      await responder(base + 3); await responder(base + 4);
    """ if pausa_lenta else r"""
      avancar(35); timer(); await flush(); await responder(base);
      avancar(44); await terminar();
      assert.equal(player.currentTime, 24);
      await responder(base + 1); await responder(base + 2);
    """) + r"""
      process.stdout.write(JSON.stringify(pedidos));
    """)
    with app.app_context():
        funcionario = Funcionario(nome='Aluno fila lenta', cpf='111.111.111-11')
        trilha = TreinoTrilha(nome='Fila lenta')
        temporada = TreinoTemporada(
            nome='Fila lenta', inicio=hoje() - timedelta(days=1),
            fim=hoje() + timedelta(days=30), status='ATIVA')
        db.session.add_all([funcionario, trilha, temporada])
        db.session.flush()
        video = TreinoVideo(trilha_id=trilha.id, titulo='Fila lenta', duracao_segundos=24)
        db.session.add(video)
        db.session.commit()
        checkpoint = None
        if pausa_lenta:
            checkpoint = TreinoCheckpoint(
                video_id=video.id, segundo=5, enunciado='Pergunta',
                alternativas=['Sim', 'Não'], indice_correto=0)
            db.session.add(checkpoint)
            db.session.commit()
        inicio = datetime(2026, 9, 10, 10)
        for amostra in amostras:
            recebido = inicio + timedelta(seconds=amostra['recebido'])
            monkeypatch.setattr(treino_video, 'agora', lambda recebido=recebido: recebido)
            if checkpoint and amostra['evento'] == 'play' and amostra['posicao'] == 5:
                treino_video.responder_checkpoint(funcionario, checkpoint, 0)
            resposta = treino_video.heartbeat(
                funcionario, video, amostra['posicao'], amostra['velocidade'],
                evento=amostra['evento'])
        assert resposta['concluido']
        salvo = TreinoProgressoVideo.query.filter_by(video_id=video.id).one()
        assert salvo.percentual == 100
        assert salvo.tempo_real_decorrido == 24
