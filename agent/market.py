"""Deterministic market analysis. The model never decides what is missing.

Two failures in the first 21:00 report made this module necessary.

**It hallucinated gaps.** It reported Databricks as the #2 missing skill and Azure
as #7. The resume says, verbatim, "Docker, Google Cloud Run, Azure / Databricks,
AI Studio, CI/CD". Both were present. A resume report that invents gaps is worse
than no report, because it will be acted on.

**It analysed the wrong market.** It read all 44 stored job descriptions. But 22
of those have titles we now exclude and 6 were explicitly rejected -- only 12 were
roles Rajiv actually wants. So the "most requested skills" were the top skills of
the Data Engineer market he had just finished rejecting: Spark, Databricks, Kafka,
Hadoop, Scala, Airflow.

The fix is a division of labour. This module decides, by string matching against a
curated vocabulary, which skills the relevant postings ask for and which of those
the resume does not mention. Those facts are then handed to the model, whose only
job is to write them up. It cannot add a gap, remove one, or reorder them by
importance, because it is never asked what the gaps are.
"""
from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger("agent.market")

#: Each skill is a GROUP of surface forms. Exact-word matching previously called
#: SQL and GCP absent from a resume showing "PostgreSQL / AlloyDB", "BigQuery" and
#: "Google Cloud Run". A trailing "*" marks a deliberate prefix.
#: skill -> (surface forms, category)
#:
#: Categories matter because a single ranked list mixed AI and infrastructure and
#: read as a cloud curriculum. They are reported separately now.
#:
#: The AI side is deliberately fine-grained. A coarse vocabulary ("LLMs", "RAG")
#: made AI gaps invisible: a posting demanding vLLM, DPO, reranking or RAGAS
#: matched nothing, so the report could only ever surface infrastructure.
SKILLS: dict[str, tuple[list[str], str]] = {
    # ---- core AI / ML -------------------------------------------------- #
    "LLMs": (["llm", "large language model", "gpt-4", "gpt4", "claude",
              "gemini", "llama", "mistral"], "ai"),
    "RAG": (["rag", "retrieval augmented", "retrieval-augmented"], "ai"),
    "GraphRAG": (["graphrag", "graph rag", "knowledge graph"], "ai"),
    "Reranking / hybrid search": (["rerank*", "hybrid search", "bm25",
                                   "cross-encoder", "colbert"], "ai"),
    "Vector databases": (["vector database", "vector store", "pinecone",
                          "milvus", "faiss", "weaviate", "qdrant", "chroma",
                          "pgvector"], "ai"),
    "Embeddings": (["embedding*", "sentence-transformer*", "sentence transformer*"],
                   "ai"),
    "Chunking / context": (["chunking", "context window", "context engineering"],
                           "ai"),
    "Agentic systems": (["agentic", "multi-agent", "multi agent", "ai agent",
                         "autonomous agent"], "ai"),
    "Agent frameworks": (["langchain", "langgraph", "llamaindex", "crewai",
                          "autogen", "semantic kernel", "adk",
                          "agent development kit"], "ai"),
    "MCP": (["mcp", "model context protocol"], "ai"),
    "Function / tool calling": (["function calling", "tool calling", "tool use",
                                 "structured output"], "ai"),
    "Prompt engineering": (["prompt engineering", "few-shot", "chain of thought",
                            "chain-of-thought"], "ai"),
    "Fine-tuning (LoRA/PEFT)": (["fine-tuning", "fine tuning", "finetun*",
                                 "lora", "qlora", "peft", "sft", "unsloth"], "ai"),
    "RLHF / DPO": (["rlhf", "dpo", "ppo", "reward model", "preference tuning"],
                   "ai"),
    "Quantisation / distillation": (["quantiz*", "quantis*", "distillation",
                                     "gguf", "awq", "bitsandbytes"], "ai"),
    "Model serving": (["vllm", "tgi", "text generation inference", "triton",
                       "ollama", "llama.cpp", "sglang"], "ai"),
    "LLM evaluation": (["ragas", "deepeval", "llm-as-a-judge", "llm as a judge",
                        "groundedness", "faithfulness", "eval harness",
                        "evaluation framework", "golden dataset"], "ai"),
    "Guardrails / safety": (["guardrail*", "nemo guardrails", "jailbreak",
                             "prompt injection", "content moderation",
                             "responsible ai", "red team*"], "ai"),
    "LLM observability": (["langfuse", "langsmith", "phoenix", "arize",
                           "helicone", "llm monitoring", "token usage"], "ai"),
    "Hallucination mitigation": (["hallucinat*", "citation", "attribution",
                                  "grounding"], "ai"),
    "Multimodal": (["multimodal", "multi-modal", "vision language", "vlm",
                    "image understanding"], "ai"),
    "Speech / audio": (["speech-to-text", "whisper", "tts", "text-to-speech",
                        "asr", "diarization"], "ai"),
    "Diffusion / generative image": (["diffusion", "stable diffusion", "sdxl",
                                      "controlnet", "face swap", "gan"], "ai"),
    "Computer vision": (["computer vision", "yolo", "opencv", "detectron",
                         "arcface", "object detection", "ocr"], "ai"),
    "NLP (classical)": (["nlp", "natural language processing", "spacy", "ner",
                         "named entity", "topic model*"], "ai"),
    "Recommenders / ranking": (["recommendation", "recommender", "learning to rank",
                                "collaborative filtering"], "ai"),
    "Time series / forecasting": (["time series", "time-series", "forecasting",
                                   "prophet", "arima", "wape"], "ai"),
    "Classical ML": (["scikit-learn", "sklearn", "xgboost", "lightgbm",
                      "catboost", "random forest", "gradient boosting"], "ai"),
    "Deep learning frameworks": (["pytorch", "tensorflow", "keras", "jax"], "ai"),
    "Experiment tracking": (["mlflow", "weights & biases", "wandb", "neptune",
                             "kubeflow", "model registry"], "ai"),
    "Feature stores": (["feature store", "feast", "tecton"], "ai"),
    "A/B testing": (["a/b test*", "ab test*", "experimentation platform"], "ai"),
    "Data curation": (["data curation", "deduplicat*", "pii redaction",
                       "synthetic data", "annotation", "labelling", "labeling"],
                      "ai"),

    # ---- engineering / platform ---------------------------------------- #
    "Python": (["python"], "eng"),
    "SQL": (["sql", "postgresql", "postgres", "alloydb", "bigquery", "mysql"],
            "eng"),
    "FastAPI": (["fastapi"], "eng"),
    "Django / Flask": (["django", "flask"], "eng"),
    "Async / streaming": (["asyncio", "async i/o", "server-sent events", "sse",
                           "websocket", "streaming response"], "eng"),
    "Redis / caching": (["redis", "memcached", "caching layer"], "eng"),
    "React / Node": (["react", "next.js", "nextjs", "node.js", "nodejs"], "eng"),
    "MongoDB": (["mongodb", "mongo"], "eng"),
    "Testing / CI": (["pytest", "unit test*", "ci/cd", "github actions",
                      "jenkins"], "eng"),

    # ---- cloud / infrastructure ---------------------------------------- #
    "AWS": (["aws", "amazon web services", "sagemaker", "bedrock", "lambda",
             "redshift", "s3"], "infra"),
    "Azure": (["azure", "azure openai", "azure ml"], "infra"),
    "GCP": (["gcp", "google cloud", "cloud run", "vertex ai", "bigquery"],
            "infra"),
    "Kubernetes": (["kubernetes", "k8s", "eks", "gke", "aks", "helm"], "infra"),
    "Docker": (["docker", "containeris*", "containeriz*"], "infra"),
    "Terraform / IaC": (["terraform", "pulumi", "cloudformation",
                         "infrastructure as code"], "infra"),
    "Observability": (["observability", "tracing", "opentelemetry", "prometheus",
                       "grafana", "datadog"], "infra"),
    "Spark / PySpark": (["spark", "pyspark"], "infra"),
    "Airflow / orchestration": (["airflow", "dagster", "prefect", "step function*"],
                                "infra"),
    "dbt": (["dbt"], "infra"),
    "Kafka / streaming": (["kafka", "pubsub", "pub/sub", "kinesis", "flink"],
                          "infra"),
    "Snowflake": (["snowflake"], "infra"),
    "Databricks": (["databricks"], "infra"),
    "Hadoop / Hive": (["hadoop", "hive", "hdfs"], "infra"),
    "Scala": (["scala"], "infra"),
    "ETL / pipelines": (["etl", "elt", "data pipeline", "medallion"], "infra"),
    "Power BI / Tableau": (["power bi", "powerbi", "tableau", "looker"], "infra"),
}

