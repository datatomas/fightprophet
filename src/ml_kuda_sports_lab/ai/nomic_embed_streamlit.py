#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Streamlit smoke test for local Nomic embeddings through Ollama.

Run:
    streamlit run src/ml_kuda_sports_lab/ai/nomic_embed_streamlit.py

Before running:
    ollama pull nomic-embed-text:latest
    ollama serve
"""

from __future__ import annotations

import json
import math
import os
from urllib import request
from urllib.error import HTTPError, URLError

import streamlit as st


DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_EMBED_MODEL = "nomic-embed-text:latest"


def embed_text(text: str, model: str, base_url: str) -> list[float]:
    """Return one embedding vector from Ollama's local embeddings endpoint."""
    payload = json.dumps({"model": model, "prompt": text}).encode("utf-8")
    req = request.Request(
        f"{base_url.rstrip('/')}/api/embeddings",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with request.urlopen(req, timeout=60) as response:
            body = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Ollama returned HTTP {exc.code}: {error_body}") from exc
    except URLError as exc:
        raise RuntimeError(
            "Could not reach Ollama. Start it with `ollama serve` and confirm the URL."
        ) from exc

    vector = body.get("embedding")
    if not isinstance(vector, list) or not vector:
        raise RuntimeError(f"Ollama response did not include an embedding: {body}")
    return [float(value) for value in vector]


def cosine_similarity(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


EXAMPLES = {
    "Python ETL": """\
def normalize_fighter_name(name: str) -> str:
    return " ".join(name.strip().lower().split())
""",
    "Streamlit UI": """\
import streamlit as st

st.title("Fight model lab")
fighter = st.text_input("Fighter")
st.write(f"Selected: {fighter}")
""",
    "SQL query": """\
select fighter_id, avg(win_probability) as mean_probability
from predictions
group by fighter_id
order by mean_probability desc;
""",
}


def main() -> None:
    st.set_page_config(page_title="Nomic Embed Test", layout="wide")
    st.title("Nomic Embed Test")

    with st.sidebar:
        base_url = st.text_input(
            "Ollama base URL",
            value=os.environ.get("OLLAMA_BASE_URL", DEFAULT_OLLAMA_BASE_URL),
        )
        model = st.text_input(
            "Embedding model",
            value=os.environ.get("OLLAMA_EMBED_MODEL", DEFAULT_EMBED_MODEL),
        )

    left_label = st.selectbox("Example A", options=list(EXAMPLES), index=0)
    right_label = st.selectbox("Example B", options=list(EXAMPLES), index=1)

    left_text = st.text_area("Text/code A", value=EXAMPLES[left_label], height=220)
    right_text = st.text_area("Text/code B", value=EXAMPLES[right_label], height=220)

    if st.button("Embed and compare", type="primary"):
        if not left_text.strip() or not right_text.strip():
            st.warning("Add text to both boxes before embedding.")
            return

        try:
            with st.spinner("Embedding with Ollama..."):
                left_vector = embed_text(left_text, model=model, base_url=base_url)
                right_vector = embed_text(right_text, model=model, base_url=base_url)
        except RuntimeError as exc:
            st.error(str(exc))
            st.info("Try: `ollama pull nomic-embed-text:latest` then `ollama serve`.")
            return

        similarity = cosine_similarity(left_vector, right_vector)
        st.metric("Cosine similarity", f"{similarity:.4f}")
        st.write("Vector dimension:", len(left_vector))
        st.write("First 12 values from vector A:", left_vector[:12])


if __name__ == "__main__":
    main()
