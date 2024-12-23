import logging
import os
import subprocess
import time
from collections.abc import Generator
from typing import Any

import openai
import psycopg
import pytest
from click.testing import CliRunner
from psycopg import Connection, sql
from psycopg.rows import dict_row
from testcontainers.postgres import PostgresContainer  # type: ignore

from pgai.cli import vectorizer_worker
from tests.vectorizer import expected

count = 10000


class TestDatabase:
    """"""

    container: PostgresContainer
    dbname: str

    def __init__(self, container: PostgresContainer):
        global count
        dbname = f"test_{count}"
        count += 1
        self.container = container
        self.dbname = dbname
        url = self._create_connection_url(dbname="template1")
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS ai CASCADE")
            conn.execute(
                sql.SQL("CREATE DATABASE {0}").format(sql.Identifier(self.dbname))
            )

    def _create_connection_url(
        self,
        username: str | None = None,
        password: str | None = None,
        dbname: str | None = None,
    ):
        host = self.container._docker.host()  # type: ignore
        return super(PostgresContainer, self.container)._create_connection_url(  # type: ignore
            dialect="postgresql",
            username=username or self.container.username,
            password=password or self.container.password,
            dbname=dbname or self.dbname,
            host=host,
            port=self.container.port,
        )

    def get_connection_url(self) -> str:
        return self._create_connection_url()


@pytest.fixture
def cli_db(
    postgres_container: PostgresContainer,
) -> Generator[tuple[TestDatabase, Connection], None, None]:
    """Creates a test database with pgai installed"""

    test_database = TestDatabase(container=postgres_container)

    # Connect
    with psycopg.connect(
        test_database.get_connection_url(),
        autocommit=True,
    ) as conn:
        yield test_database, conn


@pytest.fixture
def cli_db_url(cli_db: tuple[TestDatabase, Connection]) -> str:
    """Constructs database URL from the cli_db fixture"""
    container, _ = cli_db
    return container.get_connection_url()


def test_worker_no_tasks(cli_db_url: str):
    """Test that worker handles no tasks gracefully"""
    result = CliRunner().invoke(vectorizer_worker, ["--db-url", cli_db_url, "--once"])

    # It exits successfully
    assert result.exit_code == 0
    assert "no vectorizers found" in result.output.lower()


@pytest.fixture
def configured_vectorizer_and_source_table(
    cli_db: tuple[TestDatabase, Connection],
    test_params: tuple[int, int, int, str, str],
) -> int:
    """Creates and configures a vectorizer for testing"""
    num_items, concurrency, batch_size, chunking, formatting = test_params
    _, conn = cli_db

    with conn.cursor(row_factory=dict_row) as cur:
        # Create source table
        cur.execute("""
            CREATE TABLE blog (
                id INT NOT NULL PRIMARY KEY,
                id2 INT NOT NULL,
                content TEXT NOT NULL
            )
        """)

        # Create vectorizer
        cur.execute(f"""
            SELECT ai.create_vectorizer(
                'blog'::regclass,
                embedding => ai.embedding_openai(
                    'text-embedding-ada-002',
                    1536,
                    api_key_name => 'OPENAI_API_KEY'
                ),
                chunking => ai.{chunking},
                formatting => ai.{formatting},
                processing => ai.processing_default(batch_size => {batch_size},
                                                    concurrency => {concurrency})
            )
        """)  # type: ignore
        vectorizer_id: int = int(cur.fetchone()["create_vectorizer"])  # type: ignore

        # Insert test data
        values = [(i, i, f"post_{i}") for i in range(1, num_items + 1)]
        cur.executemany(
            "INSERT INTO blog (id, id2, content) VALUES (%s, %s, %s)", values
        )

        return vectorizer_id


@pytest.fixture
def test_params(request: pytest.FixtureRequest) -> tuple[int, int, int, str, str]:
    """Parameters for test variations:
    (num_items, concurrency, batch_size, chunking, formatting)"""
    return request.param


