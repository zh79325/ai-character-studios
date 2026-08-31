"""模型调用的收口：限流退避、换候选、记账。

会话轮次与单次调用（评审、生 prompt）在「怎么发出去」这件事上完全一致：限流退一步再试、
其余失败换个候选、每次成败都回记路由层。两边各写一份的话，改重试策略时必然只改到一边，另
一边悄悄按老规矩跑。

区别只在**换候选之后要不要记住这次换**：会话有粘性绑定，换掉了得连原因一起落进会话行；单
次调用无状态，换了就换了。这一点用 `reselect` 回调交给调用方，本模块不认识会话。

生图与文本共用同一个循环，只有两处不同：计量口径（tokens 事后读 usage / calls 选中即预扣）
与「怎么把请求发出去」。所以循环抽成 `_drive`，两条业务入口各自绑好自己的驱动与口径。
预扣型口径失败时不退额：远程用量服务只认增量，没有回滚这一说，多扣一次总比少扣一次让人以
为还有余量好。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy.orm import Session

from atelier.providers import image_gen, router, text_chat
from atelier.providers.base import (
    CallOutcome,
    Candidate,
    Decision,
    EmptyReply,
    ProviderError,
    RetryableError,
)
from atelier.settings import get_settings

_log = structlog.get_logger(__name__)

MAX_CANDIDATE_SWITCHES = 3
"""一次调用里最多换几个候选。都不通就报错，不无限换下去把每个 provider 都试到熔断。"""

RETRY_BACKOFF_SECONDS = 1.5

IMAGE_KINDS = ("images",)
"""生图除了记接口次数，还按出图张数再卡一道：同样一次调用，出 1 张跟出 4 张对画
额包的消耗差 4 倍，只看 `calls` 卡不住。模型上没配 images 限额就自然不生效。"""

ChatFn = Callable[..., text_chat.ChatReply]
"""对话调用口。测试与离线冒烟用假实现替换，签名跟 `text_chat.complete` 一致。"""

ImageFn = Callable[..., image_gen.ImageReply]
"""生图调用口，签名跟 `image_gen.generate` 一致。"""

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


def outcome_of_image(reply: image_gen.ImageReply) -> CallOutcome:
    """生图的记账事实。

    不写 used_delta：这一张在选候选时就已经按单价预扣过了，这里再报一次等于扣两遍。
    """
    return CallOutcome(
        limit_kind="calls",
        remaining=reply.remaining,
        latency_ms=reply.latency_ms,
    )


def _drive[Reply](
    runtime: Session,
    agent_code: str,
    decision: Decision,
    invoke: Callable[[Candidate], Reply],
    outcome: Callable[[Reply], CallOutcome],
    *,
    limit_kind: str,
    project_code: str | None,
    task_id: str | None,
    reselect: Reselect | None,
) -> Reply:
    """发出去并记账。限流退避重试，其余失败换候选。

    成功记 `report_success`、彻底失败记 `report_failure`、重试途中记 `note_retryable`——
    额度与熔断都靠这三笔账，漏一笔下一次选候选就会挑到刚刚打死的那个。
    """
    settings = get_settings()
    retries = settings.provider_retry_attempts
    current = decision
    last_error: ProviderError | None = None

    for switch in range(MAX_CANDIDATE_SWITCHES):
        for attempt in range(1, retries + 2):
            try:
                reply = invoke(current.candidate)
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
                    outcome(reply),
                    task_id=task_id,
                    project_code=project_code,
                )
                return reply

        assert last_error is not None  # noqa: S101 - 走到这儿必然有异常
        router.report_failure(
            runtime,
            agent_code,
            current,
            last_error,
            limit_kind=limit_kind,
            task_id=task_id,
            project_code=project_code,
        )
        if switch == MAX_CANDIDATE_SWITCHES - 1 or reselect is None:
            break
        # 记完账再重选：额度已标满、熔断已打开，select 自然会挑到别人身上
        previous = current.candidate.provider_model_id
        current = reselect(last_error)
        if current.candidate.provider_model_id == previous:
            # 没人可换（只配了一个模型，而这类失败又不熔断）。同一份请求原封不动再发一遍，大
            # 概率同一个下场，而 prompt 的钱是要再交一次的——直接把原因报给用户。
            break

    raise last_error if last_error is not None else ProviderError("没有可用候选")


def _reject_empty(candidate: Candidate, reply: text_chat.ChatReply) -> text_chat.ChatReply:
    """一字未回的应答当失败算。

    不报 `RetryableError`：同一个模型刚才没说出话，退避几秒再原封不动发一遍大概率还是不说话，
    有别的候选就直接换人。也不报普通 `ProviderError`：那会把这个模型熔断几分钟，而它并没坏。

    错误文案带上 finish_reason 与用量：空回答最常见的两个因为——输出预算被推理链吃完（length +
    有推理字数）与内容安全拦截（既没 usage 也没 finish_reason）——靠这两个数字就能分开。
    """
    if reply.content.strip():
        return reply
    facts = [f"finish_reason={reply.finish_reason or '未给'}"]
    if reply.completion_tokens:
        facts.append(f"completion={reply.completion_tokens}")
    if reply.reasoning.strip():
        facts.append(f"推理 {len(reply.reasoning.strip())} 字")
    raise EmptyReply(f"{candidate.label} 返回了空回答（{', '.join(facts)}）")


def call(
    runtime: Session,
    agent_code: str,
    decision: Decision,
    payload: Sequence[Mapping[str, Any]],
    chat: ChatFn,
    *,
    project_code: str | None = None,
    task_id: str | None = None,
    on_delta: Callable[[str], None] | None = None,
    reselect: Reselect | None = None,
) -> text_chat.ChatReply:
    """发一轮对话。空回答当失败，跟报错一样换候选（`run` 也走这里，因此评审、翻译一并覆盖）。"""
    body = [dict(one) for one in payload]
    return _drive(
        runtime,
        agent_code,
        decision,
        lambda candidate: _reject_empty(candidate, chat(candidate, body, on_delta=on_delta)),
        outcome_of,
        limit_kind="tokens",
        project_code=project_code,
        task_id=task_id,
        reselect=reselect,
    )


def select(
    runtime: Session,
    agent_code: str,
    *,
    limit_kind: str = "tokens",
    also_kinds: Sequence[str] = (),
    units: int = 1,
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
        limit_kind=limit_kind,
        also_kinds=also_kinds,
        units=units,
        task_id=task_id,
        project_code=project_code,
    )


def run(
    runtime: Session,
    agent_code: str,
    payload: Sequence[Mapping[str, Any]],
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


def draw(
    runtime: Session,
    agent_code: str,
    prompt: str,
    generate: ImageFn,
    *,
    negative_prompt: str = "",
    width: int = image_gen.DEFAULT_SIZE,
    height: int = image_gen.DEFAULT_SIZE,
    seed: int | None = None,
    references: Sequence[str | Path] = (),
    project_code: str | None = None,
    task_id: str | None = None,
) -> image_gen.ImageReply:
    """出一张图：选候选、调、失败换人再调。

    额度卡两种口径：`calls`（接口次数）跟 `images`（出图张数，一次一张），模型上配了哪种
    那种就生效。两者都在调用前就知道消耗，选中即预扣，不像 token 要事后读回来。
    """
    picked = select(
        runtime,
        agent_code,
        limit_kind="calls",
        also_kinds=IMAGE_KINDS,
        project_code=project_code,
        task_id=task_id,
    )
    reply = _drive(
        runtime,
        agent_code,
        picked,
        lambda candidate: generate(
            candidate,
            prompt,
            negative_prompt=negative_prompt,
            width=width,
            height=height,
            seed=seed,
            references=references,
        ),
        outcome_of_image,
        limit_kind="calls",
        project_code=project_code,
        task_id=task_id,
        reselect=lambda _: select(
            runtime,
            agent_code,
            limit_kind="calls",
            also_kinds=IMAGE_KINDS,
            project_code=project_code,
            task_id=task_id,
        ),
    )
    _log.info(
        "image_call",
        agent=agent_code,
        task=task_id,
        label=picked.candidate.label,
        size=reply.size_text,
    )
    return reply


def label_of(decision: Decision) -> Any:
    return decision.candidate.label
