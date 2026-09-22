# Design Analyzer Bot 🎨

Bot do Telegram que recebe uma URL pública e usa o [`designsys`](https://github.com/berodcdev/designsys) para extrair e entregar o design system do site: relatório PDF, tokens W3C, CSS variables, Tailwind, componentes e guia visual.

## Estado atual

MVP funcional sem token real no repositório. O token será adicionado apenas como variável de ambiente no deploy.

### Fluxo

1. usuário envia uma URL;
2. o bot valida o domínio e bloqueia destinos locais/privados;
3. registra a análise no SQLite e aplica a cota do plano;
4. adiciona o trabalho numa fila com concorrência limitada;
5. executa `designsys` em subprocesso com timeout;
6. entrega PDF + ZIP técnico no Telegram.

## Comandos

- `/start` — menu principal
- `/analisar <url>` — nova análise
- `/status [id]` — status
- `/historico` — últimas análises
- `/plano` — plano e uso mensal
- `/cancelar <id>` — cancela se ainda estiver na fila
- `/id` — mostra o Telegram ID
- `/setplan USER_ID free|pro|agency` — somente administradores

## Planos preparados

| Plano | Limite padrão | Páginas internas | Extração |
|---|---:|---:|---|
| Free | 1/mês | 2 | sem download de assets |
| Pro | 10/mês | 8 | completa/exhaustiva |
| Agency | 100/mês | 20 | completa/exhaustiva |

Tudo é configurável por variáveis de ambiente; ainda não há cobrança integrada.

## Segurança

- aceita apenas HTTP/HTTPS;
- rejeita credenciais embutidas na URL;
- bloqueia `localhost`, `.local`, `.internal`, `.lan` e `.home`;
- resolve DNS e rejeita IPv4/IPv6 privados, loopback, link-local, multicast, reservados e não especificados;
- valida o destino novamente imediatamente antes de iniciar a análise;
- timeout de processamento e encerramento do grupo de processos;
- número de análises simultâneas limitado;
- token e dados locais ignorados pelo Git.

> Observação: para um SaaS público em escala, a execução do Chromium deve evoluir para isolamento em containers/sandbox por job e controle de egress de rede. A validação atual reduz fortemente SSRF, mas isolamento de rede é a camada definitiva contra DNS rebinding e comportamento malicioso de páginas.

## Desenvolvimento sem token

```bash
python -m venv .venv
source .venv/bin/activate
pip install python-telegram-bot==22.8 pytest pytest-asyncio
pytest -q
ANALYZER_MOCK=1 PYTHONPATH=. python scripts/smoke.py
```

O modo `ANALYZER_MOCK=1` existe apenas para testes e gera artefatos sintéticos sem abrir Chromium.

## Produção

O Dockerfile instala o `designsys` a partir de um commit fixado e instala o Chromium do Playwright.

Variáveis mínimas:

```env
BOT_TOKEN=...
ADMIN_IDS=123456789
```

As demais estão documentadas em `.env.example`.

### Railway

O projeto já inclui `railway.toml` e `Dockerfile`. Para persistência real do SQLite no Railway, monte um volume e aponte `DATABASE_PATH`/`WORK_DIR` para ele. Em uma fase posterior, vale migrar para PostgreSQL e storage de objetos.

## Testes

A CI verifica sintaxe, testes unitários e um fluxo smoke completo offline em cada push/PR. O token do Telegram não é necessário para a suíte.