#: Back-compat shim: existing callers expect {label: forms}.
SKILL_GROUPS: dict[str, list[str]] = {k: v[0] for k, v in SKILLS.items()}
CATEGORY: dict[str, str] = {k: v[1] for k, v in SKILLS.items()}
CATEGORY_LABEL = {"ai": "AI / ML", "eng": "Engineering", "infra": "Cloud & data infra"}


def normalise(text: str) -> str:
    """Collapse whitespace and rejoin hyphens split across line breaks.

    Required before ANY matching. A PDF wraps "prompt engineering" as
    "prompt\nengineering" and "Sentence-Transformers" as "Sentence-\nTransformers",
    so a literal search for either fails and the skill is reported absent from a
    document that plainly contains it. Whether a multi-word skill was detected
    otherwise depends on where a line happens to break.
    """
    if not text:
        return ""
    # "word-\nnext" -> "word-next"; these are hyphenated terms, not syllable breaks.
    text = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1-\2", text)
    return " ".join(text.split()).lower()


def mentions(haystack: str, forms: list[str]) -> bool:
    """Whole-word match, except forms marked with a trailing '*'.

    Plain substring matching made "scala" hit "SCALAble", which appears in nearly
    every job description and ranked Scala the most-demanded skill in the market.
    """
    for f in forms:
        pat = (rf"(?<!\w){re.escape(f[:-1])}" if f.endswith("*")
               else rf"(?<!\w){re.escape(f)}(?!\w)")
        if re.search(pat, haystack):
            return True
    return False


