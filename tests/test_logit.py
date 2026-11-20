import math
import numpy as np
from misroute.fix.logit import MIN_CLUSTERS_LOGIT, clustered_logit, decompose_errors, paired_logit_test
from misroute.fix.stats import template_test
from misroute.trial import Trial


def test_firth_handles_complete_separation():
    # Base has 100% errors, treat has 0% errors across 20 templates -> complete separation
    base = {("m", "json", f"t{i}", s, 0, 0): True for i in range(20) for s in range(2)}
    treat = dict.fromkeys(base, False)
    res = paired_logit_test(base, treat)
    assert res["clusters"] == 20
    assert res["underpowered_clusters"] is True
    assert math.isfinite(res["coef"]) and res["coef"] < -2.0
    assert math.isfinite(res["odds_ratio"]) and res["odds_ratio"] < 0.1
    assert res["ame"] < -0.8
    assert res["p"] < 0.001


def test_paired_logit_respects_template_clustering():
    # Only 4 templates, repeated 10 times each: cluster count stays 4 and is marked underpowered
    base = {("m", "json", f"t{i}", s, o, 0): True for i in range(4) for s in range(5) for o in range(2)}
    treat = dict.fromkeys(base, False)
    res = paired_logit_test(base, treat)
    assert res["clusters"] == 4
    assert res["underpowered_clusters"] is True
    # Also wired into template_test alongside exact signflip
    tt = template_test(base, treat)
    assert "logit" in tt
    assert tt["logit"]["clusters"] == 4
    assert tt["p"] == 0.125


def test_paired_logit_detects_effect_at_scale():
    rng = np.random.default_rng(42)
    base, treat = {}, {}
    for i in range(MIN_CLUSTERS_LOGIT + 10):
        # True error rate drops from ~0.55 to ~0.20 per template
        b_err = rng.random(4) < 0.55
        t_err = rng.random(4) < 0.20
        for s in range(4):
            key = ("m", "json", f"t{i}", s, 0, 0)
            base[key] = bool(b_err[s])
            treat[key] = bool(t_err[s])
    res = paired_logit_test(base, treat)
    assert res["clusters"] == 40
    assert res["underpowered_clusters"] is False
    assert res["odds_ratio"] < 0.5
    assert -0.5 < res["ame"] < -0.15
    assert res["p"] < 0.01


def test_paired_logit_null_and_all_equal():
    base = {("m", "json", f"t{i}", 0, 0, 0): False for i in range(30)}
    res = paired_logit_test(base, dict(base))
    assert res["p"] == 1.0
    assert res["odds_ratio"] == 1.0
    assert res["ame"] == 0.0


def test_decompose_errors_distinguishes_description_from_model():
    # 4 models across 4 tools, 15 templates per tool.
    # Tool 'bad_tool' fails ~80% on ALL models; other tools fail ~5% on ALL models.
    rng = np.random.default_rng(7)
    models = ["m_anth", "m_oai", "m_goog", "m_qwen"]
    tools = ["clean_a", "clean_b", "clean_c", "bad_tool"]
    trials = []
    for tool in tools:
        p_err = 0.80 if tool == "bad_tool" else 0.05
        for tid in range(15):
            for m in models:
                err = bool(rng.random() < p_err)
                t = Trial(m, "json", tool, f"{tool}_{tid}", "req", calls=["other" if err else tool])
                trials.append(t)
    res = decompose_errors(trials)
    assert res["converged"] is True
    assert res["clusters"] == 60
    assert res["underpowered_clusters"] is False
    # Tool/description factor should dominate model factor in deviance share
    assert res["factors"]["tool"]["share_of_unique_deviance"] > 0.85
    assert res["factors"]["model"]["share_of_unique_deviance"] < 0.15
    # bad_tool is the alphabetical reference level, so clean tools have much lower odds of error
    assert res["references"]["tool"] == "bad_tool"
    assert res["terms"]["tool:clean_a"]["odds_ratio"] < 0.1


def test_decompose_errors_detects_generator_family_bias():
    # Models make fewer errors when generator_family matches their own family
    rng = np.random.default_rng(11)
    fams = {"claude": "anthropic", "gpt": "openai", "gemini": "google"}
    trials = []
    for tid in range(40):
        gen_fam = ["anthropic", "openai", "google"][tid % 3]
        for m, m_fam in fams.items():
            p_err = 0.10 if m_fam == gen_fam else 0.45
            err = bool(rng.random() < p_err)
            target = "t_a" if tid % 2 == 0 else "t_b"
            t = Trial(m, "json", target, f"r{tid}", "req", calls=["x" if err else target])
            t.generator_family = gen_fam
            trials.append(t)
    res = decompose_errors(trials, model_families=fams)
    assert "same_gen_family" in res["terms"]
    term = res["terms"]["same_gen_family"]
    assert term["coef"] < -1.0
    assert term["odds_ratio"] < 0.4
    assert term["ame"] < -0.2
    assert term["p"] < 0.001


def test_clustered_logit_degenerate_empty():
    res = clustered_logit(np.zeros((0, 2)), np.zeros(0), [])
    assert res["converged"] is False
    assert res["clusters"] == 0
