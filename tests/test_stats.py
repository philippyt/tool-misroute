from misroute.fix.stats import holm, mcnemar, signflip


def test_mcnemar_flag_rule():
    a = {i: True for i in range(6)}
    b = {i: False for i in range(6)}
    assert round(mcnemar(a, b)[2], 4) == 0.0312
    five = {i: True for i in range(5)}
    assert mcnemar(five, {i: False for i in range(5)})[2] == 0.0625
    assert mcnemar(a, a) == (0, 0, 1.0)


def test_signflip_floor():
    assert signflip([-1, -2, -3, -4], "less") == 1 / 16
    assert signflip([1, 2, 3, 4], "less") == 1.0


def test_holm():
    assert holm([0.01, 0.04, 0.03]) == [0.03, 0.06, 0.06]


def test_template_level_test_does_not_count_correlated_trials():
    from misroute.fix.stats import midp, template_test

    base = {("m", "json", f"t{t}", s, o, 0): True for t in range(4) for s in range(2) for o in range(2)}
    treat = dict.fromkeys(base, False)
    assert mcnemar(base, treat)[2] < 0.001
    res = template_test(base, treat)
    assert res["templates"] == 4 and res["discordant_templates"] == 4
    assert res["p"] == 0.125 and res["diff"] == -1.0
    assert res == template_test(base, treat)
    assert round(midp(6, 0), 4) == 0.0156 and midp(0, 0) == 1.0
