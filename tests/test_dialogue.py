import pytest

from is_this_us.dialogue.engine import DialogueEngine, load_locale
from is_this_us.domain import (
    ExecutionResult, ExecutionStatus, HumanObservation, MotionPlan, Reproducibility, RobotBehavior,
)


def keys(d, prefix=""):
    out = set()
    for k, v in d.items():
        out.add(prefix + k)
        if isinstance(v, dict):
            out |= keys(v, prefix + k + ".")
    return out


@pytest.mark.parametrize("lang", ["ru", "de"])
def test_locales_cover_english_keys(lang):
    missing = keys(load_locale("en")) - keys(load_locale(lang))
    assert not missing, missing


def _obs(conf=0.8):
    return HumanObservation(posture="sitting", posture_confidence=conf)


def _plan(b, fid):
    return MotionPlan(requested=b, behavior=b, fidelity=fid, explanation="")


@pytest.mark.parametrize("status,fid,behavior,expected", [
    (ExecutionStatus.SIMULATED, Reproducibility.YES, RobotBehavior.SIT, "success"),
    (ExecutionStatus.SUCCESS, Reproducibility.APPROXIMATE, RobotBehavior.SIT, "partial"),
    (ExecutionStatus.SKIPPED, Reproducibility.YES, RobotBehavior.SIT, "imagined"),
    (ExecutionStatus.SKIPPED, Reproducibility.NO, RobotBehavior.STILLNESS, "still"),
    (ExecutionStatus.FAILED, Reproducibility.YES, RobotBehavior.SIT, "failed"),
    (ExecutionStatus.STOPPED, Reproducibility.YES, RobotBehavior.SIT, "stopped"),
])
def test_outcome_mapping(status, fid, behavior, expected):
    assert DialogueEngine.outcome_key(_plan(behavior, fid), ExecutionResult(behavior=behavior, status=status)) == expected


def test_compose_is_deterministic_short_and_asks(repo):
    art = repo.get("met:437966")
    eng = DialogueEngine("en")
    kw = dict(observation=_obs(), artwork=art, plan=_plan(RobotBehavior.SIT, Reproducibility.YES),
              result=ExecutionResult(behavior=RobotBehavior.SIT, status=ExecutionStatus.SIMULATED))
    u1, u2 = eng.compose(seed="s1", **kw), eng.compose(seed="s1", **kw)
    assert u1.lines == u2.lines
    assert u1.lines[-1] == "Is this us?"
    assert "sitting beside a person like you" in u1.lines[1]
    assert "… …" not in u1.spoken and len(u1.spoken) < 260


def test_uncertainty_is_voiced(repo):
    u = DialogueEngine("en").compose(seed="x", observation=_obs(conf=0.2), artwork=repo.get("met:437966"))
    assert u.lines[1].startswith(("I'm not sure", "Maybe"))


def test_no_match_and_nobody():
    eng = DialogueEngine("en")
    assert eng.compose(seed="a", observation=_obs(), artwork=None).outcome_key == "no_match"
    nobody = HumanObservation(posture="unknown", posture_confidence=0, people_detected=0)
    assert eng.compose(seed="a", observation=nobody, artwork=None).outcome_key == "nobody"


def test_russian(repo):
    u = DialogueEngine("ru").compose(seed="r", observation=_obs(), artwork=repo.get("met:437173"))
    assert u.lines[-1] == "Это мы?"
    assert "рядом с человеком" in u.lines[1]
