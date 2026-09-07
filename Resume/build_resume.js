/*
 * Rajiv Ranjan Jha — AI Engineer resume, 2026.
 *
 * Brief chosen by him:
 *   one page, dense · Experience before Projects · aimed at AI product companies
 *   and funded startups · metric-led, short clauses, no long sentences
 *
 * Technologies are placed in PROJECT AND ROLE BULLETS, not parked in a skills
 * list, because a tool named next to a number reads as experience while the same
 * tool in a comma-separated list reads as a claim.
 *
 * Every tool here was confirmed by him as actually used:
 *   agent frameworks  LangGraph, LangChain, CrewAI, custom ReAct loop
 *   retrieval         FAISS, pgvector, Pinecone, hybrid dense + BM25 + re-rank
 *   evaluation        faithfulness/groundedness, ROUGE, BERTScore, exact match, recall@k
 *   serving           vLLM, llama.cpp / GGUF
 *   observability     Langfuse, LangSmith, Helicone, MLflow
 *
 * Deliberately ABSENT: AWS (never used), Qdrant, Triton, SGLang, Milvus, ChromaDB,
 * W&B, Azure Monitor, Prometheus, LLM-as-judge — none confirmed.
 *
 * NO INVENTED NUMBERS. He asked whether I would fabricate metrics; I would not.
 * Every figure below already existed in his own material. There were ~20 of them
 * and most were buried mid-sentence rather than used as the point of the bullet,
 * which is why the old version read as prose and this one reads as evidence.
 */
const {
  Document, Packer, Paragraph, TextRun, AlignmentType,
  BorderStyle, ExternalHyperlink, TabStopType, TabStopPosition,
} = require("docx");
const fs = require("fs");

const F = "Calibri";
const BODY = 18;        // half-points → 9pt
const MARGIN = { top: 400, bottom: 340, left: 600, right: 600 };

const t = (text, opts = {}) => new TextRun({ text, font: F, size: BODY, ...opts });
const bold = text => t(text, { bold: true });

function rule() {
  return new Paragraph({
    spacing: { before: 30, after: 60 },
    border: { bottom: { style: BorderStyle.SINGLE, size: 6, color: "AAAAAA", space: 1 } },
  });
}

function heading(text) {
  return new Paragraph({
    spacing: { before: 120, after: 32 },
    border: { bottom: { style: BorderStyle.SINGLE, size: 4, color: "CCCCCC", space: 2 } },
    children: [new TextRun({ text, font: F, size: 19, bold: true,
                             color: "111111", characterSpacing: 24 })],
  });
}

function skills(label, body) {
  return new Paragraph({
    spacing: { after: 20 },
    children: [
      new TextRun({ text: `${label}  `, font: F, size: BODY, bold: true, color: "111111" }),
      new TextRun({ text: body, font: F, size: BODY }),
    ],
  });
}

function role(title, org, dates) {
  return new Paragraph({
    keepNext: true,
    spacing: { before: 84, after: 12 },
    tabStops: [{ type: TabStopType.RIGHT, position: TabStopPosition.MAX }],
    children: [
      new TextRun({ text: title, font: F, size: 19, bold: true }),
      new TextRun({ text: `  ·  ${org}`, font: F, size: 19, bold: true, color: "333333" }),
      new TextRun({ text: `\t${dates}`, font: F, size: 17, color: "555555" }),
    ],
  });
}

function sub(text) {
  return new Paragraph({
    keepNext: true,
    spacing: { before: 30, after: 10 },
    children: [new TextRun({ text, font: F, size: 17, italics: true, color: "444444" })],
  });
}

function project(title, stack) {
  return new Paragraph({
    keepNext: true,
    spacing: { before: 80, after: 12 },
    children: [
      new TextRun({ text: title, font: F, size: 19, bold: true }),
      new TextRun({ text: `   ${stack}`, font: F, size: 16, italics: true, color: "555555" }),
    ],
  });
}

/* Bullets lead with the metric. `runs` mixes strings and bold({}) markers. */
function li(...runs) {
  return new Paragraph({
    bullet: { level: 0 },
    spacing: { after: 26 },
    indent: { left: 200, hanging: 160 },
    children: runs.map(r => (typeof r === "string" ? t(r) : bold(r.b))),
  });
}
const B = b => ({ b });

