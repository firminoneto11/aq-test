from dataclasses import replace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.job_packs import SAMPLE_JOBS
from domain.interviews import (
    AnsweredQuestion,
    InterviewPolicy,
    InterviewSnapshot,
    RolePack,
    RubricSignal,
    SignalEvidence,
    classify_control,
)


def _snapshot(topics: list[int], assessed: set[int] | None = None) -> InterviewSnapshot:
    pack = SAMPLE_JOBS[0].pack
    answers = tuple(
        AnsweredQuestion(
            question_id=uuid4(),
            answer_id=uuid4(),
            topic_id=pack.topics[topic].id,
            kind="follow_up" if ordinal in (2, 4, 7, 8, 9, 10) else "bank",
            answer_text=f"Evidence from answer {ordinal}",
            bank_question_id=(
                None
                if ordinal in (2, 4, 7, 8, 9, 10)
                else pack.topics[topic].questions[0].id
            ),
        )
        for ordinal, topic in enumerate(topics, start=1)
    )
    assessed = assessed if assessed is not None else set(topics)
    signals = tuple(
        RubricSignal(
            topic_id=topic.id,
            status=(
                "assessed"
                if index in assessed
                else "insufficient"
                if index in topics
                else "unobserved"
            ),
            skill_ids=[],
            evidence=[
                SignalEvidence(answer_id=answer.answer_id, quote=answer.answer_text)
                for answer in answers
                if answer.topic_id == topic.id
            ][:1],
        )
        for index, topic in enumerate(pack.topics)
    )
    return InterviewSnapshot(
        pack=pack,
        answers=answers,
        signals=signals,
        used_bank_ids=frozenset(
            answer.bank_question_id
            for answer in answers
            if answer.bank_question_id is not None
        ),
    )


def test_six_question_schedule_and_assessed_stop():
    policy = InterviewPolicy()
    topic_sequence = [0, 0, 1, 1, 2, 3]
    kinds = ["bank", "follow_up", "bank", "follow_up", "bank", "bank"]
    for count, kind in enumerate(kinds):
        snapshot = _snapshot(topic_sequence[:count])
        decision = policy.next_step(snapshot)
        assert decision.action == "ask"
        assert decision.kind == kind
        assert decision.topic_id == snapshot.pack.topics[topic_sequence[count]].id
        if kind == "follow_up":
            assert decision.source_answer_id == snapshot.answers[count - 1].answer_id
        else:
            assert decision.eligible_bank_ids
    assert (
        policy.next_step(_snapshot(topic_sequence)).reason_code == "coverage_complete"
    )


def test_extra_probes_target_missing_evidence_not_poor_score():
    policy = InterviewPolicy()
    topics = [0, 0, 1, 1, 2, 3]
    snapshot = _snapshot(topics, assessed={0, 1})
    next_question = policy.next_step(snapshot)
    assert next_question.reason_code == "evidence_gap"
    assert next_question.topic_id == snapshot.pack.topics[2].id
    assert next_question.source_answer_id == snapshot.answers[4].answer_id

    for _ in range(4):
        topics.append(2)
    capped = _snapshot(topics, assessed={0, 1})
    assert policy.next_step(capped).reason_code == "question_limit"


def test_signal_evidence_must_belong_to_the_same_topic_and_quote_the_answer():
    snapshot = _snapshot([0, 0, 1, 1, 2, 3])
    unrelated_answer = snapshot.answers[2]
    wrong_topic = RubricSignal(
        topic_id="ownership",
        status="assessed",
        skill_ids=[],
        evidence=[
            SignalEvidence(
                answer_id=unrelated_answer.answer_id,
                quote=unrelated_answer.answer_text,
            )
        ],
    )
    with pytest.raises(ValueError):
        InterviewPolicy().validate_signal(wrong_topic, snapshot)

    wrong_quote = RubricSignal(
        topic_id="ownership",
        status="assessed",
        skill_ids=[],
        evidence=[
            SignalEvidence(answer_id=snapshot.answers[0].answer_id, quote="invented")
        ],
    )
    with pytest.raises(ValueError):
        InterviewPolicy().validate_signal(wrong_quote, snapshot)


