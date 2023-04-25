#
#     ___                  _   ____  ____
#    / _ \ _   _  ___  ___| |_|  _ \| __ )
#   | | | | | | |/ _ \/ __| __| | | |  _ \
#   | |_| | |_| |  __/\__ \ |_| |_| | |_) |
#    \__\_\\__,_|\___||___/\__|____/|____/
#
#  Copyright (c) 2014-2019 Appsicle
#  Copyright (c) 2019-2023 QuestDB
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
import abc

import sqlalchemy as sqla
from sqlalchemy.dialects.postgresql.psycopg2 import PGDialect_psycopg2
from sqlalchemy.engine.reflection import Inspector
from sqlalchemy.orm.exc import NoResultFound
from sqlalchemy.sql.base import SchemaEventTarget
from sqlalchemy.sql.compiler import DDLCompiler, GenericTypeCompiler, IdentifierPreparer, SQLCompiler
from sqlalchemy.sql.visitors import Traversible

from questdb_connect import types

# https://docs.sqlalchemy.org/en/14/ apache-superset requires SQLAlchemy 1.4


# ===== SQLAlchemy Dialect ======

def connection_uri(host: str, port: int, username: str, password: str, database: str = 'main'):
    return f'questdb://{username}:{password}@{host}:{port}/{database}'


def create_engine(host: str, port: int, username: str, password: str, database: str = 'main'):
    return sqla.create_engine(connection_uri(host, port, username, password, database))


# ===== QUESTDB ENGINE =====

class QDBTableEngine(SchemaEventTarget, Traversible):
    def __init__(
            self,
            table_name: str,
            ts_col_name: str = None,
            partition_by: types.PartitionBy = types.PartitionBy.DAY,
            is_wal: bool = True
    ):
        Traversible.__init__(self)
        self.name = table_name
        self.ts_col_name = ts_col_name
        self.partition_by = partition_by
        self.is_wal = is_wal
        self.compiled = None

    def get_table_suffix(self):
        if self.compiled is None:
            self.compiled = ''
            has_ts = self.ts_col_name is not None
            is_partitioned = self.partition_by and self.partition_by != types.PartitionBy.NONE
            if has_ts:
                self.compiled += f'TIMESTAMP({self.ts_col_name})'
            if is_partitioned:
                if not has_ts:
                    raise types.ArgumentError(None, 'Designated timestamp must be specified for partitioned table')
                self.compiled += f' PARTITION BY {self.partition_by.name}'
            if self.is_wal:
                if not is_partitioned:
                    raise types.ArgumentError(None, 'Designated timestamp and partition by must be specified for WAL table')
                if self.is_wal:
                    self.compiled += ' WAL'
                else:
                    self.compiled += ' BYPASS WAL'
        return self.compiled

    def _set_parent(self, parent, **_kwargs):
        parent.engine = self


# ===== QUESTDB DIALECT TYPES =====


_QUOTES = ("'", '"')


def _quote_identifier(identifier: str):
    if not identifier:
        return None
    first = 0
    last = len(identifier)
    if identifier[first] in _QUOTES:
        first += 1
    if identifier[last - 1] in _QUOTES:
        last -= 1
    return f"'{identifier[first:last]}'"


class QDBIdentifierPreparer(IdentifierPreparer):
    """QuestDB's identifiers are better off with quotes"""
    quote_identifier = staticmethod(_quote_identifier)

    def _requires_quotes(self, _value):
        return True


class QDBDDLCompiler(DDLCompiler):
    def visit_create_schema(self, create, **kw):
        raise Exception('QuestDB does not support SCHEMAS')

    def visit_drop_schema(self, drop, **kw):
        raise Exception('QuestDB does not support SCHEMAS')

    def visit_create_table(self, create, **kw):
        table = create.element
        create_table = f"CREATE TABLE '{table.fullname}' ("
        create_table += ', '.join([self.get_column_specification(c.element) for c in create.columns])
        return create_table + ') ' + table.engine.get_table_suffix()

    def get_column_specification(self, column: sqla.Column, **_):
        if not isinstance(column.type, types.QDBTypeMixin):
            raise types.ArgumentError('Column type is not a valid QuestDB type')
        return column.type.column_spec(column.name)


class QDBSQLCompiler(SQLCompiler):
    def _is_safe_for_fast_insert_values_helper(self):
        return True


