from contextlib import closing
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
        with closing(self.connect()) as connection, connection:
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

    def update_timeline_trips(self, assignments):
        """Overwrite only detected points. The caller owns replacement scope.

        Legacy time-series output is committed separately; errors propagate so
        a retry can repair either output. No whole-range clearing is performed.
        """
        from psycopg2.extras import execute_values
        if not assignments:
            return
        with closing(self.connect()) as connection, connection:
            with connection.cursor() as cursor:
                cursor.execute("CREATE TEMP TABLE timeline_trip_batch ("
                               "point_iri TEXT, source_time DOUBLE PRECISION, trip INTEGER, "
                               "PRIMARY KEY (point_iri, source_time)) ON COMMIT DROP")
                # Last assignment wins for a repeated source point.
                rows = {(iri, time): trip for iri, time, trip in assignments}
                execute_values(cursor, "INSERT INTO timeline_trip_batch VALUES %s",
                               [(iri, time, trip) for (iri, time), trip in rows.items()])
                # Resolve each trajectory once, rather than storing its IRI on every point.
                cursor.execute("""
                    CREATE TEMP TABLE timeline_trip_devices (
                        device_id TEXT PRIMARY KEY, point_iri TEXT NOT NULL UNIQUE
                    ) ON COMMIT DROP
                """)
                cursor.execute("""
                    INSERT INTO timeline_trip_devices
                    SELECT d.device_id, public.get_point_iri(d.device_id)::TEXT
                    FROM (SELECT DISTINCT device_id::TEXT FROM public.devices
                          WHERE sensor_class = 'GPSDevice') d
                    WHERE public.get_point_iri(d.device_id)::TEXT IN
                          (SELECT DISTINCT point_iri FROM timeline_trip_batch)
                """)
                cursor.execute("SELECT device_id FROM timeline_trip_devices ORDER BY device_id")
                for (device_id,) in cursor.fetchall():
                    cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                                   (device_id,))
                cursor.execute("""
                    UPDATE public.trajectory_points p SET trip = b.trip
                    FROM timeline_trip_batch b JOIN timeline_trip_devices d
                      ON d.point_iri = b.point_iri
                    WHERE p.device_id = d.device_id AND p.source_time = b.source_time
                """)
                if cursor.rowcount != len(rows):
                    raise RuntimeError("Timeline points are missing; backfill the shared "
                                       "trajectory table before processing trips")


postgis_client = PostGISClient()
