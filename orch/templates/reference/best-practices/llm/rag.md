# RAG Architecture Best Practices

Retrieval-Augmented Generation grounds LLM responses in factual, current information from external knowledge bases. Building production RAG requires careful decisions across embedding models, chunking strategies, vector databases, retrieval pipelines, and reranking -- each choice compounds into system-level quality.

Covers: RAG architecture, embedding model selection, chunking strategies, vector databases (Pinecone, Qdrant, Weaviate, pgvector), hybrid search with RRF, reranking, metadata filtering, advanced RAG patterns (HyDE, multi-query, parent document), and evaluation metrics.

---

## RAG Architecture

- **Core pipeline** -- Document ingestion (chunk + embed + store) feeds a retrieval pipeline (query + search + rerank) that provides context to LLM generation
- **Separate ingestion from retrieval** -- batch processing for document updates; real-time for queries; different scaling characteristics
- **Multi-stage retrieval** -- broad initial search (50+ candidates) narrowed by reranking to 5-10 high-quality results for the LLM context window
- **Structured output for citations** -- Pydantic models with `answer`, `confidence`, `sources`, and `reasoning` fields enable programmatic validation

## Embedding Model Selection

- **Voyage AI for Claude applications** -- `voyage-3-large` (1024 dims, 32K tokens) is officially recommended by Anthropic for pairing with Claude
- **Domain-specific models** -- `voyage-code-3` for code search, `voyage-finance-2` for financial documents, `voyage-law-2` for legal text
- **OpenAI text-embedding-3-large** -- 3072 dimensions for maximum accuracy; use Matryoshka dimensionality reduction to 512-1536 for cost savings
- **OpenAI text-embedding-3-small** -- 1536 dimensions, cost-effective for general-purpose applications
- **BGE-large-en-v1.5** -- best open-source option (1024 dims) for local deployment; add `"Represent this sentence for searching relevant passages:"` query prefix
- **multilingual-e5-large** -- 1024 dimensions for multi-language support; requires `"query:"` and `"passage:"` prefixes
- **Never mix embedding models** -- vectors from different models occupy incompatible vector spaces; reindex if switching models
- **Batch embedding requests** -- process 100+ texts per API call; significantly cheaper and faster than one-by-one

## Chunking Strategies

- **Recursive character splitter** -- tries separators in order (`\n\n`, `\n`, `. `, ` `); best general-purpose chunker
- **Chunk size 500-1000 tokens** -- too small loses context, too large dilutes relevance; 512 tokens is a safe default
- **10-20% overlap** -- preserves context at chunk boundaries; prevents information loss from mid-sentence splits
- **Semantic chunking** -- splits based on embedding similarity between adjacent sentences; best for documents with varied structure
- **Token-based splitting** -- `TokenTextSplitter` with `cl100k_base` encoding ensures chunks respect model token limits precisely
- **Markdown header splitting** -- preserves document hierarchy by splitting at `#`, `##`, `###` boundaries; includes headers as metadata
- **Code-aware chunking** -- use tree-sitter to split by function/class definitions; preserves semantic units in code repositories
- **Always include metadata** -- source document ID, chunk index, category, and date enable filtering and debugging downstream

## Vector Databases

- **Pinecone** -- managed serverless with auto-scaling; best for teams that want zero operational overhead
- **Qdrant** -- Rust-based, high-performance with rich filtering; best for complex metadata queries and self-hosted deployments
- **Weaviate** -- GraphQL API with built-in hybrid search; best for teams wanting BM25 + vector search in one system
- **pgvector** -- PostgreSQL extension; best when you want SQL integration and already run Postgres
- **Chroma** -- lightweight, embedded; best for local development and prototyping, not production scale
- **Milvus** -- distributed architecture with GPU acceleration; best for massive scale (billions of vectors)

## Index Configuration

- **Start with HNSW** -- best recall/latency balance for most use cases; tune `M` (connections) and `efConstruction` (build quality)
- **IVF + PQ for 10M+ vectors** -- inverted file index with product quantization trades recall for memory efficiency at scale
- **Cosine similarity for normalized embeddings** -- equivalent to dot product after L2 normalization; most embedding models output normalized vectors
- **Benchmark recall@10 vs latency** -- measure on representative queries before deploying; parameters affect this tradeoff directly

## Hybrid Search

- **Combine vector + BM25** -- semantic search captures meaning; keyword search captures exact terms (names, codes, acronyms)
- **Reciprocal Rank Fusion (RRF)** -- `score = sum(1 / (k + rank))` with k=60; robust default that works without score normalization
- **Weight tuning** -- start with 70% vector / 30% keyword; adjust based on query analysis; domain-specific terms may need more keyword weight
- **PostgreSQL hybrid search** -- `tsvector` column with GIN index for BM25 alongside pgvector HNSW index; RRF fusion in a single SQL query
- **Elasticsearch native RRF** -- version 8.x supports `sub_searches` with built-in `rank.rrf` for zero-code hybrid search

## Reranking

