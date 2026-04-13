# Prompt Engineering Best Practices

Effective prompt engineering is the difference between a demo and a production system. Reliable LLM outputs require structured techniques, systematic evaluation, and iterative refinement -- treating prompts with the same rigor as application code.

Covers: few-shot with dynamic example selection, chain-of-thought with self-verification, structured outputs with Pydantic, progressive disclosure, error recovery and fallback, role-based system prompts, RAG integration, token efficiency, caching, evaluation (BLEU, ROUGE, BERTScore, LLM-as-judge), and A/B testing.

---

## Core Principles

- **Be specific over verbose** -- vague prompts produce inconsistent results; explicit constraints produce reliable outputs
- **Show, don't tell** -- examples are more effective than descriptions; one good demonstration beats a paragraph of instructions
- **Start simple, add complexity** -- progressive disclosure avoids over-engineering; only add chain-of-thought or few-shot when simple prompts fail
- **Treat prompts as code** -- version control, A/B testing, and regression testing apply to prompts just as they do to application code
- **Test on diverse inputs** -- edge cases, adversarial inputs, and boundary conditions reveal failure modes that happy-path testing misses

## Few-Shot Learning

- **Dynamic example selection** -- use semantic similarity to select 2-3 examples most relevant to the current query from an example bank
- **Balance example count with context window** -- more examples improve consistency but consume tokens; 2-5 examples is optimal for most tasks
- **Match example format to desired output** -- if you want JSON, show JSON examples; if you want bullet points, demonstrate bullet points
- **Include edge cases in examples** -- show how to handle ambiguous input, missing data, or "I don't know" scenarios
- **Avoid example pollution** -- examples that don't match the target task confuse the model; curate examples for the specific use case

## Chain-of-Thought Prompting

- **Zero-shot CoT** -- append "Let's think step by step" for simple reasoning tasks without providing examples
- **Few-shot CoT** -- provide examples with explicit reasoning traces for complex multi-step problems
- **Self-verification** -- add a verification step: "Check your answer against the original problem" catches arithmetic and logic errors
- **Self-consistency** -- sample multiple reasoning paths at higher temperature; majority vote on the final answer improves accuracy
- **Least-to-most decomposition** -- break complex problems into sub-problems; solve each sequentially; combine for the final answer
- **Tree-of-thoughts** -- explore multiple reasoning branches when a single chain might miss the best path; prune unpromising branches early

## Structured Outputs

- **Pydantic schema enforcement** -- `llm.with_structured_output(MyModel)` ensures type-safe, parseable responses with field validation
- **JSON mode for reliable extraction** -- request JSON output with a schema definition; validate with Pydantic on the receiving end
- **Constrained fields** -- `Field(ge=0, le=1)` for confidence scores, `Literal["a", "b", "c"]` for categorical outputs
- **Always handle parse failures** -- wrap JSON parsing in try/except; fall back to unstructured extraction when structured output fails
- **Include reasoning in the schema** -- a `reasoning: str` field forces the model to explain its answer, improving accuracy through chain-of-thought

## Progressive Disclosure

- **Level 1: Direct instruction** -- "Summarize this article" with no constraints; test whether this produces acceptable output first
- **Level 2: Add constraints** -- "Summarize in 3 bullet points focusing on key findings, conclusions, and implications"
- **Level 3: Add reasoning** -- "First identify the thesis, then extract supporting points, then summarize"
- **Level 4: Add examples** -- provide an example article with an ideal summary; model matches format and depth
- **Only escalate when needed** -- each level adds tokens and complexity; stop at the simplest level that produces acceptable results

## Error Recovery and Fallback

- **Confidence-based routing** -- high confidence (>0.8) returns direct answer; medium (0.5-0.8) adds caveats; low (<0.5) explains what information is missing
- **Structured fallback** -- when JSON parsing fails, fall back to a simpler prompt that produces unstructured text with default confidence
- **Alternative interpretations** -- for ambiguous questions, generate multiple interpretations and let the user choose
- **Retry with rephrased prompt** -- if the first attempt fails validation, rephrase and retry once before falling back
- **Circuit breaker on repeated failures** -- after N consecutive failures, return a graceful error message rather than retrying indefinitely

## Role-Based System Prompts

- **Define expertise and constraints** -- "You are a senior data analyst" sets the knowledge domain; "Do not provide medical advice" sets boundaries
- **Specify communication style** -- "Be precise and technical when discussing methodology; translate findings into business impact"
- **Include output format requirements** -- system prompts that define JSON structure, heading format, or checklist format produce consistent outputs
- **Separate concerns** -- system prompt handles persona and constraints; user prompt handles the specific task and data