class TestWithConfiguredVectorizer:
    @pytest.mark.parametrize(
        "test_params",
        [
            (
                1,
                1,
                1,
                "chunking_character_text_splitter('content')",
                "formatting_python_template('$chunk')",
            ),
            (
                4,
                2,
                2,
                "chunking_character_text_splitter('content')",
                "formatting_python_template('$chunk')",
            ),
        ],
    )
    def test_process_vectorizer(
        self,
        cli_db: tuple[TestDatabase, Connection],
        cli_db_url: str,
        configured_vectorizer_and_source_table: int,
        monkeypatch: pytest.MonkeyPatch,
        vcr_: Any,
        test_params: tuple[int, int, int, str, str],
    ):
        """Test successful processing of vectorizer tasks"""
        num_items, concurrency, batch_size, _, _ = test_params
        _, conn = cli_db
        # Insert pre-existing embedding for first item
        with conn.cursor() as cur:
            cur.execute("""
               INSERT INTO
               blog_embedding_store(embedding_uuid, id, chunk_seq, chunk, embedding)
               VALUES (gen_random_uuid(), 1, 1, 'post_1',
                array_fill(0, ARRAY[1536])::vector)
            """)
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")

        # When running the worker with cassette matching original test params
        cassette = (
            f"openai-character_text_splitter-chunk_value-"
            f"items={num_items}-batch_size={batch_size}.yaml"
        )
        logging.getLogger("vcr").setLevel(logging.DEBUG)
        with vcr_.use_cassette(cassette):
            result = CliRunner().invoke(
                vectorizer_worker,
                [
                    "--db-url",
                    cli_db_url,
                    "--once",
                    "--vectorizer-id",
                    str(configured_vectorizer_and_source_table),
                    "--concurrency",
                    str(concurrency),
                ],
                catch_exceptions=False,
            )

        assert not result.exception
        assert result.exit_code == 0

        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT count(*) as count FROM blog_embedding_store;")
            assert cur.fetchone()["count"] == num_items  # type: ignore

    @pytest.mark.parametrize(
        "test_params",
        [
            (
                2,
                1,
                2,
                "chunking_recursive_character_text_splitter('content', 128, 10,"
                " separators => array[E'\n\n'])",
                "formatting_python_template('$chunk')",
            )
        ],
    )
    def test_document_exceeds_model_context_length(
        self,
        cli_db: tuple[TestDatabase, Connection],
        cli_db_url: str,
        configured_vectorizer_and_source_table: int,
        monkeypatch: pytest.MonkeyPatch,
        vcr_: Any,
    ):
        """Test handling of documents that exceed the model's token limit"""
        _, conn = cli_db
        # Given a vectorizer configuration
        with conn.cursor(row_factory=dict_row) as cur:
            long_content = "AGI" * 5000
            cur.execute(
                f"UPDATE blog SET CONTENT = '{long_content}' where id = '2'",
            )
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")

        # When running the worker
        with vcr_.use_cassette("test_document_in_batch_too_long.yaml"):
            result = CliRunner().invoke(
                vectorizer_worker,
                [
                    "--db-url",
                    cli_db_url,
                    "--once",
                    "--vectorizer-id",
                    str(configured_vectorizer_and_source_table),
                ],
                catch_exceptions=False,
            )

        assert result.exit_code == 0

        # Then only the normal document should be embedded
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM blog_embedding_store ORDER BY id")
            records = cur.fetchall()
            assert len(records) == 1
            record = records[0]

            # Verify the embedded document
            assert record["id"] == 1
            assert record["chunk"] == "post_1"
            assert (
                record["embedding"]
                == expected.embeddings["openai-character_text_splitter-chunk_value-1-1"]
            )

            # Check error was logged
            cur.execute("SELECT * FROM ai.vectorizer_errors")
            errors = cur.fetchall()
            assert len(errors) == 1
            error = errors[0]
            assert error["message"] == "chunk exceeds model context length"
            assert error["details"]["pk"]["id"] == 2
            assert (
                error["details"]["error_reason"]
                == "chunk exceeds the text-embedding-ada-002"
                " model context length of 8192 tokens"
            )

    @pytest.mark.parametrize(
        "test_params",
        [
            (
                2,
                1,
                2,
                "chunking_recursive_character_text_splitter('content')",
                "formatting_python_template('$chunk')",
            )
        ],
    )
    def test_invalid_api_key_error(
        self,
        cli_db: tuple[TestDatabase, Connection],
        cli_db_url: str,
        configured_vectorizer_and_source_table: int,
        monkeypatch: pytest.MonkeyPatch,
        vcr_: Any,
    ):
        """Test that worker handles invalid API key appropriately"""
        # Given an invalid API key
        monkeypatch.setenv("OPENAI_API_KEY", "invalid")
        _, conn = cli_db

        # When running the worker
        with vcr_.use_cassette("test_invalid_api_key_error.yaml"):
            try:
                CliRunner().invoke(
                    vectorizer_worker,
                    [
                        "--db-url",
                        cli_db_url,
                        "--once",
                        "--vectorizer-id",
                        str(configured_vectorizer_and_source_table),
                    ],
                )
            except openai.AuthenticationError as e:
                assert e.code == 401

        # Ensure there's an entry in the errors table
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM ai.vectorizer_errors")
            records = cur.fetchall()
            assert len(records) == 1
            error = records[0]
            assert error["id"] == configured_vectorizer_and_source_table
            assert error["message"] == "embedding provider failed"
            assert error["details"] == {
                "provider": "openai",
                "error_reason": "Error code: 401 - {'error': {'message': "
                "'Incorrect API key provided: invalid."
                " You can find your API key at"
                " https://platform.openai.com/account/api-keys.',"
                " 'type': 'invalid_request_error', 'param': None,"
                " 'code': 'invalid_api_key'}}",
            }

    @pytest.mark.parametrize(
        "test_params",
        [
            (
                1,
                1,
                1,
                "chunking_character_text_splitter('content')",
                "formatting_python_template('$chunk')",
            )
        ],
    )
    def test_invalid_function_arguments(
        self,
        cli_db: tuple[TestDatabase, Connection],
        cli_db_url: str,
        configured_vectorizer_and_source_table: int,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Test that worker handles invalid embedding model arguments appropriately"""
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        _, conn = cli_db

        # And a vectorizer with invalid embedding dimensions
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE ai.vectorizer
                SET config = jsonb_set(
                    config,
                    '{embedding,dimensions}',
                    '128'::jsonb
                )
                WHERE id = %s
            """,
                (configured_vectorizer_and_source_table,),
            )

        # When running the worker
        try:
            CliRunner().invoke(
                vectorizer_worker,
                [
                    "--db-url",
                    cli_db_url,
                    "--once",
                    "--vectorizer-id",
                    str(configured_vectorizer_and_source_table),
                ],
            )
        except ValueError as e:
            assert str(e) == "dimensions must be 1536 for text-embedding-ada-002"

        # Then an error was logged
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM ai.vectorizer_errors")
            records = cur.fetchall()
            assert len(records) == 1
            error = records[0]
            assert error["id"] == configured_vectorizer_and_source_table
            assert error["message"] == "embedding provider failed"
            assert error["details"] == {
                "provider": "openai",
                "error_reason": "dimensions must be 1536 for text-embedding-ada-002",
            }


