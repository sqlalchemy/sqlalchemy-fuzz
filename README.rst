sqlalchemy-fuzz
===============

Differential fuzzers for `SQLAlchemy <https://github.com/sqlalchemy/sqlalchemy>`_.

Each fuzzer generates random scenarios and runs each one several ways that
must give the same result, so no expected result has to be written by
hand.  Any difference is a bug, reported with a seed that replays it.


Fuzzers
-------

``orm-loading``
    Random statements with nested loader options (``selectinload``,
    ``joinedload``, ``subqueryload``, ``immediateload``, ``of_type()``,
    ``relationship.and_()``, ``selectin_polymorphic()``, ``defer`` /
    ``undefer`` / ``load_only``, ``with_expression()``,
    ``with_loader_criteria()``) against plain, ``aliased()`` and
    ``with_polymorphic()`` entities of an inheritance-heavy mapping.
    Checks that each statement loads the same objects:

    * **sibling**: after a statement with the same loader options but a
      different parent statement was cached
    * **repeat**: when run a second time on the same cache, with new
      ``aliased()`` / ``with_polymorphic()`` objects
    * **polymorphic_load**: a statement that works must not raise when a
      subclass sets ``polymorphic_load="selectin"``, which is only a
      loading optimization

    The comparison is against the same statement run with the compiled
    cache disabled, using the loaded object graph (read from each object's
    ``__dict__``, so nothing is lazy loaded) and the number of SQL
    statements emitted.

``orm-evaluate``
    ORM-enabled ``update()`` / ``delete()`` statements with random WHERE
    criteria and SET values: comparisons (column to column, and against
    ``None``), ``and_()`` / ``or_()`` / ``not_()``, ``in_()`` /
    ``not_in()`` (empty, or with ``None`` in the list), ``tuple_().in_()``,
    ``is_()`` / ``is_not()``, ``startswith()`` / ``endswith()``,
    arithmetic including ``/`` and ``%``, string concatenation, and
    constructs the evaluator can't handle, such as ``case()`` and
    ``between()``.  The mapped class has integer, numeric, string, boolean
    and date columns with some NULLs; all rows are loaded into the Session
    first, and some seeds expire attributes or whole objects.  Each
    statement is run with ``synchronize_session`` set to ``"evaluate"``,
    ``"fetch"`` and ``"auto"``, then the objects in the Session are
    compared with the rows in the database.  Checks that the statement:

    * **where**: updates or deletes in the Session the objects whose rows
      its WHERE criteria matched in the database, and no others
    * **values**: leaves the objects it updated with the database's values
    * **raise**: doesn't raise with one ``synchronize_session`` setting but
      not with another.  ``"evaluate"`` raising "Could not evaluate current
      criteria in Python" is documented; such seeds are reported as
      invalid, naming the construct it couldn't evaluate

    The database's semantics are the reference, and this runs against
    SQLite only.  Semantics that differ between backends are out of scope,
    for example Oracle treating ``''`` as NULL, or division by zero (NULL
    on SQLite, an error on PostgreSQL); the generator avoids expressions
    whose result depends on them.  The order in which SQLAlchemy evaluates
    SET values depends on ``PYTHONHASHSEED``, so the workflow sets it to
    ``0``; replay with ``PYTHONHASHSEED=0`` to see the same results.

``core-cache-key``
    Random Core statements: joins and outer joins, subqueries, scalar,
    ``EXISTS`` and ``IN`` subqueries, CTEs (recursive and nesting),
    ``correlate()`` / ``correlate_except()``, ``lateral()``, set
    operations, ``with_only_columns()``, label styles, ``cast()`` /
    ``type_coerce()``, ``literal_column()``, ``text()`` and
    ``TextualSelect``, ``values()``, ``literal_execute`` and expanding
    parameters, ``.params()``, and ``insert()`` / ``update()`` /
    ``delete()`` with ``RETURNING``.  Each statement is built again for
    each use, with new alias, subquery and ``table()`` objects, alongside
    a variant with only literal values changed and variants with one
    structural change each.  Checks that the statement:

    * **collision**: compiles the same as each variant that has the same
      cache key: SQL, result columns, parameter types and
      ``literal_execute`` parameters, on the SQLite, PostgreSQL, MySQL and
      SQL Server dialects
    * **anon_labels**: also numbers its anonymous labels, which become
      result keys, the same as those variants
    * **literal_key**: has the same cache key as the variant with only
      literal values changed
    * **params**: gets the same parameters from the compiled form of an
      equal-key statement, as the compiled cache gives it, as when it's
      compiled on its own
    * **execution**: returns the same rows on SQLite (and for DML leaves
      the same table contents) when run after its variants on the same
      cache as with the compiled cache disabled

    A cache key that changes for a structural change while the SQL stays
    the same is only a missed cache hit, and isn't reported.  Anonymous
    parameter names, which aren't part of the cache key, are compared by
    position.


