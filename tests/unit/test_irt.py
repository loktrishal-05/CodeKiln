"""#43: a Rasch fit separates 'hard' from 'only weak models failed', which the solve rate cannot."""
from test_calibration import receipt, row

from codekiln.evaluation.calibration import calibrate
from codekiln.evaluation.irt import MIN_MODELS, MIN_TASKS, fit_rasch, level_for, spread, sufficient

MODELS = ["m1", "m2", "m3", "m4", "m5", "m6"]  # m6 is the strongest, m1 the weakest


def guttman() -> dict[tuple[str, str], bool]:
    """Task k is solved by exactly the k strongest models (k = 0..6): a clean difficulty ladder."""
    return {(f"t{k}", m): i >= len(MODELS) - k for k in range(7) for i, m in enumerate(MODELS)}


def test_fit_recovers_the_difficulty_order_and_stays_finite_for_all_or_nothing_tasks() -> None:
    fit = fit_rasch(guttman())
    order = [fit.difficulty[f"t{k}"] for k in range(7)]  # t0: nobody solved ... t6: everybody solved
    assert order == sorted(order, reverse=True)  # solved by fewer models = harder
    assert all(abs(v) < 10 for v in order)  # t0 / t6 are all-or-nothing and must not diverge
    assert abs(sum(order) / 7) < 1e-9  # difficulty is only defined relative to the set: mean zero
    strengths = [fit.ability[m] for m in MODELS]
    assert strengths == sorted(strengths)  # the model that solved more ranks higher


def test_the_fit_is_deterministic() -> None:
    assert fit_rasch(guttman()) == fit_rasch(guttman())


def test_with_complete_data_only_the_number_of_solvers_matters() -> None:
    """Rasch's sufficient statistic: same solve count => same difficulty, whoever solved it."""
    data = guttman()
    for i, m in enumerate(MODELS):
        data[("by_strong", m)] = i >= 3
        data[("by_weak", m)] = i < 3
    fit = fit_rasch(data)
    assert abs(fit.difficulty["by_weak"] - fit.difficulty["by_strong"]) < 1e-6


def test_missing_trials_are_corrected_for_by_who_attempted_the_task() -> None:
    """Both tasks have 1 of 2 attempts solved (same solve rate), but one was only tried by weak
    models and one only by strong models: the second is the harder task, which a rate cannot see."""
    data = guttman()
    data[("only_weak_tried", "m1")], data[("only_weak_tried", "m2")] = False, True
    data[("only_strong_tried", "m5")], data[("only_strong_tried", "m6")] = False, True
    fit = fit_rasch(data)
    assert fit.difficulty["only_weak_tried"] < fit.difficulty["only_strong_tried"]


def test_levels_are_relative_bands_of_the_fitted_difficulty() -> None:
    fit = fit_rasch(guttman())
    sd = spread(fit)
    assert level_for(fit.difficulty["t6"], sd) == "L1" and level_for(fit.difficulty["t0"], sd) == "L3"
    assert level_for(0.0, sd) == "L2"


def test_too_little_data_is_not_fit() -> None:
    assert sufficient(guttman()) and MIN_MODELS == 4 and MIN_TASKS == 5
    small = {(f"t{k}", m): k >= i for k in range(2) for i, m in enumerate(["a", "b"])}
    assert not sufficient(small)


def test_calibrate_uses_the_fit_when_there_is_enough_data_and_the_coarse_rule_otherwise() -> None:
    rows = [row(f"t{k}", m, 1.0 if solved else 0.0) for (k, m), solved in
            (((int(t[1:]), m), s) for (t, m), s in guttman().items())]
    rich = {c.task: c for c in calibrate(receipt(rows))}
    assert rich["t0"].difficulty is not None and rich["t0"].observed == "L3" and rich["t6"].observed == "L1"
    assert rich["t3"].observed in {"L1", "L2", "L3"} and rich["t3"].solved == 3

    few = [r for r in rows if r.model_id in {"m5", "m6"}]  # two models: only the coarse rule is honest
    coarse = {c.task: c for c in calibrate(receipt(few))}
    assert all(c.difficulty is None for c in coarse.values())


def test_crashed_trials_are_missing_data_not_failures() -> None:
    rows = [row(f"t{k}", m, 1.0 if s else 0.0) for (t, m), s in guttman().items() for k in [int(t[1:])]]
    crashed = [row("t3", "m6", None) if (r.task, r.model_id) == ("t3", "m6") else r for r in rows]
    full = {c.task: c.difficulty for c in calibrate(receipt(rows))}
    dropped = {c.task: c.difficulty for c in calibrate(receipt(crashed))}
    assert dropped["t3"] is not None and full["t3"] is not None
    assert dropped["t3"] != full["t3"]  # the crashed trial changed the evidence, but it was not scored as a fail


def test_discrimination_is_high_for_a_ladder_task_and_undefined_for_all_or_nothing() -> None:
    from codekiln.evaluation.irt import discrimination

    d = discrimination(guttman())
    best = max(v for v in d.values() if v is not None)
    assert d["t3"] is not None and d["t3"] == best and best > 0.7  # mid-ladder tasks separate best
    assert d["t0"] is None and d["t6"] is None  # no variance: it separates nobody


def test_a_task_the_strongest_models_fail_is_flagged_as_inverted() -> None:
    from codekiln.evaluation.irt import discrimination

    data = guttman()
    for i, m in enumerate(MODELS):
        data[("broken", m)] = i < 3  # solved by the three WEAKEST, failed by the three strongest
    assert (discrimination(data)["broken"] or 0) < -0.5
    rows = [row(t, m, 1.0 if s else 0.0) for (t, m), s in data.items()]
    cals = {c.task: c for c in calibrate(receipt(rows))}
    assert cals["broken"].inverted and not cals["t3"].inverted


def test_cli_prints_the_inverted_warning(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from typer.testing import CliRunner

    from codekiln.cli import app

    data = guttman()
    for i, m in enumerate(MODELS):
        data[("broken", m)] = i < 3
    rec = receipt([row(t, m, 1.0 if s else 0.0) for (t, m), s in data.items()])
    path = tmp_path / "eval_receipt.json"
    path.write_text(rec.to_json())
    monkeypatch.setenv("CODEKILN_DB_PATH", str(tmp_path / "none.sqlite"))
    res = CliRunner().invoke(app, ["calibrate", str(path), "--out", str(tmp_path / "c.json")])
    assert res.exit_code == 0, res.output
    assert "broken:" in res.output and "INVERTED" in res.output
    assert '"inverted": true' in (tmp_path / "c.json").read_text()
