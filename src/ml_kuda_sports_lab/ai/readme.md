# AI Tooling

Local AI utilities for embeddings, retrieval, and Ollama experiments live here.
`requirements.ai.txt` includes Chroma for vector indexing, MCP for Continue
tool integration, and Streamlit for the small local embedding demo in this
folder.

## Environment

Use a dedicated AI virtual environment if you want to keep Chroma and retrieval
dependencies separate from the Streamlit front-end environment:

```bash
cd /home/ares/Documents/gitrepos/ml_kuda_sports_lab
python3 -m venv .venv-ai
source .venv-ai/bin/activate
python -m pip install -r requirements.ai.txt
```

If you prefer to reuse the existing front-end environment, install the same file
there instead:

```bash
source /home/ares/Documents/uppercutanalytics/.venv-front/bin/activate
cd /home/ares/Documents/gitrepos/ml_kuda_sports_lab
python -m pip install -r requirements.ai.txt
```

## Chroma Code Index

Index a selected file with local Ollama/Nomic embeddings:

```bash
python src/ml_kuda_sports_lab/ai/chroma_code_index.py index \
  --file src/ml_kuda_sports_lab/front_end/mma_front_streamlit.py
```

Query the saved chunks and paste the output into Continue:

```bash
python src/ml_kuda_sports_lab/ai/chroma_code_index.py query \
  "where does the Streamlit app handle page selection?" \
  --file src/ml_kuda_sports_lab/front_end/mma_front_streamlit.py \
  --n-results 5
```

## Chroma RAG Retrieval MCP Server

Continue can call the Chroma index through the MCP server in:

```text
src/ml_kuda_sports_lab/ai/code_vector_mcp_server.py
```

The workspace MCP config is:

```text
.continue/mcpServers/chroma-code-rag-retriever.yaml
```

The MCP server exposes:

- `health_check`
- `search_code_index`

Use Continue Agent mode and ask for the Chroma Code RAG Retriever when you want
retrieval from this local Chroma DB.

## Nomic Streamlit Demo

Run the local embedding smoke test:

```bash
streamlit run src/ml_kuda_sports_lab/ai/nomic_embed_streamlit.py
```
