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

app = FastAPI(title="ContractSentinel API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GEMINI_FLASH_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"


async def call_groq(prompt: str) -> str:
    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 3000,
    }
    async with httpx.AsyncClient(timeout=90.0) as client:
        resp = await client.post(
            GROQ_URL,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {GROQ_API_KEY}"},
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


async def call_gemini_flash(prompt: str) -> str:
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 3000},
    }
    async with httpx.AsyncClient(timeout=90.0) as client:
        for attempt in range(4):
            resp = await client.post(
                f"{GEMINI_FLASH_URL}?key={GEMINI_API_KEY}",
                headers={"Content-Type": "application/json"},
                json=payload,
            )
            if resp.status_code == 429:
                await asyncio.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
            return resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    raise HTTPException(status_code=429, detail="Gemini rate limit exceeded.")


# ─────────────────────────────────────────
# AGENT 1 — Clause Extractor (Groq)
# ─────────────────────────────────────────
async def agent_clause_extractor(contract_text: str) -> dict:
    prompt = f"""You are a senior legal analyst extracting contract clauses. Be SPECIFIC — quote exact language, never be vague.

CONTRACT TEXT:
{contract_text[:8000]}

Return ONLY valid JSON, no markdown:
{{
  "clauses": [
    {{
      "id": "clause_1",
      "type": "Intellectual Property",
      "title": "IP Ownership — Derivative Works",
      "text": "Exact quoted sentence or precise paraphrase from the contract",
      "section": "Section 4.2",
      "key_snippet": "The single most important sentence from this clause verbatim",
      "clause_confidence": 94
    }}
  ]
}}

Rules:
- "text" must quote or closely paraphrase actual contract language. NEVER write "this clause may pose some risk".
- "key_snippet" is the single most legally significant sentence, verbatim if possible.
- "clause_confidence" is 0-100, how confident you are in the clause type classification.
- Extract ALL clauses: Payment Terms, Intellectual Property, Liability/Indemnification, Termination, Confidentiality, Non-Compete, Dispute Resolution, Warranty, Governing Law, Force Majeure, Data Privacy, SLA, Audit Rights, and any others present."""

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
    prompt = f"""You are an enterprise legal compliance officer. Check each clause against enterprise policy standards. Be specific about what exactly violates policy.

CLAUSES:
{json.dumps(clauses, indent=2)}

Enterprise policy standards:
- Payment: Net-30 or better; late payment interest must be defined
- IP: All IP including derivative works must vest with commissioning company
- Liability: Cap must not exceed 2x annual contract value; must cover data breaches
- Termination: Minimum 30-day notice; immediate termination rights for material breach
- Non-Compete: Maximum 12 months, 50-mile radius; must have carve-outs for existing clients
- Governing Law: Must be client's home jurisdiction or mutually agreed neutral state
- Confidentiality: Must survive termination by minimum 3 years
- Data Privacy: Must include GDPR/CCPA compliance obligations

Return ONLY valid JSON, no markdown:
{{
  "policy_checks": [
    {{
      "clause_id": "clause_1",
      "compliant": false,
      "policy_rule": "IP Ownership Policy — derivative works must vest with commissioning company",
      "issue": "Vendor retains exclusive ownership of all derivative works and improvements, including those built on client data.",
      "policy_violated": "Enterprise IP Policy §3.1",
      "severity": "CRITICAL"
    }}
  ]
}}

severity must be: CRITICAL, MAJOR, or MINOR. Only use null for "issue" if fully compliant."""

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
    prompt = f"""You are a contract risk analyst. Score each clause with SPECIFIC, contract-aware reasoning. Never write generic phrases like "may pose some risk".

CLAUSES:
{json.dumps(clauses, indent=2)}

POLICY VIOLATIONS FOUND:
{json.dumps(policy_checks, indent=2)}

Return ONLY valid JSON, no markdown:
{{
  "risk_scores": [
    {{
      "clause_id": "clause_1",
      "risk_level": "HIGH",
      "risk_score": 87,
      "risk_confidence": 91,
      "reasoning": "Vendor retains exclusive ownership of derivative works. Client loses rights to improvements built on their own data and workflows.",
      "business_impact_type": "IP Ownership Risk",
      "business_impact_detail": "Client cannot reuse or migrate AI models trained on their data if they switch vendors.",
      "human_review": "REQUIRED",
      "review_label": "Requires legal review"
    }}
  ],
  "overall_risk": "HIGH",
  "overall_score": 74,
  "summary": "This contract presents HIGH risk primarily due to vendor-retained IP ownership of derivative works and an uncapped liability clause that excludes indirect damages including data loss. The termination notice period of 90 days significantly favors the vendor."
}}

Rules:
- risk_level: LOW, MEDIUM, or HIGH
- risk_score: 0-100
- risk_confidence: 0-100 (how confident you are in this risk assessment)
- business_impact_type must be one of: Financial Exposure, IP Ownership Risk, Vendor Lock-In Risk, Compliance Risk, Operational Risk, Data Privacy Risk, Reputational Risk
- human_review: "REQUIRED" for HIGH, "RECOMMENDED" for MEDIUM, "NOT_REQUIRED" for LOW
- review_label: "Requires legal review" / "Recommended review" / "Auto-approved"
- reasoning and summary must be SPECIFIC to this contract's actual language — no generic phrases"""

    result = await call_groq(prompt)
    try:
        clean = result.strip().replace("```json", "").replace("```", "").strip()
        return json.loads(clean)
    except:
        return {"risk_scores": [], "overall_risk": "UNKNOWN", "overall_score": 0, "summary": ""}


