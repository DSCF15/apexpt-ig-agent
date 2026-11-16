# Instalar no Raspberry Pi 5

Tudo corre em Docker. O acesso público (só para os webhooks da Meta) passa pelo Cloudflare Tunnel, por isso **não abres portas no router**. O painel não fica na internet: abres-o pela rede local, através de SSH.

## 1. Preparar o Pi (uma vez)

1. Instala o **Raspberry Pi OS Lite (64-bit)** com o Raspberry Pi Imager. No Imager, ativa o SSH e define o utilizador e a palavra-passe.
2. Recomendado: arranca de um **SSD** (USB ou NVMe HAT) em vez do cartão SD, que se estraga com a base de dados.
3. Liga-te por SSH e instala o Docker:

```bash
ssh pi@raspberrypi.local
sudo apt update && sudo apt full-upgrade -y
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER   # sai e volta a entrar no SSH
```

## 2. Código e configuração

```bash
git clone https://github.com/DSCF15/apexpt-ig-agent.git
cd apexpt-ig-agent
cp .env.example .env
docker run --rm -v "$PWD":/srv -w /srv python:3.12-slim \
  sh -c "pip -q install cryptography && python scripts/gen_key.py"   # cola os 4 valores no .env
nano .env
```

No `.env`, preenche:
- `IG_ACCESS_TOKEN`, `IG_APP_SECRET`, `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`;
- `POSTGRES_PASSWORD`: uma palavra-passe forte, repetida no `DATABASE_URL`;
- `REDIS_URL=redis://redis:6379/0`;
- `DRY_RUN=false`;
- `CLOUDFLARE_TUNNEL_TOKEN` (passo 3).
- `PUBLIC_BASE_URL=https://agente.apexpt.app`: a Meta descarrega os ficheiros dos posts deste endereço.

Se o repositório for privado, o `git clone` pede login. Usa um *fine-grained token* do GitHub, só com leitura deste repositório.

## 3. Cloudflare Tunnel

1. Em dash.cloudflare.com, vai a **Zero Trust → Networks → Tunnels → Create a tunnel** e escolhe **Cloudflared**. Dá-lhe o nome `apexpt-pi`.
2. Copia o **token** (a parte longa a seguir a `--token`) para `CLOUDFLARE_TUNNEL_TOKEN` no `.env`.
3. Em **Public Hostname**, cria:
   - Subdomain `agente`, domain `apexpt.app`
   - **Path: `(webhook|media)/.*`** (só os webhooks e os ficheiros dos posts ficam públicos, não o painel)
   - Service: `HTTP` → `api:8000`

## 4. Arrancar

```bash
docker compose up -d --build
docker compose exec api python scripts/bootstrap.py   # valida o token do Instagram e guarda-o cifrado
docker compose ps
docker compose logs -f worker telegram                 # Ctrl+C para sair
```

Teste: `https://agente.apexpt.app/webhook/instagram` deve responder **403** (é o normal sem o token de verificação).

## 5. Webhooks da Meta

Na app Meta → Instagram → **Webhooks**:
- Callback URL: `https://agente.apexpt.app/webhook/instagram`
- Verify token: o `WEBHOOK_VERIFY_TOKEN` do `.env`
- Subscreve `comments`, `mentions` e `messages`.

O Telegram funciona em polling (serviço `telegram`) e não precisa de webhook.

## 6. Abrir o painel

No teu PC, cria um túnel SSH e abre o painel no browser:

```bash
ssh -L 8000:localhost:8000 pi@raspberrypi.local
```

Depois vai a http://localhost:8000 e entra com o `ADMIN_API_KEY`.

## Manutenção

| O quê | Comando |
|---|---|
| Atualizar para a versão do GitHub | `git pull && docker compose up -d --build` |
| Ver logs | `docker compose logs -f --tail=100` |
| Pausar o agente | `/pausa` no Telegram |
| Cópias da BD | ficam em `./backups/` (uma por dia, guarda 14 dias); copia-as para fora do Pi de vez em quando |
| Restaurar uma cópia | `gunzip -c backups/agent-AAAA-MM-DD.sql.gz \| docker compose exec -T postgres psql -U agent agent` |

O Docker volta a arrancar tudo sozinho depois de um corte de luz.
