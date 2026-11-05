---
type: howto
tags: [rag, embeddings, sqlite]
aliases: [Retrieval]
created: 2026-01-14
---
# RAG

Retrieval-augmented generation over documents dropped into an inbox folder.

## Pipeline
1. Split documents into chunks.
2. Embed each chunk with EmbeddingGemma via Ollama (768 dimensions).
3. Store vectors in SQLite with sqlite-vec next to the chunk text.
4. At question time, embed the query, retrieve the nearest chunks and let the chat model answer.

## Tips
Prefix queries and documents with the EmbeddingGemma task prompts; retrieval quality
drops noticeably without them. The [[FalkorDB]] graph uses the same embedding model.
