# Backend, APIs & Database Agent Skills

Backend skills cover database best practices (Supabase, Neon, MongoDB, ClickHouse, DuckDB), API frameworks (Stripe, Microsoft FastAPI/Pydantic), serverless platforms (Netlify Functions, Cloudflare Workers), messaging/eventing (Microsoft Azure Service Bus, Event Hubs, Event Grid), and content management (Sanity, WordPress). Microsoft dominates in volume with 100+ Azure SDK skills across 6 languages.

Key patterns: For Postgres, Supabase and Neon provide complementary perspectives (managed vs serverless). MongoDB covers schema design through query optimization. Stripe is authoritative for payments. Cloudflare's Durable Objects skill is essential for stateful edge computing. Netlify provides granular skills for each backend service (functions, edge functions, blobs, DB, forms).

---

## Databases

### PostgreSQL
- **supabase/postgres-best-practices** -- Schema design, RLS, query optimization for Supabase
  Source: https://officialskills.sh/supabase/skills/postgres-best-practices
- **neondatabase/neon-postgres** -- Best practices for Neon Serverless Postgres
- **neondatabase/neon-postgres-egress-optimizer** -- Optimize data transfer
  Source: https://officialskills.sh/neondatabase/skills/

### MongoDB
- **mongodb/mongodb-schema-design** -- Document schemas with validation and indexing
- **mongodb/mongodb-query-optimizer** -- Query performance with Atlas Performance Advisor
- **mongodb/mongodb-search-and-ai** -- Atlas Search and vector search
- **mongodb/mongodb-natural-language-querying** -- Natural language to MongoDB queries
- **mongodb/atlas-stream-processing** -- Pipelines with Kafka, S3, Lambda
  Source: https://officialskills.sh/mongodb/skills/

### Analytics
- **clickhouse/clickhouse-best-practices** -- ClickHouse best practices
  Source: https://officialskills.sh/clickhouse/skills/clickhouse-best-practices
- **duckdb/query** -- SQL queries using Friendly SQL dialect
- **duckdb/read-file** -- Read CSV, JSON, Parquet, Avro, Excel, spatial
  Source: https://officialskills.sh/duckdb/skills/

## API & Payments
- **stripe/stripe-best-practices** -- Payment flows, webhooks, idempotency, security
- **stripe/upgrade-stripe** -- SDK and API version upgrades
  Source: https://officialskills.sh/stripe/skills/

## Serverless & Edge

### Cloudflare
- **cloudflare/wrangler** -- Workers, KV, R2, D1, Vectorize, Queues, Workflows
- **cloudflare/durable-objects** -- Stateful coordination with RPC, SQLite, WebSockets
  Source: https://officialskills.sh/cloudflare/skills/

### Netlify
- **netlify/netlify-functions** -- Serverless API endpoints and background tasks
- **netlify/netlify-edge-functions** -- Low-latency edge middleware and geolocation
- **netlify/netlify-blobs** -- Key-value object storage
- **netlify/netlify-db** -- Managed Postgres with deploy preview branching
- **netlify/netlify-forms** -- HTML form handling with spam filtering
- **netlify/netlify-image-cdn** -- Optimize and transform images via CDN
- **netlify/netlify-caching** -- CDN caching and cache purging
- **netlify/netlify-ai-gateway** -- Access AI models via unified gateway
  Source: https://officialskills.sh/netlify/skills/

### Expo API Routes
- **expo/expo-api-routes** -- API routes in Expo Router with EAS Hosting
  Source: https://officialskills.sh/expo/skills/expo-api-routes

## Microsoft Azure Backend (selected)
- **microsoft/fastapi-router-py** -- FastAPI routers with CRUD and auth
- **microsoft/pydantic-models-py** -- Pydantic models for API schemas
- **microsoft/azure-servicebus-dotnet/java/py/ts** -- Enterprise messaging (queues and topics)
- **microsoft/azure-eventhub-dotnet/java/py/rust/ts** -- High-throughput event streaming
- **microsoft/azure-cosmos-dotnet/java/py/rust/ts** -- Cosmos DB NoSQL
- **microsoft/azure-search-documents-dotnet/py/ts** -- Full-text, vector, hybrid search
- **microsoft/azure-storage-blob-dotnet/java/py/rust/ts** -- Blob storage
  Source: https://officialskills.sh/microsoft/skills/

## Content Management
- **sanity-io/sanity-best-practices** -- Sanity Studio, GROQ queries, content workflows
- **sanity-io/content-modeling-best-practices** -- Scalable content models
  Source: https://officialskills.sh/sanity-io/skills/

### WordPress (13 skills)
- **WordPress/wp-plugin-development** -- Plugin architecture, hooks, settings, security
- **WordPress/wp-rest-api** -- REST API routes, schema, auth, response shaping
- **WordPress/wp-block-development** -- Gutenberg blocks
- **WordPress/wp-block-themes** -- Block themes with theme.json
- **WordPress/wp-performance** -- Profiling, caching, database optimization
  Source: https://officialskills.sh/WordPress/skills/

## Messaging & Email
- **trycourier/courier-skills** -- Multi-channel notifications (email, SMS, push, chat)
  Source: https://github.com/trycourier/courier-skills
- **resend/resend** -- Send and manage emails via Resend API
- **resend/react-email** -- Build emails with React Email components
- **resend/email-best-practices** -- Deliverability and design best practices
  Source: https://github.com/resend/resend-skills/
