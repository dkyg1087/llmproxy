import math
import aiosqlite
from typing import Optional, Dict, Any
from src.config import GAP_BIAS, DURATION_BIAS, USAGE_BIAS, logger
from src.router.quota import get_model_usage, get_avg_model_latency


async def select_model_and_platform(
    conn: aiosqlite.Connection,
    requested_model: str = "auto",
    difficulty: Optional[int] = None,
    estimated_prompt_tokens: int = 0
) -> Optional[Dict[str, Any]]:
    """
    Ranks candidate models using Bandits with Knapsacks (BwK) optimization
    and selects the utility-maximizing healthy model route. Validates remaining capacity
    against estimated prompt token requirements.
    """
    logger.info(f"[ROUTER SOLVER] Evaluating candidate routes for model='{requested_model}', difficulty={difficulty}, prompt_tokens={estimated_prompt_tokens}...")

    if requested_model != "auto":
        # ── Pinned Direct Route Path ──────────────────────────────────────────────
        cursor = await conn.execute(
            """
            SELECT 
                m.model_id, 
                m.platform, 
                a.id AS key_id,
                m.rpm_limit, 
                m.rpd_limit, 
                m.tpm_limit, 
                m.tpd_limit, 
                m.shared_quota_group,
                m.base_score
            FROM models m
            JOIN api_keys a ON m.platform = a.platform
            WHERE m.model_id = ? 
                AND m.enabled = 1
                AND a.enabled = 1
                AND a.status != 'error'
                AND m.model_id NOT IN (
                    SELECT model_id 
                    FROM rate_limit_cooldowns 
                    WHERE platform = m.platform AND expires_at > datetime('now')
                )
                AND m.model_id NOT IN (
                    SELECT model_id 
                    FROM key_capabilities 
                    WHERE key_id = a.id AND is_capable = 0
                )
            """,
            (requested_model,)
        )

        row = await cursor.fetchone()

        if not row:
            logger.warning(f"[ROUTER REJECTED] Pinned model '{requested_model}' unavailable, quarantined, or key disabled.")
            return None

        model_id, platform, key_id, rpm_limit, rpd_limit, tpm_limit, tpd_limit, shared_quota_group, base_score = row
        usages = await get_model_usage(conn, model_id, shared_quota_group)
        rpm_u, rpd_u, tpm_u, tpd_u = usages

        post_tpm = tpm_u + estimated_prompt_tokens
        post_tpd = tpd_u + estimated_prompt_tokens

        is_exceeded = (
            (tpm_limit is not None and post_tpm > tpm_limit) or
            (tpd_limit is not None and post_tpd > tpd_limit) or
            (rpm_limit is not None and (rpm_u + 1) > rpm_limit) or
            (rpd_limit is not None and (rpd_u + 1) > rpd_limit)
        )

        if is_exceeded:
            logger.warning(f"[ROUTER REJECTED] Pinned model '{requested_model}' exceeds remaining quota capacity for prompt ({estimated_prompt_tokens} tokens).")
            return None

        logger.info(f"[ROUTER DIRECT] Selected pinned route: platform='{platform}', model='{model_id}', key_id={key_id}")
        return {"model_id": model_id, "platform": platform, "key_id": key_id}

    else:
        # ── Dynamic BwK Wildcard Route Path ─────────────────────────────────────
        cursor = await conn.execute(
            """
            SELECT 
                m.model_id, 
                m.platform, 
                a.id AS key_id,
                m.rpm_limit, 
                m.rpd_limit, 
                m.tpm_limit, 
                m.tpd_limit, 
                m.shared_quota_group,
                m.base_score
            FROM models m
            JOIN api_keys a ON m.platform = a.platform
            WHERE m.enabled = 1
                AND a.enabled = 1
                AND a.status != 'error'
                AND m.model_id NOT IN (
                    SELECT model_id 
                    FROM rate_limit_cooldowns 
                    WHERE platform = m.platform AND expires_at > datetime('now')
                )
                AND m.model_id NOT IN (
                    SELECT model_id 
                    FROM key_capabilities 
                    WHERE key_id = a.id AND is_capable = 0
                )
            """
        )

        rows = await cursor.fetchall()
        if not rows:
            logger.warning("[ROUTER REJECTED] No enabled, non-quarantined candidate models found in database.")
            return None

        available_models = []
        for row in rows:
            model_id, platform, key_id, rpm_limit, rpd_limit, tpm_limit, tpd_limit, shared_quota_group, base_score = row
            limits = (rpm_limit, rpd_limit, tpm_limit, tpd_limit)
            usages = await get_model_usage(conn, model_id, shared_quota_group)
            rpm_u, rpd_u, tpm_u, tpd_u = usages

            post_tpm = tpm_u + estimated_prompt_tokens
            post_tpd = tpd_u + estimated_prompt_tokens

            is_exceeded = (
                (tpm_limit is not None and post_tpm > tpm_limit) or
                (tpd_limit is not None and post_tpd > tpd_limit) or
                (rpm_limit is not None and (rpm_u + 1) > rpm_limit) or
                (rpd_limit is not None and (rpd_u + 1) > rpd_limit)
            )

            if is_exceeded:
                logger.debug(f"[ROUTER EVAL] Model '{platform}/{model_id}' excluded (Capacity exceeded with prompt tokens={estimated_prompt_tokens}).")
                score = -float("inf")
            else:
                utilization = [(usage / limit) if limit else 0.0 for usage, limit in zip((rpm_u + 1, rpd_u + 1, post_tpm, post_tpd), limits)]
                gap_penalty = (difficulty - base_score) if (difficulty is not None and base_score < difficulty) else 0.0
                duration_penalty = await get_avg_model_latency(conn, model_id, shared_quota_group)
                usage_penalty = sum(math.exp(5.0 * (u - 0.8)) for u, limit in zip(utilization, limits) if limit is not None)

                score = base_score - (gap_penalty * GAP_BIAS) - (duration_penalty * DURATION_BIAS) - (usage_penalty * USAGE_BIAS)
                logger.debug(f"[ROUTER EVAL] Candidate '{platform}/{model_id}': Base={base_score}, GapPen={gap_penalty:.1f}, DurPen={duration_penalty:.2f}s, UsagePen={usage_penalty:.2f} -> UtilityScore={score:.2f}")

            available_models.append((score, model_id, platform, key_id, base_score))

        # Filter primary models (base_score >= 1) vs emergency fallbacks (base_score == 0)
        primary_candidates = [m for m in available_models if m[4] > 0 and m[0] > -float("inf")]

        if primary_candidates:
            candidate_pool = primary_candidates
        else:
            candidate_pool = [m for m in available_models if m[0] > -float("inf")]

        if not candidate_pool:
            logger.warning("[ROUTER REJECTED] All available candidate models are saturated or quarantined (-inf utility).")
            return None

        candidate_pool.sort(reverse=True, key=lambda x: x[0])
        winning_score, winning_model, winning_platform, winning_key_id, winning_base_score = candidate_pool[0]

        if winning_base_score == 0:
            logger.info(f"[ROUTER EMERGENCY FALLBACK] Primary models exhausted. Selected score 0 fallback route: platform='{winning_platform}', model='{winning_model}'")
        else:
            logger.info(f"[ROUTER WINNER] Selected route: platform='{winning_platform}', model='{winning_model}', key_id={winning_key_id} (Score: {winning_score:.2f})")

        return {"model_id": winning_model, "platform": winning_platform, "key_id": winning_key_id}
