from __future__ import annotations

from collections.abc import Callable, Iterable

from utils.chat_agent_store import PROMPT_SCOPES

AvailabilityIssue = dict


def compute_availability(
    agent: dict,
    *,
    knowledge_bases: Iterable[dict],
    prompt_lookup: Callable[[str, str], tuple[bool, bool]] | None,
    is_model_available: Callable[[str, str], bool],
    max_query_knowledge_bases: int,
) -> dict:
    """Compute whether an agent is runnable and why.

    Returns ``{"available": bool, "issues": [{code, field, message}]}``.

    ``prompt_lookup(scope, prompt_id) -> (found, active)`` returns whether the
    referenced prompt exists and whether it is active. Pass ``None`` when no
    prompt store is available; a configured prompt_ref then reports as missing.
    """
    issues: list[AvailabilityIssue] = []

    provider_id = str(agent.get("provider_id") or "")
    model_id = str(agent.get("model_id") or "")
    if not is_model_available(provider_id, model_id):
        issues.append(
            {
                "code": "model_unavailable",
                "field": "model_id",
                "message": "Il modello selezionato non è più disponibile",
            }
        )

    knowledge_base_ids = list(agent.get("knowledge_base_ids") or [])
    max_query_knowledge_bases = int(max_query_knowledge_bases)
    if len(knowledge_base_ids) > max_query_knowledge_bases:
        issues.append(
            {
                "code": "knowledge_base_limit_exceeded",
                "field": "knowledge_base_ids",
                "message": (
                    f"Superato il limite di {max_query_knowledge_bases} "
                    "knowledge base interrogabili"
                ),
            }
        )

    by_id = {
        str(record.get("id") or ""): record for record in knowledge_bases
    }
    for knowledge_base_id in knowledge_base_ids:
        record = by_id.get(knowledge_base_id)
        if record is None:
            issues.append(
                {
                    "code": "knowledge_base_missing",
                    "field": "knowledge_base_ids",
                    "message": "Una knowledge base selezionata non esiste più",
                }
            )
            continue
        if str(record.get("status") or "") != "active":
            issues.append(
                {
                    "code": "knowledge_base_inactive",
                    "field": "knowledge_base_ids",
                    "message": "Una knowledge base selezionata non è attiva",
                }
            )

    prompt_ref = agent.get("prompt_ref") or {}
    if isinstance(prompt_ref, dict) and prompt_ref.get("id"):
        scope = str(prompt_ref.get("scope") or "").strip()
        prompt_id = str(prompt_ref.get("id") or "").strip()
        if scope not in PROMPT_SCOPES or not prompt_id:
            issues.append(
                {
                    "code": "prompt_missing",
                    "field": "prompt_ref",
                    "message": "Il prompt selezionato non è valido",
                }
            )
        elif prompt_lookup is None:
            issues.append(
                {
                    "code": "prompt_missing",
                    "field": "prompt_ref",
                    "message": "Impossibile verificare il prompt selezionato",
                }
            )
        else:
            found, active = prompt_lookup(scope, prompt_id)
            if not found:
                issues.append(
                    {
                        "code": "prompt_missing",
                        "field": "prompt_ref",
                        "message": "Il prompt selezionato non esiste più",
                    }
                )
            elif not active:
                issues.append(
                    {
                        "code": "prompt_inactive",
                        "field": "prompt_ref",
                        "message": "Il prompt selezionato non è attivo",
                    }
                )

    return {"available": not issues, "issues": issues}


def with_availability(
    agent: dict,
    *,
    knowledge_bases: Iterable[dict],
    prompt_lookup: Callable[[str, str], tuple[bool, bool]] | None,
    is_model_available: Callable[[str, str], bool],
    max_query_knowledge_bases: int,
) -> dict:
    """Return a shallow copy of ``agent`` with ``available`` and ``issues``."""
    availability = compute_availability(
        agent,
        knowledge_bases=knowledge_bases,
        prompt_lookup=prompt_lookup,
        is_model_available=is_model_available,
        max_query_knowledge_bases=max_query_knowledge_bases,
    )
    return {**agent, **availability}
