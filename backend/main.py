import os
import json
import asyncio
import re
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
import httpx
import pdfplumber
import tempfile
import time
start = time.time()

# ─────────────────────────────────────────
# JSON PARSER
# ─────────────────────────────────────────
def safe_json_parse(text: str):
    try:
        clean = text.strip().replace("```json", "").replace("```", "")
        # Attempt repair for unescaped quotes
        clean = clean.replace('"AS IS"', '\\"AS IS\\"')
        clean = clean.replace('"AS AVAILABLE"', '\\"AS AVAILABLE\\"')
        
        # Try object first
        match = re.search(r'\{.*\}', clean, re.DOTALL)
        if match:
            return json.loads(match.group())
        
        # Fallback: try array and wrap it
        arr_match = re.search(r'\[.*\]', clean, re.DOTALL)
        if arr_match:
            arr = json.loads(arr_match.group())
            # Wrap array into expected object shape
            if arr and isinstance(arr, list):
                first_key = list(arr[0].keys())[0] if arr else None
                if first_key and 'clause' in first_key:
                    return {"clauses": arr}
                elif first_key and 'policy' in first_key:
                    return {"policy_checks": arr}
                elif first_key and 'risk' in first_key:
                    return {"risk_scores": arr}
                elif first_key and 'redline' in first_key:
                    return {"redlines": arr}
        
        raise ValueError("No JSON found")
    except Exception as e:
        print("\n===== JSON PARSE ERROR =====")
        print(str(e))
        print("\n===== RAW MODEL OUTPUT =====")
        print(text)
        print("============================\n")
        print(f"Analysis completed in {time.time() - start:.2f}s")
        return None
    
    
def compact_clauses(clauses):
    return [
        {
            "id": c.get("id"),
            "type": c.get("type"),
            "text": c.get("text", "")[:300]
        }
        for c in clauses
    ]
app = FastAPI(title="ContractSentinel API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─────────────────────────────────────────
# API KEYS + ENDPOINTS
# ─────────────────────────────────────────
CEREBRAS_API_KEY = os.getenv("CEREBRAS_API_KEY", "")
GROQ_API_KEY     = os.getenv("GROQ_API_KEY", "")

CEREBRAS_URL = "https://api.cerebras.ai/v1/chat/completions"
GROQ_URL     = "https://api.groq.com/openai/v1/chat/completions"


# ─────────────────────────────────────────
# CEREBRAS CALLER — Agents 1 & 2
# llama-3.3-70b, high TPM, very fast
# ─────────────────────────────────────────
async def call_cerebras(prompt: str) -> str:
    payload = {
        "model": "llama3.1-8b",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 2000,
    }
    async with httpx.AsyncClient(timeout=90.0) as client:
        resp = await client.post(
            CEREBRAS_URL,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {CEREBRAS_API_KEY}",
            },
            json=payload,
        )
        if not resp.is_success:
            raise HTTPException(
                status_code=resp.status_code,
                detail=f"Cerebras error {resp.status_code}: {resp.text}"
            )
            
        return resp.json()["choices"][0]["message"]["content"]


# ─────────────────────────────────────────
# GROQ CALLER — Agents 3 & 4
# llama-3.3-70b-versatile, sequential calls
# ─────────────────────────────────────────
async def call_groq(prompt: str) -> str:
    await asyncio.sleep(1)  # stay under TPM limit
    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 2000,
    }
    async with httpx.AsyncClient(timeout=90.0) as client:
        resp = await client.post(
            GROQ_URL,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {GROQ_API_KEY}",
            },
            json=payload,
        )
        if resp.status_code == 429:
            print("Groq rate limit hit. Waiting 45s...")
            await asyncio.sleep(45)
            return await call_groq(prompt)

        if not resp.is_success:
            raise HTTPException(
                status_code=resp.status_code,
                detail=f"Groq error {resp.status_code}: {resp.text}"
            )
        return resp.json()["choices"][0]["message"]["content"]

# ─────────────────────────────────────────
# AGENT 1 — Clause Extractor (Cerebras)
# ─────────────────────────────────────────
async def agent_clause_extractor(contract_text: str) -> dict:
    prompt = f"""You are a senior legal analyst extracting contract clauses. Be SPECIFIC — quote exact language, never be vague.
    IMPORTANT:
- Escape all internal quotation marks inside JSON strings using \"
- Return strictly valid JSON parsable by json.loads()
- Do not include trailing commas

CONTRACT TEXT:
{contract_text[:5000]}

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
- "text" must quote or closely paraphrase actual contract language.
- "key_snippet" is the single most legally significant sentence, verbatim if possible.
- "clause_confidence" is 0-100.
- - Extract ONLY the 10 most legally significant clauses.
- Prioritize these clause categories:
  Payment Terms,
  Intellectual Property,
  Liability/Indemnification,
  Termination,
  Confidentiality,
  Non-Compete,
  Dispute Resolution,
  Warranty,
  Governing Law,
  Force Majeure,
  Data Privacy,
  SLA,
  Audit Rights.
- If multiple clauses are similar, keep only the most important one.
"""

    result = await call_cerebras(prompt)
    parsed = safe_json_parse(result)
    print(f"Analysis completed in {time.time() - start:.2f}s")
    return parsed if parsed else {"clauses": [], "error": "Extraction failed"}

    

# ─────────────────────────────────────────
# AGENT 2 — Policy Checker (Cerebras)
# ─────────────────────────────────────────
async def agent_policy_checker(clauses: list) -> dict:
    prompt = f"""You are an enterprise legal compliance officer. Check each clause against enterprise policy standards.
    IMPORTANT:
- Escape all internal quotation marks inside JSON strings using \"
- Return strictly valid JSON parsable by json.loads()
- Do not include trailing commas

CLAUSES:
{json.dumps(compact_clauses(clauses), indent=2)}
    
    
    

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

severity must be: CRITICAL, MAJOR, or MINOR. Only use null for issue if fully compliant."""

    result = await call_cerebras(prompt)
    parsed = safe_json_parse(result)
    print(f"Analysis completed in {time.time() - start:.2f}s")
    return parsed if parsed else {"policy_checks": []}