const doc = new Document({
  styles: { default: { document: { run: { font: F, size: BODY } } } },
  numbering: { config: [] },
  sections: [{
    properties: { page: { margin: MARGIN } },
    children: [
      // ── header ──────────────────────────────────────────────────────────
      new Paragraph({
        alignment: AlignmentType.CENTER, spacing: { after: 26 },
        children: [new TextRun({ text: "RAJIV RANJAN JHA", font: F, size: 30,
                                 bold: true, characterSpacing: 40 })],
      }),
      new Paragraph({
        alignment: AlignmentType.CENTER, spacing: { after: 34 },
        children: [new TextRun({
          text: "AI Engineer  ·  Agentic Systems, RAG & LLMOps",
          font: F, size: 19, color: "222222" })],
      }),
      new Paragraph({
        alignment: AlignmentType.CENTER, spacing: { after: 16 },
        children: [
          new TextRun({ text: "Bengaluru (open to relocation)  ·  8825330125  ·  rajiv.jha.0003@gmail.com  ·  ",
                        font: F, size: 16, color: "444444" }),
          new ExternalHyperlink({ link: "https://linkedin.com/in/rajivranjan-jha",
            children: [new TextRun({ text: "linkedin/rajivranjan-jha", font: F,
                                     size: 16, color: "1155CC" })] }),
          new TextRun({ text: "  ·  ", font: F, size: 16, color: "444444" }),
          new ExternalHyperlink({ link: "https://github.com/Rajivjha003",
            children: [new TextRun({ text: "github/Rajivjha003", font: F,
                                     size: 16, color: "1155CC" })] }),
        ],
      }),
      rule(),

      // ── summary: five clauses, four numbers, no prose ───────────────────
      new Paragraph({
        spacing: { after: 20 },
        children: [
          bold("AI Engineer, 4 yrs"), t(" — production "), bold("agentic systems, RAG, LLMOps"),
          t(". Multi-agent RAG at "), bold("<800ms TTFT"), t(", "),
          bold(">95% faithfulness"), t(", "), bold("85%+ recall@5"), t(". "),
          bold("LoRA/QLoRA"), t(" fine-tuning on a "), bold("single 8GB GPU"), t(" — "),
          bold("2× throughput"), t(", spend "), bold("under $0.50"),
          t(". Owns the full path: data → training → eval → serving → infra."),
        ],
      }),

      // ── skills: grouped tight, no filler ────────────────────────────────
      heading("SKILLS"),
      skills("Agents",
        "LangGraph · LangChain · CrewAI · Google ADK · MCP · custom ReAct loops · " +
        "function/tool calling · Pydantic structured outputs · prompt engineering"),
      skills("RAG & Retrieval",
        "Hybrid dense + BM25 · cross-encoder re-ranking · GraphRAG · Schema-RAG · " +
        "FAISS · pgvector · Pinecone · Sentence-Transformers · recall@k tuning"),
      skills("Eval & Safety",
        "Faithfulness/groundedness · hallucination rate · ROUGE · BERTScore · exact match · " +
        "evidence gates · golden sets · PII redaction · input/output guardrails"),
      skills("Serving & Obs",
        "vLLM · llama.cpp/GGUF · Ollama · FastAPI · SSE streaming · " +
        "Langfuse · LangSmith · Helicone · MLflow"),
      skills("Fine-tuning",
        "LoRA/QLoRA (PEFT) · rsLoRA · Unsloth · BF16 · gradient checkpointing · " +
        "CPT/SFT · Hugging Face · PyTorch"),
      skills("ML",
        "LightGBM · time-series forecasting (WAPE) · YOLO · ArcFace · diffusion · " +
        "statistical modelling"),
      skills("Data & Cloud",
        "BigQuery · PostgreSQL/AlloyDB · Medallion (Bronze/Silver/Gold) · Dataform · " +
        "ETL/ELT · MinHash dedup · GCP / Google Cloud (Cloud Run, Vertex AI) · Azure/Databricks · " +
        "Docker · CI/CD · cloud-agnostic deploys"),

      // ── experience ──────────────────────────────────────────────────────
      heading("EXPERIENCE"),
      role("Senior Data Scientist / AI Engineer", "Alkye Technologies", "Sep 2024 – Present"),
      sub("Merchmix — AI inventory-planning SaaS"),
      li(B("70–80% of analyst queries eliminated"),
         " — multi-agent, multi-modal RAG chatbot: ", B("LangGraph"), " orchestration + ",
         B("CrewAI"), " role delegation over ", B("Google ADK"), " and ", B("MCP tools"), "."),
      li(B("85%+ recall@5, >95% faithfulness, <800ms TTFT"), " — ",
         B("hybrid retrieval"), ": dense vectors (", B("pgvector"), ") + ", B("BM25"),
         ", ", B("cross-encoder re-rank"), ", schema-aware over BigQuery/PostgreSQL."),
      li(B("Langfuse"), " + ", B("LangSmith"),
         " tracing on every agent run — per-run token spend, latency, tool-call traces."),
      li(B("60% dashboard query cost cut"), " — Medallion (Bronze/Silver/Gold) pipeline, ",
         "BigQuery → AlloyDB via API ingestion, zero freshness loss."),
      li(B("86% WAPE"), " forecast accuracy — ", B("LightGBM"),
         " time-series across brand/category hierarchies, daily + weekly SKU demand."),
      li("Latency cut under load — Django ", B("sync → async I/O"), ", ", B("Redis"),
         " caching, load-balanced ", B("Docker/Cloud Run"), " via CI/CD."),
      sub("Fittora — AI made-to-order tailoring platform (Australian client)"),
      li(B("95%+ identity-retrieval accuracy"), " — ", B("ArcFace"),
         " cosine similarity, matching published ", B("SOTA 94–99%"), "."),
      li("Solved ", B("multi-angle identity drift"),
         " — custom annotation strategy + targeted model fine-tuning on the failure cases."),
      li(B("<1s mockup generation, 3–5× faster"), " than the baseline ", B("diffusion"),
         " model — dual-path architecture: primary face-swap with model fallback."),
      li(B("99%+ generation success rate"),
         " held in production — fallback path absorbs primary-model failures."),

      role("Data Scientist", "SK Associates", "Jul 2022 – Sep 2024"),
      li("Shipped custom ", B("YOLO"),
         " object-detection models to production for client computer-vision applications — ",
         "owned the full path end to end."),
      li("Built the ", B("annotation and training pipeline"),
         " — dataset curation, labelling standards, iteration on failure cases."),
      li("Owned ", B("model serving and deployment"),
         " for CV workloads, from trained weights to a running client-facing service."),
      li("Built the ", B("SQL and ETL pipelines"),
         " feeding reporting — replaced raw-table extracts with modelled, query-ready data."),
      li("Replaced raw-table reporting with ", B("executive dashboards"),
         " surfacing revenue and profit drivers for banking-client leadership decisions."),

      // ── projects ────────────────────────────────────────────────────────
      heading("PROJECTS"),
      project("LLMOps Fine-Tuning Pipeline — e-commerce domain adaptation",
              "PyTorch · Unsloth · LoRA/PEFT · vLLM · llama.cpp · MLflow · LangChain · FastAPI"),
      li(B("TinyLlama 1.1B"), " domain-adapted on a ", B("single 8GB consumer GPU"),
         " — ", B("LoRA rank-32 (rsLoRA)"), ", ", B("2× throughput"),
         " via Unsloth BF16 + gradient checkpointing."),
      li("Served through ", B("vLLM"), " and ", B("llama.cpp/GGUF"),
         "; runs and checkpoints tracked in ", B("MLflow"), " model registry."),
      li(B("ROUGE"), " and ", B("BERTScore"), " gains vs base, plus ", B("exact-match"),
         " scoring on a held-out golden set via ", B("LangChain"), " eval chains."),
      li(B("14K → 6.4K docs (54% rejected)"), " — 7-filter ", B("guardrail"),
         " and curation engine: ", B("PII redaction"),
         ", MinHash dedup, quality scoring, percentile pruning, full audit trails."),
      li("Production FastAPI service — job management, ", B("SSE log streaming"),
         ", web UI; domain-portable by YAML with ", B("zero code changes"), "."),

      project("Autonomous Coding Agent — evidence-gated execution harness",
              "Asyncio · Vertex AI (Gemini) · Playwright · Pydantic · Helicone · 324 tests"),
      li(B("15 typed tools"), " in a custom ", B("ReAct loop"),
         " (plan → act → observe → verify) — ", B("function calling"), " with ",
         B("Pydantic structured outputs"), ", no LLM awareness of the tool layer."),
      li(B("Caught false success in 3 of 9 (33%)"),
         " apparently-passing runs — ", B("evidence gates"),
         " independently re-execute success predicates; verification via a real browser's ",
         "accessibility tree."),
      li(B("94% tool-output compression"), " — observation compiler preserving error spans, ",
         "holding context at ", B("22K of a 1M window"), "; total spend ", B("under $0.50"), "."),
      li(B("324 tests"), "; ", B("Helicone"), " for per-call cost and latency tracking."),

      // ── education ───────────────────────────────────────────────────────
      heading("EDUCATION"),
      new Paragraph({
        tabStops: [{ type: TabStopType.RIGHT, position: TabStopPosition.MAX }],
        children: [
          bold("B.Tech · CV Raman Global University, Bhubaneswar"),
          new TextRun({ text: "\tJul 2018 – Jun 2022", font: F, size: 17, color: "555555" }),
        ],
      }),
    ],
  }],
});

Packer.toBuffer(doc).then(buf => {
  fs.writeFileSync("D:/AI WORK/LINKDIN_BOT/Resume/Rajiv_Ranjan_Jha_Resume_2026_v2.docx", buf);
  console.log("written");
});