class QDBInspector(Inspector):
    def reflecttable(
            self,
            table,
            include_columns,
            exclude_columns=(),
            resolve_fks=True,
            _extend_on=None,
    ):
        # backward compatibility SQLAlchemy 1.3
        return self.reflect_table(table, include_columns, exclude_columns, resolve_fks, _extend_on)

    def reflect_table(
            self,
            table,
            include_columns=None,
            exclude_columns=None,
            resolve_fks=False,
            _extend_on=None,
    ):
        table_name = table.name
        result_set = self.bind.execute(f"tables() WHERE name = '{table_name}'")
        if not result_set:
            raise NoResultFound(f"Table '{table_name}' does not exist")
        table_attrs = result_set.first()
        col_ts_name = table_attrs['designatedTimestamp']
        partition_by = types.PartitionBy[table_attrs['partitionBy']]
        is_wal = True if table_attrs['walEnabled'] else False
        for row in self.bind.execute(f"table_columns('{table_name}')"):
            col_name = row[0]
            if include_columns and col_name not in include_columns:
                continue
            if exclude_columns and col_name in exclude_columns:
                continue
            col_type = types.resolve_type_from_name(row[1])
            if col_ts_name and col_ts_name.upper() == col_name.upper():
                table.append_column(sqla.Column(col_name, col_type, primary_key=True))
            else:
                table.append_column(sqla.Column(col_name, col_type))
        table.engine = QDBTableEngine(table_name, col_ts_name, partition_by, is_wal)
        table.metadata = sqla.MetaData()

    def get_columns(self, table_name, schema=None, **kw):
        result_set = self.bind.execute(f"table_columns('{table_name}')")
        if not result_set:
            raise NoResultFound(f"Table '{table_name}' does not exist")
        return [{
            'name': row[0],
            'type': types.resolve_type_from_name(row[1]),
            'nullable': True,
            'autoincrement': False,
            'persisted': True
        } for row in result_set]


# class QuestDBDialect(PGDialect_psycopg2, abc.ABC):
class QuestDBDialect(PGDialect_psycopg2, abc.ABC):
    name = 'questdb'
    psycopg2_version = (2, 9)
    default_schema_name = None
    statement_compiler = QDBSQLCompiler
    ddl_compiler = QDBDDLCompiler
    type_compiler = GenericTypeCompiler
    inspector = QDBInspector
    preparer = QDBIdentifierPreparer
    supports_schemas = False
    supports_statement_cache = False
    supports_server_side_cursors = False
    supports_views = False
    supports_empty_insert = False
    supports_multivalues_insert = True
    supports_comments = True
    inline_comments = False
    postfetch_lastrowid = False
    non_native_boolean_check_constraint = False
    max_identifier_length = 255
    _user_defined_max_identifier_length = 255
    supports_is_distinct_from = False

    @classmethod
    def dbapi(cls):
        import questdb_connect as dbapi
        return dbapi

    def get_schema_names(self, connection, **kw):
        return []

    def get_table_names(self, connection, schema=None, **kw):
        return [row.table for row in connection.execute(sqla.text('SHOW TABLES'))]

    def get_pk_constraint(self, connection, table_name, schema=None, **kw):
        return []

    def get_foreign_keys(self, connection, table_name, schema=None, postgresql_ignore_search_path=False, **kw):
        return []

    def get_temp_table_names(self, connection, **kw):
        return []

    def get_view_names(self, connection, schema=None, **kw):
        return []

    def get_temp_view_names(self, connection, schema=None, **kw):
        return []

    def get_view_definition(self, connection, view_name, schema=None, **kw):
        pass

    def get_indexes(self, connection, table_name, schema=None, **kw):
        return []

    def get_unique_constraints(self, connection, table_name, schema=None, **kw):
        return []

    def get_check_constraints(self, connection, table_name, schema=None, **kw):
        return []

    def has_table(self, connection, table_name, schema=None):
        query = f"tables() WHERE name='{table_name}'"
        result = connection.execute(sqla.text(query))
        return result.rowcount == 1

    def has_sequence(self, connection, sequence_name, schema=None, **_kw):
        return False

    def do_begin_twophase(self, connection, xid):
        raise NotImplementedError

    def do_prepare_twophase(self, connection, xid):
        raise NotImplementedError

    def do_rollback_twophase(self, connection, xid, is_prepared=True, recover=False):
        raise NotImplementedError

    def do_commit_twophase(self, connection, xid, is_prepared=True, recover=False):
        raise NotImplementedError

    def do_recover_twophase(self, connection):
        raise NotImplementedError

    def set_isolation_level(self, dbapi_connection, level):
        pass

    def get_isolation_level(self, dbapi_connection):
        return None
