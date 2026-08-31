# qwfwd mesh — malha de rotas entre proxies

Branch: `feat/mesh-routing` (fork [tibazera/qwfwd](https://github.com/tibazera/qwfwd), upstream original [QW-Group/qwfwd](https://github.com/QW-Group/qwfwd))

## O que este update faz

Hoje, cada `qwfwd` conhece apenas os servidores QW comuns que os masters
públicos (`master.quakeworld.nu` etc.) reportam, e mede o ping direto pra
cada um deles. Ele **não sabe que outros `qwfwd` existem** como tal — um
proxy é só mais um servidor na lista, indistinguível de um servidor de jogo
real.

Este update ensina cada `qwfwd` a:

1. **Descobrir outros `qwfwd`** dentro da lista de servidores já conhecida
   via masters, sondando com um comando novo (`meshprobe`) que só outro
   `qwfwd` patcheado sabe responder. A detecção é **baseada em protocolo**,
   não em heurística de versão: se responde corretamente, é um `qwfwd`; se
   não, é tratado como servidor comum, sem efeito colateral.

2. **Medir qualidade real, não só ping instantâneo.** Cada ping direto
   contínuo (o que o `qwfwd` já faz) agora acumula uma janela das últimas 8
   amostras por servidor, calculando **média, jitter e perda de pacote (%)**
   — sem gerar nenhum pacote extra, é derivado do ciclo de ping que já
   existia.

3. **Compartilhar essas medições com outros `qwfwd`.** Cada proxy passa a
   saber não só "meu ping direto pra X", mas também "o `qwfwd` Y relata Z ms
   até X" — dados de 1 salto adicional, coletados automaticamente, sem
   configuração manual.

4. **Expor tudo isso a um coletor externo** via um comando novo
   (`meshstatus`), paginado para nunca fragmentar um pacote UDP,
   independente de quantos servidores/peers existam.

## Por que isso é melhor que o `qwfwd` atual

O `qwfwd` original já resolve "melhor ping até um proxy" (via `pingstatus`,
consumido pelo `ezQuake`/`sb_findroutes`/`connectbr`). O que falta é a
**malha entre proxies**: hoje, se o proxy de Lisboa souber que o proxy de
Fortaleza tem ping ótimo até um servidor no Brasil, essa informação nunca
sai de dentro de Fortaleza — ninguém mais sabe disso, e o jogador não tem
como aproveitar rotas de 2+ saltos que realmente existem.

Com o patch, essa informação passa a fluir entre os proxies e fica
disponível para um serviço externo (a construir) montar o grafo mundial
completo e calcular a rota de **N hops** realmente mais barata — por
exemplo, `São Paulo → Fortaleza → Lisboa` pode ser mais rápido que
`São Paulo → Lisboa` direto, e isso foi **comprovado com dados reais**
nesta sessão (ver seção de validação).

Nada disso quebra o que já existe: `pingstatus` (consumido pelo `ezQuake`
hoje) permanece **byte-a-byte idêntico**. Tudo é aditivo, atrás de comandos
novos (`meshprobe`, `meshstatus`) que um `qwfwd` não-atualizado simplesmente
ignora.

## O que muda no tráfego de rede

Contrário à suposição inicial de que "os proxies já se comunicam pelo
master, então nada muda": **os masters só fazem descoberta** (a lista de
quem existe). O tráfego peer-to-peer entre `qwfwd` — sondas `meshprobe`,
respostas `meshstatus` — é **novo**, introduzido por este patch. A auditoria
de segurança (abaixo) mede exatamente esse acréscimo.

## Validação realizada

- **23 testes automatizados** (`tests/test_mesh_protocol.py`,
  `tests/test_mesh_e2e.py`), rodando contra o binário real compilado —
  não mockado — cobrindo framing do protocolo, nonce anti-replay, rate
  limiting, compatibilidade com `pingstatus`, e descoberta ponta-a-ponta
  entre múltiplas instâncias.
- **Malha real em produção**: 4 instâncias de teste (isoladas, portas
  30501–30504, sem afetar os processos de produção reais que seguem
  intocados nas portas originais) rodando em Lisboa, São Paulo, Miami e
  Fortaleza, comunicando-se pela internet real. Confirmado: descoberta
  automática, medição de ping real entre continentes, e roteamento N-hop
  via Dijkstra sobre os dados exportados — `São Paulo → Fortaleza → Lisboa`
  (48 + 169 = 217ms) mais rápido que a rota direta `São Paulo → Lisboa`
  (223ms), achado automaticamente sem intervenção manual.
- **Compatibilidade com a malha legada**: 71 dos ~354 `qwfwd` públicos reais
  hoje já respondem `pingstatus` (protocolo antigo, inalterado) — um
  coletor externo pode absorver dados 1-hop desses nós sem exigir upgrade
  de ninguém, complementando os dados 2-hop ricos dos nós já atualizados.

## Auditoria de segurança

Revisão técnica independente concluiu **"seguro com ressalvas"** — não
"nada muda", como a suposição inicial. Achados relevantes e ações tomadas
nesta sessão:

| Achado | Severidade | Status |
|---|---|---|
| `meshstatus` podia gerar bloco de resposta maior que o limite de datagrama UDP (`128×12+10 > MAX_MSGLEN`), travando paginação com peer com muitos dados | Real, corrigido | ✅ Corrigido (cap reduzido para 100 entradas, com margem) |
| `meshprobe` consumia orçamento de rate-limit antes de validar a requisição, permitindo negar serviço com 1 pacote malformado/segundo | Real, corrigido | ✅ Corrigido (validação antes do rate limit) |
| Sonda de descoberta (`QRY_Mesh_QueryPeers`) sem throttle próprio podia gerar rajada de pacotes no boot, com centenas de servidores recém-descobertos | Real, corrigido | ✅ Corrigido (mesmo throttle do ping padrão) |
| Rate limiter tem só 64 slots — atacante com mais IPs de origem (spoofing) recicla o anel e escapa do limite | Real, não resolvido nesta rodada | ⚠️ Pendente — mesma limitação já existe em `pingstatus` hoje |
| `meshstatus` não exige nonce/cookie — qualquer IP pode consultar | Real, não resolvido nesta rodada | ⚠️ Pendente — mitigado por payload limitado a 1450 bytes (fator de amplificação baixo) |
| Tráfego adicional estimado: ~10–12% acima do ping padrão já existente, por instância | Informativo | Aceitável para o volume atual de teste |

O relatório completo, com referências linha-a-linha do código, está
registrado no histórico de commits da branch (mensagens de commit
`fix(mesh): security hardening from independent audit`).

## Estado atual

- Compilado e testado em Windows (MSVC) e Linux (GCC) — build cross-platform
  confirmado nos 4 servidores de produção reais, em diretório isolado.
- Produção real (`~/qwrumble`, portas originais) **nunca foi tocada** — as
  instâncias de teste rodam em `~/qwfwd-mesh-test/`, processos e portas
  totalmente separados.

## Próximos passos (não implementados ainda)

1. Coletor externo: serviço que consulta `meshstatus` de todos os `qwfwd`
   conhecidos (patcheados e legados via `pingstatus`), monta o grafo
   mundial, calcula rotas via Dijkstra, e publica uma tabela de rotas que
   cada `qwfwd` pode cachear localmente (decisão de arquitetura já tomada:
   o cálculo fica no coletor, não em cada proxy — ver discussão registrada
   nesta sessão para justificativa completa).
2. Nonce/cookie real em `meshstatus` para reduzir a superfície de
   amplificação, se o volume de tráfego justificar.
3. Ferramenta cliente-side, separada, que consulta a malha para orientar o
   jogador — fora do escopo deste patch no `qwfwd` em si.