## RAG Prompt Integration

- **Context-first, question-second** -- place retrieved context before the question; models attend more to recent tokens
- **Explicit grounding instructions** -- "Answer ONLY based on the provided context; if the context does not contain the answer, say so"
- **Citation format** -- "Cite sources using [1], [2] notation" enables verification and trust
- **Handling insufficient context** -- instruct the model to distinguish between "the answer is X" and "the context does not address this"
- **Context relevance filtering** -- compress or filter retrieved passages before insertion; irrelevant context degrades answer quality

## Token Efficiency

- **Concise over verbose prompts** -- "Summarize the key points:" (8 tokens) beats a 50-token version of the same instruction
- **Remove redundant instructions** -- if the system prompt says "be concise," don't repeat it in the user prompt
- **Abbreviate repeated patterns** -- use shorthand in few-shot examples after the first fully detailed example
- **Batch related questions** -- one prompt with multiple questions is cheaper than multiple single-question prompts

## Caching

- **Prompt caching for repeated system prompts** -- Anthropic's `cache_control: {"type": "ephemeral"}` caches long system prompts across requests
- **Semantic caching for similar queries** -- cache responses keyed by query embedding similarity; return cached response for near-duplicate queries
- **Embedding caching** -- cache computed embeddings for static documents; only recompute when source content changes

## Evaluation Metrics

- **BLEU** -- n-gram overlap between generated and reference text; best for translation tasks with clear expected outputs
- **ROUGE** -- recall-oriented metric comparing generated summaries to reference summaries; ROUGE-L measures longest common subsequence
- **BERTScore** -- embedding-based similarity using pre-trained models; captures semantic equivalence beyond exact word matching
- **Perplexity** -- model confidence in generated text; lower is better; useful for comparing model quality on the same data
- **Custom groundedness** -- NLI model scores whether the response is entailed by the provided context; critical for RAG evaluation
- **Toxicity detection** -- Detoxify or OpenAI Moderation API scores for harmful content; threshold and alert on production outputs

## LLM-as-Judge

- **Pointwise scoring** -- rate individual responses on accuracy, helpfulness, and clarity (1-10 scale) with brief reasoning
- **Pairwise comparison** -- "Which response is better: A or B?" with confidence score; more reliable than absolute scoring for subtle differences
- **Reference-based evaluation** -- compare generated response against a gold-standard answer on semantic similarity, factual accuracy, and completeness
- **Use a stronger model as judge** -- Claude Opus judging Claude Sonnet outputs; GPT-4 judging GPT-3.5 outputs
- **Guard against position bias** -- randomize A/B ordering in pairwise comparisons; some models favor the first or last response

## A/B Testing

- **Statistical significance with t-test** -- compare variant score distributions; only declare a winner when p-value < 0.05
- **Effect size with Cohen's d** -- small (<0.2), medium (0.5), large (0.8) effects; statistical significance without practical significance is meaningless
- **Minimum sample size** -- 30+ examples per variant for reliable results; more for small expected effect sizes
- **Track multiple metrics** -- accuracy, latency, token usage, and user satisfaction; a prompt can improve accuracy while degrading latency
- **Regression testing** -- maintain a baseline benchmark; flag any metric that drops more than 5% from baseline as a regression

## Model-Specific Optimization

- **Claude: XML tags for structure** -- `<context>`, `<instructions>`, `<examples>` tags help Claude parse prompt sections clearly
- **Claude: 200K context window** -- leverage for large document analysis; cache the system prompt to reduce per-request costs
- **OpenAI: function calling** -- define tools as functions for structured interaction; more reliable than asking for JSON in the prompt
- **Open-source models: instruction formatting** -- follow model-specific chat templates (Llama uses `[INST]`, Mixtral uses `<s>[INST]`); incorrect formatting degrades quality

## Common Pitfalls

- **Over-engineering prompts** -- adding complexity before testing the simple version wastes tokens and introduces failure modes
- **Ignoring edge cases** -- prompts that work on 10 examples may fail on the 11th; test with diverse and adversarial inputs
- **No error handling** -- assuming the model always produces valid JSON or follows instructions perfectly
- **Hardcoded prompts** -- not parameterizing variables, context, and examples for reuse across use cases
- **Context overflow** -- exceeding token limits with excessive examples or context; always calculate total tokens before sending
