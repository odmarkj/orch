# AI/ML, LLM Integration & Vector Store Agent Skills

AI/ML skills cover LLM API integration (Google Gemini, OpenAI, Microsoft Azure AI), ML model training and evaluation (Hugging Face), AI agent frameworks (Cloudflare Agents SDK, VoltAgent, Composio), image/video/audio generation (fal.ai, Replicate, OpenAI), vector search (MongoDB Atlas, Qdrant, Microsoft Azure Search), and MCP server building (Anthropic, Cloudflare, Microsoft). The Hugging Face collection (13 skills) is the most comprehensive for traditional ML workflows.

Key patterns: For LLM APIs, Google Gemini skills differentiate standard/Vertex/Live/Interactions modes. Cloudflare's agent SDK enables stateful AI agents at the edge. Hugging Face covers the full ML lifecycle from dataset creation to model training to evaluation. fal.ai provides 15 skills for generative media. For vector search, MongoDB Atlas Search and Qdrant are the primary options.

---

## LLM API Integration

### Google Gemini
- **google-gemini/gemini-api-dev** -- Best practices for Gemini-powered apps
- **google-gemini/vertex-ai-api-dev** -- Gemini on Google Cloud Vertex AI
- **google-gemini/gemini-live-api-dev** -- Real-time bidirectional streaming
- **google-gemini/gemini-interactions-api** -- Text, chat, streaming, image generation
  Source: https://officialskills.sh/google-gemini/skills/

### Microsoft Azure AI
- **microsoft/azure-ai-openai-dotnet** -- GPT-4, embeddings, DALL-E, Whisper client
- **microsoft/azure-ai-projects-dotnet/java/py/ts** -- AI Foundry project management
- **microsoft/azure-ai-voicelive-dotnet/java/py/ts** -- Real-time bidirectional voice AI
- **microsoft/agent-framework-azure-ai-py** -- Agent Framework for Azure AI Foundry
- **microsoft/agents-v2-py** -- Container-based agents with custom images
  Source: https://officialskills.sh/microsoft/skills/

### OpenAI
- **openai/openai-docs** -- Authoritative guidance from OpenAI developer docs
- **openai/chatgpt-apps** -- ChatGPT Apps SDK with MCP server and widget UI
  Source: https://officialskills.sh/openai/skills/

## AI Agent Frameworks
- **cloudflare/agents-sdk** -- Stateful AI agents with scheduling, RPC, MCP servers
- **cloudflare/building-ai-agent-on-cloudflare** -- AI agents with state and WebSockets
  Source: https://officialskills.sh/cloudflare/skills/
- **voltagent/** -- VoltAgent TypeScript framework (4 skills: setup, best practices, core reference, docs)
  Source: https://officialskills.sh/voltagent/skills/
- **composiohq/composio** -- Connect agents to 1000+ apps with managed auth
  Source: https://officialskills.sh/composiohq/skills/composio

## MCP Server Building
- **anthropics/mcp-builder** -- Create MCP servers to integrate external APIs
  Source: https://officialskills.sh/anthropics/skills/mcp-builder
- **cloudflare/building-mcp-server-on-cloudflare** -- Remote MCP servers with OAuth
  Source: https://officialskills.sh/cloudflare/skills/building-mcp-server-on-cloudflare
- **microsoft/mcp-builder** -- MCP server creation guide for LLM tool integration
  Source: https://officialskills.sh/microsoft/skills/mcp-builder

## ML Model Training & Evaluation (Hugging Face)
- **huggingface/hugging-face-model-trainer** -- Train with TRL: SFT, DPO, GRPO, GGUF conversion
- **huggingface/hugging-face-vision-trainer** -- Train vision models on HF infrastructure
- **huggingface/hugging-face-evaluation** -- Evaluate with vLLM/lighteval
- **huggingface/hugging-face-datasets** -- Create and manage datasets with configs and SQL
- **huggingface/hugging-face-dataset-viewer** -- Browse and query datasets via API
- **huggingface/hugging-face-trackio** -- Track ML experiments with real-time dashboards
- **huggingface/hugging-face-jobs** -- Run compute jobs on HF infrastructure
- **huggingface/huggingface-gradio** -- Build Gradio apps and deploy to HF Spaces
- **huggingface/transformers.js** -- Run ML models in the browser
  Source: https://officialskills.sh/huggingface/skills/

## Generative Media

### fal.ai (15 skills)
- **fal-ai-community/fal-generate** -- Generate images and videos
- **fal-ai-community/fal-audio** -- Text-to-speech and speech-to-text
- **fal-ai-community/fal-3d** -- Generate 3D models from text or images
- **fal-ai-community/fal-train** -- Train custom LoRA models
- **fal-ai-community/fal-realtime** -- Real-time streaming image generation
- **fal-ai-community/fal-video-edit** -- Edit videos (remix, upscale, remove bg, add audio)
  Source: https://officialskills.sh/fal-ai-community/skills/

### Replicate
- **replicate/replicate** -- Discover, compare, and run AI models via Replicate API
  Source: https://officialskills.sh/replicate/skills/replicate

### OpenAI Media
- **openai/imagegen** -- Generate and edit images using OpenAI Image API
- **openai/sora** -- Generate and remix video clips via Sora API
- **openai/speech** -- Generate spoken audio from text
- **openai/transcribe** -- Transcribe audio with optional speaker diarization
  Source: https://officialskills.sh/openai/skills/

## Vector Search & RAG
- **mongodb/mongodb-search-and-ai** -- Atlas Search and vector search
  Source: https://officialskills.sh/mongodb/skills/mongodb-search-and-ai
- **microsoft/azure-search-documents-dotnet/py/ts** -- Full-text, vector, hybrid search with semantic ranking
  Source: https://officialskills.sh/microsoft/skills/
- **qdrant/skills** -- Qdrant vector search: scaling, performance, monitoring, multi-language SDKs
  Source: https://github.com/qdrant/skills

## LLM Evaluation (Community)
- **hamelsmu/eval-audit** -- Audit LLM eval pipelines
- **hamelsmu/error-analysis** -- Identify failure modes in LLM pipelines
- **hamelsmu/write-judge-prompt** -- Design LLM-as-Judge evaluators
- **hamelsmu/evaluate-rag** -- Evaluate RAG retrieval and generation quality
  Source: https://github.com/hamelsmu/prompts/tree/main/evals-skills/skills/

## AI Research (Community)
- **zechenzhangAGI/AI-research-SKILLs** -- 77 skills for model training, inference, MLOps
  Source: https://github.com/zechenzhangAGI/AI-research-SKILLs
- **Orchestra-Research/AI-research-SKILLs** -- 20-module library for architecture, training, paper writing
  Source: https://github.com/Orchestra-Research/AI-research-SKILLs
- **wanshuiyin/Auto-claude-code-research-in-sleep** -- Autonomous ML research with cross-model review
  Source: https://github.com/wanshuiyin/Auto-claude-code-research-in-sleep
