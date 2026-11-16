# ApexPT — Agente de Instagram

Agente que gere a conta de Instagram da ApexPT através da **API oficial da Meta** (Instagram API with Instagram Login):

- **Publica posts agendados** (imagem, Reel, Story, carrossel), respeitando um limite próprio por dia e a quota da Meta.
- **Recebe comentários, DMs e menções** em tempo real (webhooks), com deduplicação.
- **Classifica → decide → redige → verifica** cada mensagem com Claude e guardrails determinísticos.
- **Aprovação humana no Telegram**: botões Publicar / Descartar / Esconder. Para editar, respondes à mensagem do bot com o texto final.
- **Kill switch** (`/pausa`, `/retoma`), limites por hora, janela de 24 h nas DMs e renovação automática do token (cifrado com AES-256-GCM na BD).

Por omissão corre em `AUTONOMY_MODE=approval`: **nada é publicado sem passar por ti**. A especificação completa está no documento do projeto.

## Estrutura

```
app/
  agent.py      lógica do agente (webhooks, comentários, DMs, posts, tokens)
  brain.py      LLM: classificar, redigir, verificar
  policy.py     intenção → auto | approval | hide | ignore
  guardrails.py verificações depois do LLM (links, €, saúde, telefones, repetição…)
  ig.py         cliente da Graph API (httpx, só endpoints oficiais)
  telegram.py   aprovação e alertas
  ratelimit.py  limites próprios + kill switch (Redis)
  tasks.py      tarefas Celery + agenda (Beat)
  main.py       FastAPI: /webhook/instagram, /webhook/telegram, /posts, /drafts, /admin/*
knowledge/      persona.md + faq.md  ← o agente só afirma o que está aqui
scripts/        gen_key.py, bootstrap.py, reply_once.py, schedule_post.py
tests/          54 testes (pytest), sem rede
```

## 1. Preparar a conta e a app Meta (uma vez)