# ─────────────────────────────────────────
# AGENT 3 — Risk Scorer (Groq)
# ─────────────────────────────────────────
async def agent_risk_scorer(clauses: list, policy_checks: list) -> dict:
    prompt = f"""You are a contract risk analyst. Score each clause with SPECIFIC, contract-aware reasoning.
    IMPORTANT:
- Escape all internal quotation marks inside JSON strings using \"
- Return strictly valid JSON parsable by json.loads()
- Do not include trailing commas

CLAUSES:
{json.dumps(compact_clauses(clauses), indent=2)}

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
      "reasoning": "Specific reasoning about this clause's risk based on actual contract language.",
      "business_impact_type": "IP Ownership Risk",
      "business_impact_detail": "Specific business consequence if this clause is not addressed.",
      "human_review": "REQUIRED",
      "review_label": "Requires legal review"
    }}
  ],
  "overall_risk": "HIGH",
  "overall_score": 74,
  "summary": "Specific 2-3 sentence executive summary referencing actual contract terms."
}}

Rules:
- risk_level: LOW, MEDIUM, or HIGH
- risk_score: 0-100
- risk_confidence: 0-100
- business_impact_type: Financial Exposure, IP Ownership Risk, Vendor Lock-In Risk, Compliance Risk, Operational Risk, Data Privacy Risk, or Reputational Risk
- human_review: REQUIRED for HIGH, RECOMMENDED for MEDIUM, NOT_REQUIRED for LOW
- review_label: "Requires legal review" / "Recommended review" / "Auto-approved"
- Never write generic phrases like "may pose some risk" """

    result = await call_groq(prompt)
    parsed = safe_json_parse(result)
    print(f"Risk scoring completed in {time.time() - start:.2f}s")
    return parsed if parsed else {"risk_scores": [], "overall_risk": "UNKNOWN", "overall_score": 0, "summary": ""}


# ─────────────────────────────────────────
# AGENT 4 — Redline Engine (Groq)
# Always generates redlines for HIGH + MEDIUM
# ─────────────────────────────────────────
async def agent_redline_suggester(clauses: list, risk_scores: list, policy_checks: list) -> dict:
    redline_ids = {r["clause_id"] for r in risk_scores if r.get("risk_level") in ["HIGH", "MEDIUM"]}
    risky_clauses = [c for c in clauses if c["id"] in redline_ids]

    if not risky_clauses:
        risky_clauses = clauses[:5]

    risk_map = {r["clause_id"]: r for r in risk_scores}

    prompt = f"""You are a senior contract attorney. Suggest redlined improvements for EVERY clause below. Never skip one.
    IMPORTANT:
- Escape all internal quotation marks inside JSON strings using \"
- Return strictly valid JSON parsable by json.loads()
- Do not include trailing commas

CLAUSES TO REDLINE:
{json.dumps(compact_clauses(risky_clauses), indent=2)}

POLICY VIOLATIONS:
{json.dumps(policy_checks, indent=2)}

Return ONLY valid JSON, no markdown:
{{
  "redlines": [
    {{
      "clause_id": "clause_1",
      "risk_trigger": "Specific risk found in this clause",
      "policy_violated": "Which policy this violates",
      "original": "The original problematic language from the contract",
      "suggested": "Your improved protective legal language",
      "negotiation_tip": "Practical advice for negotiating this change with the vendor.",
      "redline_confidence": 88
    }}
  ]
}}

redline_confidence: 0-100. Every clause in the input MUST have a redline in the output."""

    result = await call_groq(prompt)
    parsed = safe_json_parse(result)
    print(f"Redline suggestions completed in {time.time() - start:.2f}s")
    if not parsed:
        return {"redlines": _fallback_redlines(risky_clauses, risk_map)}

    if not parsed.get("redlines"):
        parsed["redlines"] = _fallback_redlines(risky_clauses, risk_map)

    return parsed


def _fallback_redlines(clauses: list, risk_map: dict) -> list:
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
    redlines = []
    for clause in clauses[:5]:
        ctype = clause.get("type", "")
        tmpl = templates.get(ctype, {
            "original": clause.get("text", "Original clause language."),
            "suggested": "Add specific protective language addressing the identified risk.",
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
    # Agents 1 & 2 on Cerebras — can run parallel, high TPM
    extraction_result = await agent_clause_extractor(contract_text)
    clauses = extraction_result.get("clauses", [])

    if not clauses:
        raise HTTPException(status_code=422, detail="Could not extract clauses from contract")

    # Policy check on Cerebras (fast, no rate limit)
    policy_result = await agent_policy_checker(clauses)
    policy_checks = policy_result.get("policy_checks", [])

    # Agents 3 & 4 on Groq — sequential with sleep to stay under TPM
    risk_result = await agent_risk_scorer(clauses, policy_checks)
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


# ─────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────
@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "cerebras_configured": bool(CEREBRAS_API_KEY),
        "groq_configured": bool(GROQ_API_KEY),
        "version": "2.0.0"
    }


@app.post("/analyze")
async def analyze_contract(file: UploadFile = File(...)):
    if not CEREBRAS_API_KEY:
        raise HTTPException(status_code=500, detail="CEREBRAS_API_KEY not configured")
    if not GROQ_API_KEY:
        raise HTTPException(status_code=500, detail="GROQ_API_KEY not configured")
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