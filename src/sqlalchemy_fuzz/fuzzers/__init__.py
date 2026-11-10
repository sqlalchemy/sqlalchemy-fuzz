"""Registry of fuzzers.

A fuzzer is a module providing:

``NAME``, ``DESCRIPTION``
    command name and one-line description

``CHECKS``
    dict of check name -> description, completing the sentence
    "The statement ..."; findings are reported per check

``MAPPINGS`` (optional)
    names that may appear in brackets after a check name, e.g.
    ``repeat[default]``, in the order they should be reported

``init_worker()``
    called once in each worker process before running seeds

``trial(seed)``
    returns ``(seed, status, info)``, where status is ``"ok"``,
    ``"invalid"`` (info is ``(error_class, message)``), ``"diff"`` (info is a
    tuple of failed check names) or ``"crash"`` (info is a traceback)

``seed_features(seed)``
    set of short strings describing what a seed's scenario uses, used to
    group findings

``replay(seed)``
    run a single seed, printing the scenario and any differences

"""

from . import orm_evaluate
from . import orm_loading

FUZZERS = {fuzzer.NAME: fuzzer for fuzzer in (orm_loading, orm_evaluate)}