def test_worker_no_extension(
    postgres_container: PostgresContainer,
):
    """Test that the worker fails when pgai extension is not installed"""
    result = CliRunner().invoke(
        vectorizer_worker,
        ["--db-url", postgres_container.get_connection_url(), "--once"],
    )

    assert result.exit_code == 1
    assert "the pgai extension is not installed" in result.output.lower()


def test_vectorizer_exits_when_vectorizers_specified_but_missing(cli_db_url: str):
    result = CliRunner().invoke(
        vectorizer_worker,
        ["--db-url", cli_db_url, "--poll-interval", "0.1s", "--vectorizer-id", "0"],
    )
    assert result.exit_code != 0
    assert "invalid vectorizers, wanted: [0], got: []" in result.output


def test_vectorizer_picks_up_new_vectorizer(
    cli_db: tuple[TestDatabase, Connection],
):
    postgres_container, con = cli_db
    db_url = postgres_container.get_connection_url()
    test_env = os.environ.copy()
    test_env["OPENAI_API_KEY"] = (
        "empty"  # the key must be set, but doesn't need to be valid
    )
    process = subprocess.Popen(
        [
            "python",
            "-m",
            "pgai",
            "vectorizer",
            "worker",
            "--db-url",
            db_url,
            "--poll-interval",
            "0.1s",
        ],
        env=test_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )

    # the typings for subprocess.Popen are bad, see https://github.com/python/typeshed/issues/3831
    assert process.stdout is not None

    os.set_blocking(
        process.stdout.fileno(), False
    )  # allow capturing all output without blocking
    count = 0
    while True:
        output = "\n".join(process.stdout.readlines())
        if output != "":
            assert "no vectorizers found" in output
            assert "running vectorizer" not in output
            break
        else:
            # assume that we'll see the output we want to see within 10s
            assert count < 20
            count += 1
            time.sleep(0.5)

    with con.cursor() as cur:
        cur.execute("CREATE TABLE test(id bigint primary key, contents text)")
        cur.execute("""SELECT ai.create_vectorizer('test'::regclass,
            embedding => ai.embedding_openai('text-embedding-3-small', 768),
            chunking => ai.chunking_recursive_character_text_splitter('contents')
        );
        """)
    count = 0
    while True:
        output = "\n".join(process.stdout.readlines())
        if "running vectorizer" not in output:
            # assume that we see the output we want to within 10s
            assert count < 20
            count += 1
            time.sleep(0.5)
        else:
            break
    process.terminate()
