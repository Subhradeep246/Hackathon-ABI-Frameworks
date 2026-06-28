# ABI Wound Care Eligibility Dashboard

Rule-based wound care billing eligibility pipeline on the ABI Frameworks mock PCC API.

**Important:** The PCC API rate-limits ~30% of requests with HTTP 429. See [API.md](API.md) for retry requirements.

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
cp .env.example .env

# Full pipeline: ingest → extract → decide
python backend/cli.py pipeline

# Start dashboard (recommended)
./scripts/start.sh

# Or manually:
uvicorn backend.api.main:app --port 8000
```

Open http://localhost:8000

## Pipeline commands

| Command | Description |
|---------|-------------|
| `python backend/cli.py init-db` | Create SQLite schema |
| `python backend/cli.py sync` | Fetch all patients from PCC API |
| `python backend/cli.py extract` | Parse wounds from notes/assessments |
| `python backend/cli.py decide` | Run eligibility rules |
| `python backend/cli.py pipeline` | Run all three |
| `python backend/cli.py export-features` | Export CSV for Colab training |

## Train decision tree (Colab)

1. Run `export-features` after pipeline completes
2. Upload `ml/exports/features.csv` to Colab
3. Run `ml/train_model.py` (see `ml/COLAB.md`)
4. Place `decision_tree.joblib` in `ml/models/`

## Baseten GLM chatbot

Set in `.env`:
```
BASETEN_API_KEY=your_key
BASETEN_BASE_URL=https://inference.baseten.co/v1
BASETEN_MODEL=zai-org/GLM-5.2
```

Uses Baseten's OpenAI-compatible API for GLM 5.2 patient reasoning chat.

## Colab MCP

See `ml/COLAB.md`. Project MCP config: `.cursor/mcp.json` — restart Cursor after setup.

## API

- `GET /api/stats` — facility overview
- `GET /api/patients` — filtered patient list
- `GET /api/patients/{patient_id}` — detail + unknowns + sources
- `POST /api/chat` — patient reasoning chatbot