- **Cross-encoder reranking** -- `cross-encoder/ms-marco-MiniLM-L-6-v2` scores query-document pairs jointly; significantly better than bi-encoder similarity alone
- **Cohere Rerank API** -- `rerank-english-v3.0` for production quality without hosting a model; 20 candidates in, top 5 out
- **Maximal Marginal Relevance (MMR)** -- `lambda_mult=0.5` balances relevance with diversity; prevents returning near-duplicate passages
- **LLM-based reranking** -- use the target LLM to score relevance when cross-encoders underperform on domain-specific content
- **Over-retrieve then rerank** -- fetch 20-50 candidates from vector search, rerank to top 5-10; quality improvement justifies latency cost

## Advanced RAG Patterns

- **Multi-query retrieval** -- LLM generates 3-5 query variations from the original; union of results improves recall for ambiguous questions
- **HyDE (Hypothetical Document Embeddings)** -- LLM generates a hypothetical answer, then uses that answer's embedding for retrieval; bridges vocabulary gap
- **Parent document retriever** -- index small chunks (400 tokens) for precise matching; return parent chunks (2000 tokens) for full context
- **Contextual compression** -- LLM extracts only relevant portions from each retrieved document; reduces noise in the generation context
- **RAG-Fusion** -- generates multiple queries, retrieves for each, fuses results with RRF; combines multi-query with rank fusion
- **Self-RAG** -- model decides whether to retrieve, generates, then self-evaluates faithfulness; adaptive retrieval reduces unnecessary searches
- **GraphRAG** -- knowledge graph + vector search; entities and relationships provide structured context alongside unstructured text

## Metadata Filtering

- **Pre-filter to reduce search space** -- `filter={"category": "technical"}` applied before vector search is cheaper than post-filtering
- **Temporal filtering** -- restrict to recent documents for time-sensitive queries; include `date` in metadata for range filtering
- **Access control filtering** -- user role or tenant ID in metadata enforces data isolation at the retrieval layer

## Evaluation

- **Retrieval precision** -- fraction of retrieved documents that are relevant; measures noise in the context
- **Retrieval recall** -- fraction of relevant documents that were retrieved; measures completeness
- **Answer faithfulness** -- is the generated answer grounded in the retrieved context? LLM-as-judge with NLI models
- **Answer relevance** -- does the answer address the original question? Separate from faithfulness
- **Context relevance** -- is the retrieved context relevant to the question? Helps diagnose retrieval vs generation issues
- **MRR and NDCG@K** -- ranking quality metrics; MRR rewards the position of the first relevant result; NDCG accounts for all positions
- **LLM-as-judge** -- use a stronger model to evaluate weaker model outputs on accuracy, helpfulness, and groundedness (1-10 scale)
- **A/B test with real users** -- automated metrics are proxies; measure click-through, user ratings, and task completion in production

## Document Processing Pipeline

- **Preprocessing matters** -- clean whitespace, remove boilerplate headers/footers, normalize unicode before chunking; garbage in, garbage out
- **Metadata enrichment** -- extract and attach source, category, author, date, and document type during ingestion for downstream filtering
- **Incremental indexing** -- track document hashes; only re-embed and re-index changed documents; avoids full reindex on every update
- **Document deduplication** -- use MinHash or embedding similarity to detect and remove near-duplicate content before indexing
- **Version tracking** -- maintain document version metadata so queries can filter for the most recent version of each source

## Prompt Engineering for RAG

- **Context-first, question-second** -- place retrieved passages before the question; models attend more to recent content
- **Explicit grounding instructions** -- "Answer ONLY based on the provided context; say 'I don't know' otherwise" reduces hallucination
- **Citation format** -- "Cite sources using [1], [2] notation" enables users to verify answers against source material
- **Structured RAG responses** -- Pydantic model with `answer`, `confidence`, `sources[]`, and `reasoning` fields enables programmatic quality checks
- **Handle insufficient context gracefully** -- distinguish "the answer is X" from "the context does not address this question"

## Production Operations

- **Cache embeddings for static content** -- avoid recomputing embeddings for documents that have not changed
- **Monitor embedding drift** -- model updates or domain shifts change the embedding space; re-embed when retrieval quality degrades
- **Blue-green index deployments** -- build new index alongside old; switch traffic atomically; rollback by reverting the switch
- **Alert on latency degradation** -- P95 retrieval latency increase often indicates index growth or configuration issues
- **Cost monitoring** -- track embedding API calls, vector storage, and LLM token usage per feature; set budget alerts
- **Implement rate limiting** -- protect embedding and LLM APIs from burst traffic; queue and batch during peak load
- **Plan for scale** -- test with 10x current document count; verify index performance and query latency at projected scale

## Anti-Patterns to Avoid

- **Skipping reranking** -- initial retrieval is noisy; even a simple cross-encoder reranker significantly improves precision
- **One-size-fits-all chunking** -- different document types (code, prose, tables) need different chunking strategies
- **Ignoring empty results** -- when retrieval returns nothing relevant, the LLM should say so rather than hallucinate from its training data
- **Embedding model mismatch** -- using one model for indexing and another for queries produces meaningless similarity scores
- **Over-stuffing context** -- filling the entire context window with retrieved passages leaves no room for reasoning; 5-10 passages is usually sufficient