@pytest.mark.parametrize(
    "invalid",
    [
        "duplicate_skills",
        "skill_from_other_topic",
        "duplicate_questions",
        "wrong_category_order",
        "wrong_first_topic",
        "bad_weight_total",
        "duplicate_topics",
        "duplicate_questions_across_topics",
        "wrong_job_question_id",
    ],
)
def test_role_pack_rejects_ambiguous_or_misgrounded_questions(invalid: str) -> None:
    data = SAMPLE_JOBS[0].pack.model_dump()
    topics = data["topics"]
    if invalid == "duplicate_skills":
        topics[0]["skill_ids"][1] = topics[0]["skill_ids"][0]
    elif invalid == "skill_from_other_topic":
        topics[0]["skill_ids"][1] = topics[1]["skill_ids"][0]
    elif invalid == "duplicate_questions":
        topics[0]["questions"][1]["id"] = topics[0]["questions"][0]["id"]
    elif invalid == "wrong_category_order":
        topics[1]["category"] = "behavioral"
    elif invalid == "wrong_first_topic":
        topics[0]["id"] = "substituted"
    elif invalid == "bad_weight_total":
        topics[0]["weight"] += 1
    elif invalid == "duplicate_topics":
        topics[2]["id"] = topics[1]["id"]
    elif invalid == "duplicate_questions_across_topics":
        topics[1]["questions"][0]["id"] = topics[0]["questions"][0]["id"]
    else:
        topics[0]["questions"][0]["id"] = "unrelated.ownership.a"

    with pytest.raises(ValidationError):
        RolePack.model_validate(data)


def test_coverage_gaps_rotate_instead_of_reprobing_one_topic() -> None:
    policy = InterviewPolicy()
    after_six = _snapshot([0, 0, 1, 1, 2, 3], assessed={0, 1})
    assert policy.next_step(after_six).topic_id == after_six.pack.topics[2].id

    after_one_extra_probe = _snapshot([0, 0, 1, 1, 2, 3, 2], assessed={0, 1})
    next_step = policy.next_step(after_one_extra_probe)
    assert next_step.reason_code == "evidence_gap"
    assert next_step.topic_id == after_one_extra_probe.pack.topics[3].id
    assert next_step.source_answer_id == after_one_extra_probe.answers[5].answer_id


def test_policy_rejects_unsupported_skills_duplicate_answers_and_bank_exhaustion() -> (
    None
):
    policy = InterviewPolicy()
    snapshot = _snapshot([0, 0, 1, 1, 2, 3])
    signal = RubricSignal(
        topic_id="ownership",
        status="assessed",
        skill_ids=[snapshot.pack.topics[1].skill_ids[0]],
        evidence=[
            SignalEvidence(
                answer_id=snapshot.answers[0].answer_id,
                quote=snapshot.answers[0].answer_text,
            )
        ],
    )
    with pytest.raises(ValueError, match="Unsupported skill"):
        policy.validate_signal(signal, snapshot)

    duplicated = replace(
        snapshot,
        answers=(
            snapshot.answers[0],
            replace(snapshot.answers[1], answer_id=snapshot.answers[0].answer_id),
            *snapshot.answers[2:],
        ),
    )
    with pytest.raises(ValueError, match="Duplicate answer"):
        policy.next_step(duplicated)

    bank_needed = _snapshot([0, 0])
    exhausted = replace(
        bank_needed,
        used_bank_ids=frozenset(
            question.id for question in bank_needed.pack.topics[1].questions
        ),
    )
    with pytest.raises(ValueError, match="No unused bank"):
        policy.next_step(exhausted)


def test_only_a_standalone_spoken_command_controls_the_session():
    assert classify_control("Please stop the interview?") == "stop"
    assert classify_control("Can you repeat the question?") == "repeat"
    assert (
        classify_control('My manager said "stop the interview" during the incident.')
        == "answer"
    )
    assert classify_control("  ...  ") == "empty"
