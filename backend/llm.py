"""Baseten GLM client — shared connection, caching, graceful fallbacks."""

from __future__ import annotations

import json
import os
import time
from typing import Any

import httpx

_client: httpx.AsyncClient | None = None
_summary_cache: dict[str, tuple[float, str]] = {}
CACHE_TTL = int(os.getenv("LLM_CACHE_SECONDS", "120"))

SYSTEM_PROMPT = (
    "You are a Medicare Part B wound care billing expert for skilled nursing facilities. "
    "Use ONLY the provided structured patient data. Never invent ICD codes, measurements, "
    "payer status, or routing decisions. Be concise (3-5 sentences max for summaries). "
    "If a field is missing or unknown, say so explicitly. "
    "Speak in plain language a biller can act on immediately."
)


def _cfg() -> tuple[str | None, str, str]:
    return (
        os.getenv("BASETEN_API_KEY"),
        os.getenv("BASETEN_BASE_URL", "https://inference.baseten.co/v1").rstrip("/"),
        os.getenv("BASETEN_MODEL", "zai-org/GLM-5.2"),
    )


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(45.0, connect=10.0))
    return _client


async def close_client() -> None:
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
        _client = None


async def complete(user_prompt: str, max_tokens: int = 512) -> tuple[str, str]:
    """Returns (answer, source) where source is baseten|unavailable|error."""
    api_key, base_url, model = _cfg()
    if not api_key:
        return "", "unavailable"

    try:
        client = get_client()
        resp = await client.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.1,
                "max_tokens": max_tokens,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        answer = (
            data.get("choices", [{}])[0].get("message", {}).get("content")
            or data.get("output")
            or ""
        ).strip()
        return answer, "baseten"
    except httpx.HTTPStatusError as e:
        return f"AI service error ({e.response.status_code}). Using rule-based summary.", "error"
    except (httpx.TimeoutException, httpx.TransportError):
        return "", "error"


def _summarize_flags(flags: list[dict]) -> str:
    if not flags:
        return "None identified."
    from collections import Counter
    counts = Counter(f.get("flag_type", "unknown") for f in flags)
    parts = []
    for flag_type, n in counts.most_common():
        label = flag_type.replace("_", " ")
        if flag_type == "envive_narrative_only":
            label = "Envive narrative notes (wound details only in free text, not structured fields)"
        parts.append(f"{label}" + (f" ({n} note{'s' if n != 1 else ''})" if n > 1 else ""))
    return "; ".join(parts)


def _next_step(patient: dict, flags: list[dict]) -> str:
    decision = patient.get("routing_decision")
    if decision == "reject" and not patient.get("has_active_medicare_b"):
        return "Route to the patient's active payer (e.g. Medicaid), not Medicare Part B."
    if decision == "reject":
        return "Resolve the rejection reason before billing."
    if any(f.get("flag_type") == "envive_narrative_only" for f in flags):
        return "Ask clinical staff to complete structured wound fields or extract details from the Envive narrative."
    if decision == "flag_for_review":
        return "Have a biller or clinician review flagged items before submitting."
    return "Ready to bill — verify measurements and payer one more time."


def _parse_summary_sections(text: str) -> dict[str, str] | None:
    labels = {
        "DECISION": "Billing decision",
        "MEDICARE_B": "Medicare Part B",
        "MEDICARE B": "Medicare Part B",
        "DATA_GAPS": "Data gaps",
        "DATA GAPS": "Data gaps",
        "NEXT_STEP": "What to do next",
        "NEXT STEP": "What to do next",
        "RECOMMENDED NEXT ACTION": "What to do next",
    }
    sections: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        upper = stripped.upper()
        for key, title in labels.items():
            prefix = key + ":"
            if upper.startswith(prefix):
                sections[title] = stripped.split(":", 1)[1].strip().strip("*")
                break
    return sections if len(sections) >= 2 else None


