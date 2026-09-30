# Marks `tests` as a package so test modules can share conftest helpers via
# relative imports. pytest then puts the repo root on sys.path, which is also
# how `bnfmm` resolves (see pythonpath in pyproject.toml).
