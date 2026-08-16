# Assistente da Igreja — WhatsApp + LLM + Departamentos

Quando um número qualquer manda mensagem para o contato da igreja no WhatsApp,
o assistente:

1. Recebe a mensagem via **Evolution API** (webhook).
2. Usa uma **LLM online** (API compatível com OpenAI) para classificar a
   mensagem no departamento certo e gerar uma resposta para o membro.
3. Encaminha a mensagem para o **grupo do departamento** correspondente
   (ex.: Louvor, Juventude, Crianças) e responde o membro de volta.

O **board** (frontend React + Tailwind) serve para configurar o contato principal,
a LLM, os departamentos com seus grupos, e acompanhar o log de mensagens.

> Este repositório contém o **backend**. O frontend vive em um repositório
> separado (`frontend/`).

## Arquitetura

```
[WhatsApp] <-> [Evolution API] <-> [Backend FastAPI] <-> [LLM online (OpenAI-compatível)]
                                        |
                                   [Postgres] + [Board React]
```

- `backend/` — FastAPI + SQLAlchemy + Postgres (este repositório)
- `frontend/` — React + Vite + Tailwind CSS (repositório separado)

## Subir tudo com Docker (recomendado)

Um único `docker compose` sobe Postgres, Redis, Evolution API e o backend:

```bash
cd backend
cp .env.example .env        # edite EVOLUTION_API_KEY e as configs da LLM online
docker compose up -d --build
```

> A LLM é consumida **online** (ex.: OpenAI, Groq, Mistral). Configure
> `LLM_BASE_URL`, `LLM_MODEL` e `LLM_API_KEY` no `.env` — o backend exige
> acesso à internet para funcionar.

O backend sobe em `http://localhost:8000` (docs em `/docs`) usando Postgres
(bancos `evolution` e `church` no mesmo container Postgres).

Depois:

1. Crie a instância `church` na Evolution
   (`POST /instance/create` com `"instanceName": "church"` e o `AUTHENTICATION_API_KEY`)
   ou escaneie o QR direto pelo board.
2. Aponte o webhook da Evolution para
   `http://backend:8000/webhook/evolution` (já configurado no compose,
   serviço `WEBHOOK_GLOBAL_URL`; troque por IP público se precisar de acesso externo).

Comandos úteis:

```bash
make up      # docker compose up -d --build
make logs    # docker compose logs -f
make down    # docker compose down
```

## Deploy no Render

O Render **não executa o docker-compose**. Use o `render.yaml` (Blueprint),
que cria o Web Service do backend (via Dockerfile) + um Postgres gerenciado.

1. Envie este repositório para o GitHub e ajuste o campo `repo` em `render.yaml`.
2. Em https://dashboard.render.com/blueprints, cole a URL do repositório.
3. Preencha no dashboard os segredos marcados com `sync: false`:
   - `EVOLUTION_BASE_URL` — URL **pública** da sua Evolution API
     (ex.: `http://SEU-IP:8080`), pois o Render não enxerga redes internas.
   - `EVOLUTION_API_KEY` — a global `AUTHENTICATION_API_KEY` da Evolution.
   - `LLM_API_KEY` — chave do provedor da LLM online.
   - `WEBHOOK_TOKEN` — token opcional para validar o webhook.
4. Aponte o webhook da Evolution para a URL pública do Render:
   `https://<church-backend>.onrender.com/webhook/evolution`.

Observações:
- O `DATABASE_URL` é injetado automaticamente pelo Render Postgres
  (o backend normaliza `postgresql://` para o driver psycopg no `database.py`).
- A porta do Render (`$PORT`) é respeitada pelo Dockerfile (`${PORT:-8000}`).
- Evolution API e Redis continuam rodando fora do Render.

## Backend sem Docker (desenvolvimento)

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # ajuste os valores
make dev               # uvicorn main:app --reload  -> :8000
```

Sem Docker o default é SQLite (`church.db`). Para usar Postgres local,
altere `DATABASE_URL` no `.env` (ex.: `postgresql+psycopg://postgres:postgres@localhost:5432/church`).

## Variáveis de ambiente

| Variável | Descrição |
|---|---|
| `DATABASE_URL` | URL do banco (SQLite local / Postgres no Docker) |
| `EVOLUTION_BASE_URL` | URL da Evolution API (Docker: `http://evolution:8080`) |
| `EVOLUTION_API_KEY` | A global `AUTHENTICATION_API_KEY` da Evolution |
| `EVOLUTION_INSTANCE` | Nome da instância (padrão `church`) |
| `WEBHOOK_TOKEN` | Token opcional para validar o webhook |
| `LLM_BASE_URL` | URL da API online compatível com OpenAI (ex.: `https://api.openai.com/v1`) |
| `LLM_MODEL` | Modelo (ex.: `gpt-4o-mini`) |
| `LLM_API_KEY` | Chave da API do provedor (obrigatória — LLM online) |
| `LLM_TEMPERATURE` | Temperatura da LLM |

## Frontend

```bash
cd frontend
npm install
npm run dev        # http://localhost:5173 (proxy /api -> :8000)
```

Build de produção: `npm run build` (gera `dist/`).

## Evolution API

O webhook da Evolution deve apontar para:

```
POST http://<seu-backend>:8000/webhook/evolution
```

No painel da instância, em **Webhooks**, habilite (pelo menos) o evento
**MESSAGES UPSERT** com essa URL. Se definir `WEBHOOK_TOKEN`, configure o mesmo
valor no campo de API Key do webhook na Evolution.

Para descobrir o JID de um grupo (necessário no board): entre no grupo, envie
qualquer mensagem e veja o `remoteJid` (termina em `@g.us`) nas mensagens
recebidas da instância.

## Fluxo de funcionamento

1. Alguém manda WhatsApp para o contato da igreja.
2. O webhook da Evolution entrega a mensagem ao backend.
3. A LLM classifica a mensagem entre os departamentos cadastrados e gera uma
   resposta.
4. O backend encaminha a mensagem ao grupo do departamento escolhido e envia a
   resposta da LLM de volta ao membro.
5. Tudo fica registrado no log de mensagens do board.
