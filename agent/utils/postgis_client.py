from urllib.parse import urlparse

import psycopg2

from agent.utils.env_configs import DATABASE
from agent.utils.stack_configs import RDB_PASSWORD, RDB_URL, RDB_USER


class PostGISClient:
    def __init__(self):
        parsed_url = urlparse(RDB_URL.removeprefix("jdbc:"))
        self.dbname = DATABASE
        self.host = parsed_url.hostname
        self.port = parsed_url.port

    def connect(self):
        return psycopg2.connect(
            dbname=self.dbname,
            user=RDB_USER,
            password=RDB_PASSWORD,
            host=self.host,
            port=self.port,
        )

    def add_point_to_trip(self, point_iri: str, trip_iri: str):
        """Create the supporting database objects and store a point-trip mapping."""
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS point_to_trip (
                        point_iri VARCHAR PRIMARY KEY,
                        trip_iri VARCHAR NOT NULL UNIQUE
                    )
                    """
                )
                cursor.execute(
                    """
                    INSERT INTO point_to_trip (point_iri, trip_iri)
                    VALUES (%s, %s)
                    ON CONFLICT (point_iri)
                    DO UPDATE SET trip_iri = EXCLUDED.trip_iri
                    """,
                    (point_iri, trip_iri),
                )


postgis_client = PostGISClient()