def relevant_jobs(store, jobs: list[dict[str, Any]], *,
                  min_score: int = 40) -> tuple[list[dict], dict[str, int]]:
    """Keep only postings that represent roles actually being targeted.

    Without this the report describes whatever happened to be scraped, including
    every role type the user has rejected.
    """
    rejected = {d.id for d in store.db.collection("rejections").stream()}
    excludes = [w.lower() for w in store.get_search_config().get("title_exclude", [])]

    dropped = {"no_jd": 0, "rejected": 0, "title_excluded": 0, "low_score": 0}
    kept: list[dict] = []
    for j in jobs:
        if not (j.get("jd_text") or "").strip():
            dropped["no_jd"] += 1
            continue
        if j.get("job_id") in rejected:
            dropped["rejected"] += 1
            continue
        title = (j.get("title") or "").lower()
        if any(w in title for w in excludes):
            dropped["title_excluded"] += 1
            continue
        if (j.get("match_score") or 0) < min_score:
            dropped["low_score"] += 1
            continue
        kept.append(j)
    return kept, dropped


def analyse(store, jobs: list[dict[str, Any]], resume_text: str,
            *, min_score: int = 40) -> dict[str, Any]:
    """Demand and gaps, computed by string matching. No model involved."""
    kept, dropped = relevant_jobs(store, jobs, min_score=min_score)
    resume = normalise(resume_text)

    demand: dict[str, int] = {}
    for j in kept:
        blob = normalise((j.get("jd_text") or "") + " " + (j.get("title") or ""))
        for label, forms in SKILL_GROUPS.items():
            if mentions(blob, forms):
                demand[label] = demand.get(label, 0) + 1

    on_resume = {label: mentions(resume, SKILL_GROUPS[label]) for label in demand}
    gaps = sorted(((n, s) for s, n in demand.items() if not on_resume[s]),
                  reverse=True)
    covered = sorted(((n, s) for s, n in demand.items() if on_resume[s]),
                     reverse=True)

    gap_rows = [{"skill": s, "postings": n, "category": CATEGORY.get(s, "eng"),
                 "share": round(n / len(kept), 2) if kept else 0.0}
                for n, s in gaps]
    return {
        "analysed": len(kept),
        "dropped": dropped,
        "demand": demand,
        "on_resume": on_resume,
        "gaps": gap_rows,
        # Split out, because one mixed ranking read as a cloud curriculum and
        # buried the AI gaps that actually matter to an AI engineer.
        "gaps_by_category": {
            cat: [g for g in gap_rows if g["category"] == cat]
            for cat in ("ai", "eng", "infra")
        },
        "covered": [{"skill": s, "postings": n,
                     "category": CATEGORY.get(s, "eng")} for n, s in covered],
        "titles": [j.get("title", "") for j in kept][:20],
    }
