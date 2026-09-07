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
SKILL_GROUPS: dict[str, list[str]] = {
    "Python": ["python"],
    "SQL": ["sql", "postgresql", "postgres", "alloydb", "bigquery", "mysql"],
    "Spark / PySpark": ["spark", "pyspark"],
    "Scala": ["scala"],
    "Airflow": ["airflow"],
    "dbt": ["dbt"],
    "Kafka": ["kafka"],
    "Snowflake": ["snowflake"],
    "Databricks": ["databricks"],
    "Hadoop / Hive": ["hadoop", "hive", "hdfs"],
    "AWS": ["aws", "amazon web services", "sagemaker", "redshift"],
    "Azure": ["azure"],
    "GCP": ["gcp", "google cloud", "cloud run", "vertex ai", "bigquery"],
    "Kubernetes": ["kubernetes", "k8s", "eks", "gke", "aks"],
    "Docker": ["docker", "containeris*", "containeriz*"],
    "Terraform": ["terraform"],
    "PyTorch": ["pytorch", "torch"],
    "TensorFlow": ["tensorflow", "keras"],
    "scikit-learn": ["scikit-learn", "sklearn"],
    "MLOps / MLflow": ["mlflow", "kubeflow", "mlops", "model registry"],
    "LLMs": ["llm", "large language model", "gpt", "gemini", "claude"],
    "RAG": ["rag", "retrieval augmented", "retrieval-augmented"],
    "LangChain / LangGraph": ["langchain", "langgraph"],
    "Fine-tuning (LoRA/PEFT)": ["fine-tuning", "fine tuning", "lora", "qlora",
                                "peft", "sft"],
    "Vector databases": ["vector database", "pinecone", "milvus", "faiss",
                         "weaviate", "chroma", "vector search"],
    "Embeddings": ["embedding*"],
    "NLP": ["nlp", "natural language processing"],
    "Computer vision": ["computer vision", "yolo", "opencv"],
    "Time series / forecasting": ["time series", "time-series", "forecasting"],
    "FastAPI": ["fastapi"],
    "Django / Flask": ["django", "flask"],
    "React / Node": ["react", "node.js", "nodejs"],
    "MongoDB": ["mongodb", "mongo"],
    "Power BI / Tableau": ["power bi", "powerbi", "tableau", "looker"],
    "ETL / pipelines": ["etl", "elt", "data pipeline", "medallion"],
    "MCP": ["mcp", "model context protocol"],
    "Agentic / multi-agent": ["agentic", "multi-agent", "multi agent",
                              "ai agent", "adk"],
    "Guardrails / evaluation": ["guardrail*", "evaluation framework",
                                "groundedness", "hallucination"],
    "Observability / tracing": ["observability", "tracing", "langfuse",
                                "monitoring"],
}


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
    resume = (resume_text or "").lower()

    demand: dict[str, int] = {}
    for j in kept:
        blob = ((j.get("jd_text") or "") + " " + (j.get("title") or "")).lower()
        for label, forms in SKILL_GROUPS.items():
            if mentions(blob, forms):
                demand[label] = demand.get(label, 0) + 1

    on_resume = {label: mentions(resume, SKILL_GROUPS[label]) for label in demand}
    gaps = sorted(((n, s) for s, n in demand.items() if not on_resume[s]),
                  reverse=True)
    covered = sorted(((n, s) for s, n in demand.items() if on_resume[s]),
                     reverse=True)

    return {
        "analysed": len(kept),
        "dropped": dropped,
        "demand": demand,
        "on_resume": on_resume,
        "gaps": [{"skill": s, "postings": n,
                  "share": round(n / len(kept), 2) if kept else 0.0}
                 for n, s in gaps],
        "covered": [{"skill": s, "postings": n} for n, s in covered],
        "titles": [j.get("title", "") for j in kept][:20],
    }
