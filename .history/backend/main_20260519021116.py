import os
import json
import asyncio
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
import httpx
import pdfplumber
import tempfile
import time

app = FastAPI(title="ContractSentinel API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- API Keys ---
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# --- Endpoints ---
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GEMINI_FLASH_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent"


# ─────────────────────────────────────────
# GROQ CALLER — Agents 1, 2, 3
# llama-3.3-70b: fast, free, no rate issues
# ─────────────────────────────────────────
async def call_groq(prompt: str) -> str:
    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": 1024,
    }

    async with httpx.AsyncClient(timeout=90.0) as client:

        for attempt in range(4):

            resp = await client.post(
                GROQ_URL,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                },
                json=payload,
            )

            if resp.status_code == 429:
                wait_time = 2 ** attempt
                print(f"Groq rate limited. Retrying in {wait_time}s...")
                await asyncio.sleep(wait_time)
                continue

            if resp.status_code != 200:
                print("GROQ ERROR:", resp.status_code, resp.text)

            resp.raise_for_status()

            return resp.json()["choices"][0]["message"]["content"]

    raise HTTPException(
        status_code=429,
        detail="Groq rate limit exceeded. Try again shortly."
    )

start = time.time()
# ─────────────────────────────────────────
# GEMINI FLASH CALLER — Agent 4 (Redline)
# Keeps Google prize eligibility
# ─────────────────────────────────────────
async def call_gemini_flash(prompt: str) -> str:
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 768},
    }
    async with httpx.AsyncClient(timeout=90.0) as client:
        for attempt in range(4):
            resp = await client.post(
                f"{GEMINI_FLASH_URL}?key={GEMINI_API_KEY}",
                headers={"Content-Type": "application/json"},
                json=payload,
            )
            if resp.status_code == 429:
                print("GEMINI STATUS:", resp.status_code)
                await asyncio.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
            return resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    raise HTTPException(status_code=429, detail="Gemini rate limit exceeded. Try again in a moment.")


# ─────────────────────────────────────────
# AGENT 1 — Clause Extractor (Groq)
# ─────────────────────────────────────────
async def agent_clause_extractor(contract_text: str) -> dict:
    prompt = f"""You are a legal clause extraction specialist. Analyze this contract and extract ALL important clauses.

CONTRACT TEXT:
{contract_text[:8000]}

Return ONLY a valid JSON object, no markdown, no explanation:
{{
  "clauses": [
    {{
      "id": "clause_1",
      "type": "Payment Terms",
      "title": "Short title",
      "text": "The exact clause text or summary",
      "section": "Section number if available"
    }}
  ]
}}

Extract clauses for: Payment Terms, Intellectual Property, Liability/Indemnification, Termination, Confidentiality, Non-Compete, Dispute Resolution, Warranty, Governing Law, Force Majeure, and any other significant clauses."""

    result = await call_groq(prompt)
    try:
        clean = result.strip().replace("```json", "").replace("```", "").strip()
        return json.loads(clean)
    except:
        return {"clauses": [], "error": "Extraction failed"}


# ─────────────────────────────────────────
# AGENT 2 — Policy Checker (Groq)
# ─────────────────────────────────────────
async def agent_policy_checker(clauses: list) -> dict:
    prompt = f"""You are an enterprise legal policy compliance officer. Check these clauses against standard enterprise best practices.

CLAUSES:
{json.dumps(clauses, indent=2)}

Policies to check:
- Payment terms: Net-30 or better
- IP: must vest with commissioning company
- Liability cap: should not exceed 2x contract value
- Termination notice: at least 30 days
- Non-compete: max 1 year and 50-mile radius
- Governing law: must be favorable jurisdiction

Return ONLY valid JSON, no markdown:
{{
  "policy_checks": [
    {{
      "clause_id": "clause_1",
      "compliant": true,
      "policy_rule": "Which policy this was checked against",
      "issue": "Description of issue if not compliant, or null if compliant"
    }}
  ]
}}"""

    result = await call_groq(prompt)
    try:
        clean = result.strip().replace("```json", "").replace("```", "").strip()
        return json.loads(clean)
    except:
        return {"policy_checks": []}