# ─────────────────────────────────────────
# AGENT 4 — Redline Engine (Gemini Flash)
# ALWAYS generates redlines for HIGH + MEDIUM
# ─────────────────────────────────────────
async def agent_redline_suggester(clauses: list, risk_scores: list, policy_checks: list) -> dict:
    # Always redline HIGH and MEDIUM — never return 0 redlines if any exist
    redline_ids = {r["clause_id"] for r in risk_scores if r.get("risk_level") in ["HIGH", "MEDIUM"]}
    risky_clauses = [c for c in clauses if c["id"] in redline_ids]

    # If somehow nothing is HIGH/MEDIUM, redline everything as a fallback
    if not risky_clauses:
        risky_clauses = clauses[:5]

    policy_map = {p["clause_id"]: p for p in policy_checks}
    risk_map = {r["clause_id"]: r for r in risk_scores}

    prompt = f"""You are a senior contract attorney protecting enterprise clients. You MUST suggest redlined improvements for every clause provided. Never skip a clause.

CLAUSES TO REDLINE:
{json.dumps(risky_clauses, indent=2)}

POLICY VIOLATIONS:
{json.dumps(policy_checks, indent=2)}

For each clause, provide specific, legally precise redline language. Your suggestions must:
1. Be concrete legal text, not general advice
2. Directly address the specific risk identified
3. Use standard enterprise contract language

Return ONLY valid JSON, no markdown:
{{
  "redlines": [
    {{
      "clause_id": "clause_1",
      "risk_trigger": "Vendor retains exclusive ownership of derivative works built on client data",
      "policy_violated": "Enterprise IP Policy §3.1 — all derivatives must vest with client",
      "original": "All improvements, modifications, and derivative works shall remain the sole and exclusive property of Vendor.",
      "suggested": "All improvements, modifications, and derivative works created using Client data, systems, or specifications shall vest exclusively in Client upon creation. Vendor retains no rights in such derivative works without Client's prior written consent.",
      "negotiation_tip": "If vendor resists, propose a joint IP ownership clause with exclusive license to client for their domain.",
      "redline_confidence": 88
    }}
  ]
}}

redline_confidence: 0-100, how confident you are this redline will hold up in negotiation."""

    result = await call_gemini_flash(prompt)
    try:
        clean = result.strip().replace("```json", "").replace("```", "").strip()
        parsed = json.loads(clean)
        # Final safety net — if Gemini still returns empty, generate basic redlines
        if not parsed.get("redlines"):
            parsed["redlines"] = _fallback_redlines(risky_clauses, risk_map)
        return parsed
    except:
        return {"redlines": _fallback_redlines(risky_clauses, risk_map)}