Bugs found
----------

``orm-loading``:

* `#13777 <https://github.com/sqlalchemy/sqlalchemy/issues/13777>`_ -
  loader options under ``selectin_polymorphic()`` skipped depending on
  what was cached before (found by hand; the reason this fuzzer exists)
* `#13779 <https://github.com/sqlalchemy/sqlalchemy/issues/13779>`_ -
  options nested under ``subqueryload()`` of an aliased entity ignored once
  the statement is cached
* `#13780 <https://github.com/sqlalchemy/sqlalchemy/issues/13780>`_ -
  ``polymorphic_load="selectin"`` raises for loader options against an
  ``aliased()`` base entity

How this repo came about: `#1 <https://github.com/sqlalchemy/sqlalchemy-fuzz/issues/1>`_.


Why this runs every week
------------------------

Each bug found gets a regression test in SQLAlchemy, but a regression test
pins down one statement.  The bugs found so far live in how loader options
are matched up with cached statements, paths and aliased entities, which
can break for any of thousands of combinations of options and entities.
#13777 is an example: its test covers ``selectin_polymorphic()``, while
the fuzzer also reaches the same code through ``polymorphic_load="selectin"``,
which no test covers specifically.  A change to option and path handling,
cache key generation or a loader strategy could break a combination that
no test covers, and nothing else would notice.

So the `Fuzz workflow <.github/workflows/fuzz.yaml>`_ runs every Monday
against SQLAlchemy ``main`` and ``rel_2_0``, and does two things:

* Runs a fixed range of seeds, ``0:20000``.  The result only changes when
  SQLAlchemy or the fuzzer changes, so a failure after a green run means
  something merged that week broke loading.
* Runs a different range of seeds each week, to keep looking for bugs that
  the fixed range has not reached.  A failure only here is more likely an
  existing bug than a new one.

A push to ``main`` that changes the fuzzers also runs the fixed range,
without filing issues, so a new or changed fuzzer gets a run right away.

It can also be run by hand from the Actions tab, or with::

    gh workflow run fuzz.yaml -R sqlalchemy/sqlalchemy-fuzz \
        -f seeds=0:20000 -f refs='["main"]'


Running locally
---------------

The fuzzers test whichever SQLAlchemy is importable, so install a
SQLAlchemy checkout and this repo into the same virtualenv.  SQLAlchemy is
deliberately not a dependency, so that pip won't replace the checkout with
a release::

    python -m venv .venv
    .venv/bin/pip install /path/to/sqlalchemy   # or pip install -e
    .venv/bin/pip install -e .

    .venv/bin/sqla-fuzz orm-loading --seeds 0:20000

``--procs`` sets the number of worker processes (default: one per CPU);
20000 seeds take a few seconds on 8 cores.  The exit status is 1 if
anything was found.  ``--markdown FILE`` appends the report to a file,
which the workflow uses for the run summary.

To look at a single finding::

    .venv/bin/sqla-fuzz orm-loading --replay 35

which prints the generated scenario and every difference, for example::

    ('Paperwork', 1).person_id: expected '<unloaded>', got 1

A seed always generates the same scenario for a given version of this
repo, so a seed from a report replays against the same commit of
sqlalchemy-fuzz; the report header names it.


Reading the report
------------------

The first lines name the SQLAlchemy commit tested and count the seeds:
those *with differences*, those *OK*, and those *skipped as invalid*,
where the generated scenario raises regardless (for example, two loader
options that conflict on the same path); the most common such errors are
listed at the end.

Then there is a section per check that failed, listing how many seeds
failed it.  A seed failing more than one check is listed under each.  A
section lists which features every failing seed has in common, which is
the best clue to the cause, the most frequent features, and the smallest
seeds with a replay command.  Many seeds are usually the same bug reached
in different ways; the number of seeds says how easy a bug is to hit, not
how many bugs there are.


Adding a fuzzer
---------------

Add a module to ``src/sqlalchemy_fuzz/fuzzers/`` and register it in
``fuzzers/__init__.py``, whose docstring describes what a fuzzer module
provides.  It gets a ``sqla-fuzz <name>`` command, and is added to the
workflow by adding its name to the ``fuzzer`` matrix in
``.github/workflows/fuzz.yaml``.
