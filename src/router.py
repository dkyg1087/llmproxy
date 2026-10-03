import math
import random
from typing import Optional, Dict, Any, List, Set, Tuple
from src.config import GAP_BIAS, DURATION_BIAS, USAGE_BIAS, logger
from src.db import (
    get_candidate_models,
    get_model_usage,
    get_model_ms_per_token,
)


def _softmax_sample_top2(
    candidate1: Dict[str, Any],
    score1: float,
    candidate2: Dict[str, Any],
    score2: float,
    temperature: float = 0.7
) -> Dict[str, Any]:
    """Applies softmax temperature exploration between the top 2 candidates."""
    diff = (score2 - score1) / temperature
    p2 = 1.0 / (1.0 + math.exp(-diff))  # Sigmoid of diff
    chosen = candidate2 if random.random() < p2 else candidate1
    logger.info(
        f"[ROUTER EXPLORE] Scores close ({score1:.2f} vs {score2:.2f}, delta={abs(score1-score2):.2f}). "
        f"Sampled {chosen['platform']}/{chosen['model_id']} (p={p2 if chosen == candidate2 else 1.0 - p2:.2f})"
    )
    return chosen


async def select_model_and_platform(
    requested_model: str = "auto",
    difficulty: Optional[int] = None,
    estimated_prompt_tokens: int = 0,
    max_base_score: Optional[int] = None,
    gap_bias: float = GAP_BIAS,
    duration_bias: float = DURATION_BIAS,
    usage_bias: float = USAGE_BIAS,
    exclude_models: Optional[Set[Tuple[str, str]]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Ranks candidate models using Bandits with Knapsacks (BwK) optimization
    and selects the utility-maximizing healthy model route.
    """
    logger.info(
        f"[ROUTER] Selecting route for model='{requested_model}', difficulty={difficulty}, "
        f"prompt_tokens={estimated_prompt_tokens}, exclude={len(exclude_models or set())}"
    )

    is_pinned = requested_model != "auto"
    candidates = await get_candidate_models(requested_model if is_pinned else None)

    if not candidates:
        logger.warning(f"[ROUTER REJECTED] No enabled or healthy candidates found for '{requested_model}'.")
        return None

    target_diff = difficulty if difficulty is not None else 3
    surviving_candidates: List[Tuple[Dict[str, Any], float]] = []

    for m in candidates:
        platform = m["platform"]
        model_id = m["model_id"]

        # 1. In-memory retry exclusion
        if exclude_models and (platform, model_id) in exclude_models:
            continue

        # 2. Maximum base score filter (e.g. for lightweight triage)
        if not is_pinned and max_base_score is not None and m["base_score"] > max_base_score:
            continue

        # 3. Context window check (nullable: only skip if explicitly populated)
        ctx = m.get("context_window")
        if ctx is not None and estimated_prompt_tokens > ctx:
            logger.debug(f"[ROUTER SKIP] {platform}/{model_id} context ({ctx}) < prompt ({estimated_prompt_tokens})")
            continue

        # 4. Quota check (Hard constraints)
        rpm_u, rpd_u, tpm_u, tpd_u = await get_model_usage(model_id, m.get("shared_quota_group"))
        post_rpm = rpm_u + 1
        post_rpd = rpd_u + 1
        post_tpm = tpm_u + estimated_prompt_tokens
        post_tpd = tpd_u + estimated_prompt_tokens

        rpm_limit = m.get("rpm_limit")
        rpd_limit = m.get("rpd_limit")
        tpm_limit = m.get("tpm_limit")
        tpd_limit = m.get("tpd_limit")

        if (
            (rpm_limit is not None and post_rpm > rpm_limit) or
            (rpd_limit is not None and post_rpd > rpd_limit) or
            (tpm_limit is not None and post_tpm > tpm_limit) or
            (tpd_limit is not None and post_tpd > tpd_limit)
        ):
            logger.warning(f"[ROUTER REJECTED] {platform}/{model_id} quota exceeded.")
            continue

        # Pinned direct route: return immediately once capacity verified
        if is_pinned:
            logger.info(f"[ROUTER DIRECT] Selected pinned route: {platform}/{model_id} (key_id={m['key_id']})")
            return {"model_id": model_id, "platform": platform, "key_id": m["key_id"]}

        # ── 5. BwK Utility Scoring ───────────────────────────────────────────
        # A. Capability Score (Weighted absolute distance)
        base = m.get("base_score", 3)
        gap = abs(target_diff - base)
        gap_penalty = (gap * 1.5) if base < target_diff else (gap * 0.8)
        capability_score = max(0.0, 5.0 - (gap_penalty * gap_bias))

        # B. Normalized Latency Score (ms per output token; optimistic 15.0ms for untried models)
        ms_per_tok = await get_model_ms_per_token(model_id, platform)
        if ms_per_tok <= 0.0:
            ms_per_tok = 15.0
        latency_score = max(0.0, 5.0 - ((ms_per_tok / 10.0) * duration_bias))

        # C. Usage Barrier (Exponential penalty for quotas above 80%)
        ratios = [
            (post_rpm / rpm_limit) if rpm_limit and rpm_limit > 0 else 0.0,
            (post_rpd / rpd_limit) if rpd_limit and rpd_limit > 0 else 0.0,
            (post_tpm / tpm_limit) if tpm_limit and tpm_limit > 0 else 0.0,
            (post_tpd / tpd_limit) if tpd_limit and tpd_limit > 0 else 0.0,
        ]
        max_ratio = max(ratios) if ratios else 0.0
        usage_barrier = math.exp(5.0 * (max_ratio - 0.8)) * usage_bias if max_ratio > 0.8 else 0.0

        utility = capability_score + latency_score - usage_barrier
        logger.debug(
            f"[ROUTER SCORE] {platform}/{model_id}: U={utility:.2f} "
            f"(cap={capability_score:.2f}, lat={latency_score:.2f}, bar={usage_barrier:.2f}, ms/tok={ms_per_tok:.1f})"
        )
        surviving_candidates.append((m, utility))

    if not surviving_candidates:
        logger.warning(f"[ROUTER REJECTED] No healthy routes survived filtering for '{requested_model}'.")
        return None

    # Sort descending by utility
    surviving_candidates.sort(key=lambda x: x[1], reverse=True)

    # ── 6. Exploration / Selection ───────────────────────────────────────────
    if len(surviving_candidates) >= 2:
        top1, score1 = surviving_candidates[0]
        top2, score2 = surviving_candidates[1]
        if (score1 - score2) <= 0.5:
            chosen = _softmax_sample_top2(top1, score1, top2, score2)
            return {"model_id": chosen["model_id"], "platform": chosen["platform"], "key_id": chosen["key_id"]}

    winner = surviving_candidates[0][0]
    logger.info(f"[ROUTER SELECTED] Winner: {winner['platform']}/{winner['model_id']} (key_id={winner['key_id']})")
    return {"model_id": winner["model_id"], "platform": winner["platform"], "key_id": winner["key_id"]}


__all__ = [
    "select_model_and_platform",
    "get_model_usage",
    "get_model_ms_per_token",
]
