from codekiln.planner.difficulty import balance, level
from codekiln.planner.models import Category, Plan


def mk(files: list[str], fanout: int = 0) -> Plan:
    return Plan(Category.CORE_LOGIC, frozenset(files), frozenset({files[0]}), "reverse", fanout)


def test_levels():
    assert level(mk(["a.py"])) == "L1"
    assert level(mk(["a.py", "b.py"])) == "L2"
    assert level(mk(["a/x.py", "b/y.py", "c/z.py"])) == "L3"  # 3 directories
    assert level(mk(["a.py"], fanout=5)) == "L3"
    assert level(mk(["a.py", "b.py", "c.py", "d.py", "e.py"])) == "L3"


def test_balance_mixes_levels_and_respects_slots():
    plans = [mk([f"l1_{i}.py"]) for i in range(3)] + [mk([f"l2_{i}.py", "z.py"]) for i in range(3)]
    plans += [mk([f"l3_{i}.py"], fanout=9) for i in range(3)]
    picked = balance(plans, 6)
    assert len(picked) == 6
    assert [level(p) for p in picked].count("L1") == 2 and [level(p) for p in picked].count("L3") == 2


def test_balance_tops_up_when_a_level_is_missing():
    plans = [mk([f"l1_{i}.py"]) for i in range(4)]
    assert len(balance(plans, 3)) == 3
    assert len(balance(plans, 10)) == 4


def test_custom_mix():
    plans = [mk([f"a{i}.py"]) for i in range(4)] + [mk([f"b{i}.py"], fanout=9) for i in range(4)]
    picked = balance(plans, 4, mix=(1, 0, 3))
    assert [level(p) for p in picked].count("L3") == 3
