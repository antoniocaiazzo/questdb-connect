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
import os
from typing import NamedTuple

import pytest
import questdb_connect as qdbc
from sqlalchemy import Column, MetaData, text
from sqlalchemy.orm import declarative_base

os.environ.setdefault('SQLALCHEMY_SILENCE_UBER_WARNING', '1')

ALL_TYPES_TABLE_NAME = 'all_types_table'
METRICS_TABLE_NAME = 'metrics_table'


class TestConfig(NamedTuple):
    host: str
    port: int
    username: str
    password: str
    database: str
    __test__ = True


@pytest.fixture(scope='session', autouse=True, name='test_config')
def test_config_fixture() -> TestConfig:
    return TestConfig(
        host=os.environ.get('QUESTDB_CONNECT_HOST', 'localhost'),
        port=int(os.environ.get('QUESTDB_CONNECT_PORT', '8812')),
        username=os.environ.get('QUESTDB_CONNECT_USER', 'admin'),
        password=os.environ.get('QUESTDB_CONNECT_PASSWORD', 'quest'),
        database=os.environ.get('QUESTDB_CONNECT_DATABASE', 'main')
    )


@pytest.fixture(scope='module', name='test_engine')
def test_engine_fixture(test_config: TestConfig):
    engine = None
    try:
        engine = qdbc.create_engine(
            test_config.host,
            test_config.port,
            test_config.username,
            test_config.password,
            test_config.database)
        return engine
    finally:
        if engine:
            engine.dispose()
            del engine


@pytest.fixture(autouse=True, name='test_model')
def test_model_fixture(test_engine):
    Base = declarative_base(metadata=MetaData())

    class TableModel(Base):
        __tablename__ = ALL_TYPES_TABLE_NAME
        __table_args__ = (qdbc.QDBTableEngine(ALL_TYPES_TABLE_NAME, 'col_ts', qdbc.PartitionBy.DAY, is_wal=True),)
        col_boolean = Column('col_boolean', qdbc.Boolean)
        col_byte = Column('col_byte', qdbc.Byte)
        col_short = Column('col_short', qdbc.Short)
        col_int = Column('col_int', qdbc.Int)
        col_long = Column('col_long', qdbc.Long)
        col_float = Column('col_float', qdbc.Float)
        col_double = Column('col_double', qdbc.Double)
        col_symbol = Column('col_symbol', qdbc.Symbol)
        col_string = Column('col_string', qdbc.String)
        col_char = Column('col_char', qdbc.Char)
        col_uuid = Column('col_uuid', qdbc.UUID)
        col_date = Column('col_date', qdbc.Date)
        col_ts = Column('col_ts', qdbc.Timestamp, primary_key=True)
        col_geohash = Column('col_geohash', qdbc.GeohashInt)
        col_long256 = Column('col_long256', qdbc.Long256)

    Base.metadata.drop_all(test_engine)
    Base.metadata.create_all(test_engine)
    return TableModel


@pytest.fixture(autouse=True, name='test_metrics')
def test_metrics_fixture(test_engine):
    Base = declarative_base(metadata=MetaData())

    class TableMetrics(Base):
        __tablename__ = METRICS_TABLE_NAME
        __table_args__ = (
            qdbc.QDBTableEngine(
                METRICS_TABLE_NAME,
                'ts',
                qdbc.PartitionBy.HOUR,
                is_wal=True,
                dedup_upsert_keys=('source', 'attr_name', 'ts')
            ),
        )
        source = Column(qdbc.Symbol)
        attr_name = Column(qdbc.Symbol)
        attr_value = Column(qdbc.Double)
        ts = Column(qdbc.Timestamp, primary_key=True)

    Base.metadata.drop_all(test_engine)
    Base.metadata.create_all(test_engine)
    return TableMetrics


def collect_select_all(session, expected_rows) -> str:
    while True:
        rs = session.execute(text(f'select * from public.{ALL_TYPES_TABLE_NAME} order by 1 asc'))
        if rs.rowcount == expected_rows:
            return '\n'.join(str(row) for row in rs)


def collect_select_all_raw_connection(test_engine, expected_rows) -> str:
    conn = test_engine.raw_connection()
    try:
        while True:
            with conn.cursor() as cursor:
                cursor.execute(f'select * from public.{ALL_TYPES_TABLE_NAME} order by 1 asc')
                if cursor.rowcount == expected_rows:
                    return '\n'.join(str(row) for row in cursor)
    finally:
        if conn:
            conn.close()
