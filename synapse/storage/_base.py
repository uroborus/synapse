# -*- coding: utf-8 -*-
# Copyright 2014 OpenMarket Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import logging

from twisted.internet import defer

from synapse.api.errors import StoreError
from synapse.util.logutils import log_function

import collections
import copy
import json


logger = logging.getLogger(__name__)

sql_logger = logging.getLogger("synapse.storage.SQL")


class LoggingTransaction(object):
    """An object that almost-transparently proxies for the 'txn' object
    passed to the constructor. Adds logging to the .execute() method."""
    __slots__ = ["txn"]

    def __init__(self, txn):
        object.__setattr__(self, "txn", txn)

    def __getattribute__(self, name):
        if name == "execute":
            return object.__getattribute__(self, "execute")

        return getattr(object.__getattribute__(self, "txn"), name)

    def __setattr__(self, name, value):
        setattr(object.__getattribute__(self, "txn"), name, value)

    def execute(self, sql, *args, **kwargs):
        # TODO(paul): Maybe use 'info' and 'debug' for values?
        sql_logger.debug("[SQL] %s", sql)
        try:
            if args and args[0]:
                values = args[0]
                sql_logger.debug("[SQL values] " +
                    ", ".join(("<%s>",) * len(values)), *values)
        except:
            # Don't let logging failures stop SQL from working
            pass

        # TODO(paul): Here would be an excellent place to put some timing
        #   measurements, and log (warning?) slow queries.
        return object.__getattribute__(self, "txn").execute(
            sql, *args, **kwargs
        )