1. A conta @apexpt tem de ser **profissional** (Business ou Creator).
2. Em [developers.facebook.com](https://developers.facebook.com) cria uma app do tipo **Business** e adiciona o produto **Instagram → API setup with Instagram Login**.
3. Em *Generate access tokens*, adiciona a conta e gera o token com as permissões `instagram_business_basic`, `instagram_business_content_publish`, `instagram_business_manage_comments` e `instagram_business_manage_messages`.
4. Copia o **Instagram App Secret** (é com ele que se validam os webhooks).

Como a app só serve a tua própria conta, *Standard Access* chega e não precisas de App Review.

## 2. Arrancar em local (sem Docker)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python scripts/gen_key.py        # cola os valores no .env
# preenche IG_ACCESS_TOKEN, IG_APP_SECRET, ANTHROPIC_API_KEY
# e, para local: DATABASE_URL=sqlite:///./agent.db  REDIS_URL=redis://localhost:6379/0
python scripts/bootstrap.py      # valida o token, mostra o IG_USER_ID, guarda-o cifrado
python scripts/reply_once.py     # PoC: responde ao comentário mais recente, com confirmação
```

Serviços (três terminais, com o Redis a correr):

```bash
uvicorn app.main:app --reload --port 8000
celery -A app.tasks worker --loglevel=info
celery -A app.tasks beat --loglevel=info
```

## 3. Telegram (aprovação)

1. No Telegram, fala com o **@BotFather** → `/newbot` → copia o token para `TELEGRAM_BOT_TOKEN`.
2. Manda uma mensagem ao teu bot e abre `https://api.telegram.org/bot<TOKEN>/getUpdates` para ver o teu `chat.id` → `TELEGRAM_CHAT_ID`.
3. Regista o webhook (precisa de HTTPS público, ver passo 4):

```bash
curl "https://api.telegram.org/bot<TOKEN>/setWebhook" \
  -d url=https://agente.apexpt.app/webhook/telegram \
  -d secret_token=<TELEGRAM_WEBHOOK_SECRET>
```

Comandos: `/pausa`, `/retoma`, `/estado`.

## 4. Produção (VPS com Docker)

```bash
docker compose up -d --build
docker compose exec api python scripts/bootstrap.py
```

A API fica em `127.0.0.1:8000`; põe à frente um proxy com HTTPS (Caddy: `agente.apexpt.app { reverse_proxy localhost:8000 }`) ou um Cloudflare Tunnel.

Depois, na app Meta → Instagram → **Webhooks**:
- Callback URL: `https://agente.apexpt.app/webhook/instagram`
- Verify token: o `WEBHOOK_VERIFY_TOKEN` do `.env`
- Subscreve `comments`, `mentions` e `messages`.

## 5. Agendar posts

O ficheiro tem de estar num **URL https público** (ex.: Cloudflare R2), porque é a Meta que o descarrega.

```bash
python scripts/schedule_post.py REELS https://media.apexpt.app/reel-01.mp4 \
  "Convida os teus clientes em segundos. apexpt.app" 2026-11-03T18:30:00+00:00
```

Ou diretamente: `POST /posts` com o cabeçalho `X-API-Key`. Listar: `GET /posts`; cancelar: `DELETE /posts/{id}`; rascunhos: `GET /drafts`.

## Painel

Abre `http://localhost:8000/` e cola o `ADMIN_API_KEY`. No painel podes:
- ver o estado do agente (pausa, modo, token, o que falta configurar);
- aprovar, editar, descartar ou esconder rascunhos, tal como no Telegram;
- agendar e cancelar posts;
- pausar e retomar o agente.

Usa `GET /admin/status` e `POST /drafts/{id}/decision` (`{"action": "approve|discard|hide", "edited_text": "..."}`).
Em produção, o painel fica atrás do mesmo proxy HTTPS que a API.

No Windows, o worker do Celery precisa de `--pool=solo`: `celery -A app.tasks worker --pool=solo --loglevel=info`.

## Aprovação de posts

Com `POST_APPROVAL=true` (por omissão), um post agendado fica **por aprovar**. O bot envia-te a pré-visualização (tipo, hora em Lisboa, links e legenda) com os botões **✅ Aprovar** e **❌ Rejeitar**:
- Para mudar a legenda, responde à mensagem do bot com o texto final. O post fica aprovado com a legenda nova.
- Se chegar a hora sem aprovação, o post **não sai** (fica `expired`) e recebes um aviso.
- O painel tem os mesmos botões (`POST /posts/{id}/decision` com `approve` ou `reject`).

## Testar em local (sem Instagram)

Com `DRY_RUN=true` no `.env`:
- Nada vai para o Instagram: as respostas e os posts só aparecem no log do worker como `[DRY_RUN]`.
- O painel mostra um formulário **Simular** que faz entrar comentários e DMs falsos pelo fluxo verdadeiro: classificação, rascunho, guardrails, Telegram e publicação.
- Sem `ANTHROPIC_API_KEY`, as respostas são fixas e tudo vai para aprovação. Com a chave, quem escreve é o Claude.
- O bot do Telegram corre em **modo polling** (`scripts/telegram_poll.py`), por isso não precisa de HTTPS. Na primeira vez, sem `TELEGRAM_CHAT_ID`, mostra-te o teu chat.id.

Para arrancar tudo (API, worker, agenda e bot, cada um na sua janela):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start_local.ps1
```

Antes de produção: `DRY_RUN=false`, webhook do Telegram em vez do polling, e o resto da secção 4.

## 6. Antes de ligar o modo `auto`

- [ ] Preencher `knowledge/faq.md` (planos, preços, suporte): sem isto, todas as perguntas de preço vão para ti.
- [ ] Substituir os exemplos em `knowledge/persona.md` por 15–20 respostas tuas.
- [ ] Correr 2–3 semanas em `approval` e ver em `GET /drafts` quantos rascunhos aprovas sem editar.
- [ ] Só com ≥90% aprovados sem edição: `AUTONOMY_MODE=auto`. Saúde, reclamações e suporte continuam sempre a passar por ti.

## Testes

```bash
pytest -q
```

## O que fica de fora (de propósito)

- Seguir, gostar ou comentar em contas de terceiros: a API oficial não o permite e automatizar isso pelo browser viola os Termos do Instagram. A interação com o nicho continua a ser manual.
- `pgvector`/RAG: a base de conhecimento ainda é pequena e vai inteira no prompt. Quando passar de ~20 páginas, troca-se por pesquisa vetorial.