def _structured_fallback(patient: dict, flags: list[dict], model_insight: dict | None) -> dict[str, str]:
    decision = patient.get("routing_decision", "unknown").replace("_", " ").title()
    reason = patient.get("routing_reason", "")
    mcb = (
        "Active — patient has Medicare Part B on file."
        if patient.get("has_active_medicare_b")
        else "Not active — cannot bill this claim to Medicare Part B."
    )
    gaps = _summarize_flags(flags)
    if patient.get("unknown_risk_tier") == "red":
        gaps += f" High data-quality risk score ({patient.get('unknown_risk_score', 0)})."
    ml = ""
    if model_insight and not model_insight.get("rule_agrees"):
        ml = f" ML model suggests {model_insight.get('model_suggestion')} (differs from rules)."
    return {
        "Billing decision": f"{decision} — {reason}{ml}",
        "Medicare Part B": mcb,
        "Data gaps": gaps,
        "What to do next": _next_step(patient, flags),
    }


def _fallback_summary(patient: dict, flags: list[dict], model_insight: dict | None) -> str:
    sections = _structured_fallback(patient, flags, model_insight)
    return " ".join(f"{k}: {v}" for k, v in sections.items())


async def patient_summary(patient: dict, flags: list[dict], diagnoses: list[dict], coverage: list[dict], model_insight: dict | None) -> dict[str, str]:
    cache_key = f"{patient.get('patient_id')}:{patient.get('computed_at')}"
    cached = _summary_cache.get(cache_key)
    if cached and time.time() - cached[0] < CACHE_TTL:
        sections = _parse_summary_sections(cached[1]) or _structured_fallback(patient, flags, model_insight)
        return {"summary": cached[1], "sections": sections, "source": "cache"}

    context = json.dumps(
        {
            "patient": patient,
            "unknown_flags": flags[:10],
            "active_diagnoses": [d for d in diagnoses if (d.get("clinical_status") or "").lower() == "active"][:8],
            "coverage": coverage[:5],
            "model_insight": model_insight,
        },
        default=str,
    )[:6000]

    prompt = (
        "Write a biller-friendly summary using EXACTLY this format (4 lines, plain text, no markdown):\n"
        "DECISION: <billing decision and why in one sentence>\n"
        "MEDICARE_B: <active or not, and what payer is on file>\n"
        "DATA_GAPS: <missing wound fields or quality issues; say None if clear>\n"
        "NEXT_STEP: <one concrete action for the biller>\n\n"
        f"Data:\n{context}"
    )
    answer, source = await complete(prompt, max_tokens=400)
    sections = _parse_summary_sections(answer) if answer else None
    if not answer:
        sections = _structured_fallback(patient, flags, model_insight)
        answer = " ".join(f"{k}: {v}" for k, v in sections.items())
        source = "rules_fallback"
    elif not sections:
        sections = _structured_fallback(patient, flags, model_insight)

    _summary_cache[cache_key] = (time.time(), answer)
    if len(_summary_cache) > 500:
        oldest = sorted(_summary_cache.items(), key=lambda x: x[1][0])[:100]
        for k, _ in oldest:
            _summary_cache.pop(k, None)

    return {"summary": answer, "sections": sections, "source": source}


async def chat_answer(question: str, context: dict) -> dict[str, str]:
    ctx = json.dumps(context, default=str)[:8000]
    prompt = (
        "Answer the biller's question using ONLY the context below.\n"
        "Use EXACTLY this format (3 lines, plain text, no markdown asterisks):\n"
        "ANSWER: <direct answer in one sentence>\n"
        "DETAILS: <key supporting facts from the data>\n"
        "NEXT_STEP: <one concrete action for the biller>\n\n"
        f"Context:\n{ctx}\n\nQuestion: {question}"
    )
    answer, source = await complete(prompt, max_tokens=768)
    if not answer:
        p = context.get("patient") or {}
        answer = (
            f"{p.get('patient_id', 'Patient')}: {p.get('routing_decision', '—')} — "
            f"{p.get('routing_reason', 'No additional context.')}"
        )
        source = "rules_fallback"
    return {"answer": answer, "source": source}