class SQLBaseStore(object):

    def __init__(self, hs):
        self.hs = hs
        self._db_pool = hs.get_db_pool()
        self.event_factory = hs.get_event_factory()
        self._clock = hs.get_clock()

    def runInteraction(self, func, *args, **kwargs):
        """Wraps the .runInteraction() method on the underlying db_pool."""
        def inner_func(txn, *args, **kwargs):
            return func(LoggingTransaction(txn), *args, **kwargs)

        return self._db_pool.runInteraction(inner_func, *args, **kwargs)

    def cursor_to_dict(self, cursor):
        """Converts a SQL cursor into an list of dicts.

        Args:
            cursor : The DBAPI cursor which has executed a query.
        Returns:
            A list of dicts where the key is the column header.
        """
        col_headers = list(column[0] for column in cursor.description)
        results = list(
            dict(zip(col_headers, row)) for row in cursor.fetchall()
        )
        return results

    def _execute(self, decoder, query, *args):
        """Runs a single query for a result set.

        Args:
            decoder - The function which can resolve the cursor results to
                something meaningful.
            query - The query string to execute
            *args - Query args.
        Returns:
            The result of decoder(results)
        """
        def interaction(txn):
            cursor = txn.execute(query, args)
            if decoder:
                return decoder(cursor)
            else:
                return cursor.fetchall()

        return self.runInteraction(interaction)

    def _execute_and_decode(self, query, *args):
        return self._execute(self.cursor_to_dict, query, *args)

    # "Simple" SQL API methods that operate on a single table with no JOINs,
    # no complex WHERE clauses, just a dict of values for columns.

    def _simple_insert(self, table, values, or_replace=False):
        """Executes an INSERT query on the named table.

        Args:
            table : string giving the table name
            values : dict of new column names and values for them
            or_replace : bool; if True performs an INSERT OR REPLACE
        """
        return self.runInteraction(
            self._simple_insert_txn, table, values, or_replace=or_replace
        )

    @log_function
    def _simple_insert_txn(self, txn, table, values, or_replace=False):
        sql = "%s INTO %s (%s) VALUES(%s)" % (
            ("INSERT OR REPLACE" if or_replace else "INSERT"),
            table,
            ", ".join(k for k in values),
            ", ".join("?" for k in values)
        )

        logger.debug(
            "[SQL] %s  Args=%s Func=%s",
            sql, values.values(),
        )

        txn.execute(sql, values.values())
        return txn.lastrowid

    def _simple_select_one(self, table, keyvalues, retcols,
                           allow_none=False):
        """Executes a SELECT query on the named table, which is expected to
        return a single row, returning a single column from it.

        Args:
            table : string giving the table name
            keyvalues : dict of column names and values to select the row with
            retcols : list of strings giving the names of the columns to return

            allow_none : If true, return None instead of failing if the SELECT
              statement returns no rows
        """
        return self._simple_selectupdate_one(
            table, keyvalues, retcols=retcols, allow_none=allow_none
        )

    @defer.inlineCallbacks
    def _simple_select_one_onecol(self, table, keyvalues, retcol,
                                  allow_none=False):
        """Executes a SELECT query on the named table, which is expected to
        return a single row, returning a single column from it."

        Args:
            table : string giving the table name
            keyvalues : dict of column names and values to select the row with
            retcol : string giving the name of the column to return
        """
        ret = yield self._simple_select_one(
            table=table,
            keyvalues=keyvalues,
            retcols=[retcol],
            allow_none=allow_none
        )

        if ret:
            defer.returnValue(ret[retcol])
        else:
            defer.returnValue(None)

    @defer.inlineCallbacks
    def _simple_select_onecol(self, table, keyvalues, retcol):
        """Executes a SELECT query on the named table, which returns a list
        comprising of the values of the named column from the selected rows.

        Args:
            table (str): table name
            keyvalues (dict): column names and values to select the rows with
            retcol (str): column whos value we wish to retrieve.

        Returns:
            Deferred: Results in a list
        """
        sql = "SELECT %(retcol)s FROM %(table)s WHERE %(where)s" % {
            "retcol": retcol,
            "table": table,
            "where": " AND ".join("%s = ?" % k for k in keyvalues.keys()),
        }

        def func(txn):
            txn.execute(sql, keyvalues.values())
            return txn.fetchall()

        res = yield self.runInteraction(func)

        defer.returnValue([r[0] for r in res])

    def _simple_select_list(self, table, keyvalues, retcols):
        """Executes a SELECT query on the named table, which may return zero or
        more rows, returning the result as a list of dicts.

        Args:
            table : string giving the table name
            keyvalues : dict of column names and values to select the rows with
            retcols : list of strings giving the names of the columns to return
        """
        sql = "SELECT %s FROM %s WHERE %s" % (
            ", ".join(retcols),
            table,
            " AND ".join("%s = ?" % (k) for k in keyvalues)
        )

        def func(txn):
            txn.execute(sql, keyvalues.values())
            return self.cursor_to_dict(txn)

        return self.runInteraction(func)

    def _simple_update_one(self, table, keyvalues, updatevalues,
                           retcols=None):
        """Executes an UPDATE query on the named table, setting new values for
        columns in a row matching the key values.

        Args:
            table : string giving the table name
            keyvalues : dict of column names and values to select the row with
            updatevalues : dict giving column names and values to update
            retcols : optional list of column names to return

        If present, retcols gives a list of column names on which to perform
        a SELECT statement *before* performing the UPDATE statement. The values
        of these will be returned in a dict.

        These are performed within the same transaction, allowing an atomic
        get-and-set.  This can be used to implement compare-and-set by putting
        the update column in the 'keyvalues' dict as well.
        """
        return self._simple_selectupdate_one(table, keyvalues, updatevalues,
                                             retcols=retcols)

    def _simple_selectupdate_one(self, table, keyvalues, updatevalues=None,
                                 retcols=None, allow_none=False):
        """ Combined SELECT then UPDATE."""
        if retcols:
            select_sql = "SELECT %s FROM %s WHERE %s" % (
                ", ".join(retcols),
                table,
                " AND ".join("%s = ?" % (k) for k in keyvalues)
            )

        if updatevalues:
            update_sql = "UPDATE %s SET %s WHERE %s" % (
                table,
                ", ".join("%s = ?" % (k) for k in updatevalues),
                " AND ".join("%s = ?" % (k) for k in keyvalues)
            )

        def func(txn):
            ret = None
            if retcols:
                txn.execute(select_sql, keyvalues.values())

                row = txn.fetchone()
                if not row:
                    if allow_none:
                        return None
                    raise StoreError(404, "No row found")
                if txn.rowcount > 1:
                    raise StoreError(500, "More than one row matched")

                ret = dict(zip(retcols, row))

            if updatevalues:
                txn.execute(
                    update_sql,
                    updatevalues.values() + keyvalues.values()
                )

                if txn.rowcount == 0:
                    raise StoreError(404, "No row found")
                if txn.rowcount > 1:
                    raise StoreError(500, "More than one row matched")

            return ret
        return self.runInteraction(func)

    def _simple_delete_one(self, table, keyvalues):
        """Executes a DELETE query on the named table, expecting to delete a
        single row.

        Args:
            table : string giving the table name
            keyvalues : dict of column names and values to select the row with
        """
        sql = "DELETE FROM %s WHERE %s" % (
            table,
            " AND ".join("%s = ?" % (k) for k in keyvalues)
        )

        def func(txn):
            txn.execute(sql, keyvalues.values())
            if txn.rowcount == 0:
                raise StoreError(404, "No row found")
            if txn.rowcount > 1:
                raise StoreError(500, "more than one row matched")
        return self.runInteraction(func)

    def _simple_max_id(self, table):
        """Executes a SELECT query on the named table, expecting to return the
        max value for the column "id".

        Args:
            table : string giving the table name
        """
        sql = "SELECT MAX(id) AS id FROM %s" % table

        def func(txn):
            txn.execute(sql)
            max_id = self.cursor_to_dict(txn)[0]["id"]
            if max_id is None:
                return 0
            return max_id

        return self.runInteraction(func)

    def _parse_event_from_row(self, row_dict):
        d = copy.deepcopy({k: v for k, v in row_dict.items() if v})

        d.pop("stream_ordering", None)
        d.pop("topological_ordering", None)
        d.pop("processed", None)

        d.update(json.loads(row_dict["unrecognized_keys"]))
        d["content"] = json.loads(d["content"])
        del d["unrecognized_keys"]

        if "age_ts" not in d:
            # For compatibility
            d["age_ts"] = d["ts"] if "ts" in d else 0

        return self.event_factory.create_event(
            etype=d["type"],
            **d
        )

    def _parse_events(self, rows):
        return self.runInteraction(self._parse_events_txn, rows)

    def _parse_events_txn(self, txn, rows):
        events = [self._parse_event_from_row(r) for r in rows]

        sql = "SELECT * FROM events WHERE event_id = ?"

        for ev in events:
           if hasattr(ev, "prev_state"):
                # Load previous state_content. 
                # TODO: Should we be pulling this out above?
                cursor = txn.execute(sql, (ev.prev_state,))
                prevs = self.cursor_to_dict(cursor)
                if prevs:
                    prev = self._parse_event_from_row(prevs[0])
                    ev.prev_content = prev.content

        return events

