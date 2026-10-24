import pytest

from is_this_us.dialogue.engine import DialogueEngine, load_locale
from is_this_us.domain import (
    ExecutionResult, ExecutionStatus, HumanObservation, MotionPlan, Reproducibility, RobotBehavior,
)
from is_this_us.memes.repository import MemeRepository
from tests.conftest import ROOT


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


PAINTING_CAPTIONS = {"me and who?🥺", "us?👉👈"}
MEME_CAPTIONS = {"hehe or not hehe?"}
SPEECH = {
    "en": {"Am I a good boy already?", "A really good boy now?"},
    "ru": {"Я уже хороший мальчик?", "Теперь я правда хороший мальчик?"},
    "de": {"Bin ich schon ein guter Junge?", "Jetzt ein wirklich guter Junge?"},
}


@pytest.fixture(params=["art", "meme"])
def encounter_scene(request, repo):
    if request.param == "meme":
        return MemeRepository(ROOT / "data" / "memes").get("meme:this-is-fine")
    return repo.get("met:437966")


@pytest.mark.parametrize("lang", ["en", "ru", "de", "unknown"])
def test_encounter_approved_captions_and_separate_speech(encounter_scene, lang):
    engine = DialogueEngine(lang)
    outputs = [engine.compose_encounter(seed=f"encounter:{i}", artwork=encounter_scene, speak=True)
               for i in range(32)]
    expected = MEME_CAPTIONS if encounter_scene.kind == "meme" else PAINTING_CAPTIONS
    assert {output.caption for output in outputs} == expected
    assert {output.spoken for output in outputs} == SPEECH.get(lang, SPEECH["en"])
    for i, output in enumerate(outputs):
        assert output == engine.compose_encounter(seed=f"encounter:{i}", artwork=encounter_scene, speak=True)
        assert output.spoken not in output.caption


@pytest.mark.parametrize("lang", ["en", "ru", "de"])
def test_silent_encounter_preserves_caption(encounter_scene, lang):
    engine = DialogueEngine(lang)
    kwargs = dict(seed="quiet", artwork=encounter_scene)
    default = engine.compose_encounter(**kwargs)
    silent = engine.compose_encounter(**kwargs, speak=False)
    speaking = engine.compose_encounter(**kwargs, speak=True)
    assert default == silent
    assert silent.spoken is None
    assert silent.caption == speaking.caption
    assert speaking.spoken in SPEECH[lang]


@pytest.mark.parametrize("speak", [False, True])
def test_unmatched_encounter_has_no_visitor_text(speak):
    output = DialogueEngine().compose_encounter(seed="no-match", artwork=None, speak=speak)
    assert output.caption is None
    assert output.spoken is None


@pytest.mark.parametrize("key", ["painting_caption", "meme_caption", "speech"])
def test_encounter_missing_locale_key_falls_back_to_english(encounter_scene, key):
    engine = DialogueEngine("ru")
    # Do not mutate the cached locale dictionary used by other engines.
    engine.texts._data = {**engine.texts._data, "encounter": {
        k: v for k, v in engine.texts._data["encounter"].items() if k != key
    }}
    kwargs = dict(seed="fallback", artwork=encounter_scene, speak=True)
    actual = engine.compose_encounter(**kwargs)
    english = DialogueEngine("en").compose_encounter(**kwargs)
    if key == "speech":
        assert actual.spoken == english.spoken
    else:
        assert actual.caption == english.caption
        assert actual.spoken in SPEECH["ru"]


def test_encounter_missing_locale_section_falls_back_to_english(encounter_scene):
    engine = DialogueEngine("de")
    engine.texts._data = {k: v for k, v in engine.texts._data.items() if k != "encounter"}
    kwargs = dict(seed="fallback", artwork=encounter_scene, speak=True)
    assert engine.compose_encounter(**kwargs) == DialogueEngine("en").compose_encounter(**kwargs)


def test_encounter_does_not_copy_archive_narration(repo):
    scene = repo.get("met:437966").model_copy(update={
        "title": "Long gallery narration " * 100,
        "poem": "An explanation that must not become an image caption.",
    })
    output = DialogueEngine().compose_encounter(seed="archive", artwork=scene, speak=True)
    assert output.caption in PAINTING_CAPTIONS
    assert output.spoken in SPEECH["en"]
