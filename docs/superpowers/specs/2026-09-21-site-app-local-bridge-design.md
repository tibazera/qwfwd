# Site ↔ App local bridge — design

Branch alvo: `feat/mesh-routing` (app) + `gh-pages` (site)
Data: 2026-09-21

## Problema

O site público (`gh-pages/index.html`) mede o ping do jogador via STUN
(`measureProxyRttViaStun`), que só funciona nos proxies `qwfwd` que rodam o
STUN responder (opt-in, nem todos rodam). Quando STUN não está disponível,
o site cai para uma **estimativa geográfica** (distância em linha reta),
que não é medição real.

O app desktop (`player-ping-app/`, C#/.NET) já mede ping real via UDP
(`getchallenge`, o próprio protocolo QW) contra qualquer servidor — mais
preciso e universal que STUN, mas roda isolado: o site não sabe que o app
existe nem usa seus dados.

## Spike de validação (já executado)

Antes de desenhar, validou-se a suposição crítica: uma página servida via
HTTPS real (GitHub Pages) consegue `fetch()` um endpoint HTTP local
(`127.0.0.1:PORTA`)? Testado com um servidor Python mínimo na porta 5757 e
um `fetch()` temporário publicado no site real — **confirmado que
funciona**, sem bloqueio de mixed-content nem exigência de permissão
extra (Private Network Access) no Chrome testado. Teste removido do site
após validação (commit revertido).

## Escopo desta mudança

Quando o app está aberto na máquina do jogador, o site usa a medição real
do app em vez de STUN/estimativa. Quando o app não está aberto (a maioria
dos visitantes), o site continua exatamente como hoje — sem popup, sem
erro visível, fallback silencioso.

**Fora de escopo:**
- App não vira serviço sempre-ativo (sem auto-start com o Windows) — só
  responde enquanto o processo está aberto (mesmo minimizado na bandeja).
- Não modifica o backend Python (`collector.py`) nem o algoritmo de rota
  (`/player-route`, `/routes-to`) — ambos já corretos, confirmados por
  teste real (São Paulo → Fortaleza → Lisboa via mesh, ver sessão anterior).
- Não adiciona autenticação/HTTPS ao servidor local — dado exposto (RTT em
  ms pra um IP:porta já público na malha) não é sensível.

## Arquitetura

```
[Site, HTTPS, gh-pages]
  1. Usuário clica num servidor no mapa (handleProxySelection, já existe)
  2. Para cada proxy candidato de entrada (nearbyEntryCandidates, já existe):
     a. Tenta measureViaLocalApp(addr) - fetch local, timeout ~400ms
        - Sucesso -> RTT real do app, marca "medido via app"
        - Falha/timeout -> tenta measureProxyRttViaStun(addr) (já existe)
          - Sucesso -> RTT real via STUN, marca "medido via STUN"
          - Falha -> estimatePlayerLegMs(distanceKm) (já existe), marca "estimado"
  3. Resto do fluxo (chooseNearbyDestination, /routes-to) inalterado

[App, player-ping-app/, C#/.NET, mesmo processo que já roda hoje]
  4. HttpListener em 127.0.0.1:5757, sobe junto com o MainForm
  5. GET /ping?target=ip:port -> chama QwPing.MeasureAsync (já existe)
     -> {"rtt_ms": 45.2} ou {"error": "timeout"}
```

Nenhum novo processo, nenhum novo protocolo binário — HTTP simples, mesmo
padrão CORS-aberto que o coletor Python já usa.

## Componentes

### App: `LocalPingServer.cs` (novo arquivo)

- Classe que encapsula um `HttpListener` escutando em
  `http://127.0.0.1:5757/`.
- `Start()` chamado no construtor do `MainForm`, `Stop()` chamado em
  `OnExit` (quando o processo realmente encerra, não ao minimizar pra
  bandeja).
- Único endpoint: `GET /ping?target={ip}:{port}`.
  - Valida o parâmetro `target` (formato `ip:port`, ip é IPv4 válido,
    port numérico) — 400 se malformado.
  - Chama `QwPing.MeasureAsync(ip, port)` (já existe, sem modificação).
  - Responde `200 {"rtt_ms": <double>}` se mediu, `200 {"error": "timeout"}`
    se não respondeu (200, não erro HTTP — é uma resposta válida "não
    consegui medir", não uma falha do endpoint em si).
  - Headers CORS: `Access-Control-Allow-Origin: *` (dado não sensível,
    mesma postura do coletor Python existente).
- Tratamento de erro: se a porta 5757 já estiver em uso (segunda instância
  do app aberta, ou outro processo usando a porta), `Start()` falha
  silenciosamente — logga mas não impede o resto do app de funcionar. Uma
  segunda instância do app simplesmente não expõe o servidor local, mas
  continua utilizável para o fluxo normal (scan manual).

### Site: nova função `measureViaLocalApp` em `index.html`

- `async function measureViaLocalApp(addr)`: faz
  `fetch('http://127.0.0.1:5757/ping?target=' + encodeURIComponent(addr), { signal: AbortSignal.timeout(400) })`,
  retorna o RTT numérico ou `null` em qualquer falha (timeout, conexão
  recusada, erro de parsing) — nunca lança exceção pro chamador.
- `chooseNearbyDestination` (já existe): antes de chamar
  `measureProxyRttViaStun(candidate.addr)`, tenta
  `measureViaLocalApp(candidate.addr)` primeiro. Usa o primeiro que
  retornar um número; se ambos falharem, cai para
  `estimatePlayerLegMs(candidate.distanceKm)` como já faz hoje.
- Novo rótulo de origem da medição (`playerLegSource`: `"app"` | `"stun"` |
  `"estimate"`) substituindo o atual booleano `playerLegMeasured` —
  `renderNearbyResults` usa isso pra mostrar "medido via app" (novo texto
  de tradução) em vez de "medido via STUN" quando aplicável.

### Testes

- App: `tests/test_local_ping_server.py`? Não — é C#, sem framework de
  teste formal no app hoje (v1 já documentou essa decisão). Verificação
  manual: subir o app, `curl http://127.0.0.1:5757/ping?target=<ip
  conhecido>:<porta>`, confirmar resposta JSON válida com RTT real.
- Site: sem framework de teste no `index.html` hoje. Verificação manual:
  com o app aberto, abrir o site, clicar num servidor, confirmar que o
  badge mostra "medido via app"; fechar o app, repetir, confirmar fallback
  silencioso para STUN/estimativa sem erro visível.

## Riscos aceitos

- `AbortSignal.timeout(400)` pode não ser suportado em navegadores muito
  antigos — degrada para o comportamento de erro genérico (cai pro STUN
  de qualquer forma, só não com o timeout preciso). Aceitável, não é alvo
  de suporte a navegador legado.
- Porta 5757 fixa, sem configuração — se colidir com outro software na
  máquina do usuário (raro, não é porta convencional), o servidor local
  simplesmente falha ao subir, app continua funcional sem a ponte.
- Site fazendo `fetch()` pra `127.0.0.1` de qualquer visitante (mesmo sem
  o app rodando) gera uma tentativa de conexão que falha rápido
  (timeout 400ms) — leve custo de latência por proxy candidato testado
  (`nearbyEntryCandidates` já limita a 12 candidatos), aceitável.
