from codekiln.evaluation.mergeability_grader import diff_stats, grade

REF = """--- a/src/a.py
+++ b/src/a.py
@@ -1 +1,3 @@
 x
+def f():
+    return 1
"""


def test_diff_stats():
    s = diff_stats(REF)
    assert s.files == {"src/a.py"} and s.loc == 2


def test_clean_diff_is_mergeable():
    g = grade(REF, REF)
    assert g.mergeable and g.bloat_ratio == 1.0 and g.blast_radius == 0


def test_bloat_and_scope_creep_block_merge():
    bloated = REF + "".join(f"+l{i}\n" for i in range(5))
    assert not grade(REF, bloated).mergeable
    extra = REF + "--- a/other.py\n+++ b/other.py\n@@ -1 +1 @@\n+y\n"
    g = grade(REF, extra)
    assert g.blast_radius == 1 and not g.mergeable


def test_lint_regression_blocks_merge():
    assert not grade(REF, REF, lint_delta=1).mergeable


def test_deleted_file_counts_in_scope():
    gone = "--- a/old.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"
    assert diff_stats(gone).files == {"old.py"}
