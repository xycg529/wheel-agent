from __future__ import annotations

# SWE-bench Lite official test split, first 25 instance_ids in
# lexicographic order (string sort: "-6938" comes after "-14995").
# Frozen so a tiny SWE run is reproducible without pulling the full dataset just to list ids.
INSTANCE_IDS: tuple[str, ...] = (
    "astropy__astropy-12907",
    "astropy__astropy-14182",
    "astropy__astropy-14365",
    "astropy__astropy-14995",
    "astropy__astropy-6938",
    "astropy__astropy-7746",
    "django__django-10914",
    "django__django-10924",
    "django__django-11001",
    "django__django-11019",
    "django__django-11039",
    "django__django-11049",
    "django__django-11099",
    "django__django-11133",
    "django__django-11179",
    "django__django-11283",
    "django__django-11422",
    "django__django-11564",
    "django__django-11583",
    "django__django-11620",
    "django__django-11630",
    "django__django-11742",
    "django__django-11797",
    "django__django-11815",
    "django__django-11848",
)

DATASET = "princeton-nlp/SWE-bench_Lite"

DATASETS = {
    "lite": "princeton-nlp/SWE-bench_Lite",
    "verified": "princeton-nlp/SWE-bench_Verified",
}

# SWE-bench Verified test split, first 5 instance_ids in lexicographic order.
# Frozen (same convention as the Lite set) so a tiny Verified run is
# reproducible without pulling the dataset just to list ids.
# Note: all five are astropy — lexicographic consequence; use --ids to
# override with a hand-picked cross-repo set.
INSTANCE_IDS_VERIFIED5: tuple[str, ...] = (
    "astropy__astropy-12907",
    "astropy__astropy-13033",
    "astropy__astropy-13236",
    "astropy__astropy-13398",
    "astropy__astropy-13453",
)

# Default tiny set. Three Django issues from the frozen 25; full Lite is 300.
INSTANCE_IDS_TINY: tuple[str, ...] = (
    "django__django-10914",
    "django__django-11001",
    "django__django-11179",
)

# Headline set: one instance from each of five canonical repos, each with a
# small single-file gold patch and a short, unambiguous issue statement.
# django-11179 and astropy-12907 also appear in the frozen 25 for continuity.
INSTANCE_IDS_CLASSIC5: tuple[str, ...] = (
    "django__django-11179",
    "sympy__sympy-20590",
    "scikit-learn__scikit-learn-10508",
    "matplotlib__matplotlib-23299",
    "astropy__astropy-12907",
)
