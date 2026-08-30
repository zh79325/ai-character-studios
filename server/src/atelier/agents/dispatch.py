"""模型调用的收口：限流退避、换候选、记账。

会话轮次与单次调用（评审、生 prompt）在「怎么发出去」这件事上完全一致：限流退一步再试、
其余失败换个候选、每次成败都回记路由层。两边各写一份的话，改重试策略时必然只改到一边，另
一边悄悄按老规矩跑。

区别只在**换候选之后要不要记住这次换**：会话有粘性绑定，换掉了得连原因一起落进会话行；单
次调用无状态，换了就换了。这一点用 `reselect` 回调交给调用方，本模块不认识会话。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence

import structlog
from sqlalchemy.orm import Session

from atelier.providers import router, text_chat
from atelier.providers.base import CallOutcome, Decision, ProviderError, RetryableError
from atelier.settings import get_settings

_log = structlog.get_logger(__name__)

MAX_CANDIDATE_SWITCHES = 3
"""一次调用里最多换几个候选。都不通就报错，不无限换下去把每个 provider 都试到熔断。"""

RETRY_BACKOFF_SECONDS = 1.5

ChatFn = Callable[..., text_chat.ChatReply]
"""对话调用口。测试与离线冒烟用假实现替换，签名跟 `text_chat.complete` 一致。"""

Reselect = Callable[[ProviderError], Decision]
"""换候选：拿到上一次的失败原因，返回新的 Decision。返回 None 的余地不留——选不出来
的时候路由层自己会抛 `NoCandidateError`，在这里用 None 表示「没有了」会把两种情况混成
一种。"""


def outcome_of(reply: text_chat.ChatReply) -> CallOutcome:
    """把回答里的用量翻译成路由层记账用的事实。

    没拿到 usage 就不写 used_delta：估算值混进额度台账，后面对不上账时根本分不清是估歪了
    还是真的用超了。
    """
    return CallOutcome(
        limit_kind="tokens",
        used_delta=reply.total_tokens,
        remaining=reply.remaining,
        latency_ms=reply.latency_ms,
    )


def call(
    runtime: Session,
    agent_code: str,
    decision: Decision,
    payload: Sequence[dict[str, str]],
    chat: ChatFn,
    *,
    project_code: str | None = None,
    task_id: str | None = None,
    on_delta: Callable[[str], None] | None = None,
    reselect: Reselect | None = None,
) -> text_chat.ChatReply:
    """发出去并记账。限流退避重试，其余失败换候选。

    成功记 `report_success`、彻底失败记 `report_failure`、重试途中记 `note_retryable`——
    额度与熔断都靠这三笔账，漏一笔下一次选候选就会挑到刚刚打死的那个。
    """
    settings = get_settings()
    retries = settings.provider_retry_attempts
    current = decision
    last_error: ProviderError | None = None
    body = [dict(one) for one in payload]

    for switch in range(MAX_CANDIDATE_SWITCHES):
        for attempt in range(1, retries + 2):
            try:
                reply = chat(current.candidate, body, on_delta=on_delta)
            except RetryableError as exc:
                last_error = exc
                router.note_retryable(runtime, agent_code, current, exc, attempt)
                if attempt <= retries:
                    time.sleep(RETRY_BACKOFF_SECONDS * attempt)
                    continue
                break
            except ProviderError as exc:
                last_error = exc
                break
            else:
                router.report_success(
                    runtime,
                    agent_code,
                    current,
                    outcome_of(reply),
                    task_id=task_id,
                    project_code=project_code,
                )
                return reply

        assert last_error is not None  # noqa: S101 - 走到这儿必然有异常
        router.report_failure(
            runtime, agent_code, current, last_error, task_id=task_id, project_code=project_code
        )
        if switch == MAX_CANDIDATE_SWITCHES - 1 or reselect is None:
            break
        # 记完账再重选：额度已标满、熔断已打开，select 自然会挑到别人身上
        current = reselect(last_error)

    raise last_error if last_error is not None else ProviderError("没有可用候选")


def select(
    runtime: Session,
    agent_code: str,
    *,
    project_code: str | None = None,
    task_id: str | None = None,
) -> Decision:
    """无粘性地选一个候选，给单次调用用。

    单次调用不传 binding：它没有下一轮，绑在谁身上都没有复用价值，反倒会把会话的绑定
    字段占掉。
    """
    return router.select_candidate(
        runtime,
        agent_code,
        limit_kind="tokens",
        task_id=task_id,
        project_code=project_code,
    )


def run(
    runtime: Session,
    agent_code: str,
    payload: Sequence[dict[str, str]],
    chat: ChatFn,
    *,
    project_code: str | None = None,
    task_id: str | None = None,
) -> text_chat.ChatReply:
    """一发一收：选候选、调、失败换人再调。"""
    decision = select(runtime, agent_code, project_code=project_code, task_id=task_id)
    reply = call(
        runtime,
        agent_code,
        decision,
        payload,
        chat,
        project_code=project_code,
        task_id=task_id,
        reselect=lambda _: select(runtime, agent_code, project_code=project_code, task_id=task_id),
    )
    _log.info("oneshot_call", agent=agent_code, task=task_id, label=decision.candidate.label)
    return reply
