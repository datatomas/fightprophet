import requests
import numpy as np

OLLAMA_URL = "http://localhost:11434/api/embeddings"

def embed(text):
    res = requests.post(OLLAMA_URL, json={
        "model": "nomic-embed-text",
        "prompt": text
    })
    return np.array(res.json()["embedding"])


def cosine_sim(a, b):
    return np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))


# Example: store chunks
chunks = [
    "def moving_average_slow(arr, window): ...",
    "def load_data(): ...",
]

vectors = [embed(c) for c in chunks]


# Query
query = "optimize moving average"
q_vec = embed(query)

scores = [cosine_sim(q_vec, v) for v in vectors]

# Top match
best_chunk = chunks[np.argmax(scores)]

print(best_chunk)