# ─────────────────────────────────────────
# AGENT 3 — Risk Scorer (Groq)
# ─────────────────────────────────────────
async def agent_risk_scorer(clauses: list, policy_checks: list) -> dict:
    prompt = f"""You are a contract risk assessment specialist. Score risk for each clause.

CLAUSES:
{json.dumps(clauses, indent=2)}

POLICY ISSUES:
{json.dumps(policy_checks, indent=2)}

Return ONLY valid JSON, no markdown:
{{
  "risk_scores": [
    {{
      "clause_id": "clause_1",
      "risk_level": "HIGH",
      "risk_score": 85,
      "reasoning": "Why this is risky",
      "impact": "Business impact if not addressed"
    }}
  ],
  "overall_risk": "HIGH",
  "overall_score": 72,
  "summary": "2-3 sentence executive summary of the contract risk profile."
}}

risk_level must be: LOW, MEDIUM, or HIGH. risk_score is 0-100."""

    result = await call_groq(prompt)
    try:
        clean = result.strip().replace("```json", "").replace("```", "").strip()
        return json.loads(clean)
    except:
        return {"risk_scores": [], "overall_risk": "UNKNOWN", "overall_score": 0, "summary": ""}


# ─────────────────────────────────────────
# AGENT 4 — Redline Engine (Gemini Flash)
# Uses Google model — keeps prize eligibility
# ─────────────────────────────────────────
async def agent_redline_suggester(clauses: list, risk_scores: list) -> dict:

    high_risk_ids = {
        r["clause_id"]
        for r in risk_scores
        if r.get("risk_level") == "HIGH"
    }

    risky_clauses = [
        c for c in clauses
        if c["id"] in high_risk_ids
    ]

    if not risky_clauses:
        return {"redlines": []}

    prompt = f"""You are a contract attorney. Suggest improved protective wording for these risky clauses.

RISKY CLAUSES:
{json.dumps(risky_clauses, indent=2)}

Return ONLY valid JSON, no markdown:
{{
  "redlines": [
    {{
      "clause_id": "clause_1",
      "original": "Original problematic text (summarized)",
      "suggested": "Your improved, safer wording",
      "rationale": "Why this change protects the company"
    }}
  ]
}}"""

    try:

        result = await call_gemini_flash(prompt)

        clean = (
            result.strip()
            .replace("```json", "")
            .replace("```", "")
            .strip()
        )

        return json.loads(clean)

    except Exception as e:

        print("Gemini failed:", str(e))

        return {
            "redlines": [],
            "warning": "Redline generation temporarily unavailable due to Gemini rate limits."
        }


# ─────────────────────────────────────────
# PLANNER AGENT — Orchestrator
# ─────────────────────────────────────────
async def planner_agent(contract_text: str) -> dict:
    # Step 1: Extract clauses (sequential — others depend on this)
    extraction_result = await agent_clause_extractor(contract_text)
    clauses = extraction_result.get("clauses", [])

    if not clauses:
        raise HTTPException(status_code=422, detail="Could not extract clauses from contract")

    # Step 2: Policy check + Risk score in parallel (both use Groq, no conflicts)
    policy_result = await agent_policy_checker(clauses)

    risk_result = await agent_risk_scorer(
    clauses,
    policy_result.get("policy_checks", [])
)

    policy_checks = policy_result.get("policy_checks", [])
    risk_scores = risk_result.get("risk_scores", [])

    # Step 3: Redline suggestions via Gemini Flash (single call, no rate limit issues)
    redline_result = await agent_redline_suggester(clauses, risk_scores)

    return {
        "clauses": clauses,
        "policy_checks": policy_checks,
        "risk_scores": risk_scores,
        "overall_risk": risk_result.get("overall_risk", "UNKNOWN"),
        "overall_score": risk_result.get("overall_score", 0),
        "summary": risk_result.get("summary", ""),
        "redlines": redline_result.get("redlines", [])
    }


# ─────────────────────────────────────────
# API ROUTES
# ─────────────────────────────────────────
@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "groq_configured": bool(GROQ_API_KEY),
        "gemini_configured": bool(GEMINI_API_KEY),
    }


@app.post("/analyze")
async def analyze_contract(file: UploadFile = File(...)):
    if not GROQ_API_KEY:
        raise HTTPException(status_code=500, detail="GROQ_API_KEY not configured")
    if not GEMINI_API_KEY:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY not configured")
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files supported")

    contents = await file.read()
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(contents)
        tmp_path = tmp.name

    try:
        contract_text = ""
        with pdfplumber.open(tmp_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    contract_text += text + "\n"
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"PDF parsing failed: {str(e)}")
    finally:
        os.unlink(tmp_path)

    if len(contract_text.strip()) < 100:
        raise HTTPException(status_code=422, detail="Could not extract text. Use a text-based PDF.")

    result = await planner_agent(contract_text)
    result["filename"] = file.filename
    result["char_count"] = len(contract_text)
    return JSONResponse(content=result)


# Serve frontend — must be LAST
if os.path.exists("static"):
    app.mount("/", StaticFiles(directory="static", html=True), name="static")
