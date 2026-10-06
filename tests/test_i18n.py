from projectflow import i18n
from projectflow.i18n import Labels, tr


def test_resolve_prefers_the_flag_then_the_environment_then_the_saved_language_then_the_locale():
    locale = {"LANG": "ko_KR.UTF-8"}
    assert i18n.resolve(environ={}) == "en"
    assert i18n.resolve(environ=locale) == "ko"
    assert i18n.resolve(environ={"LC_ALL": "en_US.UTF-8", **locale}) == "en"
    assert i18n.resolve(saved="English", environ=locale) == "en"
    assert i18n.resolve(saved="English", environ={i18n.ENV: "ko", **locale}) == "ko"
    assert i18n.resolve(explicit="English", saved="Korean", environ={i18n.ENV: "ko", **locale}) == "en"
    assert i18n.resolve(explicit="auto", saved="Korean", environ={}) == "ko"  # an unknown name is no choice
    assert i18n.resolve(environ={"LANG": "fr_FR.UTF-8"}) == "en"
    assert i18n.code("Korean") == "ko" and i18n.code("C") == "en" and i18n.code(None) is None


def test_tr_and_labels_follow_the_language_set_at_call_time():
    labels = Labels({"a": "가"}, {"a": "a"})
    i18n.set_language("en")
    assert tr("가", "a") == "a" and labels["a"] == "a" and i18n.language() == "en"
    i18n.set_language("Korean")
    assert tr("가", "a") == "가" and labels["a"] == "가" and dict(labels) == {"a": "가"}