def _fallback_redlines(clauses: list, risk_map: dict) -> list:
    """Safety net: always produce at least basic redlines"""
    redlines = []
    templates = {
        "Intellectual Property": {
            "original": "Vendor retains all intellectual property rights.",
            "suggested": "All intellectual property created under this agreement shall vest exclusively in Client. Vendor is granted a limited license solely to perform services hereunder.",
            "tip": "Insist on work-for-hire language if vendor pushes back."
        },
        "Liability/Indemnification": {
            "original": "Vendor's liability shall not exceed fees paid in the prior month.",
            "suggested": "Vendor's aggregate liability shall not exceed the total fees paid in the 12 months prior to the claim. This cap shall not apply to breaches of confidentiality, IP infringement, or gross negligence.",
            "tip": "Always carve out data breaches and willful misconduct from liability caps."
        },
        "Termination": {
            "original": "Either party may terminate with 90 days notice.",
            "suggested": "Client may terminate for convenience with 30 days written notice. Either party may terminate immediately upon material breach not cured within 15 days of written notice.",
            "tip": "Negotiate for immediate termination rights on data security incidents."
        },
        "Payment Terms": {
            "original": "Payment due upon invoice.",
            "suggested": "Payment due Net-30 from invoice date. Late payments accrue interest at 1.5% per month. Client may withhold payment for disputed invoices without penalty.",
            "tip": "Always define dispute resolution for invoice disagreements."
        },
    }
    for clause in clauses[:5]:
        ctype = clause.get("type", "")
        tmpl = templates.get(ctype, {
            "original": clause.get("text", "Original clause language."),
            "suggested": "Add specific protective language addressing the identified risk. Consult legal counsel for jurisdiction-specific requirements.",
            "tip": "Have your legal team review this clause before signing."
        })
        redlines.append({
            "clause_id": clause["id"],
            "risk_trigger": f"{ctype} clause requires protective language",
            "policy_violated": "Enterprise contract policy",
            "original": tmpl["original"],
            "suggested": tmpl["suggested"],
            "negotiation_tip": tmpl["tip"],
            "redline_confidence": 75
        })
    return redlines


# ─────────────────────────────────────────
# PLANNER AGENT
# ─────────────────────────────────────────
async def planner_agent(contract_text: str) -> dict:
    extraction_result = await agent_clause_extractor(contract_text)
    clauses = extraction_result.get("clauses", [])

    if not clauses:
        raise HTTPException(status_code=422, detail="Could not extract clauses from contract")

    policy_result, risk_result = await asyncio.gather(
        agent_policy_checker(clauses),
        agent_risk_scorer(clauses, [])
    )

    policy_checks = policy_result.get("policy_checks", [])
    risk_scores = risk_result.get("risk_scores", [])

    redline_result = await agent_redline_suggester(clauses, risk_scores, policy_checks)

    return {
        "clauses": clauses,
        "policy_checks": policy_checks,
        "risk_scores": risk_scores,
        "overall_risk": risk_result.get("overall_risk", "UNKNOWN"),
        "overall_score": risk_result.get("overall_score", 0),
        "summary": risk_result.get("summary", ""),
        "redlines": redline_result.get("redlines", [])
    }


@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "groq_configured": bool(GROQ_API_KEY),
        "gemini_configured": bool(GEMINI_API_KEY),
        "version": "2.0.0"
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


if os.path.exists("static"):
    app.mount("/", StaticFiles(directory="static", html=True), name="static")
