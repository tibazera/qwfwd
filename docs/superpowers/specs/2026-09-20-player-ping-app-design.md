# App cliente de ping do jogador — design

Branch alvo: `feat/mesh-routing` (backend), app novo em repositório/pasta separada.
Data: 2026-09-20

## Problema

O mesh routing atual (`feat/mesh-routing`, ver `MESH_UPDATE.md`) mede ping
**entre proxies (`qwfwd`)**, mas nunca mede o "último salto" — o ping real do
**jogador** até cada servidor/proxy. Um jogador em São Paulo pode ter uma rota
ótima calculada entre proxies que ainda assim é ruim pra ele, porque o ponto
de entrada da malha (o proxy mais próximo dele) não é necessariamente o
melhor pra sua conexão específica.

Ideia original (mushi, QW community): app desktop que mede o ping do próprio
jogador e usa isso pra sugerir a melhor rota, e futuramente alimentar uma
malha "jogador→servidor" maior que a malha atual "servidor→servidor".

## Escopo desta v1

- App tray Windows (C#/.NET) que mede ping UDP nativo QW sob demanda.
- Dois endpoints HTTP novos no coletor Python existente (`collector/collector.py`).
- Rota calculada é **isolada por jogador**: os pings que ele reporta nunca
  viram aresta na malha global (`GraphState`), evitando poisoning por
  cliente malicioso/forjado.
- **Fora de escopo nesta v1** (fica pra depois, cada um com seu próprio
  design quando for priorizado):
  - Agregação compartilhada de pings de jogadores na malha global (exige
    outlier rejection / defesa contra sybil).
  - Integração automática com ezQuake (arquivo de config ou IPC).
  - Autenticação real (Discord OAuth).
  - Notificações de broadcast .qw / match-ready.
  - App mobile.
  - Mudança no protocolo/binário `qwfwd` em C — nada aqui toca nisso.

## Arquitetura

```
[App tray, C#/.NET, Windows]
  1. GET  {collector}/player-targets?ip=<detectado pelo request>
         → lista curta de servidores/proxies relevantes (por geo + malha conhecida)
  2. mede ping UDP QW (pacote getchallenge) pra cada um, local, sob demanda
  3. POST {collector}/player-route  {uuid, samples: [{ip, port, rtt_ms}, ...]}
         → resposta: rota calculada (lista de hops + RTT total)

[collector/collector.py — mesmo processo, 2 endpoints novos]
  4. /player-targets: reusa GraphState já carregado (sem lock extra além do
     já existente), filtra por geo, devolve lista curta.
  5. /player-route: valida payload, roda dijkstra() injetando os pings do
     jogador como arestas *locais à requisição* — nunca grava em GraphState.
```

Sem novo serviço, sem novo protocolo peer-to-peer. Reusa `_rate_limited`,
`GraphState`, `dijkstra()` já existentes no coletor.

## Componentes

### App C#/.NET (tray, Windows)

- WinForms mínimo, ícone de bandeja, sem janela principal — usuário clica
  pra "achar melhor rota" e uma janela simples de texto mostra o resultado.
- UUID gerado na primeira execução (não é login), persistido localmente
  (registry ou arquivo em `%APPDATA%`), enviado em todo request.
- Ping UDP QW: monta o pacote `getchallenge` no mesmo formato que o
  protocolo QW já usa (mesmo padrão que ezQuake usa pra sondar servidor),
  mede RTT por resposta recebida, timeout curto (ex. 1.5s), sem retry
  agressivo — falha de um alvo não bloqueia os outros.
- Erros de rede (timeout, backend fora): mostra "sem dados", nunca trava o
  tray.

### Backend: `collector/collector.py`

**`GET /player-targets?ip=<opcional>`**
- Se `ip` vier vazio, usa IP de origem do request (`self.client_address[0]`).
- Filtra a lista de nós conhecidos (mesma fonte que já alimenta
  `GraphState`) por proximidade geográfica (reusa `GeoInfo` já existente no
  coletor) — devolve lista curta (dezenas, não os ~354 nós totais).
- Sujeito ao rate-limit por IP já existente (`_rate_limited`).

**`POST /player-route`**
- Corpo: `{"uuid": "<uuid4 string>", "samples": [{"ip": str, "port": int, "rtt_ms": float}, ...]}`.
- Validação antes de processar:
  - `uuid` presente e formato uuid4 válido.
  - `samples` não vazio, tamanho máximo (ex. 50 entradas) — evita payload
    gigante forçando Dijkstra caro.
  - cada `rtt_ms`: `0 < rtt_ms <= 2000`; fora disso, amostra descartada
    (não rejeita o request inteiro, só ignora a amostra ruim).
  - `ip`/`port` precisam corresponder a nós conhecidos em `GraphState`
    (rejeita alvo arbitrário — evita usar este endpoint como oráculo de
    ping genérico/looking-glass disfarçado).
- Rate-limit: reusa `_rate_limited` (por IP). Adiciona rate-limit leve por
  `uuid` também (mesma janela, contador em memória) — cobre caso de vários
  UUIDs atrás do mesmo IP (NAT) sem enfraquecer o limite por IP existente.
- Processamento: constrói adjacency local = snapshot de `GraphState.edges`
  **mais** arestas sintéticas `(jogador_virtual → cada amostra.ip:port,
  peso=rtt_ms)`. Roda `dijkstra()` (ou uma variante que aceite adjacency
  injetada) a partir do nó virtual do jogador até o destino pedido pelo
  cliente (se o cliente não pedir destino específico, calcula o melhor
  destino de "menor ping" ou devolve top-N rotas — a definir na
  implementação, é detalhe de resposta, não de arquitetura).
  **Nada disso é escrito em `GraphState`** — vive só na duração do request.
- Resposta: `{"path": [{"ip","port"}...], "rtt_ms": float}` ou `404` se
  nenhuma rota encontrada.

### Testes

- `tests/test_player_route.py` (backend, sem socket real):
  - validação de samples (rtt fora de faixa, uuid malformado, payload
    vazio/gigante rejeitado);
  - rate-limit por uuid (segunda rajada é bloqueada);
  - isolamento: chamar `/player-route` não altera `graph.edges` global
    (assert antes/depois);
  - resposta de rota correta dado um `GraphState` fake + amostras fake
    (caminho e RTT esperados batem).
- App C#: sem suíte formal na v1 (protótipo) — teste manual do parsing do
  pacote QW e do round-trip HTTP. `ponytail:` cobertura mínima, adicionar
  testes automatizados se o app sair de protótipo.

## Erros e limites

- Todo erro de validação em `/player-route` responde 400 com corpo mínimo
  (sem detalhar motivo linha-a-linha, pra não virar oráculo de debug pra
  quem está sondando o endpoint).
- `/player-targets` e `/player-route` competem pelo mesmo
  `_MAX_CONCURRENT_REQUESTS` (semáforo já existente) — sem budget dedicado
  na v1; se o volume justificar, separar depois.

## Riscos aceitos nesta v1 (documentados, não resolvidos agora)

- Sybil: jogador pode gerar múltiplos UUIDs falsos. Não afeta a malha
  global (isolamento por design), só poderia inflar rate-limit — mitigado
  pelo rate-limit por IP já existente.
- App C# sem assinatura de código: Windows Defender pode alertar no
  primeiro uso. Aceito pra fase de protótipo; assinatura fica pra quando
  o app for distribuído fora de um grupo de teste.