class Table(object):
    """ A base class used to store information about a particular table.
    """

    table_name = None
    """ str: The name of the table """

    fields = None
    """ list: The field names """

    EntryType = None
    """ Type: A tuple type used to decode the results """

    _select_where_clause = "SELECT %s FROM %s WHERE %s"
    _select_clause = "SELECT %s FROM %s"
    _insert_clause = "INSERT OR REPLACE INTO %s (%s) VALUES (%s)"

    @classmethod
    def select_statement(cls, where_clause=None):
        """
        Args:
            where_clause (str): The WHERE clause to use.

        Returns:
            str: An SQL statement to select rows from the table with the given
            WHERE clause.
        """
        if where_clause:
            return cls._select_where_clause % (
                ", ".join(cls.fields),
                cls.table_name,
                where_clause
            )
        else:
            return cls._select_clause % (
                ", ".join(cls.fields),
                cls.table_name,
            )

    @classmethod
    def insert_statement(cls):
        return cls._insert_clause % (
            cls.table_name,
            ", ".join(cls.fields),
            ", ".join(["?"] * len(cls.fields)),
        )

    @classmethod
    def decode_single_result(cls, results):
        """ Given an iterable of tuples, return a single instance of
            `EntryType` or None if the iterable is empty
        Args:
            results (list): The results list to convert to `EntryType`
        Returns:
            EntryType: An instance of `EntryType`
        """
        results = list(results)
        if results:
            return cls.EntryType(*results[0])
        else:
            return None

    @classmethod
    def decode_results(cls, results):
        """ Given an iterable of tuples, return a list of `EntryType`
        Args:
            results (list): The results list to convert to `EntryType`

        Returns:
            list: A list of `EntryType`
        """
        return [cls.EntryType(*row) for row in results]

    @classmethod
    def get_fields_string(cls, prefix=None):
        if prefix:
            to_join = ("%s.%s" % (prefix, f) for f in cls.fields)
        else:
            to_join = cls.fields

        return ", ".join(to_join)


class JoinHelper(object):
    """ Used to help do joins on tables by looking at the tables' fields and
    creating a list of unique fields to use with SELECTs and a namedtuple
    to dump the results into.

    Attributes:
        taples (list): List of `Table` classes
        EntryType (type)
    """

    def __init__(self, *tables):
        self.tables = tables

        res = []
        for table in self.tables:
            res += [f for f in table.fields if f not in res]

        self.EntryType = collections.namedtuple("JoinHelperEntry", res)

    def get_fields(self, **prefixes):
        """Get a string representing a list of fields for use in SELECT
        statements with the given prefixes applied to each.

        For example::

            JoinHelper(PdusTable, StateTable).get_fields(
                PdusTable="pdus",
                StateTable="state"
            )
        """
        res = []
        for field in self.EntryType._fields:
            for table in self.tables:
                if field in table.fields:
                    res.append("%s.%s" % (prefixes[table.__name__], field))
                    break

        return ", ".join(res)

    def decode_results(self, rows):
        return [self.EntryType(*row) for row in rows]
