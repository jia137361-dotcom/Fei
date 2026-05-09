from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

from app.automation.browser_factory import BrowserSession, open_adspower_profile
from app.automation.pinterest_flow import PinterestFlow, PinDraft, PublishResult
from app.evomap.prompt_evolve import PromptContext, PromptEvolver
from app.models.pin_performance import PinPerformance
from app.models.social_account import SocialAccount
from app.tools.adspower_api import AdsPowerClient


@dataclass(frozen=True)
class AccountPublishWorkflowInput:
    account_id: str
    board_name: str
    image_path: Path
    prompt_context: PromptContext
    title: str
    description: str
    destination_url: str | None = None
    campaign_id: str | None = None
    visual_prompt: str | None = None


async def run_pin_publish_with_adspower(
    *,
    db: Session,
    workflow_input: AccountPublishWorkflowInput,
    adspower_client: AdsPowerClient | None = None,
    content_batch_id: str | None = None,
    variant_angle: str | None = None,
    content_hash: str | None = None,
    title_hash: str | None = None,
    description_hash: str | None = None,
) -> PublishResult:
    account = db.scalar(
        select(SocialAccount).where(SocialAccount.account_id == workflow_input.account_id)
    )
    if account is None:
        raise ValueError(f"SocialAccount not found: {workflow_input.account_id}")
    if not account.adspower_profile_id:
        raise ValueError(f"Account has no AdsPower profile bound: {workflow_input.account_id}")

    from app.safety.proxy_check import verify_us_ip

    session: BrowserSession | None = None
    try:
        session = await open_adspower_profile(
            account.adspower_profile_id,
            adspower_client=adspower_client,
        )
        await verify_us_ip(session.page)
        pinterest = PinterestFlow(session.page)
        evolver = PromptEvolver(db=db)
        content_prompt = evolver.build_content_prompt(workflow_input.prompt_context)
        result = await pinterest.publish_pin(_draft_from_input(workflow_input))
        try:
            record = record_publish(
                db=db,
                evolver=evolver,
                workflow_input=workflow_input,
                result=result,
                content_prompt=content_prompt,
                content_batch_id=content_batch_id,
                variant_angle=variant_angle,
                content_hash=content_hash,
                title_hash=title_hash,
                description_hash=description_hash,
            )
            pin_perf_id: int | None = record.id
        except Exception:
            logger.exception("PinPerformance recording failed — publish is still successful")
            pin_perf_id = None
        return PublishResult(
            success=result.success,
            pin_url=result.pin_url,
            message=result.message,
            debug_artifact_dir=result.debug_artifact_dir,
            pin_performance_id=pin_perf_id,
            publish_evidence=result.publish_evidence,
        )
    finally:
        if session is not None:
            await session.close()


def _draft_from_input(workflow_input: PublishWorkflowInput | AccountPublishWorkflowInput) -> PinDraft:
    return PinDraft(
        title=workflow_input.title,
        description=workflow_input.description,
        board_name=workflow_input.board_name,
        image_path=workflow_input.image_path,
        destination_url=workflow_input.destination_url,
    )


def record_publish(
    *,
    db: Session,
    evolver: PromptEvolver,
    workflow_input: PublishWorkflowInput | AccountPublishWorkflowInput,
    result: PublishResult,
    content_prompt: str,
    content_batch_id: str | None = None,
    variant_angle: str | None = None,
    content_hash: str | None = None,
    title_hash: str | None = None,
    description_hash: str | None = None,
) -> PinPerformance:
    pin_id = _extract_pin_id_from_url(result.pin_url) if result.pin_url else None
    signals = evolver.get_keyword_signals(
        niche=workflow_input.prompt_context.niche,
        product_type=workflow_input.prompt_context.product_type,
    )
    record = PinPerformance(
        account_id=workflow_input.account_id,
        campaign_id=workflow_input.campaign_id,
        pinterest_pin_id=pin_id,
        board_id=workflow_input.board_name,
        product_type=workflow_input.prompt_context.product_type,
        niche=workflow_input.prompt_context.niche,
        title=workflow_input.title,
        description=workflow_input.description,
        destination_url=workflow_input.destination_url,
        image_url=str(workflow_input.image_path),
        content_prompt=content_prompt,
        visual_prompt=workflow_input.visual_prompt,
        keywords=[signal.keyword for signal in signals],
        strategy_snapshot={
            "source": "evomap_prompt_evolve",
            "keyword_signals": [signal.model_dump() for signal in signals],
        },
        published_at=datetime.now(UTC) if result.success else None,
        content_batch_id=content_batch_id,
        variant_angle=variant_angle,
        content_hash=content_hash,
        title_hash=title_hash,
        description_hash=description_hash,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def _extract_pin_id_from_url(pin_url: str) -> str | None:
    """Extract the numeric Pin ID from a Pinterest URL.

    >>> _extract_pin_id_from_url('https://www.pinterest.com/pin/123456789/')
    '123456789'
    """
    import re

    match = re.search(r"/pin/(\d+)", pin_url)
    return match.group(1) if match else None
