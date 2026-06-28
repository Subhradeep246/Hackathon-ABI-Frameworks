# Colab MCP + Baseten Setup for Pulse

## Fastest path: one-click notebook (no uploads)

Use **`ml/train_colab_oneclick.ipynb`** — features.csv is embedded. No file uploads.

1. In Colab (logged in as **tanmaysahu392001@gmail.com**): **File → Upload notebook**
2. Select `ml/train_colab_oneclick.ipynb` from this repo
3. **Runtime → Change runtime type → A100 GPU**
4. **Run the single code cell** (Shift+Enter)
5. Download `decision_tree.joblib` when prompted
6. On your Mac:
   ```bash
   mv ~/Downloads/decision_tree.joblib ml/models/
   python backend/cli.py apply-model
   ```

Regenerate notebook after re-exporting features:
```bash
python scripts/generate_colab_notebook.py
```

---

## Colab MCP (Cursor control)

Project config is at [`.cursor/mcp.json`](../.cursor/mcp.json).

**After adding MCP, restart Cursor** (Settings → MCP → verify `colab-mcp` is connected).

### Prerequisites
- `uv` installed (`pip install uv`) — already on your machine
- Log into [Google Colab](https://colab.research.google.com) in Chrome as **tanmaysahu392001@gmail.com**
- Open a notebook tab (e.g. upload `ml/train_colab.ipynb`)

### How Colab MCP works
1. Keep a Colab notebook **open in your browser** (same machine as Cursor)
2. In Cursor chat, ask the agent to run cells / train the model
3. The MCP server bridges Cursor ↔ your active Colab session

### Train the decision tree via Colab
1. Open `ml/train_colab.ipynb` in Colab (Pro → A100 runtime)
2. Upload `ml/exports/features.csv` and `ml/train_model.py`
3. Run all cells
4. Download `decision_tree.joblib` → place in `ml/models/`
5. Locally: `python backend/cli.py apply-model`

Or ask Cursor (with MCP connected): *"Run the training notebook in Colab on A100"*

---

## 2. Baseten GLM 5.2 (chatbot)

Configured in `.env` (gitignored):

```
BASETEN_API_KEY=...
BASETEN_BASE_URL=https://inference.baseten.co/v1
BASETEN_MODEL=zai-org/GLM-5.2
```

The dashboard chat panel at http://localhost:8000 uses this for patient reasoning.

**Security:** Never commit `.env` or paste API keys in chat. Rotate keys if exposed.

---

## 3. Quick local test

```bash
source .venv/bin/activate
uvicorn backend.api.main:app --reload --port 8000
```

Test chat:
```bash
curl -X POST http://localhost:8000/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"patient_id":"FA-001","question":"Why was this patient routed this way?"}'
```
