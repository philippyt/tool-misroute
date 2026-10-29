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
